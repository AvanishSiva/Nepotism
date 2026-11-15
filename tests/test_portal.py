import json

import pytest

from job_agent.db import init_db
from job_agent.portal.commands import execute


@pytest.fixture
def conn():
    c = init_db(":memory:")
    cid = c.execute("INSERT INTO companies(name, name_norm) VALUES('Stripe','stripe')").lastrowid
    c.execute("INSERT INTO jobs(id, company_id, source, title, location, jd_text, jd_status, apply_url, dedupe_key) "
              "VALUES(7, ?, 'greenhouse', 'Software Engineer', 'Dublin', ?, 'ok', 'https://x.test/7', 'k')",
              (cid, "<script>alert(1)</script>\nBuild payments."))
    c.execute("INSERT INTO job_scores(job_id, score, decision, reasons_json, explanation, facts_json) "
              "VALUES(7, 8.4, 'referral_first', '[]', '✓ Java', ?)", (json.dumps({"must_have": ["Java"]}),))
    c.commit()
    yield c
    c.close()


def text(out):
    return "\n".join("".join(s["t"] for s in ln) for ln in out.lines)


def test_ls_and_show(conn):
    ls = execute(conn, "ls")
    assert "Software Engineer" in text(ls) and "↗ apply" in text(ls)
    assert any(s.get("href") == "https://x.test/7" for ln in ls.lines for s in ln)
    out = execute(conn, "show 7")
    t = text(out)
    assert "8.4" in t and "✓ Java" in t and "https://x.test/7" in t
    assert "<script>" in t          # passed through as plain text; the browser renders via textContent
    assert any(s.get("href") == "https://x.test/7" for ln in out.lines for s in ln)


def test_applied_hides_from_ls_and_feedback(conn):
    assert "#7 marked applied" in text(execute(conn, "applied 7"))
    assert "Software Engineer" not in text(execute(conn, "ls"))
    assert "Software Engineer" in text(execute(conn, "ls applied"))
    execute(conn, "unapply 7")
    execute(conn, "skip 7")
    assert "Software Engineer" not in text(execute(conn, "ls"))


def test_open_and_run_and_errors(conn):
    assert execute(conn, "open 7").client["open"] == "https://x.test/7"
    assert execute(conn, "run score").client["run"] == "score"
    assert "usage" in text(execute(conn, "run rm -rf /"))       # only fixed stages
    assert "command not found" in text(execute(conn, "sudo ls"))
    assert "expected a job id" in text(execute(conn, "show abc"))
    assert execute(conn, "clear").client["clear"]
