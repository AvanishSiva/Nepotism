"""Lever public postings API (US host, falls back to EU host on 404)."""

from __future__ import annotations

from job_agent.http import Http, NotFound
from job_agent.normalize import html_to_text, infer_remote_type, to_utc_str
from job_agent.sources.base import JobPosting, PreFilter

HOSTS = {"us": "https://api.lever.co", "eu": "https://api.eu.lever.co"}
_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}
_COUNTRY = {"IE": "Ireland", "GB": "United Kingdom"}


def _jd_text(j: dict) -> str:
    parts = [j.get("descriptionPlain") or ""]
    for lst in j.get("lists") or []:
        parts.append(lst.get("text") or "")
        parts.append(html_to_text(lst.get("content")))
    parts.append(j.get("additionalPlain") or "")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def parse_listing(data: list, company: str) -> list[JobPosting]:
    out = []
    for j in data or []:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ""
        extra = [x for x in (cats.get("allLocations") or []) if x and x != loc]
        if j.get("country") in _COUNTRY:
            extra.append(_COUNTRY[j["country"]])
        remote = _WORKPLACE.get((j.get("workplaceType") or "").lower()) or infer_remote_type(loc)
        sal = j.get("salaryRange") or {}
        out.append(JobPosting(
            source="lever",
            company=company,
            title=(j.get("text") or "").strip(),
            location=loc or None,
            location_texts=extra,
            remote_type=remote,
            posted_at=to_utc_str(j.get("createdAt")),
            apply_url=j.get("hostedUrl") or j.get("applyUrl"),
            jd_text=_jd_text(j) or None,
            salary_min=sal.get("min"),
            salary_max=sal.get("max"),
            currency=sal.get("currency"),
            ats="lever",
            ats_job_id=j.get("id"),
        ))
    return out


def fetch(http: Http, company: dict, flt: PreFilter) -> tuple[int, list[JobPosting]]:
    """Returns (total_seen, kept). Sets company['region'] to the host that worked."""
    order = ["eu", "us"] if company.get("region") == "eu" else ["us", "eu"]
    last: Exception | None = None
    for region in order:
        try:
            data = http.get_json(f"{HOSTS[region]}/v0/postings/{company['slug']}", {"mode": "json"})
        except NotFound as e:
            last = e
            continue
        company["region"] = region
        postings = parse_listing(data, company["name"])
        return len(postings), [p for p in postings if flt.keep(p)]
    raise last or NotFound(company["slug"])
