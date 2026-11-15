"""Closed-job detection: feed absence with grace, Workday detail check, alert expiry, reopen."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from job_agent.db import init_db
from job_agent.http import NotFound
from job_agent.normalize import TS_FMT
from job_agent.sources import closures
from job_agent.sources.ats_workday import detail_path
from job_agent.sources.base import JobPosting
from job_agent.store import upsert_job


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(TS_FMT)


@pytest.fixture
def conn():
    c = init_db(":memory:")
    c.execute("INSERT INTO companies(id, name, name_norm, ats, slug, watch) VALUES(1,'Stripe','stripe','greenhouse','stripe',1)")
    c.execute("INSERT INTO companies(id, name, name_norm, ats, slug, watch) "
              "VALUES(2,'Salesforce','salesforce','workday','salesforce/wd12/External_Career_Site',1)")
    c.execute("INSERT INTO companies(id, name, name_norm, watch) VALUES(3,'Acme','acme',0)")
    c.commit()
    yield c
    c.close()


def job(conn, jid, cid, ats, ats_id, last_seen, url=None):
    conn.execute("""INSERT INTO jobs(id, company_id, source, title, ats, ats_job_id, apply_url, dedupe_key, last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (jid, cid, ats or "gmail_linkedin", f"Job {jid}", ats, ats_id, url,
                                                   f"k{jid}", last_seen))


def closed(conn, jid):
    return conn.execute("SELECT closed_at FROM jobs WHERE id = ?", (jid,)).fetchone()[0] is not None


GH = {"id": 1, "name": "Stripe", "ats": "greenhouse"}


def test_missing_from_feed_closes_after_grace(conn):
    job(conn, 1, 1, "greenhouse", "100", ago(4))      # missing 4 days → closed
    job(conn, 2, 1, "greenhouse", "200", ago(1))      # missing 1 day → grace
    job(conn, 3, 1, "greenhouse", "300", ago(9))      # still in feed (filtered out of kept) → open
    out = closures.reconcile(conn, None, GH, {"300", "999"}, close_after_days=3)
    assert out["closed"] == ["Stripe: Job 1"]
    assert closed(conn, 1) and not closed(conn, 2) and not closed(conn, 3)
    assert conn.execute("SELECT last_seen_at FROM jobs WHERE id = 3").fetchone()[0] > ago(0.01)


def test_empty_feed_never_closes(conn):
    job(conn, 1, 1, "greenhouse", "100", ago(10))
    assert closures.reconcile(conn, None, GH, set())["closed"] == []
    assert not closed(conn, 1)


def test_reappearing_job_reopens(conn):
    job(conn, 1, 1, "greenhouse", "100", ago(10))
    closures.reconcile(conn, None, GH, {"999"})
    assert closed(conn, 1)
    assert closures.reconcile(conn, None, GH, {"100"})["reopened"] == 1
    assert not closed(conn, 1)


def test_upsert_reopens(conn):
    job(conn, 1, 1, "greenhouse", "100", ago(10))
    conn.execute("UPDATE jobs SET closed_at = '2026-01-01', closed_reason = 'x' WHERE id = 1")
    upsert_job(conn, 1, JobPosting(source="greenhouse", company="Stripe", title="Job 1", ats="greenhouse",
                                   ats_job_id="100"))
    assert not closed(conn, 1)


class FakeHttp:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def get_json(self, url, params=None):
        self.calls.append(url)
        a = self.answers[url.rsplit("/", 1)[-1]]
        if isinstance(a, Exception):
            raise a
        return a


def test_workday_detail_check(conn):
    base = "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/Dublin/"
    job(conn, 1, 2, "workday", "JR1", ago(2), base + "Gone_JR1")
    job(conn, 2, 2, "workday", "JR2", ago(2), base + "Open_JR2")
    job(conn, 3, 2, "workday", "JR3", ago(2), base + "Flaky_JR3")
    job(conn, 4, 2, "workday", "JR4", ago(0), base + "Today_JR4")         # refreshed today: not checked
    http = FakeHttp({"Gone_JR1": NotFound("x"), "Open_JR2": {"jobPostingInfo": {"title": "Open"}},
                     "Flaky_JR3": RuntimeError("503")})
    wd = {"id": 2, "name": "Salesforce", "ats": "workday", "slug": "salesforce/wd12/External_Career_Site"}
    out = closures.reconcile(conn, http, wd, None)
    assert out["closed"] == ["Salesforce: Job 1"]
    assert not closed(conn, 2) and not closed(conn, 3)                     # errors never close
    assert len(http.calls) == 3
    assert http.calls[0] == ("https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/"
                             "External_Career_Site/job/Dublin/Gone_JR1")


def test_detail_path():
    assert detail_path("https://x.wd3.myworkdayjobs.com/en-US/Site/job/Dublin/T_JR9?src=a") == "/job/Dublin/T_JR9"
    assert detail_path(None) is None


def test_alert_only_and_unwatched_expire(conn):
    job(conn, 1, None, None, None, ago(31))                 # alert-only, old
    job(conn, 2, None, None, None, ago(5))                  # alert-only, recent
    job(conn, 3, 3, "lever", "abc", ago(40))                # matched to an unwatched company's ATS
    job(conn, 4, 1, "greenhouse", "100", ago(40))           # watched ATS: handled by reconcile, not here
    assert len(closures.expire_alert_only(conn, 30)) == 2
    assert closed(conn, 1) and not closed(conn, 2) and closed(conn, 3) and not closed(conn, 4)


def test_unsent_outreach_dropped_when_all_roles_closed(conn):
    job(conn, 1, 1, "greenhouse", "100", ago(0))
    job(conn, 2, 1, "greenhouse", "200", ago(0))
    conn.execute("INSERT INTO referral_points(id, company_id, name, profile_url) VALUES(1,1,'Pranav Jain','u1')")
    conn.execute("INSERT INTO referral_points(id, company_id, name, profile_url) VALUES(2,1,'Manoj R','u2')")
    q = conn.execute("INSERT INTO outreach(referral_point_id, job_ids_json, lane, status) VALUES(1, ?, 'B', 'queued')",
                     (json.dumps([1]),)).lastrowid
    both = conn.execute("INSERT INTO outreach(referral_point_id, job_ids_json, lane, status) VALUES(2, ?, 'B', 'queued')",
                        (json.dumps([1, 2]),)).lastrowid
    sent = conn.execute("INSERT INTO outreach(referral_point_id, job_ids_json, lane, status) VALUES(2, ?, 'B', 'sent')",
                        (json.dumps([1]),)).lastrowid
    conn.execute("UPDATE jobs SET closed_at = '2026-11-15' WHERE id = 1")
    assert closures.drop_outreach_for_closed(conn) == ["Pranav Jain"]
    st = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM outreach")}
    assert st[q] == "aborted" and st[both] == "queued" and st[sent] == "sent"   # one role still open; sent untouched
