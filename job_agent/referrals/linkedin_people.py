"""Referral discovery on LinkedIn (spec §7), read-only.

Per company with referral_first jobs (one search per company per run):
  1. LinkedIn job page (alert jobs only): "Meet the hiring team" / "People you can reach out to"
  2. resolve the company's LinkedIn slug once (company search), cached in companies.linkedin_slug
  3. company People tab, one keyword at a time (your schools / ex-employers), cached 30 days
Stop at >= 3 good candidates, <= 3 page views per company, <= daily cap overall.
People search (monthly allowance) is NOT used.

Cards are parsed deterministically from the page (name / degree / headline / mutuals);
profile URLs come only from real links on the page, never from an LLM.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote

from rapidfuzz import fuzz

from job_agent.config import Config
from job_agent.normalize import norm_company, norm_text

log = logging.getLogger(__name__)

CACHE_DAYS = 30
KEEP_PER_PAGE = 20
# Verified links only. People-tab keyword search is fuzzy ("National College of Ireland"
# also matches "University of Limerick, Ireland"), so keyword hits are only "maybe_*"
# until the headline or (later) the profile page confirms them.
GOOD_TAGS = {"connection", "hiring_team", "alum", "ex_colleague"}
RANK = {"connection": 100, "hiring_team": 80, "alum": 60, "ex_colleague": 55,
        "maybe_alum": 40, "maybe_ex_colleague": 38, "engineer": 30, "recruiter": 20}
_ROLE_BONUS = {"engineer": 8, "recruiter": 5}

_NOISE = re.compile(
    r"^(connect|message|follow|following|pending|view profile|view .{0,60}profile|status is \w+|"
    r"·|•|\d+(st|nd|rd|th)\+?|\d+(st|nd|rd|th)\+? degree connection|show all|see all|more|"
    r"premium|open to work|hiring|job poster)$", re.I)
_DEGREE = re.compile(r"\b(1st|2nd|3rd)\+?\b")
_MUTUAL = re.compile(r"(\d+)\s+other\s+mutual|(\d+)\s+mutual", re.I)
_RECRUITER = re.compile(r"recruit|talent|sourcer|sourcing|people partner|\bhr\b|human resources", re.I)
_ENGINEER = re.compile(r"engineer|\bengineering\b|developer|\bswe\b|\bsde\b|scientist|\bml\b|programmer", re.I)
_NOT_ENGINEER = re.compile(r"account manager|solutions? engineer|sales engineer|customer engineer|"
                           r"operations|analyst|recruit", re.I)


@dataclass
class Person:
    name: str
    headline: str
    profile_url: str
    degree: str | None
    mutuals: int
    tags: set[str] = field(default_factory=set)
    source: str = ""

    @property
    def rank(self) -> int:
        role = max((_ROLE_BONUS.get(t, 0) for t in self.tags), default=0)
        return max((RANK.get(t, 0) for t in self.tags), default=0) + role + min(10, self.mutuals)

    @property
    def good(self) -> bool:
        return bool(self.tags & GOOD_TAGS)


def profile_url(slug: str) -> str:
    return f"https://www.linkedin.com/in/{slug.strip('/').lower()}/"


def parse_card(card: dict, source: str) -> Person | None:
    text = card.get("text") or ""
    if re.match(r"\s*linkedin member\b", text, re.I):
        return None                       # out-of-network profile with hidden name
    lines = []
    for raw in text.splitlines():
        ln = re.sub(r"\s+", " ", raw).strip(" ·•")
        if ln and not _NOISE.match(ln):
            lines.append(ln)
    if not lines:
        return None
    name = re.sub(r"\s*[·•]?\s*(1st|2nd|3rd)\+?$", "", lines[0]).strip()
    if not name or name.lower() == "linkedin member" or len(name) > 80:
        return None
    rest = [ln for ln in lines[1:] if ln != name and not _DEGREE.fullmatch(ln)
            and not _MUTUAL.search(ln) and not ln.lower().startswith(f"view {name.lower()}")]
    dm = _DEGREE.search(text)
    mutuals = 0
    for m in _MUTUAL.finditer(text):
        mutuals = int(m.group(1)) + 1 if m.group(1) else int(m.group(2))
        break
    if not mutuals and re.search(r"mutual connection", text, re.I):
        mutuals = 1
    p = Person(name=name, headline=(rest[0] if rest else "")[:200], profile_url=profile_url(card["slug"]),
               degree=dm.group(1) if dm else None, mutuals=mutuals, source=source)
    if p.degree == "1st":
        p.tags.add("connection")
    sec = (card.get("section") or "").lower()
    if "hiring team" in sec or "job poster" in text.lower():
        p.tags.add("hiring_team")
    if _RECRUITER.search(p.headline):
        p.tags.add("recruiter")
    elif _ENGINEER.search(p.headline) and not _NOT_ENGINEER.search(p.headline):
        p.tags.add("engineer")
    return p


class Hooks:
    """Your schools / ex-employers, used as People-tab keywords and for tagging."""

    def __init__(self, cfg: Config):
        prof = cfg.profile
        self.schools = [e.get("school") for e in prof.get("education") or [] if e.get("school")]
        self.employers = []
        for e in prof.get("past_employers") or []:
            self.employers += [e.get("company")] + list(e.get("aliases") or [])
        self.keywords = list(dict.fromkeys((cfg.referrals or {}).get("keywords") or (self.schools + self.employers)))
        own = ((prof.get("links") or {}).get("linkedin") or "").rstrip("/").rsplit("/", 1)[-1].lower()
        self.own_slug = own or None

    def tag_for_keyword(self, kw: str) -> str | None:
        """Unverified tag for a People-tab keyword hit."""
        k = norm_text(kw)
        if any(fuzz.token_set_ratio(k, norm_text(s)) >= 90 for s in self.schools):
            return "maybe_alum"
        if any(fuzz.token_set_ratio(k, norm_company(e)) >= 90 for e in self.employers if e):
            return "maybe_ex_colleague"
        return None

    def tag_from_headline(self, headline: str) -> set[str]:
        tags = set()
        h = norm_text(headline)
        if any(norm_text(s) and norm_text(s) in h for s in self.schools):
            tags.add("alum")
        if any(norm_company(e) and re.search(rf"\b{re.escape(norm_company(e))}\b", h) for e in self.employers if e):
            tags.add("ex_colleague")
        return tags


# -- persistence -----------------------------------------------------------

def save_person(conn: sqlite3.Connection, company_id: int, p: Person) -> None:
    row = conn.execute("SELECT id, tags_json FROM referral_points WHERE profile_url = ?", (p.profile_url,)).fetchone()
    if row:
        tags = sorted(set(json.loads(row["tags_json"] or "[]")) | p.tags)
        conn.execute("""UPDATE referral_points SET company_id = ?, name = ?, headline = coalesce(nullif(?, ''), headline),
                        degree = coalesce(?, degree), tags_json = ?, mutuals = max(coalesce(mutuals,0), ?)
                        WHERE id = ?""", (company_id, p.name, p.headline, p.degree, json.dumps(tags), p.mutuals, row["id"]))
    else:
        conn.execute("""INSERT INTO referral_points(company_id, name, headline, profile_url, degree, tags_json, mutuals, source)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (company_id, p.name, p.headline, p.profile_url, p.degree, json.dumps(sorted(p.tags)), p.mutuals, p.source))


def people_for(conn: sqlite3.Connection, company_id: int) -> list[Person]:
    out = []
    for r in conn.execute("SELECT * FROM referral_points WHERE company_id = ?", (company_id,)):
        out.append(Person(r["name"], r["headline"] or "", r["profile_url"], r["degree"], r["mutuals"] or 0,
                          set(json.loads(r["tags_json"] or "[]")), r["source"] or ""))
    return sorted(out, key=lambda p: -p.rank)


def _kv_get(conn, key):
    r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def _kv_set(conn, key, value):
    conn.execute("INSERT INTO kv(key, value, updated_at) VALUES(?, ?, datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at", (key, value))


def _fresh(conn, key) -> bool:
    v = _kv_get(conn, key)
    return bool(v) and datetime.fromisoformat(v) > datetime.now() - timedelta(days=CACHE_DAYS)


# -- planning --------------------------------------------------------------

def target_companies(conn: sqlite3.Connection, only: str | None = None) -> list[sqlite3.Row]:
    rows = conn.execute(
        """SELECT c.*, max(s.score) AS best, group_concat(j.id) AS job_ids
           FROM job_scores s JOIN jobs j ON j.id = s.job_id JOIN companies c ON c.id = j.company_id
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE s.decision = 'referral_first' AND a.job_id IS NULL
             AND coalesce((SELECT verdict FROM feedback f WHERE f.job_id = j.id ORDER BY f.id DESC LIMIT 1), '') != 'skip'
           GROUP BY c.id ORDER BY best DESC""").fetchall()
    if only:
        rows = [r for r in rows if norm_company(r["name"]) == norm_company(only)]
    return rows


@dataclass
class Step:
    kind: str          # job_page | resolve | people_tab
    url: str
    key: str           # kv cache key marking it done
    keyword: str | None = None
    job_id: int | None = None


def plan_company(conn: sqlite3.Connection, company: sqlite3.Row, hooks: Hooks) -> list[Step]:
    steps = []
    for jid in (company["job_ids"] or "").split(","):
        r = conn.execute("SELECT linkedin_job_url FROM jobs WHERE id = ?", (jid,)).fetchone()
        if r and r["linkedin_job_url"] and not _fresh(conn, f"jobpage:{jid}"):
            steps.append(Step("job_page", r["linkedin_job_url"], f"jobpage:{jid}", job_id=int(jid)))
    slug = company["linkedin_slug"]
    if not slug:
        steps.append(Step("resolve", "https://www.linkedin.com/search/results/companies/?keywords="
                          + quote(company["name"]), f"resolve:{company['id']}"))
    for kw in hooks.keywords:
        key = f"people:{company['id']}:{norm_text(kw)}"
        if not _fresh(conn, key):
            url = f"https://www.linkedin.com/company/{slug or '{slug}'}/people/?keywords={quote(kw)}"
            steps.append(Step("people_tab", url, key, keyword=kw))
    return steps


def _resolve_slug(company_name: str, companies: list[dict]) -> str | None:
    target = norm_company(company_name)
    for c in companies:
        if c.get("text") and fuzz.ratio(norm_company(c["text"].splitlines()[0]), target) >= 90:
            return c["slug"]
    return None


# -- stage -----------------------------------------------------------------

def run(conn: sqlite3.Connection, cfg: Config, *, plan_only: bool = False, only: str | None = None,
        ignore_window: bool = False, notify=None) -> dict:
    from job_agent.referrals.browser import LinkedInBrowser, chrome_available
    from job_agent.safety import Safety, SafetyStop

    from job_agent.referrals import connections_csv

    hooks = Hooks(cfg)
    csv_stats = connections_csv.import_matches(conn, cfg, only=only)   # source 1: no browsing
    stale = conn.execute(
        """DELETE FROM referral_points WHERE found_at < datetime('now', ?) AND id NOT IN
           (SELECT referral_point_id FROM outreach)""", (f"-{CACHE_DAYS} days",)).rowcount
    conn.commit()
    per_company = int((cfg.referrals or {}).get("max_views_per_company", 3))
    companies = target_companies(conn, only)
    stats = {"companies": len(companies), "views": 0, "people_new": 0, "per_company": {}, "stopped": None,
             "privacy_deleted": stale,
             "connections_csv": ("not found: export it from LinkedIn" if csv_stats.get("missing")
                                 else f"{csv_stats['matched']} matches in {csv_stats['rows']} connections")}

    plans = {c["id"]: plan_company(conn, c, hooks) for c in companies}
    if plan_only:
        stats["plan"] = {c["name"]: [f"{s.kind}: {s.url}" for s in plans[c["id"]][:per_company]] for c in companies}
        return stats

    safety = Safety(conn, cfg, ignore_window=ignore_window, notify=notify)
    try:
        safety.check_can_browse()
    except SafetyStop as e:
        stats["stopped"] = str(e)
        return stats
    if not chrome_available():
        stats["stopped"] = "Chrome not reachable on 127.0.0.1:9222 (run scripts/start_chrome.sh)"
        return stats

    try:
        with LinkedInBrowser(safety) as browser:
            for company in companies:
                cid, name = company["id"], company["name"]
                have = [p for p in people_for(conn, cid) if p.good]
                cs = stats["per_company"].setdefault(name, {"views": 0, "found": 0, "good_total": len(have)})
                if len(have) >= 3:
                    cs["note"] = "already >= 3 good contacts"
                    continue
                slug = company["linkedin_slug"]
                for step in plans[cid]:
                    if cs["views"] >= per_company or len(have) >= 3:
                        break
                    if step.kind == "people_tab" and not slug:
                        cs["note"] = "no LinkedIn slug; skipped People tab"
                        break
                    url = step.url.replace("{slug}", slug or "")
                    data = browser.view(url)
                    cs["views"] += 1
                    stats["views"] += 1

                    if step.kind == "resolve":
                        slug = _resolve_slug(name, data.companies)
                        if slug:
                            conn.execute("UPDATE companies SET linkedin_slug = ? WHERE id = ?", (slug, cid))
                            log.info("%s → linkedin.com/company/%s", name, slug)
                        else:
                            cs["note"] = "could not resolve LinkedIn company page"
                        _kv_set(conn, step.key, datetime.now().isoformat())
                        conn.commit()
                        continue

                    if step.kind == "job_page" and not slug and data.companies:
                        slug = _resolve_slug(name, data.companies) or None
                        if slug:
                            conn.execute("UPDATE companies SET linkedin_slug = ? WHERE id = ?", (slug, cid))

                    kw_tag = hooks.tag_for_keyword(step.keyword) if step.keyword else None
                    page_people = []
                    for card in data.cards:
                        if hooks.own_slug and card["slug"] == hooks.own_slug:
                            continue
                        p = parse_card(card, step.kind)
                        if not p:
                            continue
                        if step.kind == "people_tab" and kw_tag:
                            p.tags.add(kw_tag)
                        p.tags |= hooks.tag_from_headline(p.headline)
                        if step.kind == "job_page" and "hiring_team" not in p.tags and "reach out" not in \
                                (card.get("section") or "").lower():
                            continue                 # other profile links on job pages (ads, "people also viewed")
                        if {"alum", "ex_colleague"} & p.tags:
                            p.tags -= {"maybe_alum", "maybe_ex_colleague"}
                        page_people.append(p)
                    for p in sorted(page_people, key=lambda x: -x.rank)[:KEEP_PER_PAGE]:
                        save_person(conn, cid, p)
                        cs["found"] += 1
                        stats["people_new"] += 1
                    _kv_set(conn, step.key, datetime.now().isoformat())
                    conn.commit()
                    have = [p for p in people_for(conn, cid) if p.good]
                    cs["good_total"] = len(have)
    except SafetyStop as e:
        stats["stopped"] = str(e)
    stats["views_left_today"] = safety.views_left()
    return stats
