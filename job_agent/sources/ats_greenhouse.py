"""Greenhouse public job board API."""

from __future__ import annotations

from job_agent.http import Http
from job_agent.normalize import html_to_text, infer_remote_type, to_utc_str
from job_agent.sources.base import JobPosting, PreFilter

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def parse_listing(data: dict, company: str) -> list[JobPosting]:
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name") or ""
        offices = [o.get("location") or o.get("name") or "" for o in j.get("offices") or []]
        out.append(JobPosting(
            source="greenhouse",
            company=company,
            title=(j.get("title") or "").strip(),
            location=loc or None,
            location_texts=[o for o in offices if o],
            remote_type=infer_remote_type(loc),
            posted_at=to_utc_str(j.get("first_published") or j.get("updated_at")),
            apply_url=j.get("absolute_url"),
            jd_text=html_to_text(j.get("content"), escaped=True) or None,
            ats="greenhouse",
            ats_job_id=str(j["id"]),
        ))
    return out


def fetch(http: Http, company: dict, flt: PreFilter) -> tuple[int, list[JobPosting]]:
    data = http.get_json(API.format(slug=company["slug"]), {"content": "true"})
    postings = parse_listing(data, company["name"])
    return len(postings), [p for p in postings if flt.keep(p)]
