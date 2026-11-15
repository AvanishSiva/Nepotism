"""Acceptance tracking + follow-ups (spec §8). Runs every 2 h; Gmail read-only, no LinkedIn.

Acceptance emails (verified 2026-11-15 from real mail):
  From:    "Krishnanunni Raju via LinkedIn" <invitations@linkedin.com>
  Subject: "Krishnanunni accepted your invitation, explore their network"
  Body:    links to the accepter's /in/<slug> (plus "people you may know" slugs).
Matching a Lane B request needs the person's profile slug among the email's links AND the
sender name to match, so a namesake or a suggested profile can't be mistaken for them.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime

from rapidfuzz import fuzz

from job_agent.config import Config
from job_agent.db import kv_get, kv_set
from job_agent.normalize import norm_text, now_utc

log = logging.getLogger(__name__)

FOLLOWUP_AFTER_DAYS = 5
_SENDER = re.compile(r'^\s*"?(.+?)"?\s+via LinkedIn', re.I)
_SLUG = re.compile(r"linkedin\.com/(?:comm/)?in/([A-Za-z0-9\-_%]+)")


def parse_acceptance(sender: str, subject: str, body: str) -> tuple[str, set[str]] | None:
    """(full name, profile slugs in the email) or None if it isn't an acceptance email."""
    if "accepted your invitation" not in (subject or "").lower():
        return None
    m = _SENDER.match(sender or "")
    name = m.group(1).strip() if m else (subject or "").split(" accepted")[0].strip()
    return name, {s.lower() for s in _SLUG.findall(body or "")}


def _slug_of(url: str) -> str | None:
    m = re.search(r"/in/([^/?#]+)", url or "")
    return m.group(1).lower() if m else None


def match_outreach(conn: sqlite3.Connection, name: str, slugs: set[str]) -> sqlite3.Row | None:
    rows = conn.execute(
        """SELECT o.*, r.name, r.profile_url FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
           WHERE o.lane = 'B' AND o.status = 'sent'""").fetchall()
    for r in rows:
        if _slug_of(r["profile_url"]) in slugs and fuzz.token_set_ratio(norm_text(r["name"]), norm_text(name)) >= 80:
            return r
    return None


def followup_text(cfg: Config, first: str, role: str, company: str) -> str:
    me = cfg.profile.get("short_name") or cfg.profile.get("name") or ""
    return (f"Hi {first}, just following up on my message about the {role} role at {company}. "
            f"I know you're busy, so no worries if it's not a fit. If you're comfortable referring me, "
            f"I'm happy to send my CV and anything else that helps. Thanks, {me}")


def _lane_a_for(conn, cfg, b_row, stats) -> int:
    """Create the referral-ask draft for someone who just accepted."""
    from job_agent.outreach import drafts
    from job_agent.referrals.linkedin_people import people_for

    rp = conn.execute("SELECT * FROM referral_points WHERE id = ?", (b_row["referral_point_id"],)).fetchone()
    person = next(p for p in people_for(conn, rp["company_id"]) if p.profile_url == rp["profile_url"])
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (rp["company_id"],)).fetchone()["name"]
    ids = json.loads(b_row["job_ids_json"] or "[]")
    jobs = conn.execute(f"SELECT id, title, apply_url, linkedin_job_url FROM jobs WHERE id IN "
                        f"({','.join('?' * len(ids))})", ids).fetchall() if ids else []
    roles = [j["title"] for j in jobs] or ["software engineering"]
    url = (jobs[0]["apply_url"] or jobs[0]["linkedin_job_url"]) if jobs else None
    ctx = drafts.Context(person, company, roles[:2], ids, url, "A",
                         drafts.shared_link(person, cfg, company, roles[0]), drafts.proof_point(cfg, None))
    note, low = drafts.write_note(ctx, cfg, stats)
    oid = conn.execute("""INSERT INTO outreach(referral_point_id, job_ids_json, lane, note_text, low_quality, status)
                          VALUES(?,?,?,?,?,'drafted')""", (rp["id"], json.dumps(ids), "A", note, int(low))).lastrowid
    conn.commit()
    return oid


def check_acceptances(conn: sqlite3.Connection, cfg: Config, notify=None) -> dict:
    from job_agent.sources import gmail_api

    stats = {"emails": 0, "matched": 0, "drafted": 0}
    query = (cfg.gmail or {}).get("accepted_query",
                                  'from:invitations@linkedin.com subject:"accepted your invitation" newer_than:3d')
    svc = gmail_api.service()
    for mid in gmail_api.search(svc, query, 50):
        if conn.execute("SELECT 1 FROM processed_emails WHERE message_id = ?", (mid,)).fetchone():
            continue
        m = gmail_api.get(svc, mid)
        stats["emails"] += 1
        parsed = parse_acceptance(m.sender, m.subject, m.html + "\n" + m.text)
        row = match_outreach(conn, *parsed) if parsed else None
        if row:
            stats["matched"] += 1
            conn.execute("UPDATE outreach SET status = 'accepted', accepted_at = ? WHERE id = ?",
                         (m.date or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), row["id"]))
            rp = conn.execute("SELECT tags_json FROM referral_points WHERE id = ?", (row["referral_point_id"],)).fetchone()
            tags = sorted(set(json.loads(rp["tags_json"] or "[]")) | {"connection"})
            conn.execute("UPDATE referral_points SET degree = '1st', tags_json = ? WHERE id = ?",
                         (json.dumps(tags), row["referral_point_id"]))
            conn.commit()
            new_id = _lane_a_for(conn, cfg, row, stats)
            stats["drafted"] += 1
            if notify:
                notify(f"✅ {row['name']} accepted your connection request.\n"
                       f"Referral ask drafted: /draft {new_id}\nSend it yourself, then /sent {new_id}")
        conn.execute("INSERT OR IGNORE INTO processed_emails(message_id, kind) VALUES(?, 'accepted')", (mid,))
        conn.commit()
    return stats


def draft_followups(conn: sqlite3.Connection, cfg: Config, notify=None) -> dict:
    from job_agent.outreach.drafts import first_name

    stats = {"followups": 0}
    rows = conn.execute(
        f"""SELECT o.*, r.name, c.name AS company FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
            LEFT JOIN companies c ON c.id = r.company_id
            WHERE o.lane = 'A' AND o.status = 'asked' AND o.replied_at IS NULL AND o.followup_text IS NULL
              AND o.asked_at <= datetime('now', '-{FOLLOWUP_AFTER_DAYS} days')""").fetchall()
    for r in rows:
        ids = json.loads(r["job_ids_json"] or "[]")
        t = conn.execute("SELECT title FROM jobs WHERE id = ?", (ids[0],)).fetchone() if ids else None
        text = followup_text(cfg, first_name(r["name"]), t["title"] if t else "open", r["company"] or "")
        conn.execute("UPDATE outreach SET followup_text = ? WHERE id = ?", (text, r["id"]))
        conn.commit()
        stats["followups"] += 1
        if notify:
            notify(f"⏰ No reply from {r['name']} ({r['company']}) after {FOLLOWUP_AFTER_DAYS} days.\n"
                   f"One gentle follow-up is ready: /draft {r['id']}  (then /sent {r['id']}, or /replied {r['id']})")
    return stats


def gmail_health(conn: sqlite3.Connection, notify, error: str) -> None:
    """Telegram warning once per day when the Gmail token is dead (Testing mode: weekly)."""
    today = datetime.now().date().isoformat()
    if kv_get(conn, "gmail_alert_day") == today:
        return
    kv_set(conn, "gmail_alert_day", today)
    if notify:
        notify(f"🔑 Gmail access failed: {error}\nOn your Mac run: python3 -m job_agent gmail-auth")


def run(conn: sqlite3.Connection, cfg: Config, notify=None) -> dict:
    from job_agent.sources.gmail_api import GmailAuthError

    stats: dict = {}
    try:
        stats.update(check_acceptances(conn, cfg, notify))
    except GmailAuthError as e:
        stats["gmail_error"] = str(e)
        gmail_health(conn, notify, str(e))
    stats.update(draft_followups(conn, cfg, notify))
    return stats
