"""Gmail API access (read-only): OAuth, search, and message decoding."""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from job_agent.db import PROJECT_ROOT
from job_agent.normalize import to_utc_str

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CLIENT_FILE = PROJECT_ROOT / "credentials" / "gmail_client.json"
TOKEN_FILE = PROJECT_ROOT / "credentials" / "gmail_token.json"
WEB_CLIENT_PORT = 8080   # a "Web application" OAuth client must list http://localhost:8080/


class GmailAuthError(Exception):
    pass


@dataclass
class EmailMsg:
    id: str
    subject: str
    sender: str
    date: str | None       # UTC 'YYYY-MM-DD HH:MM:SS'
    text: str
    html: str


def _client_type() -> str:
    with open(CLIENT_FILE, encoding="utf-8") as f:
        return next(iter(json.load(f)))   # 'installed' (Desktop app) or 'web'


def authorize() -> None:
    """Interactive one-time login. Opens a browser; saves the token file."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_FILE.exists():
        raise GmailAuthError(f"Missing {CLIENT_FILE}. Download your OAuth client JSON there.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    if _client_type() == "web":
        print(f"Web-type OAuth client: http://localhost:{WEB_CLIENT_PORT}/ must be an "
              "authorised redirect URI on it (or use a Desktop-app client instead).")
        creds = flow.run_local_server(port=WEB_CLIENT_PORT, open_browser=True)
    else:
        creds = flow.run_local_server(port=0, open_browser=True)
    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)
    print(f"Saved {TOKEN_FILE}")


def service():
    """Non-interactive Gmail service for scheduled runs (refreshes the token)."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN_FILE.exists():
        raise GmailAuthError("Not authorised yet: run `python -m job_agent gmail-auth` once.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise GmailAuthError(
                    f"Gmail token refresh failed ({e}). Re-run `python -m job_agent gmail-auth`. "
                    "If this happens weekly, set the OAuth consent screen to 'In production'."
                ) from e
            TOKEN_FILE.write_text(creds.to_json())
        else:
            raise GmailAuthError("Gmail token invalid: re-run `python -m job_agent gmail-auth`.")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def search(svc, query: str, max_messages: int = 50) -> list[str]:
    ids: list[str] = []
    page = None
    while len(ids) < max_messages:
        resp = svc.users().messages().list(
            userId="me", q=query, pageToken=page, maxResults=min(100, max_messages - len(ids))
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids


def _decode(data: str | None) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk(part: dict, out: dict) -> None:
    mime = part.get("mimeType", "")
    if mime.startswith("multipart/"):
        for p in part.get("parts") or []:
            _walk(p, out)
    elif mime in ("text/plain", "text/html") and not out.get(mime):
        out[mime] = _decode((part.get("body") or {}).get("data"))


def parse_message(raw: dict) -> EmailMsg:
    headers = {h["name"].lower(): h["value"] for h in raw.get("payload", {}).get("headers", [])}
    bodies: dict[str, str] = {}
    _walk(raw.get("payload", {}), bodies)
    internal = raw.get("internalDate")
    return EmailMsg(
        id=raw["id"],
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        date=to_utc_str(int(internal)) if internal else None,
        text=bodies.get("text/plain", ""),
        html=bodies.get("text/html", ""),
    )


def get(svc, msg_id: str) -> EmailMsg:
    return parse_message(svc.users().messages().get(userId="me", id=msg_id, format="full").execute())
