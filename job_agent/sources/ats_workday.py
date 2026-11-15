"""Workday CXS endpoints (undocumented; may change).

slug format in `companies`: "<tenant>/wd<N>/<site>", e.g. "salesforce/wd12/External_Career_Site".

The listing only has title + a short location string, so we run title/location
checks on the listing first and fetch the detail page (full JD, date, remote
type) only for jobs that pass.
"""

from __future__ import annotations

import logging
import re

from job_agent.http import Http
from job_agent.normalize import html_to_text, infer_remote_type, to_utc_str
from job_agent.sources.base import JobPosting, PreFilter

log = logging.getLogger(__name__)
PAGE = 20
_MULTI_LOC = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def split_slug(slug: str) -> tuple[str, str, str]:
    tenant, wd, site = slug.split("/", 2)
    return tenant, wd, site


def base_url(slug: str) -> str:
    tenant, wd, site = split_slug(slug)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def parse_detail(data: dict, company: str, slug: str) -> JobPosting:
    info = data.get("jobPostingInfo") or {}
    tenant, wd, site = split_slug(slug)
    loc = info.get("location") or ""
    extra = [x for x in info.get("additionalLocations") or [] if x]
    country = (info.get("country") or {}).get("descriptor")
    if country:
        extra.append(country)
    path = info.get("externalPath") or ""
    return JobPosting(
        source="workday",
        company=company,
        title=(info.get("title") or "").strip(),
        location=loc or None,
        location_texts=extra,
        remote_type=infer_remote_type(info.get("remoteType"), loc),
        posted_at=to_utc_str(info.get("startDate")),
        apply_url=info.get("externalUrl") or f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
        jd_text=html_to_text(info.get("jobDescription")) or None,
        ats="workday",
        ats_job_id=info.get("jobReqId") or info.get("id") or path,
    )


def fetch(http: Http, company: dict, flt: PreFilter, search_terms: list[str],
          max_pages: int = 5) -> tuple[int, list[JobPosting], None]:
    """Returns (seen, kept, None): the listing is a capped keyword search, so absence from it
    doesn't mean closed. Closed Workday jobs are detected per job via detail_path() instead."""
    base = base_url(company["slug"])
    candidates: dict[str, dict] = {}   # externalPath -> listing row
    seen = 0
    for term in search_terms:
        total = None
        for page in range(max_pages):
            data = http.post_json(f"{base}/jobs", {
                "appliedFacets": {}, "limit": PAGE, "offset": page * PAGE, "searchText": term,
            })
            rows = data.get("jobPostings") or []
            if total is None:
                total = data.get("total") or 0   # later pages often report 0
            seen += len(rows)
            for r in rows:
                path = r.get("externalPath")
                if not path or path in candidates or not flt.title_ok(r.get("title") or ""):
                    continue
                loc = r.get("locationsText") or ""
                # "3 Locations" can't be judged from the listing; let the detail decide.
                if not _MULTI_LOC.match(loc) and not flt.location_ok(
                        JobPosting(source="workday", company=company["name"], title="", location=loc)):
                    continue
                candidates[path] = r
            if not rows or (page + 1) * PAGE >= total:
                break

    kept = []
    for path in candidates:
        try:
            detail = http.get_json(f"{base}{path}")
        except Exception as e:  # one bad detail page shouldn't kill the company
            log.warning("workday detail failed for %s%s: %s", base, path, e)
            continue
        p = parse_detail(detail, company["name"], company["slug"])
        if flt.keep(p):
            kept.append(p)
    return seen, kept, None


def detail_path(apply_url: str | None) -> str | None:
    """'/job/...' part of an externalUrl, for GET {base}/job/... (the CXS detail endpoint)."""
    m = re.search(r"(/job/[^?#]+)", apply_url or "")
    return m.group(1) if m else None
