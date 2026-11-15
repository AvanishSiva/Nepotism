"""Deterministic 0–10 score (spec §6.3) + decision (§6.4) + explanation (§6.5).

Pure functions: no DB, no LLM. Unit-tested in tests/test_score.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from job_agent.normalize import TS_FMT, norm_text
from job_agent.scoring.extract import HARD_RED_FLAGS, Facts

TIER_CREDIT = {"strong": 1.0, "working": 0.8, "learning": 0.3}
_GENERIC = {"development", "learning", "engineering", "systems", "system", "design", "data", "software",
            "programming", "cloud", "web", "apis", "api", "testing", "evaluation", "ai", "ml", "full",
            "stack", "react native", "java ee"}
_SPECIAL = [(re.compile(r"c\+\+", re.I), " cplusplus "), (re.compile(r"c#", re.I), " csharp "),
            (re.compile(r"f#", re.I), " fsharp "), (re.compile(r"\.net\b", re.I), " dotnet ")]


def skill_norm(s: str | None) -> str:
    """norm_text that keeps C, C++, C# and .NET distinct (norm_text maps all to 'c')."""
    s = s or ""
    for rx, rep in _SPECIAL:
        s = rx.sub(rep, s)
    return norm_text(s)


DEFAULT_WEIGHTS = {"must_have": 0.40, "experience": 0.20, "role_type": 0.15,
                   "nice_to_have": 0.10, "freshness": 0.15}


@dataclass
class SkillMatcher:
    """Maps free-text JD skill items onto the user's tiered skills."""
    skills: dict[str, float]                  # norm skill -> credit
    display: dict[str, str]                   # norm skill -> original name
    synonyms: dict[str, str]                  # norm JD phrase -> norm user skill
    ignore: frozenset = frozenset()           # norm JD items that are neither matched nor missing

    @classmethod
    def from_config(cls, profile: dict, synonyms: dict, ignore: list | None = None) -> "SkillMatcher":
        skills, display = {}, {}
        tiers = profile.get("skills") or {}
        if isinstance(tiers, list):           # flat list = all strong
            tiers = {"strong": tiers}
        for tier, names in tiers.items():
            for name in names or []:
                k = skill_norm(name)
                if k and TIER_CREDIT.get(tier, 0.8) > skills.get(k, 0):
                    skills[k], display[k] = TIER_CREDIT.get(tier, 0.8), name
        syn = {skill_norm(a): skill_norm(b) for a, b in (synonyms or {}).items()}
        return cls(skills, display, syn, frozenset(skill_norm(x) for x in ignore or []))

    @staticmethod
    def _contains(text: str, phrase: str) -> bool:
        return bool(phrase) and re.search(rf"(^| ){re.escape(phrase)}( |$)", text) is not None

    def credit(self, item: str) -> tuple[float, str | None]:
        """Best credit for one JD skill item, and which of the user's skills earned it."""
        t = skill_norm(item)
        best, via = 0.0, None
        for k, c in self.skills.items():
            if len(k) <= 2:                   # "C", "Go": exact item only (not "Objective-C")
                if t == k and c > best:
                    best, via = c, self.display[k]
                continue
            # JD item inside your skill ("AWS" in "AWS Lambda") only for real skill
            # names, not generic words ("development" in "Full-stack development").
            reverse = t not in _GENERIC and self._contains(k, t)
            if c > best and (self._contains(t, k) or reverse):
                best, via = c, self.display[k]
        for phrase, target in self.synonyms.items():
            c = self.skills.get(target, 0.0)
            if c > best and self._contains(t, phrase):
                best, via = c, self.display.get(target)
        return best, via

    def match(self, items: list[str]) -> tuple[float, list[str], list[str]]:
        """(fraction 0..1, matched items, missing items). Ignored items are dropped."""
        items = [it for it in items if skill_norm(it) not in self.ignore]
        if not items:
            return 0.0, [], []
        total, hit, miss = 0.0, [], []
        for it in items:
            c, _ = self.credit(it)
            total += c
            (hit if c >= 0.5 else miss).append(it)
        return total / len(items), hit, miss


@dataclass
class ScoreResult:
    score: float
    decision: str
    components: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    possible_repost: bool = False
    low_confidence: bool = False
    hard_skip: bool = False


def _days_between(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    return (datetime.strptime(b, TS_FMT) - datetime.strptime(a, TS_FMT)).total_seconds() / 86400


def experience_fit(years_required: int | None, user_years: float) -> float:
    if years_required is None:
        return 0.8                            # not stated: mild uncertainty
    over = years_required - (user_years + 1)
    if over <= 0:
        return 1.0
    return max(0.0, 1.0 - over / 3.0)         # 0 at user_years + 4


def freshness(age_days: float | None) -> float:
    if age_days is None:
        return 0.5
    if age_days <= 2:
        return 1.0
    return max(0.0, 1.0 - (age_days - 2) / 19.0)   # 0 at 21 days


def compute(facts: Facts, *, matcher: SkillMatcher, user_years: float, role_types: list[str],
            weights: dict | None, thresholds: dict, jd_text: str | None, posted_at: str | None,
            first_seen_at: str, last_seen_at: str, now: str, low_confidence: bool = False,
            flag_patterns: list[re.Pattern] = (), boost_keywords: list[str] = (),
            is_agency: bool = False) -> ScoreResult:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    reasons: list[str] = []

    if low_confidence:                         # no JD: skills can't be checked → neutral
        must, matched, missing = 0.5, [], []
        nice = 0.5
    else:
        must_items = [x for x in facts.must_have if skill_norm(x) not in matcher.ignore]
        nice_items = [x for x in facts.nice_to_have if skill_norm(x) not in matcher.ignore]
        must, matched, missing = matcher.match(must_items)
        if not must_items:
            must = 0.5
        nice, _, _ = matcher.match(nice_items)
        if not nice_items:
            nice = 1.0

    exp = experience_fit(facts.years_required, user_years)
    role = 1.0 if facts.role_type in role_types else 0.3
    age = _days_between(posted_at or first_seen_at, now)
    fresh = freshness(age)

    components = {"must_have": round(must, 2), "experience": round(exp, 2), "role_type": role,
                  "nice_to_have": round(nice, 2), "freshness": round(fresh, 2)}
    base = 10 * sum(w[k] * v for k, v in components.items())
    adj = 0.0

    hard = sorted(set(facts.red_flags) & HARD_RED_FLAGS)
    soft = sorted(set(facts.red_flags) - HARD_RED_FLAGS)
    text = jd_text or ""
    if any(p.search(text) for p in flag_patterns) and "no_visa_sponsorship" not in soft:
        soft.append("no_visa_sponsorship")
    soft_penalty = min(4.0, 2.0 * len(soft))
    if soft:
        adj -= soft_penalty
        reasons.append(f"red flags: {', '.join(soft)} (-{soft_penalty:g})")

    if text and boost_keywords:
        hits = {k for k in boost_keywords if re.search(rf"\b{re.escape(k)}\b", text, re.I)}
        bonus = min(1.0, 0.25 * len(hits))
        if bonus:
            adj += bonus
            components["boost"] = sorted(hits)

    repost = False
    seen_span = _days_between(first_seen_at, last_seen_at)
    if seen_span is not None and seen_span > 60:
        repost = True
        reasons.append(f"seen for {seen_span:.0f} days (possible repost, -1)")
    elif age is not None and age > 90:
        repost = True
        reasons.append(f"posted {age:.0f} days ago (stale listing, -1)")
    if repost:
        adj -= 1.0

    score = round(max(0.0, min(10.0, base + adj)), 1)
    components["adjustments"] = round(adj, 2)

    if hard:
        decision = "skip"
        reasons.insert(0, f"cannot take: {', '.join(hard)}")
    elif score >= thresholds.get("referral_first", 8) and not is_agency:
        decision = "referral_first"
    elif score >= thresholds.get("referral_first", 8):
        decision = "apply_direct"
        reasons.append("recruiter/aggregator: apply directly, no referral")
    elif score >= thresholds.get("apply_direct", 6):
        decision = "apply_direct"
    else:
        decision = "skip"
        reasons.append(f"score {score} below {thresholds.get('apply_direct', 6)}")
    if low_confidence:
        reasons.append("no JD: scored on title only (open manually)")

    return ScoreResult(score, decision, components, reasons, matched, missing,
                       possible_repost=repost, low_confidence=low_confidence, hard_skip=bool(hard))


def pick_project(projects: list[dict], jd_items: list[str], matcher: SkillMatcher) -> str | None:
    """The profile project whose skills overlap the JD most (never invented)."""
    if not projects:
        return None
    jd_norm = [skill_norm(x) for x in jd_items]

    def overlap(p):
        n = 0
        for s in p.get("skills") or []:
            k = skill_norm(s)
            if any(SkillMatcher._contains(j, k) or SkillMatcher._contains(k, j) for j in jd_norm):
                n += 1
        return n
    best = max(projects, key=overlap)
    return best["name"] if overlap(best) >= 2 else None   # one shared language is not a reason


def explain(result: ScoreResult, facts: Facts, project: str | None) -> list[str]:
    """≤ 3 short lines: strengths, gaps, project to lead with."""
    lines = []
    if result.matched:
        lines.append("✓ " + ", ".join(result.matched[:6]))
    gaps = list(result.missing[:5])
    if facts.years_required is not None and result.components.get("experience", 1) < 1:
        gaps.append(f"{facts.years_required}+ yrs asked")
    if gaps:
        lines.append("✗ " + ", ".join(gaps))
    if project:
        lines.append(f"Lead with: {project}")
    return lines[:3]
