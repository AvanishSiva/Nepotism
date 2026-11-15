"""Gmail alert parsing + alert→ATS matching.

The alert emails below are SYNTHETIC, written in LinkedIn's usual layout. Replace with
real saved samples (`python -m job_agent gmail-peek --save`) once Gmail is authorised.
"""

import pytest

from job_agent.db import init_db
from job_agent.sources import gmail_alerts, matcher
from job_agent.sources.base import JobPosting
from job_agent.sources.gmail_api import EmailMsg, parse_message
from job_agent.store import upsert_job

TEXT = """Your job alert for software engineer in Dublin
3 new jobs match your preferences.

Software Engineer, Payments
Stripe
Dublin, County Dublin, Ireland
Apply with resume & profile
View job: https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc%3D&refId=x

---------------------------------------------------------

Machine Learning Engineer
Wayflyer
Dublin (Hybrid)
Actively recruiting
3 school alumni
View job: https://www.linkedin.com/comm/jobs/view/4012345679/?trackingId=def

---------------------------------------------------------

AI Engineer
Some Startup Ltd
Ireland (Remote)
View job: https://www.linkedin.com/comm/jobs/view/4012345680/

---------------------------------------------------------

See all jobs: https://www.linkedin.com/comm/jobs/search/?keywords=software
Unsubscribe: https://www.linkedin.com/comm/psettings/email-unsubscribe
"""

HTML = """<html><body><table>
<tr><td><table><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc"><img alt="Stripe logo"></a>
  <a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc">Software Engineer, Payments</a>
  <p>Stripe · Dublin, County Dublin, Ireland</p><p>Apply with resume &amp; profile</p>
</td></tr></table></td></tr>
<tr><td><table><tr><td>
  <a href="https://www.linkedin.com/comm/jobs/view/4012345679/?trackingId=def">Machine Learning Engineer</a>
  <p>Wayflyer · Dublin (Hybrid)</p><p>3 school alumni</p>
</td></tr></table></td></tr>
</table>
<a href="https://www.linkedin.com/comm/jobs/search/">See all jobs</a></body></html>"""


def msg(text="", html=""):
    return EmailMsg(id="m1", subject="software engineer: Stripe and more", sender="LinkedIn Job Alerts",
                    date="2026-11-14 07:00:00", text=text, html=html)


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


# --- parsing -------------------------------------------------------------

def test_job_ids_deduped_and_ignore_search_links():
    assert gmail_alerts.job_ids_in(msg(TEXT, HTML)) == ["4012345678", "4012345679", "4012345680"]


def test_job_id_from_slug_style_url():
    m = msg("https://www.linkedin.com/jobs/view/senior-engineer-at-acme-4099999999?x=1")
    assert gmail_alerts.job_ids_in(m) == ["4099999999"]


def test_parse_text():
    jobs = gmail_alerts.parse_text(msg(TEXT))
    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("Software Engineer, Payments", "Stripe", "Dublin, County Dublin, Ireland"),
        ("Machine Learning Engineer", "Wayflyer", "Dublin (Hybrid)"),
        ("AI Engineer", "Some Startup Ltd", "Ireland (Remote)"),
    ]
    j = jobs[0]
    assert j.linkedin_job_url == "https://www.linkedin.com/jobs/view/4012345678/"   # tracking stripped
    assert j.alert_date == "2026-11-14 07:00:00" and j.source == "gmail_linkedin"


def test_parse_html():
    jobs = gmail_alerts.parse_html(msg(html=HTML))
    assert [(j.title, j.company, j.location) for j in jobs] == [
        ("Software Engineer, Payments", "Stripe", "Dublin, County Dublin, Ireland"),
        ("Machine Learning Engineer", "Wayflyer", "Dublin (Hybrid)"),
    ]


def test_parse_alert_prefers_complete_parser():
    jobs, method = gmail_alerts.parse_alert(msg(TEXT, HTML), ollama_model=None)
    assert method == "text+html" and len(jobs) == 3
    assert jobs[0].location == "Dublin, County Dublin, Ireland"  # HTML location kept


def test_parse_alert_falls_back_to_html_when_text_missing():
    jobs, method = gmail_alerts.parse_alert(msg("", HTML), ollama_model=None)
    assert method == "html" and len(jobs) == 2


def test_llm_results_limited_to_real_job_ids(monkeypatch):
    fake = {"jobs": [
        {"job_id": "4012345678", "title": "SWE", "company": "Stripe", "location": "Dublin"},
        {"job_id": "1111111111", "title": "Invented", "company": "Evil", "location": ""},
    ]}
    monkeypatch.setattr(gmail_alerts.ollama, "chat", lambda *a, **k: fake)
    jobs = gmail_alerts.parse_llm(msg("junk https://www.linkedin.com/jobs/view/4012345678/"), "m")
    assert [j.linkedin_job_id for j in jobs] == ["4012345678"]


def test_parse_message_decodes_multipart():
    import base64
    enc = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
    raw = {"id": "x", "internalDate": "1794639600000", "payload": {
        "mimeType": "multipart/alternative",
        "headers": [{"name": "Subject", "value": "Hi"}, {"name": "From", "value": "a@b"}],
        "parts": [{"mimeType": "text/plain", "body": {"data": enc("plain é")}},
                  {"mimeType": "text/html", "body": {"data": enc("<b>html</b>")}}]}}
    m = parse_message(raw)
    assert m.text == "plain é" and m.html == "<b>html</b>" and m.subject == "Hi" and m.date


# --- matching ------------------------------------------------------------

def P(title, loc, remote="unknown", extra=()):
    return JobPosting(source="greenhouse", company="Stripe", title=title, location=loc,
                      remote_type=remote, location_texts=list(extra), ats="greenhouse", ats_job_id=title)


def test_best_match_prefers_exact_title_over_superset():
    alert = JobPosting(source="gmail_linkedin", company="Stripe", title="Software Engineer",
                       location="Dublin, County Dublin, Ireland")
    cands = [P("Software Engineer, Payments", "Dublin"), P("Software Engineer", "Dublin")]
    assert matcher.best_match(alert, cands).title == "Software Engineer"


def test_best_match_ignores_level_numerals():
    alert = JobPosting(source="gmail_linkedin", company="MongoDB", title="Software Engineer 3",
                       location="Dublin")
    assert matcher.best_match(alert, [P("Software Engineer III", "Dublin, Ireland")]) is not None


def test_best_match_requires_compatible_location():
    alert = JobPosting(source="gmail_linkedin", company="Stripe", title="Software Engineer",
                       location="Dublin, County Dublin, Ireland")
    assert matcher.best_match(alert, [P("Software Engineer", "London, UK")]) is None
    assert matcher.best_match(alert, [P("Software Engineer", "London, UK", extra=["Dublin, Ireland"])])


def test_best_match_remote_alert_matches_remote_posting():
    alert = JobPosting(source="gmail_linkedin", company="X", title="AI Engineer", location="Ireland (Remote)")
    assert matcher.best_match(alert, [P("AI Engineer", "Remote - EMEA", remote="remote")])


def test_best_match_rejects_different_role():
    alert = JobPosting(source="gmail_linkedin", company="Stripe", title="Machine Learning Engineer",
                       location="Dublin")
    assert matcher.best_match(alert, [P("Account Executive", "Dublin")]) is None


def test_find_company_by_alias_and_fuzzy(conn):
    conn.execute("INSERT INTO companies(name, name_norm, aliases_json) VALUES('Intercom','intercom','[\"Fin\"]')")
    conn.execute("INSERT INTO companies(name, name_norm) VALUES('ElevenLabs','elevenlabs')")
    assert matcher.find_company(conn, "Fin")["name"] == "Intercom"
    assert matcher.find_company(conn, "Intercom Ltd")["name"] == "Intercom"
    assert matcher.find_company(conn, "Eleven Labs")["name"] == "ElevenLabs"
    assert matcher.find_company(conn, "Unrelated Co") is None


def test_slug_guesses():
    assert matcher.slug_guesses("Kitman Labs Ltd") == ["kitmanlabs", "kitman-labs"]


def test_enrich_matches_watched_company(conn, monkeypatch):
    conn.execute("INSERT INTO companies(name, name_norm, ats, slug, watch) "
                 "VALUES('Stripe','stripe','greenhouse','stripe',1)")
    ats_job = P("Software Engineer, Payments", "Dublin")
    ats_job.jd_text = "Full JD"
    monkeypatch.setattr(matcher, "_candidates", lambda *a, **k: [ats_job])
    alert = gmail_alerts.parse_text(msg(TEXT))[0]
    posting, cid, outcome = matcher.enrich(conn, http=None, alert=alert, probe_unknown=False)
    assert outcome == "matched" and posting.jd_text == "Full JD"
    assert posting.linkedin_job_id == "4012345678"


def test_enrich_unknown_company_kept_as_missing_jd(conn):
    alert = gmail_alerts.parse_text(msg(TEXT))[2]
    posting, cid, outcome = matcher.enrich(conn, http=None, alert=alert, probe_unknown=False)
    assert outcome == "no_ats" and posting.jd_text is None
    assert upsert_job(conn, cid, posting) == "new"
    row = conn.execute("SELECT jd_status, linkedin_job_url FROM jobs").fetchone()
    assert row["jd_status"] == "missing" and row["linkedin_job_url"]
    # Company recorded (not polled daily) so it can be added to the watchlist later.
    assert conn.execute("SELECT watch FROM companies WHERE id = ?", (cid,)).fetchone()[0] == 0


def test_alert_only_row_merged_when_ats_row_appears(conn):
    alert = gmail_alerts.parse_text(msg(TEXT))[0]
    upsert_job(conn, None, alert)                               # alert first, JD missing
    upsert_job(conn, None, P("Software Engineer, Payments", "Dublin"))   # ATS row, separate
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    matched = P("Software Engineer, Payments", "Dublin")
    matched.linkedin_job_id = alert.linkedin_job_id
    upsert_job(conn, None, matched)                             # matcher links them → merge
    rows = conn.execute("SELECT ats_job_id, linkedin_job_id FROM jobs").fetchall()
    assert len(rows) == 1 and rows[0]["ats_job_id"] and rows[0]["linkedin_job_id"] == "4012345678"


# --- REAL LinkedIn emails (2026-11, URL query strings scrubbed) ------------

from pathlib import Path

FX = Path(__file__).parent / "fixtures"


def real(name):
    return EmailMsg(id=name, subject="", sender="", date="2026-11-14 00:00:00",
                    text=(FX / f"linkedin_{name}.txt").read_text(),
                    html=(FX / f"linkedin_{name}.html").read_text())


def test_real_alert_with_badges():
    m = real("alert_fast_growing")
    jobs, method = gmail_alerts.parse_alert(m, ollama_model=None)
    assert method == "text+html" and len(jobs) == len(gmail_alerts.job_ids_in(m)) == 10
    first = jobs[0]
    # "Fast growing" badge right under the location must not shift fields.
    assert (first.title, first.company) == ("Back End Developer (AI Infrastructure)", "Alignerr")
    assert first.location == "Dublin (Remote)" and first.remote_type == "remote"
    assert all("alum" not in j.location.lower() and "hiring" not in j.title.lower() for j in jobs)


def test_real_recommendation_email_headings_not_jobs():
    m = real("recommendation_connections")
    jobs, _ = gmail_alerts.parse_alert(m, ollama_model=None)
    assert len(jobs) == 5
    assert not any("connections" in j.title.lower() or "ask them" in j.company.lower() for j in jobs)
    assert ("Full-Stack Engineer", "ElevenLabs") in {(j.title, j.company) for j in jobs}


def test_remote_alert_rejects_other_country_copy():
    # Aggregators (e.g. Jobgether) post one copy per country.
    alert = JobPosting(source="gmail_linkedin", company="Jobgether", title="AI Platform Engineer",
                       location="Ireland (Remote)")
    assert matcher.best_match(alert, [P("AI Platform Engineer", "Romania", remote="remote")]) is None
    assert matcher.best_match(alert, [P("AI Platform Engineer", "Remote - Europe", remote="remote")])


def test_title_with_extra_requirement_is_different_job():
    assert not matcher.title_compatible("Forward Deployed Engineer",
                                        "Forward Deployed Engineer Dutch/German speakers", "Dublin")
    assert matcher.title_compatible("Software Engineer", "Software Engineer - Dublin", "Dublin")
    assert matcher.title_compatible("Software Engineer 3", "Software Engineer III", "Dublin")
    assert matcher.title_compatible("Full Stack Engineer", "Full-Stack Engineer", "Dublin")
