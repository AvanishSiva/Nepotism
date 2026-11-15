import json
from pathlib import Path

import pytest

from job_agent.config import Preferences, load_config
from job_agent.db import init_db
from job_agent.normalize import dedupe_key, norm_company, norm_title_for_match, to_utc_str
from job_agent.sources import ats_ashby, ats_greenhouse, ats_lever, ats_workday
from job_agent.sources.base import JobPosting, PreFilter
from job_agent.sources.companies import parse_careers_url, sync_watchlist
from job_agent.store import upsert_job

FX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FX / name).read_text())


# Fixed test settings (independent of your personal config.yaml), using the
# real title patterns but a broad remote-region list.
TEST_PREFS = Preferences(
    target_titles=load_config().preferences.target_titles,
    locations=["Dublin", "Ireland", "Cork", "Galway", "Limerick"],
    remote_regions=["Ireland", "EMEA", "Europe", "EU", "Worldwide", "Anywhere"],
)


@pytest.fixture
def flt():
    return PreFilter(TEST_PREFS)


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


# --- normalisation -------------------------------------------------------

def test_norm_company_strips_legal_suffixes():
    assert norm_company("Stripe, Inc.") == "stripe"
    assert norm_company("Workhuman Ltd") == "workhuman"
    assert norm_company("AT&T") == "at and t"
    assert norm_company("Co") == "co"  # never strip to empty


def test_dedupe_key_ignores_case_and_punctuation():
    assert dedupe_key("Stripe Inc", "Software Engineer, Tax", "Dublin, IE") == \
           dedupe_key("stripe", "software engineer tax", "Dublin IE")


def test_title_for_match_drops_levels():
    assert norm_title_for_match("Software Engineer III") == "software engineer"
    assert norm_title_for_match("Staff Software Engineer (L4)") == "staff software engineer"


def test_to_utc_str_formats():
    assert to_utc_str("2026-11-09T20:52:57-05:00") == "2026-11-10 01:52:57"
    assert to_utc_str(1786469891368).startswith("2026-")
    assert to_utc_str("2026-11-11") == "2026-11-11 00:00:00"
    assert to_utc_str("garbage") is None


# --- pre-filter ----------------------------------------------------------

@pytest.mark.parametrize("title,ok", [
    ("AI Engineer", True),
    ("AI Infrastructure Engineer", True),
    ("Machine Learning Engineer", True),
    ("Senior ML Engineer", True),
    ("Software Developer", True),
    ("Backend Engineer", True),
    ("Full-Stack Engineer", True),
    ("Applied AI Engineer", True),
    ("Account Executive", False),
    ("Security Engineer", False),
    ("Product Manager, AI", False),
    ("Email Marketing Manager", False),   # 'ml' must be a whole word
])
def test_title_filter(flt, title, ok):
    assert flt.title_ok(title) is ok


@pytest.mark.parametrize("loc,extras,remote,ok", [
    ("Dublin, IE", [], "unknown", True),
    ("Ireland - Dublin", [], "hybrid", True),
    ("London, UK", ["Dublin, Ireland"], "unknown", True),
    ("London, UK", [], "unknown", False),
    ("Remote - EMEA", [], "remote", True),
    ("Remote", ["Europe"], "remote", True),
    ("Remote", [], "remote", False),             # bare remote = usually US-only
    ("Remote - US", [], "remote", False),
    ("Euston, London", [], "onsite", False),     # 'EU' must be a whole word
])
def test_location_filter(flt, loc, extras, remote, ok):
    p = JobPosting(source="x", company="c", title="t", location=loc, location_texts=extras, remote_type=remote)
    assert flt.location_ok(p) is ok


def test_bare_remote_allowed_when_configured():
    f = PreFilter(Preferences(target_titles=[], accept_unqualified_remote=True))
    assert f.location_ok(JobPosting(source="x", company="c", title="t", location="Remote", remote_type="remote"))


def test_describe_location(flt):
    p = JobPosting(source="x", company="c", title="t", location="London, UK", location_texts=["Dublin, Ireland"])
    assert flt.describe_location(p) == "London, UK / Dublin, Ireland"
    p = JobPosting(source="x", company="c", title="t", location="Remote", location_texts=["Europe"], remote_type="remote")
    assert flt.describe_location(p) == "Remote (Europe)"
    p = JobPosting(source="x", company="c", title="t", location="Dublin")
    assert flt.describe_location(p) == "Dublin"


# --- careers URL → ATS ---------------------------------------------------

@pytest.mark.parametrize("url,ats,slug,region", [
    ("https://job-boards.greenhouse.io/stripe", "greenhouse", "stripe", None),
    ("https://boards.greenhouse.io/embed/job_board?for=acme", "greenhouse", "acme", None),
    ("https://jobs.lever.co/palantir/abc-123", "lever", "palantir", "us"),
    ("https://jobs.eu.lever.co/acme", "lever", "acme", "eu"),
    ("jobs.ashbyhq.com/openai", "ashby", "openai", None),
    ("https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/x",
     "workday", "salesforce/wd12/External_Career_Site", None),
])
def test_parse_careers_url(url, ats, slug, region):
    assert parse_careers_url(url) == {"ats": ats, "slug": slug, "region": region}


def test_parse_careers_url_rejects_unknown():
    with pytest.raises(ValueError):
        parse_careers_url("https://careers.example.com/jobs")


# --- ATS parsers (real saved responses) ----------------------------------

def test_greenhouse_parser(flt):
    jobs = ats_greenhouse.parse_listing(load("greenhouse_intercom.json"), "Intercom")
    ai = next(j for j in jobs if j.title.startswith("AI Infrastructure"))
    assert ai.ats == "greenhouse" and ai.ats_job_id and ai.apply_url.startswith("https://")
    assert ai.jd_text and "<" not in ai.jd_text[:200]          # HTML stripped & unescaped
    assert ai.posted_at and len(ai.posted_at) == 19
    assert flt.keep(ai)
    assert not flt.keep(next(j for j in jobs if "Account Executive" in j.title))


def test_ashby_parser(flt):
    jobs = ats_ashby.parse_listing(load("ashby_elevenlabs.json"), "ElevenLabs")
    fs = next(j for j in jobs if "Full-Stack" in j.title)
    assert fs.remote_type == "remote" and fs.location == "Remote"
    assert fs.location_texts and fs.jd_text
    assert flt.keep(fs)


def test_lever_parser():
    jobs = ats_lever.parse_listing(load("lever_palantir.json"), "Palantir")
    assert len(jobs) == 2
    j = jobs[0]
    assert j.ats == "lever" and j.ats_job_id and j.title and j.jd_text
    assert j.remote_type in {"remote", "hybrid", "onsite", "unknown"}
    assert j.posted_at


def test_workday_detail_parser(flt):
    slug = "salesforce/wd12/External_Career_Site"
    p = ats_workday.parse_detail(load("workday_salesforce_detail_dublin.json"), "Salesforce", slug)
    assert p.ats == "workday" and p.ats_job_id and p.jd_text and p.posted_at
    assert "Dublin" in p.all_locations_text() and flt.keep(p)
    assert p.apply_url.startswith("https://salesforce.wd12.myworkdayjobs.com/")
    # A US multi-location role: parsed fine, rejected by the location filter.
    us = ats_workday.parse_detail(load("workday_salesforce_detail_us.json"), "Salesforce", slug)
    assert us.jd_text and not flt.location_ok(us)


# --- storage -------------------------------------------------------------

def test_upsert_is_idempotent(conn):
    cid = conn.execute("INSERT INTO companies(name, name_norm) VALUES('Stripe','stripe')").lastrowid
    p = JobPosting(source="greenhouse", company="Stripe", title="Software Engineer", location="Dublin",
                   jd_text="JD", ats="greenhouse", ats_job_id="1")
    assert upsert_job(conn, cid, p) == "new"
    p.jd_text = "JD v2"
    assert upsert_job(conn, cid, p) == "updated"
    rows = conn.execute("SELECT jd_text, jd_status FROM jobs").fetchall()
    assert len(rows) == 1 and rows[0]["jd_text"] == "JD v2" and rows[0]["jd_status"] == "ok"


def test_alert_job_enriched_by_ats_later(conn):
    alert = JobPosting(source="gmail_linkedin", company="Stripe", title="Software Engineer",
                       location="Dublin", linkedin_job_id="999", linkedin_job_url="https://li/999")
    assert upsert_job(conn, None, alert) == "new"
    ats = JobPosting(source="greenhouse", company="Stripe Inc", title="Software Engineer",
                     location="Dublin", jd_text="full JD", ats="greenhouse", ats_job_id="1")
    assert upsert_job(conn, None, ats) == "updated"
    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["jd_status"] == "ok" and row["linkedin_job_id"] == "999" and row["ats_job_id"] == "1"


def test_sync_watchlist_unwatches_removed(conn):
    e = lambda n, s: {"name": n, "aliases": [], "careers_url": None, "linkedin_slug": None,
                      "watch": True, "ats": "greenhouse", "slug": s, "region": None}
    assert sync_watchlist(conn, [e("A", "a"), e("B", "b")])["watched"] == 2
    assert sync_watchlist(conn, [e("A", "a")]) == {"watched": 1, "unwatched": 1}


def test_ireland_only_remote_regions():
    f = PreFilter(Preferences(target_titles=[], locations=["Dublin", "Ireland"], remote_regions=["Ireland"]))
    remote = lambda loc, extra=(): JobPosting(source="x", company="c", title="t", location=loc,
                                              location_texts=list(extra), remote_type="remote")
    assert not f.location_ok(remote("Remote - Europe"))
    assert f.location_ok(remote("Remote", ["Ireland"]))
    assert f.location_ok(remote("Remote - Ireland"))
