"""Job persistence: insert new postings, refresh ones we've seen before."""

from __future__ import annotations

import sqlite3

from job_agent.normalize import dedupe_key, now_utc
from job_agent.sources.base import JobPosting

ATS_SOURCES = {"greenhouse", "lever", "ashby", "workday"}

# Columns an ATS refresh may overwrite (the ATS is the source of truth for these).
_ATS_FIELDS = ("title", "location", "remote_type", "posted_at", "apply_url", "jd_text",
               "salary_min", "salary_max", "currency", "ats", "ats_job_id")
# Columns that are only filled in if currently empty (e.g. alert data on an ATS job).
_FILL_FIELDS = ("linkedin_job_url", "linkedin_job_id", "alert_date")


def _merge_rows(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    """Fold a duplicate job row into another (alert-only row → its ATS row)."""
    if not conn.execute("SELECT 1 FROM applications WHERE job_id = ?", (keep_id,)).fetchone():
        conn.execute("UPDATE applications SET job_id = ? WHERE job_id = ?", (keep_id, drop_id))
    conn.execute("DELETE FROM applications WHERE job_id = ?", (drop_id,))
    conn.execute("UPDATE feedback SET job_id = ? WHERE job_id = ?", (keep_id, drop_id))
    conn.execute("DELETE FROM job_scores WHERE job_id = ?", (drop_id,))
    drop = conn.execute("SELECT first_seen_at, linkedin_job_url, linkedin_job_id, alert_date "
                        "FROM jobs WHERE id = ?", (drop_id,)).fetchone()
    conn.execute("DELETE FROM jobs WHERE id = ?", (drop_id,))
    conn.execute(
        "UPDATE jobs SET first_seen_at = min(first_seen_at, ?), "
        "linkedin_job_url = coalesce(linkedin_job_url, ?), linkedin_job_id = coalesce(linkedin_job_id, ?), "
        "alert_date = coalesce(alert_date, ?) WHERE id = ?",
        (drop["first_seen_at"], drop["linkedin_job_url"], drop["linkedin_job_id"], drop["alert_date"], keep_id),
    )


def _find_existing(conn: sqlite3.Connection, p: JobPosting, key: str):
    by_ats = by_li = None
    if p.ats and p.ats_job_id:
        by_ats = conn.execute("SELECT * FROM jobs WHERE ats = ? AND ats_job_id = ?",
                              (p.ats, p.ats_job_id)).fetchone()
    if p.linkedin_job_id:
        by_li = conn.execute("SELECT * FROM jobs WHERE linkedin_job_id = ?",
                             (p.linkedin_job_id,)).fetchone()
    if by_ats and by_li and by_ats["id"] != by_li["id"]:
        _merge_rows(conn, by_ats["id"], by_li["id"])
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (by_ats["id"],)).fetchone()
    if by_ats or by_li:
        return by_ats or by_li
    return conn.execute("SELECT * FROM jobs WHERE dedupe_key = ?", (key,)).fetchone()


def upsert_job(conn: sqlite3.Connection, company_id: int | None, p: JobPosting) -> str:
    """Returns 'new' or 'updated'. Caller commits."""
    key = dedupe_key(p.company, p.title, p.location)
    now = now_utc()
    jd_status = "ok" if p.jd_text else "missing"
    existing = _find_existing(conn, p, key)

    if existing is None:
        conn.execute(
            """INSERT INTO jobs(company_id, source, title, location, remote_type, posted_at,
                   alert_date, apply_url, linkedin_job_url, linkedin_job_id, jd_text, jd_status,
                   salary_min, salary_max, currency, ats, ats_job_id, dedupe_key,
                   first_seen_at, last_seen_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (company_id, p.source, p.title, p.location, p.remote_type, p.posted_at,
             p.alert_date, p.apply_url, p.linkedin_job_url, p.linkedin_job_id, p.jd_text,
             jd_status, p.salary_min, p.salary_max, p.currency, p.ats, p.ats_job_id, key,
             now, now),
        )
        return "new"

    sets: dict[str, object] = {"last_seen_at": now, "closed_at": None, "closed_reason": None}  # seen → open
    if p.source in ATS_SOURCES:
        for f in _ATS_FIELDS:
            v = getattr(p, f)
            if v is not None:
                sets[f] = v
        if p.jd_text:
            sets["jd_status"] = "ok"
    for f in _FILL_FIELDS:
        v = getattr(p, f)
        if v is not None and existing[f] is None:
            sets[f] = v
    if company_id and existing["company_id"] is None:
        sets["company_id"] = company_id

    conn.execute(
        f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
        (*sets.values(), existing["id"]),
    )
    return "updated"
