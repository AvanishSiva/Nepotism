"""LLM fact extraction from a JD (spec §6.2). Facts only, no opinions.

Ollama with a JSON schema (temperature 0) → Pydantic validation → one retry →
`claude -p` fallback. Results are cached in job_scores.facts_json keyed by a hash
of the JD, so a job is only sent to the LLM again when its JD changes.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from job_agent.llm import claude_cli, ollama

log = logging.getLogger(__name__)

PROMPT_VERSION = 1
JD_MAX_CHARS = 12000

RED_FLAGS = [
    "unpaid", "commission_only", "short_contract", "relocation_outside_ireland",
    "requires_non_english_language", "requires_security_clearance", "requires_citizenship",
    "no_visa_sponsorship", "night_or_weekend_shifts",
]
# LLM-detected flags that mean "can't take this job" (same as a hard filter).
HARD_RED_FLAGS = {"requires_non_english_language", "requires_security_clearance", "requires_citizenship"}


class Facts(BaseModel):
    seniority: Literal["junior", "mid", "senior", "staff"]
    years_required: int | None = Field(default=None, ge=0, le=30)
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    domain: str | None = None
    role_type: Literal["backend", "ai_ml", "fullstack", "frontend", "data", "devops", "other"]
    red_flags: list[str] = Field(default_factory=list)
    remote: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    visa_or_authorisation_note: str | None = None

    @field_validator("years_required", mode="before")
    @classmethod
    def _neg_is_none(cls, v):
        return None if v is None or (isinstance(v, (int, float)) and v < 0) else int(v)

    @field_validator("must_have", "nice_to_have", mode="before")
    @classmethod
    def _clean_list(cls, v):
        out = []
        for s in v or []:
            s = str(s).strip()
            if s and s.lower() not in {x.lower() for x in out}:
                out.append(s[:60])
        return out[:15]

    @field_validator("red_flags", mode="before")
    @classmethod
    def _known_flags(cls, v):
        return sorted({f for f in (v or []) if f in RED_FLAGS})

    @field_validator("domain", "visa_or_authorisation_note", mode="before")
    @classmethod
    def _empty_none(cls, v):
        return (str(v).strip() or None) if v is not None else None


SCHEMA = {
    "type": "object",
    "properties": {
        "seniority": {"type": "string", "enum": ["junior", "mid", "senior", "staff"]},
        "years_required": {"type": "integer"},
        "must_have": {"type": "array", "items": {"type": "string"}},
        "nice_to_have": {"type": "array", "items": {"type": "string"}},
        "domain": {"type": "string"},
        "role_type": {"type": "string",
                      "enum": ["backend", "ai_ml", "fullstack", "frontend", "data", "devops", "other"]},
        "red_flags": {"type": "array", "items": {"type": "string", "enum": RED_FLAGS}},
        "remote": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]},
        "visa_or_authorisation_note": {"type": "string"},
    },
    "required": ["seniority", "years_required", "must_have", "nice_to_have", "domain", "role_type",
                 "red_flags", "remote", "visa_or_authorisation_note"],
}

INSTRUCTIONS = """Extract FACTS from this job posting. No opinions, no advice.

Fields:
- seniority: junior | mid | senior | staff (from the title and required experience).
- years_required: minimum years of experience explicitly stated (for "3-5 years" use 3). -1 if not stated.
- must_have: REQUIRED technical skills/technologies only, as short names
  (e.g. "Java", "Kubernetes", "distributed systems", "REST APIs", "machine learning").
  No soft skills, no degrees, no years. Max 12.
- nice_to_have: preferred/bonus technical skills, same style. Max 10.
- domain: the business domain in 1-3 words (e.g. "payments", "developer tools"). "" if unclear.
- role_type: backend | ai_ml | fullstack | frontend | data | devops | other.
  ai_ml = building LLM/ML/AI systems or models. other = support, sales, solutions, research-only, non-engineering.
- red_flags: ONLY items from the allowed list that the posting clearly states. Usually empty.
  requires_non_english_language = a language other than English is REQUIRED (not "a plus").
  no_visa_sponsorship = says it cannot/will not sponsor work permits or visas.
- remote: remote | hybrid | onsite | unknown.
- visa_or_authorisation_note: short verbatim phrase about work authorisation/sponsorship, or "".
"""


def jd_hash(title: str, jd: str) -> str:
    return hashlib.sha256(f"v{PROMPT_VERSION}|{title}|{jd[:JD_MAX_CHARS]}".encode()).hexdigest()[:16]


def build_prompt(title: str, company: str, jd: str) -> str:
    return (f"{INSTRUCTIONS}\nJob title: {title}\nCompany: {company}\n\n"
            + ollama.wrap_data(jd[:JD_MAX_CHARS]))


def _via_ollama(prompt, model, stats):
    last = None
    for attempt in range(2):
        try:
            raw = ollama.chat(prompt, SCHEMA, model=model, retries=0)
            stats["ollama_calls"] = stats.get("ollama_calls", 0) + 1
            return Facts.model_validate(raw)
        except (ollama.OllamaError, ValidationError) as e:
            last = e
            log.warning("ollama extraction attempt %d failed: %s", attempt + 1, e)
    raise RuntimeError(f"ollama extraction failed: {last}")


def _via_claude(prompt, binary, model, stats):
    stats["claude_calls"] = stats.get("claude_calls", 0) + 1
    raw = claude_cli.run_json(
        f"{prompt}\n\nUse exactly these JSON keys: {list(SCHEMA['properties'])}. "
        f"Types: years_required integer (-1 if not stated); lists of strings; "
        f"red_flags may only contain: {RED_FLAGS}.",
        binary=binary, model=model, system=ollama.UNTRUSTED_NOTE)
    return Facts.model_validate(raw)


def extract(title: str, company: str, jd: str, *, model: str, claude_binary: str | None = "claude",
            engine: str = "ollama", claude_model: str | None = None,
            stats: dict | None = None) -> tuple[Facts, str]:
    """Returns (facts, engine_used). engine: 'ollama' (Claude as fallback) or
    'claude' (Ollama as fallback, e.g. when the Pro usage limit is hit)."""
    stats = stats if stats is not None else {}
    prompt = build_prompt(title, company, jd)
    if engine == "claude" and claude_binary:
        try:
            return _via_claude(prompt, claude_binary, claude_model, stats), "claude"
        except claude_cli.ClaudeUsageLimit as e:
            stats["claude_limit_hit"] = True
            log.warning("Claude usage limit hit, falling back to Ollama: %s", e)
        except (claude_cli.ClaudeError, ValidationError, ValueError) as e:
            log.warning("claude extraction failed for %s / %s, falling back to Ollama: %s", company, title, e)
        return _via_ollama(prompt, model, stats), "ollama"

    try:
        return _via_ollama(prompt, model, stats), "ollama"
    except RuntimeError:
        if not claude_binary:
            raise
    return _via_claude(prompt, claude_binary, claude_model, stats), "claude"


def facts_from_title(title: str) -> Facts:
    """No JD available: minimal facts from the title alone (low confidence)."""
    t = title.lower()
    seniority = ("staff" if any(w in t for w in ("staff", "principal")) else
                 "senior" if any(w in t for w in ("senior", "sr.", "lead")) else
                 "junior" if any(w in t for w in ("graduate", "junior", "entry", "new grad", "associate")) else "mid")
    role = ("ai_ml" if any(w in t for w in (" ai", "ai ", "machine learning", " ml", "ml ", "llm", "agentic", "data scien"))
            else "fullstack" if "full" in t and "stack" in t
            else "frontend" if "front" in t
            else "backend" if any(w in t for w in ("backend", "back-end", "back end", "software", "developer", "engineer"))
            else "other")
    return Facts(seniority=seniority, role_type=role)
