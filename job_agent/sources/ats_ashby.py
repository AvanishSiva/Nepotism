"""Ashby public job board API."""

from __future__ import annotations

from job_agent.http import Http
from job_agent.normalize import infer_remote_type, to_utc_str
from job_agent.sources.base import JobPosting, PreFilter

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
_WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


def _salary(comp: dict | None) -> tuple[float | None, float | None, str | None]:
    for c in (comp or {}).get("summaryComponents") or []:
        if (c.get("compensationType") or "").lower() == "salary":
            return c.get("minValue"), c.get("maxValue"), c.get("currencyCode")
    return None, None, None


def _address_text(addr: dict | None) -> str:
    pa = (addr or {}).get("postalAddress") or {}
    return ", ".join(x for x in (pa.get("addressLocality"), pa.get("addressRegion"), pa.get("addressCountry")) if x)


def parse_listing(data: dict, company: str) -> list[JobPosting]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        extra = [_address_text(j.get("address"))]
        for s in j.get("secondaryLocations") or []:
            extra += [s.get("location") or "", _address_text(s.get("address"))]
        # workplaceType is more reliable than isRemote (Ashby sets isRemote on hybrid roles).
        remote = _WORKPLACE.get((j.get("workplaceType") or "").lower().replace("-", ""))
        if not remote:
            remote = "remote" if j.get("isRemote") else infer_remote_type(loc)
        smin, smax, cur = _salary(j.get("compensation"))
        out.append(JobPosting(
            source="ashby",
            company=company,
            title=(j.get("title") or "").strip(),
            location=loc or None,
            location_texts=[e for e in extra if e],
            remote_type=remote,
            posted_at=to_utc_str(j.get("publishedAt")),
            apply_url=j.get("jobUrl") or j.get("applyUrl"),
            jd_text=(j.get("descriptionPlain") or "").strip() or None,
            salary_min=smin, salary_max=smax, currency=cur,
            ats="ashby",
            ats_job_id=j.get("id"),
        ))
    return out


def fetch(http: Http, company: dict, flt: PreFilter) -> tuple[int, list[JobPosting]]:
    data = http.get_json(API.format(slug=company["slug"]), {"includeCompensation": "true"})
    postings = parse_listing(data, company["name"])
    return len(postings), [p for p in postings if flt.keep(p)]
