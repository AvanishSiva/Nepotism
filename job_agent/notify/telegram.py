"""Telegram Bot API (polling, no server). Token + chat id live in .env."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx

from job_agent.db import PROJECT_ROOT

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000          # Telegram limit is 4096; keep headroom
ENV_FILE = PROJECT_ROOT / ".env"


class TelegramError(Exception):
    pass


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not t:
        raise TelegramError("TELEGRAM_BOT_TOKEN missing from .env")
    return t


def chat_id() -> str:
    c = os.environ.get("TELEGRAM_CHAT_ID")
    if not c:
        raise TelegramError("TELEGRAM_CHAT_ID not set: message the bot, then run `python -m job_agent telegram-setup`")
    return c


def call(method: str, **params):
    r = httpx.post(API.format(token=_token(), method=method), json=params,
                   timeout=40 + int(params.get("timeout") or 0))
    data = r.json()
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description')}")
    return data["result"]


def esc(s: str | None) -> str:
    """Escape for parse_mode=HTML."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _chunks(text: str) -> list[str]:
    """Split on line boundaries (each line is self-contained HTML) under the limit."""
    out, cur = [], ""
    for line in text.split("\n"):
        piece = line if not cur else "\n" + line
        if len(cur) + len(piece) > MAX_LEN and cur:
            out.append(cur)
            cur = line
        else:
            cur += piece
    if cur:
        out.append(cur)
    return out


def send(text: str, *, html: bool = True) -> int:
    """Send (possibly long) text to the configured chat. Returns messages sent."""
    n = 0
    for part in _chunks(text):
        call("sendMessage", chat_id=chat_id(), text=part, disable_web_page_preview=True,
             **({"parse_mode": "HTML"} if html else {}))
        n += 1
    return n


def send_document(path: Path, caption: str = "") -> None:
    with open(path, "rb") as f:
        r = httpx.post(API.format(token=_token(), method="sendDocument"),
                       data={"chat_id": chat_id(), "caption": caption[:1000]},
                       files={"document": (path.name, f, "text/html")}, timeout=60)
    if not r.json().get("ok"):
        raise TelegramError(f"sendDocument: {r.json().get('description')}")


def find_chat_id() -> tuple[str, str] | None:
    """The private chat that most recently messaged the bot: (id, name)."""
    for upd in reversed(call("getUpdates", timeout=0)):
        chat = (upd.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            return str(chat["id"]), chat.get("first_name") or chat.get("username") or ""
    return None


def save_chat_id(cid: str, env_file: Path = ENV_FILE) -> None:
    text = env_file.read_text() if env_file.exists() else ""
    if re.search(r"^TELEGRAM_CHAT_ID=.*$", text, re.M):
        text = re.sub(r"^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={cid}", text, flags=re.M)
    else:
        text += f"\nTELEGRAM_CHAT_ID={cid}\n"
    env_file.write_text(text)
    os.chmod(env_file, 0o600)
    os.environ["TELEGRAM_CHAT_ID"] = cid
