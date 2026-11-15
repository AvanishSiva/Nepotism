"""Outreach state changes shared by the CLI, the portal and the Telegram bot.

Lane A lifecycle: drafted → (you send it, /sent) asked → (5 days, no reply: follow-up drafted;
you send it, /sent) followed_up → /replied → /referred.
Lane B: queued → sent (by the sender) → accepted (tracker) → a new Lane A row is drafted.
"""

from __future__ import annotations

import json
import sqlite3


def outreach_row(conn: sqlite3.Connection, oid: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT o.*, r.name, r.profile_url, r.headline, r.tags_json, r.company_id, c.name AS company
           FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
           LEFT JOIN companies c ON c.id = r.company_id WHERE o.id = ?""", (oid,)).fetchone()


def current_text(r: sqlite3.Row) -> tuple[str, str]:
    """(label, text) of what the user should copy now for this row."""
    if r["lane"] == "A" and r["status"] == "asked" and r["followup_text"] and not r["followed_up_at"]:
        return "follow-up", r["followup_text"]
    return ("referral ask" if r["lane"] == "A" else "connection note"), r["note_text"] or ""


def mark_sent(conn: sqlite3.Connection, oid: int) -> str:
    r = outreach_row(conn, oid)
    if not r:
        return f"no draft #{oid}"
    if r["lane"] == "B":
        return f"#{oid} is a connection note: the sender sends those (status {r['status']})"
    if r["status"] == "drafted":
        conn.execute("UPDATE outreach SET status = 'asked', asked_at = datetime('now') WHERE id = ?", (oid,))
        msg = f"#{oid} marked asked ({r['name']}, {r['company']}). Follow-up reminder in 5 days if no reply."
    elif r["status"] == "asked" and r["followup_text"]:
        conn.execute("UPDATE outreach SET status = 'followed_up', followed_up_at = datetime('now') WHERE id = ?", (oid,))
        msg = f"#{oid} follow-up marked sent. No more follow-ups for {r['name']}."
    else:
        return f"#{oid} is already '{r['status']}'"
    conn.commit()
    return msg


def mark_replied(conn: sqlite3.Connection, oid: int, *, referred: bool = False) -> str:
    r = outreach_row(conn, oid)
    if not r:
        return f"no draft #{oid}"
    status = "referred" if referred else "replied"
    conn.execute(f"UPDATE outreach SET status = ?, replied_at = coalesce(replied_at, datetime('now')) WHERE id = ?",
                 (status, oid))
    if referred:
        for jid in json.loads(r["job_ids_json"] or "[]"):
            conn.execute("UPDATE applications SET referred_by = ? WHERE job_id = ?", (r["referral_point_id"], jid))
    conn.commit()
    return f"#{oid} marked {status} ({r['name']}, {r['company']})"
