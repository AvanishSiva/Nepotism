"""Discover stage, ATS part: poll every watched company and store matching jobs."""

from __future__ import annotations

import logging
import sqlite3

from job_agent.config import Config
from job_agent.http import Http
from job_agent.normalize import norm_company
from job_agent.sources import ats_ashby, ats_greenhouse, ats_lever, ats_workday
from job_agent.sources.base import JobPosting, PreFilter
from job_agent.sources.companies import load_watchlist, sync_watchlist
from job_agent.store import upsert_job

log = logging.getLogger(__name__)


def _fetch(http: Http, cfg: Config, company: dict, flt: PreFilter) -> tuple[int, list[JobPosting]]:
    ats = company["ats"]
    if ats == "greenhouse":
        return ats_greenhouse.fetch(http, company, flt)
    if ats == "lever":
        return ats_lever.fetch(http, company, flt)
    if ats == "ashby":
        return ats_ashby.fetch(http, company, flt)
    if ats == "workday":
        return ats_workday.fetch(http, company, flt, cfg.discover.workday_search_terms,
                                 cfg.discover.workday_max_pages)
    raise ValueError(f"unknown ats {ats}")


def discover_ats(conn: sqlite3.Connection, cfg: Config, *, only: str | None = None,
                 use_cache: bool = True, write: bool = True) -> dict:
    """Poll ATS feeds. With write=False, nothing is stored (prints only)."""
    sync = sync_watchlist(conn, load_watchlist(cfg.path(cfg.discover.watchlist_file)))
    rows = conn.execute("SELECT * FROM companies WHERE watch = 1 AND ats IS NOT NULL ORDER BY name").fetchall()
    if only:
        rows = [r for r in rows if norm_company(r["name"]) == norm_company(only)]
        if not rows:
            raise SystemExit(f"'{only}' is not a watched company (check data/companies.yaml)")

    flt = PreFilter(cfg.preferences)
    http = Http(conn, cfg.discover.user_agent, cfg.discover.request_interval_s,
                cfg.discover.http_cache_hours, use_cache=use_cache)
    stats = {"companies": len(rows), "seen": 0, "kept": 0, "new": 0, "updated": 0,
             "errors": [], "per_company": {}, "watchlist": sync}
    try:
        for row in rows:
            company = dict(row)
            try:
                seen, kept = _fetch(http, cfg, company, flt)
            except Exception as e:
                log.error("%s (%s/%s): %s", company["name"], company["ats"], company["slug"], e)
                stats["errors"].append(f"{company['name']}: {e}")
                continue

            for p in kept:
                p.location = flt.describe_location(p)

            new = updated = 0
            if write:
                for p in kept:
                    if upsert_job(conn, company["id"], p) == "new":
                        new += 1
                    else:
                        updated += 1
                if company.get("region") != row["region"]:  # Lever host detection
                    conn.execute("UPDATE companies SET region = ? WHERE id = ?",
                                 (company["region"], company["id"]))
                conn.commit()
            else:
                for p in kept:
                    log.info("  [dry] %s | %s | %s", company["name"], p.title, p.location)

            stats["seen"] += seen
            stats["kept"] += len(kept)
            stats["new"] += new
            stats["updated"] += updated
            stats["per_company"][company["name"]] = {"seen": seen, "kept": len(kept), "new": new}
            log.info("%-12s %-10s seen=%4d kept=%3d new=%3d", company["name"], company["ats"],
                     seen, len(kept), new)
    finally:
        stats["http"] = dict(http.stats)
        http.close()
    return stats


# ---------------------------------------------------------------------------
# Gmail alerts
# ---------------------------------------------------------------------------

def discover_gmail(conn: sqlite3.Connection, cfg: Config, *, use_cache: bool = True,
                   write: bool = True, reprocess: bool = False) -> dict:
    """Read LinkedIn job-alert emails, match each job to an ATS for the full JD, store."""
    from job_agent.sources import gmail_alerts, gmail_api, matcher

    query = cfg.gmail.get("linkedin_alert_query")
    if not query or "<verify>" in query:
        raise SystemExit("Set gmail.linkedin_alert_query in config.yaml (sender not verified yet).")
    model = cfg.llm.get("ollama_model")
    probe = bool(cfg.gmail.get("probe_unknown_ats", True))

    flt = PreFilter(cfg.preferences)
    svc = gmail_api.service()
    ids = gmail_api.search(svc, query, int(cfg.gmail.get("max_messages", 50)))
    http = Http(conn, cfg.discover.user_agent, cfg.discover.request_interval_s,
                cfg.discover.http_cache_hours, use_cache=use_cache)
    stats = {"emails": len(ids), "emails_skipped": 0, "emails_parsed": 0, "jobs": 0,
             "new": 0, "updated": 0, "parse_methods": {}, "match": {}, "incomplete_emails": []}
    try:
        for mid in ids:
            if not reprocess and conn.execute(
                    "SELECT 1 FROM processed_emails WHERE message_id = ?", (mid,)).fetchone():
                stats["emails_skipped"] += 1
                continue
            msg = gmail_api.get(svc, mid)
            jobs, method = gmail_alerts.parse_alert(msg, model)
            expected = len(gmail_alerts.job_ids_in(msg))
            stats["parse_methods"][method] = stats["parse_methods"].get(method, 0) + 1
            if len(jobs) < expected:
                stats["incomplete_emails"].append(f"{mid}: {len(jobs)}/{expected} ({msg.subject[:60]})")
            log.info("email %s %r: %d/%d jobs via %s", mid, msg.subject[:50], len(jobs), expected, method)

            for alert in jobs:
                posting, company_id, outcome = matcher.enrich(conn, http, alert, probe_unknown=probe)
                if outcome == "matched":   # same display rule as the ATS stage ("UK / Dublin")
                    posting.location = flt.describe_location(posting)
                stats["match"][outcome] = stats["match"].get(outcome, 0) + 1
                log.info("  %-15s %s | %s | %s", outcome, alert.company, alert.title, alert.location)
                if write:
                    if upsert_job(conn, company_id, posting) == "new":
                        stats["new"] += 1
                    else:
                        stats["updated"] += 1
            stats["jobs"] += len(jobs)
            stats["emails_parsed"] += 1
            # Only mark done if everything in it was parsed; otherwise retry next run.
            if write and len(jobs) == expected:
                conn.execute("INSERT OR IGNORE INTO processed_emails(message_id, kind) VALUES(?, 'job_alert')",
                             (mid,))
            if write:
                conn.commit()
            else:
                conn.rollback()   # dry run: best effort (the HTTP cache commits on its own)
    finally:
        stats["http"] = dict(http.stats)
        http.close()
    return stats
