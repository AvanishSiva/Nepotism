"""Closed-job detection (runs inside discover).

A job is closed when:
  - Greenhouse / Lever / Ashby: its id has been missing from the company's full feed for
    `close_after_days` (3) days. The grace period absorbs a feed glitch, and an empty feed
    never closes anything.
  - Workday: the listing is a capped keyword search, so absence means nothing. Each open
    Workday job not refreshed today gets its detail page checked; 404 or no posting → closed.
  - Alert-only jobs (no ATS link; we never open LinkedIn job pages): not seen in any alert
    for `alert_expire_days` (30) days.
A closed job that shows up again is reopened (store.upsert_job clears closed_at).
Closed jobs drop out of referrals, drafts, the digest and `ls`. Outreach that hasn't gone
out yet (queued notes, unsent Lane A drafts) for roles that are all closed is dropped.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from job_agent.http import Http, NotFound
from job_agent.normalize import TS_FMT, now_utc

log = logging.getLogger(__name__)


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(TS_FMT)


def _close(conn: sqlite3.Connection, job_id: int, reason: str) -> None:
    conn.execute("UPDATE jobs SET closed_at = ?, closed_reason = ? WHERE id = ?", (now_utc(), reason, job_id))


def _label(r) -> str:
    return f"{r['company'] or '?'}: {r['title']}"


def reconcile(conn: sqlite3.Connection, http: Http, company: dict, feed_ids: set[str] | None,
              close_after_days: int = 3) -> dict:
    """After a successful fetch of one company. Returns {'closed': [labels], 'reopened': n}."""
    out = {"closed": [], "reopened": 0}
    rows = conn.execute(
        """SELECT j.id, j.title, j.ats_job_id, j.apply_url, j.last_seen_at, j.closed_at, c.name AS company
           FROM jobs j LEFT JOIN companies c ON c.id = j.company_id
           WHERE j.company_id = ? AND j.ats = ? AND j.ats_job_id IS NOT NULL""",
        (company["id"], company["ats"])).fetchall()
    if not rows:
        return out
    now, cutoff, today = now_utc(), _ago(close_after_days), now_utc()[:10]

    if feed_ids is not None:
        if not feed_ids:
            log.warning("%s: empty feed, not closing anything", company["name"])
            return out
        for r in rows:
            if r["ats_job_id"] in feed_ids:
                # Still listed (maybe filtered out of `kept` now): it's open.
                if r["closed_at"]:
                    out["reopened"] += 1
                conn.execute("UPDATE jobs SET last_seen_at = ?, closed_at = NULL, closed_reason = NULL WHERE id = ?",
                             (now, r["id"]))
            elif not r["closed_at"] and r["last_seen_at"] <= cutoff:
                _close(conn, r["id"], f"gone from {company['ats']} feed since {r['last_seen_at'][:10]}")
                out["closed"].append(_label(r))
        return out

    # Workday: check each open job the listing didn't refresh today.
    from job_agent.sources.ats_workday import base_url, detail_path
    for r in rows:
        if r["closed_at"] or r["last_seen_at"][:10] == today:
            continue
        path = detail_path(r["apply_url"])
        if not path:
            continue
        try:
            data = http.get_json(f"{base_url(company['slug'])}{path}")
            gone = not (data.get("jobPostingInfo") or {}).get("title")
        except NotFound:
            gone = True
        except Exception as e:                       # network/5xx: unknown, never close on that
            log.warning("workday check failed for %s: %s", r["title"], e)
            continue
        if gone:
            _close(conn, r["id"], "Workday posting removed")
            out["closed"].append(_label(r))
        else:
            conn.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ?", (now, r["id"]))
    return out


def expire_alert_only(conn: sqlite3.Connection, days: int = 30) -> list[str]:
    """Jobs no daily feed refreshes (alert-only, or matched to an unwatched company's ATS)."""
    rows = conn.execute(
        """SELECT j.id, j.title, c.name AS company FROM jobs j LEFT JOIN companies c ON c.id = j.company_id
           WHERE j.closed_at IS NULL AND j.last_seen_at <= ?
             AND (j.ats IS NULL OR j.ats_job_id IS NULL OR coalesce(c.watch, 0) = 0)""",
        (_ago(days),)).fetchall()
    for r in rows:
        _close(conn, r["id"], f"alert-only, not seen for {days} days")
    return [_label(r) for r in rows]


def drop_outreach_for_closed(conn: sqlite3.Connection) -> list[str]:
    """Unsent outreach whose roles are ALL closed → aborted ('role closed'). Returns who."""
    dropped = []
    rows = conn.execute(
        """SELECT o.id, o.job_ids_json, r.name FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
           WHERE (o.lane = 'B' AND o.status = 'queued') OR (o.lane = 'A' AND o.status = 'drafted')""").fetchall()
    for r in rows:
        ids = json.loads(r["job_ids_json"] or "[]")
        if not ids:
            continue
        open_n = conn.execute(f"SELECT count(*) FROM jobs WHERE id IN ({','.join('?' * len(ids))}) "
                              f"AND closed_at IS NULL", ids).fetchone()[0]
        if open_n == 0:
            conn.execute("UPDATE outreach SET status = 'aborted', error = 'role closed before sending' WHERE id = ?",
                         (r["id"],))
            dropped.append(r["name"])
    return dropped
