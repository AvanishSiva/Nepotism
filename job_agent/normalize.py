"""Text normalisation, dedupe keys, HTML→text and timestamp helpers."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import date, datetime, timezone

from bs4 import BeautifulSoup

TS_FMT = "%Y-%m-%d %H:%M:%S"  # same format as SQLite datetime('now'), always UTC

_COMPANY_SUFFIXES = {
    "inc", "incorporated", "ltd", "limited", "llc", "plc", "gmbh", "corp",
    "corporation", "co", "sa", "ag", "bv", "dac", "uc", "teoranta",
}
_LEVEL_TOKENS = re.compile(r"\b(i{1,3}|iv|v|l\d+|\d+)\b")


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_company(name: str | None) -> str:
    tokens = norm_text(name).split()
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def norm_title(title: str | None) -> str:
    return norm_text(title)


def norm_title_for_match(title: str | None) -> str:
    """Looser form for fuzzy matching: also drops level numerals (II, 3, L4)."""
    t = _LEVEL_TOKENS.sub(" ", norm_text(title))
    return re.sub(r"\s+", " ", t).strip()


def norm_location(loc: str | None) -> str:
    return norm_text(loc)


def dedupe_key(company: str, title: str, location: str | None) -> str:
    return f"{norm_company(company)}|{norm_title(title)}|{norm_location(location)}"


def html_to_text(raw: str | None, *, escaped: bool = False) -> str:
    """Convert (optionally HTML-entity-escaped) HTML into readable plain text."""
    if not raw:
        return ""
    if escaped:
        raw = html.unescape(raw)
    text = BeautifulSoup(raw, "html.parser").get_text("\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime(TS_FMT)


def to_utc_str(value) -> str | None:
    """ISO string / epoch (s or ms) / date → 'YYYY-MM-DD HH:MM:SS' UTC, or None."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            secs = value / 1000 if value > 1e11 else value
            dt = datetime.fromtimestamp(secs, tz=timezone.utc)
        elif isinstance(value, datetime):
            dt = value
        elif isinstance(value, date):
            dt = datetime(value.year, value.month, value.day)
        else:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(TS_FMT)


def infer_remote_type(*texts: str | None) -> str:
    blob = " ".join(t for t in texts if t).lower()
    if re.search(r"\bremote\b", blob):
        return "remote"
    if re.search(r"\bhybrid\b|\bflexible\b", blob):
        return "hybrid"
    if re.search(r"\bon-?site\b|\bin[- ]office\b|\boffice\b", blob):
        return "onsite"
    return "unknown"
