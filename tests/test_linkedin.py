from datetime import datetime

import pytest

from job_agent.config import Config
from job_agent.db import init_db
from job_agent.referrals import linkedin_people as lp
from job_agent.safety import Safety, SafetyStop, detect

CFG = Config.model_validate({
    "profile": {"education": [{"school": "National College of Ireland"}, {"school": "Anna University"}],
                "past_employers": [{"company": "Zoho Corporation (ManageEngine)", "aliases": ["Zoho", "ManageEngine"]}],
                "links": {"linkedin": "https://linkedin.com/in/sivaavanish-k"}},
    "referrals": {"keywords": ["National College of Ireland", "Zoho"]},
    "linkedin": {"caps": {"page_views": 2}, "pacing": {"view_gap_s": [0, 0], "settle_s": [0, 0]}},
})


# --- kill switch -----------------------------------------------------------

@pytest.mark.parametrize("url,text,kind", [
    ("https://www.linkedin.com/checkpoint/challenge/abc", "", "restricted"),
    ("https://www.linkedin.com/feed/", "We've detected unusual activity from your account", "restricted"),
    ("https://www.linkedin.com/feed/", "Let's do a quick security check", "restricted"),
    ("https://www.linkedin.com/feed/", "You've reached the weekly invitation limit", "restricted"),
    ("https://www.linkedin.com/authwall?trk=x", "", "logged_out"),
    ("https://www.linkedin.com/login", "", "logged_out"),
    ("https://www.linkedin.com/company/stripe/people/", "Jane Doe ✓ Verified · 2nd\nSoftware Engineer", None),
    ("https://www.linkedin.com/company/stripe/people/", "Restricted Stock Units expert", None),
])
def test_kill_switch_detection(url, text, kind):
    hit = detect(url, text)
    assert (hit[0] if hit else None) == kind


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def test_restriction_pauses_seven_days_and_notifies(conn):
    msgs = []
    s = Safety(conn, CFG, ignore_window=True, notify=msgs.append)
    with pytest.raises(SafetyStop):
        s.after_view("https://www.linkedin.com/checkpoint/x", "")
    assert s.paused_until() and msgs and "PAUSED" in msgs[0]
    with pytest.raises(SafetyStop, match="paused"):
        s.check_can_browse()
    s.resume()                               # after a restriction: manual-only week (spec §9)
    with pytest.raises(SafetyStop, match="manual-only"):
        s.check_can_browse()
    s.pause("manual", days=1)                # a manual pause/resume never triggers that rule
    s.resume()
    conn.execute("DELETE FROM kv WHERE key = 'last_restriction_at'")
    s.check_can_browse()


def test_daily_view_cap(conn):
    s = Safety(conn, CFG, ignore_window=True)
    for _ in range(2):
        s.before_view()
        s.after_view("https://www.linkedin.com/feed/", "ok")
    with pytest.raises(SafetyStop, match="cap"):
        s.before_view()


@pytest.mark.parametrize("when,ok", [
    (datetime(2026, 11, 16, 8, 30), True),    # Monday 08:30
    (datetime(2026, 11, 16, 10, 30), False),  # end is exclusive
    (datetime(2026, 11, 16, 7, 59), False),
    (datetime(2026, 11, 14, 9, 0), False),    # Saturday
])
def test_window(conn, when, ok):
    assert Safety(conn, CFG).in_window(when) is ok


# --- card parsing / tagging / ranking ----------------------------------------

def test_parse_people_card():
    card = {"slug": "Jane-Doe-123", "section": "", "text":
            "Jane Doe\n· 2nd\nSoftware Engineer at Stripe | ex-Zoho\nDublin, Ireland\nAoife and 12 other mutual connections\nConnect"}
    p = lp.parse_card(card, "people_tab")
    assert p.name == "Jane Doe" and p.degree == "2nd" and p.mutuals == 13
    assert p.headline.startswith("Software Engineer at Stripe")
    assert p.profile_url == "https://www.linkedin.com/in/jane-doe-123/"
    assert "engineer" in p.tags


def test_parse_skips_anonymous_and_tags_hiring_team():
    assert lp.parse_card({"slug": "x", "text": "LinkedIn Member\nSoftware Engineer"}, "people_tab") is None
    p = lp.parse_card({"slug": "bob", "section": "Meet the hiring team",
                       "text": "Bob Ray\n1st\nTalent Partner at Toast\nJob poster\nMessage"}, "job_page")
    assert {"hiring_team", "connection", "recruiter"} <= p.tags


def test_hooks_tagging_and_rank_order():
    h = lp.Hooks(CFG)
    assert h.tag_for_keyword("National College of Ireland") == "maybe_alum"   # keyword search is fuzzy
    assert h.tag_for_keyword("Zoho") == "maybe_ex_colleague"
    assert h.own_slug == "sivaavanish-k"
    assert "ex_colleague" in h.tag_from_headline("Backend engineer at Stripe, previously ManageEngine")
    people = [lp.Person("R", "Recruiter", "u1", "2nd", 0, {"recruiter"}),
              lp.Person("A", "SWE", "u2", "2nd", 3, {"alum", "engineer"}),
              lp.Person("M", "SWE", "u5", "2nd", 9, {"maybe_alum", "engineer"}),
              lp.Person("C", "SWE", "u3", "1st", 0, {"connection"}),
              lp.Person("H", "EM", "u4", "3rd", 0, {"hiring_team"})]
    assert [p.name for p in sorted(people, key=lambda p: -p.rank)] == ["C", "H", "A", "M", "R"]


def test_plan_resolves_slug_first_and_caches(conn):
    cid = conn.execute("INSERT INTO companies(name, name_norm) VALUES('Stripe','stripe')").lastrowid
    conn.execute("INSERT INTO jobs(id, company_id, source, title, dedupe_key) VALUES(1, ?, 'x', 'SWE', 'k')", (cid,))
    conn.execute("INSERT INTO job_scores(job_id, score, decision) VALUES(1, 8.5, 'referral_first')")
    company = lp.target_companies(conn)[0]
    steps = lp.plan_company(conn, company, lp.Hooks(CFG))
    assert [s.kind for s in steps] == ["resolve", "people_tab", "people_tab"]
    assert "keywords=National%20College%20of%20Ireland" in steps[1].url
    lp._kv_set(conn, steps[1].key, datetime.now().isoformat())
    assert len(lp.plan_company(conn, company, lp.Hooks(CFG))) == 2   # cached keyword skipped


def test_resolve_slug_requires_name_match():
    cos = [{"slug": "stripe-press", "text": "Stripe Press"}, {"slug": "stripe", "text": "Stripe\nFinancial Services"}]
    assert lp._resolve_slug("Stripe", cos) == "stripe"
    assert lp._resolve_slug("Toast", cos) is None


@pytest.mark.parametrize("headline,eng", [
    ("Software Engineer at Stripe | ex-Amazon", True), ("Engineering @ Stripe", True),
    ("Risk Operations @ Stripe | Data & AI Specialist", False), ("Technical Account Manager at Stripe", False),
    ("Senior Data Analyst @ Stripe", False), ("Solutions Engineer, EMEA", False),
])
def test_engineer_tag(headline, eng):
    p = lp.parse_card({"slug": "x", "text": f"Ann Lee\n2nd\n{headline}"}, "people_tab")
    assert ("engineer" in p.tags) is eng
