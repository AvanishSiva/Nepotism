"""Shared job record + the discover-stage pre-filter (title family + location)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from job_agent.config import Preferences


@dataclass
class JobPosting:
    source: str
    company: str
    title: str
    location: str | None = None
    remote_type: str = "unknown"
    posted_at: str | None = None
    apply_url: str | None = None
    jd_text: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    ats: str | None = None
    ats_job_id: str | None = None
    linkedin_job_url: str | None = None
    linkedin_job_id: str | None = None
    alert_date: str | None = None
    # All location strings (primary + secondary offices); used for filtering only.
    location_texts: list[str] = field(default_factory=list)

    def all_locations_text(self) -> str:
        parts = [self.location or ""] + self.location_texts
        return " | ".join(p for p in parts if p)


def _word_regex(words: list[str]) -> re.Pattern | None:
    words = [w.strip() for w in words if w and w.strip()]
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


class PreFilter:
    """Cheap, code-only filter applied while collecting.

    Keeps a posting if its title is in a target role family AND it's in a
    preferred location (or remote within an allowed region). Seniority /
    intern / JD-based exclusions happen later in the score stage.
    """

    def __init__(self, prefs: Preferences):
        self.title_res = [re.compile(p, re.I) for p in prefs.target_titles]
        self.loc_re = _word_regex(prefs.locations)
        self.region_re = _word_regex(prefs.remote_regions)
        self.accept_unqualified_remote = prefs.accept_unqualified_remote

    def title_ok(self, title: str) -> bool:
        if not self.title_res:
            return True
        return any(r.search(title or "") for r in self.title_res)

    def location_ok(self, posting: JobPosting) -> bool:
        text = posting.all_locations_text()
        if self.loc_re and self.loc_re.search(text):
            return True
        is_remote = posting.remote_type == "remote" or re.search(r"\bremote\b", text, re.I)
        if not is_remote:
            return False
        if self.region_re and self.region_re.search(text):
            return True
        # Remote with no region named at all.
        stripped = re.sub(r"\bremote\b|[^a-z]", "", text.lower())
        return self.accept_unqualified_remote and not stripped

    def describe_location(self, posting: JobPosting) -> str | None:
        """Location string to store: makes it visible *why* the job passed.

        "London, UK" kept for its Dublin option  → "London, UK / Dublin, Ireland"
        "Remote" kept for an EU region          → "Remote (Europe)"
        """
        primary = posting.location or ""
        if not self.loc_re or self.loc_re.search(primary):
            return posting.location
        for extra in posting.location_texts:
            if self.loc_re.search(extra):
                return f"{primary} / {extra}" if primary else extra
        if self.region_re:
            m = self.region_re.search(posting.all_locations_text())
            if m and not self.region_re.search(primary):
                return f"{primary or 'Remote'} ({m.group(0)})"
        return posting.location

    def keep(self, posting: JobPosting) -> bool:
        return self.title_ok(posting.title) and self.location_ok(posting)
