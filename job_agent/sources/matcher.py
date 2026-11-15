"""Alert job → ATS job, so we get the full JD (spec §5.3).

1. Find the company (exact normalised name → aliases → fuzzy name).
2. Unknown ATS? Optionally probe Greenhouse/Ashby/Lever with slugs guessed from the name.
3. Fetch the company's ATS feed (cached), fuzzy-match title (token_set_ratio ≥ 85)
   and require a compatible location.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from rapidfuzz import fuzz

from job_agent.http import Http, NotFound
from job_agent.normalize import norm_company, norm_text, norm_title_for_match
from job_agent.sources import ats_ashby, ats_greenhouse, ats_lever, ats_workday
from job_agent.sources.base import JobPosting
from job_agent.sources.companies import upsert_company

log = logging.getLogger(__name__)

TITLE_MIN = 85
COMPANY_FUZZY_MIN = 92
PROBE_EVERY_DAYS = 30
_LOC_STOP = {"county", "co", "remote", "hybrid", "on", "site", "onsite", "area", "greater",
             "metropolitan", "region", "the", "of", "and"}


# -- company lookup --------------------------------------------------------

def find_company(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    key = norm_company(name)
    if not key:
        return None
    row = conn.execute("SELECT * FROM companies WHERE name_norm = ?", (key,)).fetchone()
    if row:
        return row
    rows = conn.execute("SELECT * FROM companies").fetchall()
    for r in rows:
        if key in {norm_company(a) for a in json.loads(r["aliases_json"] or "[]")}:
            return r
    best = max(rows, key=lambda r: fuzz.ratio(key, r["name_norm"]), default=None)
    if best is not None and fuzz.ratio(key, best["name_norm"]) >= COMPANY_FUZZY_MIN:
        return best
    return None


def ensure_company(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = find_company(conn, name)
    if row:
        return row
    cid = upsert_company(conn, name, watch=0)
    return conn.execute("SELECT * FROM companies WHERE id = ?", (cid,)).fetchone()


# -- ATS probing for companies not on the watchlist --------------------------

def slug_guesses(name: str) -> list[str]:
    toks = norm_company(name).split()
    if not toks:
        return []
    guesses = ["".join(toks), "-".join(toks)]
    return list(dict.fromkeys(g for g in guesses if len(g) >= 3))


def probe_ats(conn: sqlite3.Connection, http: Http, company: sqlite3.Row) -> sqlite3.Row:
    """Try to discover a company's ATS. Remembers attempts for PROBE_EVERY_DAYS."""
    kv_key = f"ats_probe:{company['name_norm']}"
    last = conn.execute("SELECT value FROM kv WHERE key = ?", (kv_key,)).fetchone()
    if last and datetime.fromisoformat(last["value"]) > datetime.now(timezone.utc) - timedelta(days=PROBE_EVERY_DAYS):
        return company

    found = None
    for slug in slug_guesses(company["name"]):
        for ats, url in (
            ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
            ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{slug}"),
            ("lever", f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1"),
        ):
            try:
                data = http.get_json(url)
            except NotFound:
                continue
            except Exception as e:
                log.debug("probe %s %s: %s", ats, slug, e)
                continue
            jobs = data if isinstance(data, list) else data.get("jobs")
            if jobs:   # an empty board is as good as no board
                found = (ats, slug)
                break
        if found:
            break

    conn.execute(
        "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (kv_key, datetime.now(timezone.utc).isoformat()),
    )
    if found:
        log.info("probe: %s uses %s/%s", company["name"], *found)
        conn.execute("UPDATE companies SET ats = ?, slug = ?, updated_at = datetime('now') WHERE id = ?",
                     (*found, company["id"]))
    return conn.execute("SELECT * FROM companies WHERE id = ?", (company["id"],)).fetchone()


# -- title / location matching ----------------------------------------------

class _TitleFilter:
    """Duck-typed PreFilter: keep postings whose title is close to the alert's."""

    def __init__(self, title: str):
        self.t = norm_title_for_match(title)

    def title_ok(self, title: str) -> bool:
        return fuzz.token_set_ratio(self.t, norm_title_for_match(title)) >= TITLE_MIN

    def location_ok(self, posting: JobPosting) -> bool:
        return True

    def keep(self, posting: JobPosting) -> bool:
        return self.title_ok(posting.title)


def _loc_tokens(loc: str | None) -> set[str]:
    return {t for t in norm_text(loc).split() if t not in _LOC_STOP and len(t) > 1}


# Remote postings open to these regions are compatible with a remote alert anywhere in them.
_WIDE_REGIONS = {"emea", "europe", "eu", "european", "union", "worldwide", "anywhere", "global"}
# Words a company may add to a title that don't make it a different job.
_TITLE_LOC_WORDS = {"dublin", "ireland", "cork", "galway", "limerick", "remote", "hybrid", "onsite",
                    "site", "on", "emea", "europe", "eu", "uk", "ie", "based", "in"}


def location_compatible(alert_loc: str | None, posting: JobPosting) -> bool:
    want = _loc_tokens(alert_loc)
    if not want:
        return True
    have = _loc_tokens(posting.all_locations_text())
    if want & have:
        return True
    # Remote alert ("Ireland (Remote)") vs remote posting: the posting must not be
    # tied to some other country (aggregators post one copy per country).
    alert_remote = bool(re.search(r"\bremote\b", alert_loc or "", re.I))
    if alert_remote and posting.remote_type == "remote":
        return not have or bool(have & _WIDE_REGIONS)
    return False


def title_compatible(alert_title: str, ats_title: str, alert_loc: str | None = None) -> bool:
    """Same job title, allowing level numerals and added location words only.

    "Software Engineer III" ≈ "Software Engineer 3"; "Software Engineer - Dublin" ≈
    "Software Engineer"; but "Forward Deployed Engineer Dutch/German speakers" is a
    different job from "Forward Deployed Engineer".
    """
    a, b = norm_title_for_match(alert_title), norm_title_for_match(ats_title)
    if fuzz.token_set_ratio(a, b) < TITLE_MIN:
        return False
    if fuzz.token_sort_ratio(a, b) >= 90:
        return True
    allowed = _TITLE_LOC_WORDS | _loc_tokens(alert_loc)
    ta, tb = set(a.split()), set(b.split())
    return (tb - ta) <= allowed and (ta - tb) <= allowed


def best_match(alert: JobPosting, candidates: list[JobPosting]) -> JobPosting | None:
    t = norm_title_for_match(alert.title)
    scored = []
    for c in candidates:
        ct = norm_title_for_match(c.title)
        set_r = fuzz.token_set_ratio(t, ct)
        if not title_compatible(alert.title, c.title, alert.location) \
                or not location_compatible(alert.location, c):
            continue
        # token_set alone says "Software Engineer" ≈ "Software Engineer, Payments";
        # rank by the stricter token_sort_ratio, exact title first.
        scored.append((norm_text(alert.title) == norm_text(c.title), fuzz.token_sort_ratio(t, ct), set_r, c))
    if not scored:
        return None
    scored.sort(key=lambda s: s[:3], reverse=True)
    return scored[0][3]


def _candidates(http: Http, company: dict, title: str, workday_pages: int) -> list[JobPosting]:
    flt = _TitleFilter(title)
    ats = company["ats"]
    if ats == "greenhouse":
        return ats_greenhouse.fetch(http, company, flt)[1]
    if ats == "lever":
        return ats_lever.fetch(http, company, flt)[1]
    if ats == "ashby":
        return ats_ashby.fetch(http, company, flt)[1]
    if ats == "workday":
        return ats_workday.fetch(http, company, flt, [title], max_pages=workday_pages)[1]
    return []


def enrich(conn: sqlite3.Connection, http: Http, alert: JobPosting, *,
           probe_unknown: bool = True) -> tuple[JobPosting, int, str]:
    """Returns (posting_to_store, company_id, outcome).

    outcome: matched | no_title_match | no_ats | ats_error
    """
    company = ensure_company(conn, alert.company)
    if not company["ats"] and probe_unknown:
        company = probe_ats(conn, http, company)
    if not company["ats"]:
        return alert, company["id"], "no_ats"

    try:
        cands = _candidates(http, dict(company), alert.title, workday_pages=1)
    except Exception as e:
        log.warning("ATS fetch for %s failed: %s", company["name"], e)
        return alert, company["id"], "ats_error"

    match = best_match(alert, cands)
    if match is None:
        return alert, company["id"], "no_title_match"

    match.linkedin_job_id = alert.linkedin_job_id
    match.linkedin_job_url = alert.linkedin_job_url
    match.alert_date = alert.alert_date
    return match, company["id"], "matched"
