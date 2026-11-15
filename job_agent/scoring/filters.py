"""Hard filters (spec §6.1): code only, no LLM. Any reason returned → decision = skip."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from rapidfuzz import fuzz

from job_agent.config import Config
from job_agent.normalize import norm_company, norm_title_for_match
from job_agent.sources.base import JobPosting, PreFilter


class HardFilters:
    def __init__(self, cfg: Config):
        f = cfg.filters
        self.title_exclude = [re.compile(rf"\b{re.escape(w)}\b", re.I) for w in f.get("title_exclude", [])]
        self.jd_exclude = [re.compile(p, re.I) for p in f.get("jd_exclude_patterns", [])]
        self.pre = PreFilter(cfg.preferences)
        self.applied = []   # (norm company, norm title or "*", kind)
        for kind, items in (f.get("already_applied") or {}).items():
            for item in items or []:
                company, _, title = item.partition("|")
                self.applied.append((norm_company(company), norm_title_for_match(title) if title.strip() != "*" else "*", kind))

    def _already_applied(self, company: str, title: str) -> str | None:
        c, t = norm_company(company), norm_title_for_match(title)
        for ac, at, kind in self.applied:
            if ac == c and (at == "*" or fuzz.token_set_ratio(at, t) >= 85):
                return f"already applied ({kind})"
        return None

    def check(self, conn: sqlite3.Connection, job: sqlite3.Row, company: sqlite3.Row | None) -> list[str]:
        reasons: list[str] = []
        title = job["title"] or ""
        company_name = company["name"] if company else ""

        for r in self.title_exclude:
            if r.search(title):
                reasons.append(f"title contains '{r.pattern[2:-2].replace(chr(92), '')}'")
                break
        if not self.pre.title_ok(title):
            reasons.append("not a target role")

        posting = JobPosting(source=job["source"], company=company_name, title=title,
                             location=job["location"], remote_type=job["remote_type"] or "unknown")
        if not self.pre.location_ok(posting):
            reasons.append(f"location not in preferences ({job['location']})")

        text = f"{title}\n{job['jd_text'] or ''}"
        for r in self.jd_exclude:
            m = r.search(text)
            if m:
                reasons.append(f"JD: '{m.group(0)[:60]}'")
                break

        applied = conn.execute("SELECT status FROM applications WHERE job_id = ?", (job["id"],)).fetchone()
        if applied:
            reasons.append(f"already applied ({applied['status']})")
        elif (hit := self._already_applied(company_name, title)):
            reasons.append(hit)

        if company and company["cooldown_until"]:
            if company["cooldown_until"] > datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                reasons.append(f"company on rejection cooldown until {company['cooldown_until']}")
        return reasons
