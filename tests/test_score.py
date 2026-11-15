import re

import pytest

from job_agent.config import Config
from job_agent.db import init_db
from job_agent.scoring.extract import Facts, facts_from_title
from job_agent.scoring.filters import HardFilters
from job_agent.scoring.score import (SkillMatcher, compute, experience_fit, explain, freshness,
                                     pick_project)

PROFILE = {
    "years_experience": 3,
    "skills": {"strong": ["Java", "Python", "TypeScript", "AWS Lambda", "Full-stack development"],
               "working": ["LangGraph", "React"], "learning": ["PyTorch"]},
    "projects": [{"name": "Negotiation agents", "skills": ["LangGraph", "Python"]},
                 {"name": "Portfolio", "skills": ["Next.js", "TypeScript"]}],
}
SYN = {"aws": "AWS Lambda", "serverless": "AWS Lambda", "agentic ai": "LangGraph", "node.js": "TypeScript"}
M = SkillMatcher.from_config(PROFILE, SYN)
NOW = "2026-11-14 08:00:00"


def run(facts, **kw):
    args = dict(matcher=M, user_years=3, role_types=["backend", "ai_ml", "fullstack"], weights=None,
                thresholds={"referral_first": 8, "apply_direct": 6}, jd_text="", posted_at=NOW,
                first_seen_at=NOW, last_seen_at=NOW, now=NOW)
    args.update(kw)
    return compute(facts, **args)


# --- skill matching --------------------------------------------------------

@pytest.mark.parametrize("item,credit", [
    ("Java", 1.0), ("Java 17", 1.0), ("AWS", 1.0),            # "AWS" inside "AWS Lambda"
    ("serverless", 1.0), ("Node.js", 1.0),                      # synonyms
    ("LangGraph", 0.8), ("React", 0.8), ("PyTorch", 0.3),     # tiers
    ("Kubernetes", 0.0), ("Go", 0.0),
    ("software development", 0.0),                              # generic word must not hit "Full-stack development"
    ("machine learning", 0.0),
])
def test_skill_credit(item, credit):
    assert M.credit(item)[0] == credit


def test_match_fraction():
    frac, hit, miss = M.match(["Java", "Kubernetes", "AWS", "Go"])
    assert frac == pytest.approx(0.5) and hit == ["Java", "AWS"] and miss == ["Kubernetes", "Go"]


# --- components ------------------------------------------------------------

@pytest.mark.parametrize("req,fit", [(None, 0.8), (0, 1.0), (4, 1.0), (5, pytest.approx(2 / 3)), (7, 0.0), (10, 0.0)])
def test_experience_fit(req, fit):
    assert experience_fit(req, 3) == fit


@pytest.mark.parametrize("days,f", [(0, 1.0), (2, 1.0), (21, 0.0), (40, 0.0), (11.5, 0.5), (None, 0.5)])
def test_freshness(days, f):
    assert freshness(days) == pytest.approx(f)


# --- full score ------------------------------------------------------------

def test_perfect_match_is_referral_first():
    f = Facts(seniority="mid", years_required=3, must_have=["Java", "Python", "AWS"],
              nice_to_have=["LangGraph"], role_type="backend")
    r = run(f)
    # 10*(0.4*1 + 0.2*1 + 0.15*1 + 0.1*0.8 + 0.15*1) = 9.8
    assert r.score == 9.8 and r.decision == "referral_first"


def test_weak_match_skipped_with_reason():
    f = Facts(seniority="senior", years_required=8, must_have=["Go", "Kubernetes", "Rust"],
              nice_to_have=["Terraform"], role_type="devops")
    r = run(f)
    assert r.decision == "skip" and any("below" in x for x in r.reasons)


def test_red_flag_penalty_and_sponsorship_pattern():
    f = Facts(seniority="mid", years_required=3, must_have=["Java"], role_type="backend",
              red_flags=["night_or_weekend_shifts"])
    base = run(Facts(seniority="mid", years_required=3, must_have=["Java"], role_type="backend")).score
    r = run(f, jd_text="We cannot sponsor visas.", flag_patterns=[re.compile(r"cannot sponsor", re.I)])
    assert r.score == pytest.approx(base - 4.0)       # two soft flags, -2 each
    assert "no_visa_sponsorship" in r.reasons[0]


def test_hard_red_flag_always_skips():
    f = Facts(seniority="mid", years_required=2, must_have=["Java"], role_type="backend",
              red_flags=["requires_non_english_language"])
    r = run(f)
    assert r.decision == "skip" and r.hard_skip and "cannot take" in r.reasons[0]


def test_stale_listing_penalised():
    f = Facts(seniority="mid", must_have=["Java"], role_type="backend")
    fresh_r = run(f)
    old_r = run(f, posted_at="2026-01-01 00:00:00")
    assert old_r.possible_repost and old_r.score < fresh_r.score


def test_title_only_is_low_confidence_and_cannot_reach_referral():
    f = facts_from_title("AI Engineer")
    assert f.role_type == "ai_ml"
    r = run(f, low_confidence=True, jd_text=None)
    assert r.low_confidence and r.decision != "referral_first"


def test_boost_keywords_capped():
    f = Facts(seniority="mid", must_have=["Java"], role_type="backend")
    base = run(f).score
    r = run(f, jd_text="agentic LLM multi-agent MCP LangGraph evals", boost_keywords=[
        "agentic", "LLM", "multi-agent", "MCP", "LangGraph", "evals"])
    assert r.score == pytest.approx(min(10, base + 1.0))


def test_explanation_uses_only_profile_projects():
    f = Facts(seniority="mid", must_have=["LangGraph", "Python", "Kubernetes"], role_type="ai_ml")
    r = run(f)
    proj = pick_project(PROFILE["projects"], f.must_have, M)
    lines = explain(r, f, proj)
    assert proj == "Negotiation agents" and lines[-1] == "Lead with: Negotiation agents"
    assert "Kubernetes" in lines[1] and len(lines) <= 3


# --- hard filters ----------------------------------------------------------

@pytest.fixture
def hf_env():
    conn = init_db(":memory:")
    cfg = Config.model_validate({
        "preferences": {"target_titles": [r"software (engineer|developer)", r"\bai\b.*engineer"],
                        "locations": ["Dublin", "Ireland"], "remote_regions": ["Ireland"]},
        "filters": {"title_exclude": ["staff", "intern", "manager", "lead"],
                    "jd_exclude_patterns": [r"\bfluent\b[^.\n]{0,30}\bgerman\b"],
                    "already_applied": {"rejected": ["Workday|*", "Genesys|Agentic Orchestration"]}},
    })
    cid = conn.execute("INSERT INTO companies(name, name_norm) VALUES('Acme','acme')").lastrowid

    def job(title, location="Dublin", jd="", company="Acme"):
        c = conn.execute("SELECT * FROM companies WHERE name = ?", (company,)).fetchone()
        if c is None:
            conn.execute("INSERT INTO companies(name, name_norm) VALUES(?, lower(?))", (company, company))
            c = conn.execute("SELECT * FROM companies WHERE name = ?", (company,)).fetchone()
        jid = conn.execute("INSERT INTO jobs(company_id, source, title, location, jd_text, dedupe_key) "
                           "VALUES(?,?,?,?,?,?)", (c["id"], "x", title, location, jd, f"{title}{location}{company}")).lastrowid
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone(), c
    yield HardFilters(cfg), conn, job
    conn.close()


@pytest.mark.parametrize("title,location,jd,company,skip", [
    ("Software Engineer", "Dublin", "", "Acme", False),
    ("Staff Software Engineer", "Dublin", "", "Acme", True),
    ("Software Engineer Intern", "Dublin", "", "Acme", True),
    ("Software Engineer - International", "Dublin", "", "Acme", False),   # 'intern' is a whole word only
    ("Lead AI Engineer", "Dublin", "", "Acme", True),
    ("Barista", "Dublin", "", "Acme", True),                             # not a target role
    ("Software Engineer", "London, UK", "", "Acme", True),
    ("Software Engineer", "Dublin", "Fluent German is required.", "Acme", True),
    ("Software Engineer", "Dublin", "", "Workday", True),                # already applied (any role)
    ("Software Engineer", "Dublin", "", "Genesys", False),               # different role at Genesys
])
def test_hard_filters(hf_env, title, location, jd, company, skip):
    hf, conn, job = hf_env
    row, c = job(title, location, jd, company)
    assert bool(hf.check(conn, row, c)) is skip


def test_c_family_kept_distinct():
    m = SkillMatcher.from_config({"skills": {"strong": ["C", "C++", "Java"]}}, {})
    assert m.credit("C++")[0] == 1.0 and m.credit("C")[0] == 1.0
    assert m.credit("C#")[0] == 0.0 and m.credit(".NET")[0] == 0.0
    assert m.credit("Objective-C")[0] == 0.0
    assert m.credit("Modern C++ (17/20)")[0] == 1.0


def test_ignored_fundamentals_neither_match_nor_miss():
    m = SkillMatcher.from_config(PROFILE, SYN, ["data structures", "algorithms"])
    f = Facts(seniority="mid", must_have=["Java", "data structures", "algorithms"], role_type="backend")
    r = run(f, matcher=m)
    assert r.components["must_have"] == 1.0 and r.missing == []


def test_agency_never_referral_first():
    f = Facts(seniority="mid", years_required=3, must_have=["Java", "Python"], role_type="backend")
    r = run(f, is_agency=True)
    assert r.score >= 8 and r.decision == "apply_direct"
    assert any("aggregator" in x for x in r.reasons)


def test_no_project_suggested_for_single_shared_language():
    assert pick_project(PROFILE["projects"], ["Python", "Kubernetes"], M) is None
