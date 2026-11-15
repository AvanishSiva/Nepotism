"""Telegram command bot (spec §10). Polling only, no webhook/server.

Only messages from YOUR chat (TELEGRAM_CHAT_ID) are acted on; anything else is ignored.
Command text is never passed to an LLM.

`python -m job_agent bot`        one pass over pending messages (cron-style)
`python -m job_agent bot --loop` long-poll forever (the launchd agent uses this)
"""

from __future__ import annotations

import logging
import sqlite3
import time

from job_agent.config import Config
from job_agent.db import kv_get, kv_set
from job_agent.notify import telegram as tg

log = logging.getLogger(__name__)

HELP = [
    ("draft", "<id>  show a draft to copy"),
    ("sent", "<id>  you sent that Lane A message (or its follow-up)"),
    ("replied", "<id>  they replied (stops follow-ups)"),
    ("referred", "<id>  they referred you"),
    ("drafts", "outreach waiting on you"),
    ("applied", "<job_id>  mark a job applied"),
    ("skip", "<job_id>  hide a job"),
    ("good", "<job_id>  good match (feedback)"),
    ("bad", "<job_id>  bad match (feedback)"),
    ("pause", "stop all LinkedIn activity"),
    ("resume", "resume LinkedIn activity"),
    ("status", "safety counters, queue, last runs"),
    ("help", "this list"),
]


def _int(args: list[str]) -> int | None:
    try:
        return int(args[0].lstrip("#"))
    except (IndexError, ValueError):
        return None


def _portal(conn, line: str) -> str:
    """Reuse the portal's job commands (applied/skip/good/bad) and flatten to plain text."""
    from job_agent.portal.commands import execute
    out = execute(conn, line)
    return "\n".join("".join(seg.get("t", "") for seg in row) for row in out.lines).strip() or "ok"


def cmd_status(conn: sqlite3.Connection, cfg: Config) -> str:
    from job_agent.referrals.browser import chrome_available
    from job_agent.safety import Safety

    s = Safety(conn, cfg)
    c = s.counters()
    cap, why = s.requests_cap()
    mode = (cfg.linkedin or {}).get("send_mode", "dry_run")
    q = dict(conn.execute("SELECT lane || ':' || status, count(*) FROM outreach GROUP BY 1").fetchall())
    lines = [f"<b>Status</b> ({time.strftime('%a %H:%M')})",
             f"LinkedIn: {'⏸ PAUSED until ' + s.paused_until()[:16] if s.paused_until() else 'active'}"
             f", {'in' if s.in_window() else 'outside'} window {s.window['start']}–{s.window['end']}",
             f"Today: views {c['page_views']}/{s.views_left() + c['page_views']}, requests {c['requests_sent']}/{cap}, "
             f"withdrawals {c['withdrawals']}/{s.caps['withdrawals']}",
             f"Send mode: {tg.esc(mode)}" + (f" ({tg.esc('; '.join(why))})" if why and mode == "live" else ""),
             f"Chrome :9222: {'up' if chrome_available() else 'DOWN'}",
             f"Outreach: A drafted {q.get('A:drafted', 0)}, asked {q.get('A:asked', 0)} | "
             f"B queued {q.get('B:queued', 0)}, pending {q.get('B:sent', 0)}, accepted {q.get('B:accepted', 0)}"]
    rate, n = s.acceptance_rate()
    if rate is not None:
        lines.append(f"Acceptance rate (30d): {rate:.0%} of {n}")
    lines.append("\n<b>Last runs</b>")
    for r in conn.execute("SELECT stage, ok, max(started_at) AS t FROM runs GROUP BY stage ORDER BY t DESC LIMIT 8"):
        lines.append(f"{'✅' if r['ok'] else '❌'} {tg.esc(r['stage'])} {r['t'][5:16]} UTC")
    return "\n".join(lines)


def cmd_drafts(conn: sqlite3.Connection) -> str:
    from job_agent.outreach.actions import current_text
    rows = conn.execute(
        """SELECT o.*, r.name, c.name AS company FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
           LEFT JOIN companies c ON c.id = r.company_id
           WHERE (o.lane = 'A' AND o.status = 'drafted')
              OR (o.lane = 'A' AND o.status = 'asked' AND o.followup_text IS NOT NULL AND o.followed_up_at IS NULL)
              OR (o.lane = 'B' AND o.status = 'queued')
           ORDER BY o.lane, o.id""").fetchall()
    if not rows:
        return "Nothing waiting on you."
    out = ["<b>Waiting</b>"]
    for r in rows:
        label, _ = current_text(r)
        who = f"{tg.esc(r['name'])} ({tg.esc(r['company'])})"
        if r["lane"] == "A":
            out.append(f"A) {who}: {label} → /draft {r['id']}")
        else:
            out.append(f"B) {who}: connection note queued (sender)")
    return "\n".join(out)


def cmd_draft(conn: sqlite3.Connection, oid: int) -> list[str]:
    from job_agent.outreach.actions import current_text, outreach_row
    r = outreach_row(conn, oid)
    if not r:
        return [f"No draft #{oid}."]
    label, text = current_text(r)
    head = (f"<b>#{oid} [{r['lane']}] {tg.esc(label)}</b>: {tg.esc(r['name'])}, {tg.esc(r['company'])} "
            f"({r['status']})\n{tg.esc(r['profile_url'])}")
    if r["low_quality"]:
        head += "\n⚠ drafted by the local model (lower quality): read it before sending"
    tail = (f"Copy the text above, send it on LinkedIn, then /sent {oid}" if r["lane"] == "A"
            else "Lane B: the sender sends this automatically.")
    return [head, f"<pre>{tg.esc(text)}</pre>", tail]


def handle(conn: sqlite3.Connection, cfg: Config, text: str) -> list[str]:
    """One command → reply messages (HTML)."""
    from job_agent.outreach import actions
    from job_agent.safety import Safety

    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return ["Send /help for commands."]
    cmd, args = parts[0][1:].split("@")[0].lower(), parts[1:]
    n = _int(args)
    needs_id = {"draft", "sent", "replied", "referred", "applied", "skip", "good", "bad"}
    if cmd in needs_id and n is None:
        return [f"Usage: /{cmd} &lt;id&gt;"]
    if cmd in ("help", "start"):
        return ["\n".join(f"/{c} {tg.esc(d)}" for c, d in HELP)]
    if cmd == "status":
        return [cmd_status(conn, cfg)]
    if cmd == "drafts":
        return [cmd_drafts(conn)]
    if cmd == "draft":
        return cmd_draft(conn, n)
    if cmd == "sent":
        return [tg.esc(actions.mark_sent(conn, n))]
    if cmd in ("replied", "referred"):
        return [tg.esc(actions.mark_replied(conn, n, referred=cmd == "referred"))]
    if cmd in ("applied", "skip", "good", "bad"):
        return [tg.esc(_portal(conn, f"{cmd} {n}"))]
    if cmd == "pause":
        until = Safety(conn, cfg).pause("manual /pause from Telegram", days=365)
        return [f"⏸ LinkedIn paused (until {until[:10]} or /resume). Job discovery and digests continue."]
    if cmd == "resume":
        s = Safety(conn, cfg)
        s.resume()
        phase = s.restriction_phase()
        extra = ("\nAfter a restriction: manual-only for 7 days, then 25% caps for 7 days."
                 if phase else "")
        return [f"▶ LinkedIn resumed.{extra}"]
    return [f"Unknown command /{tg.esc(cmd)}. Send /help."]


def poll_once(conn: sqlite3.Connection, cfg: Config, timeout: int = 0) -> int:
    mine = str(tg.chat_id())
    offset = int(kv_get(conn, "tg_offset") or 0)
    updates = tg.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])
    handled = 0
    for u in updates:
        kv_set(conn, "tg_offset", str(u["update_id"] + 1))       # never re-handle, even if it fails
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != mine:
            log.warning("ignored Telegram message from another chat %s", (msg.get("chat") or {}).get("id"))
            continue
        try:
            replies = handle(conn, cfg, msg.get("text") or "")
        except Exception as e:                                    # never kill the loop
            log.exception("bot command failed")
            replies = [f"Error: {tg.esc(str(e))[:300]}"]
        for r in replies:
            tg.call("sendMessage", chat_id=mine, text=r, parse_mode="HTML", disable_web_page_preview=True)
        handled += 1
    return handled


def register_commands() -> None:
    try:
        tg.call("setMyCommands", commands=[{"command": c, "description": d[:60]} for c, d in HELP])
    except tg.TelegramError as e:
        log.warning("setMyCommands failed: %s", e)


def loop(conn: sqlite3.Connection, cfg: Config) -> None:
    register_commands()
    backoff = 5
    while True:
        try:
            poll_once(conn, cfg, timeout=50)
            backoff = 5
        except Exception as e:                                    # network down, laptop asleep, ...
            log.warning("bot poll failed: %s (retry in %ds)", e, backoff)
            time.sleep(backoff)
            backoff = min(300, backoff * 2)
