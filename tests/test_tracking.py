"""Milestones 6, 8 (live gating/caps), 9 and 10: no network, no LinkedIn, no Gmail."""

import json
import plistlib
from datetime import datetime, timedelta, timezone

import pytest

from job_agent.config import Config
from job_agent.db import init_db, kv_set
from job_agent.notify import bot
from job_agent.outreach import actions, sender, track
from job_agent.referrals import connections_csv as cc
from job_agent.safety import Safety, SafetyStop

CFG = Config.model_validate({
    "profile": {"name": "Sivaavanish Kanagasabapathi", "short_name": "Siva",
                "education": [{"school": "National College of Ireland"}]},
    "linkedin": {"caps": {"page_views": 15, "requests": 10, "withdrawals": 5}, "warmup": [3, 6],
                 "send_mode": "live", "min_dry_run_days": 5},
})


@pytest.fixture
def conn():
    c = init_db(":memory:")
    c.execute("INSERT INTO companies(id, name, name_norm) VALUES(1, 'Stripe', 'stripe')")
    c.execute("INSERT INTO jobs(id, company_id, source, title, dedupe_key, apply_url) "
              "VALUES(63, 1, 'greenhouse', 'Software Engineer', 'k63', 'https://stripe.com/jobs/63')")
    c.execute("INSERT INTO job_scores(job_id, score, decision) VALUES(63, 8.4, 'referral_first')")
    c.execute("""INSERT INTO referral_points(id, company_id, name, headline, profile_url, degree, tags_json)
                 VALUES(1, 1, 'Pranav Jain', 'SWE at Stripe', 'https://www.linkedin.com/in/pranavj/', '2nd', '["engineer"]')""")
    c.commit()
    yield c
    c.close()


def _outreach(conn, lane="B", status="sent", **cols):
    keys = ["referral_point_id", "job_ids_json", "lane", "note_text", "status", *cols]
    vals = [1, "[63]", lane, "Hi Pranav, note", status, *cols.values()]
    return conn.execute(f"INSERT INTO outreach({','.join(keys)}) VALUES({','.join('?' * len(vals))})", vals).lastrowid


# --- Connections.csv (milestone 6) -------------------------------------------------

CSV = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Aoife,Murphy,https://www.linkedin.com/in/aoifem,,Stripe,Software Engineer,01 Nov 2026
Hidden,Person,,,Stripe,Recruiter,02 Nov 2026
Bob,Smith,https://www.linkedin.com/in/bobs,,Stripe Payments Europe Ltd,Talent Partner,03 Nov 2026
Carol,Jones,https://www.linkedin.com/in/carolj,,Intercom,Engineer,04 Nov 2026
"""


def test_connections_csv_parsing_and_matching(conn, tmp_path):
    rows = cc.read_rows(CSV)
    assert [r["name"] for r in rows][:2] == ["Aoife Murphy", "Hidden Person"]
    assert cc.company_matches("Stripe", "Stripe", []) and not cc.company_matches("Intercom", "Stripe", [])
    path = tmp_path / "Connections.csv"
    path.write_text(CSV)
    cfg = Config.model_validate({**CFG.model_dump(), "referrals": {"connections_csv": str(path)}})
    st = cc.import_matches(conn, cfg)
    assert st["matched"] == 1 and st["no_url"] == 1          # Carol is at Intercom: never stored
    r = conn.execute("SELECT * FROM referral_points WHERE profile_url LIKE '%aoifem%'").fetchone()
    assert r["degree"] == "1st" and "connection" in json.loads(r["tags_json"])
    assert not conn.execute("SELECT 1 FROM referral_points WHERE name LIKE 'Carol%'").fetchone()


def test_connections_install_from_zip(tmp_path):
    import zipfile
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Basic_LinkedInDataExport/Connections.csv", CSV)
    dest = cc.install(z, tmp_path / "data" / "Connections.csv")
    assert "Aoife" in dest.read_text()


# --- acceptance tracking (milestone 9) ---------------------------------------------

def test_parse_acceptance_real_format():
    name, slugs = track.parse_acceptance(
        "Pranav Jain via LinkedIn <invitations@linkedin.com>",
        "Pranav accepted your invitation, explore their network",
        'see <a href="https://www.linkedin.com/comm/in/pranavj?midToken=x">x</a> and linkedin.com/in/someone-else')
    assert name == "Pranav Jain" and {"pranavj", "someone-else"} <= slugs
    assert track.parse_acceptance("x", "New jobs for you", "") is None


def test_match_needs_slug_and_name(conn):
    oid = _outreach(conn)
    assert track.match_outreach(conn, "Pranav Jain", {"pranavj"})["id"] == oid
    assert track.match_outreach(conn, "Pranav Jain", {"otherpranav"}) is None      # namesake
    assert track.match_outreach(conn, "Someone Else", {"pranavj"}) is None         # suggested profile only


def test_followup_after_five_days(conn):
    oid = _outreach(conn, lane="A", status="asked",
                    asked_at=(datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d %H:%M:%S"))
    young = _outreach(conn, lane="A", status="asked", asked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    msgs = []
    assert track.draft_followups(conn, CFG, msgs.append)["followups"] == 1
    r = actions.outreach_row(conn, oid)
    assert "Pranav" in r["followup_text"] and "Stripe" in r["followup_text"] and len(r["followup_text"]) <= 600
    assert actions.outreach_row(conn, young)["followup_text"] is None
    assert actions.current_text(r)[0] == "follow-up"
    assert "follow-up marked sent" in actions.mark_sent(conn, oid)
    assert track.draft_followups(conn, CFG)["followups"] == 0                        # never more than one


def test_lane_a_lifecycle(conn):
    oid = _outreach(conn, lane="A", status="drafted")
    assert "asked" in actions.mark_sent(conn, oid)
    assert "already" in actions.mark_sent(conn, oid)
    assert "replied" in actions.mark_replied(conn, oid)
    b = _outreach(conn, lane="B", status="queued")
    assert "sender sends" in actions.mark_sent(conn, b)


# --- Telegram bot ------------------------------------------------------------------

def test_bot_ignores_other_chats(conn, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    sent = []

    def fake_call(method, **p):
        if method == "getUpdates":
            return [{"update_id": 5, "message": {"chat": {"id": 999}, "text": "/pause"}},
                    {"update_id": 6, "message": {"chat": {"id": 111}, "text": "/help"}}]
        sent.append(p)
        return {}
    monkeypatch.setattr(bot.tg, "call", fake_call)
    assert bot.poll_once(conn, CFG) == 1
    assert len(sent) == 1 and "/draft" in sent[0]["text"]
    assert Safety(conn, CFG).paused_until() is None                                 # stranger's /pause ignored
    assert conn.execute("SELECT value FROM kv WHERE key='tg_offset'").fetchone()[0] == "7"


def test_bot_commands(conn):
    oid = _outreach(conn, lane="A", status="drafted")
    out = bot.handle(conn, CFG, f"/draft {oid}")
    assert "<pre>Hi Pranav, note</pre>" in out[1]
    assert "asked" in bot.handle(conn, CFG, f"/sent {oid}")[0]
    assert "Usage" in bot.handle(conn, CFG, "/sent")[0]
    assert "paused" in bot.handle(conn, CFG, "/pause")[0].lower()
    assert Safety(conn, CFG).paused_until()
    assert "resumed" in bot.handle(conn, CFG, "/resume")[0]
    assert Safety(conn, CFG).paused_until() is None
    assert "Unknown" in bot.handle(conn, CFG, "/rm -rf")[0]


# --- live sender gating, warm-up, caps (milestone 8) -------------------------------

def _dry_runs(conn, days):
    for d in range(days):
        conn.execute("INSERT INTO runs(stage, dry_run, started_at, ok, stats_json) VALUES('send', 1, ?, 1, ?)",
                     ((datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d 09:00:00"), '{"checked": 2}'))


def test_live_refused_until_five_dry_run_days(conn):
    _dry_runs(conn, 4)
    conn.execute("INSERT INTO runs(stage, dry_run, ok, stats_json) VALUES('send', 1, 1, '{\"checked\": 0}')")
    assert sender.resolve_mode(conn, CFG) == ("dry_run", "live sending needs 5 days of dry runs first (have 4)")
    _dry_runs(conn, 5)
    assert sender.resolve_mode(conn, CFG) == ("live", None)
    assert sender.resolve_mode(conn, CFG, force_dry=True)[0] == "dry_run"
    default = Config.model_validate({})
    assert sender.resolve_mode(conn, default)[0] == "dry_run"                        # default is dry run


def test_warmup_caps(conn):
    s = Safety(conn, CFG, ignore_window=True)
    assert s.requests_cap()[0] == 3                                                   # before the first send
    kv_set(conn, "warmup_started_on", (datetime.now() - timedelta(days=8)).date().isoformat())
    assert s.requests_cap()[0] == 6
    kv_set(conn, "warmup_started_on", (datetime.now() - timedelta(days=15)).date().isoformat())
    assert s.requests_cap()[0] == 10


def test_request_cap_and_count(conn):
    s = Safety(conn, CFG, ignore_window=True)
    for _ in range(3):
        s.check_can_send()
        s.count_request()
    with pytest.raises(SafetyStop, match="cap"):
        s.check_can_send()
    assert conn.execute("SELECT value FROM kv WHERE key='warmup_started_on'").fetchone()[0]


def test_low_acceptance_rate_halves_cap(conn):
    kv_set(conn, "warmup_started_on", "2020-01-01")
    for i in range(12):
        conn.execute("""INSERT INTO referral_points(company_id, name, profile_url) VALUES(1, ?, ?)""",
                     (f"P{i}", f"https://www.linkedin.com/in/p{i}/"))
        rid = conn.execute("SELECT max(id) FROM referral_points").fetchone()[0]
        conn.execute("INSERT INTO outreach(referral_point_id, lane, status, sent_at, accepted_at) "
                     "VALUES(?, 'B', 'sent', datetime('now', '-10 days'), ?)",
                     (rid, "2026-01-01" if i < 2 else None))
    cap, why = Safety(conn, CFG).requests_cap()
    assert cap == 5 and "halved" in why[-1]


def test_pending_over_200_blocks_until_100(conn):
    s = Safety(conn, CFG, ignore_window=True)
    monkey = {"n": 201}
    s.pending_count = lambda: monkey["n"]
    with pytest.raises(SafetyStop, match="pending"):
        s.check_can_send()
    monkey["n"] = 150
    with pytest.raises(SafetyStop, match="pending"):
        s.check_can_send()                                 # hysteresis: still blocked above 100
    monkey["n"] = 99
    s.check_can_send()


def test_after_restriction_manual_week_then_quarter_caps(conn):
    s = Safety(conn, CFG, ignore_window=True)
    t0 = datetime.now() - timedelta(days=10)
    kv_set(conn, "last_restriction_at", t0.isoformat())
    kv_set(conn, "resumed_at", (t0 + timedelta(days=1)).isoformat())                # resumed 9 days ago
    assert s.restriction_phase() == "reduced"
    assert s.views_left() == 3                                                       # 25% of 15
    kv_set(conn, "resumed_at", (datetime.now() - timedelta(days=2)).isoformat())
    assert s.restriction_phase() == "manual_only"


# --- scheduler (milestone 10) ------------------------------------------------------

def test_launchd_plists_are_valid():
    from job_agent import scheduler
    for name, spec in scheduler.jobs().items():
        d = plistlib.loads(plistlib.dumps(scheduler.plist(name, spec)))
        assert d["Label"] == f"com.nepotism.jobagent.{name}"
        assert d["ProgramArguments"][1:3] == ["-m", "job_agent"]
        assert "/" in d["EnvironmentVariables"]["PATH"]
    send = scheduler.jobs()["send"]["when"]["StartCalendarInterval"]
    assert {x["Weekday"] for x in send} == {1, 2, 3, 4, 5}                           # weekdays only
