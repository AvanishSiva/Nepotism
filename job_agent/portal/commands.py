"""Portal command interpreter: one text command in → styled terminal lines out.

Every line is a list of segments: {"t": text, "c": css-class, "href"?: url, "cmd"?: command}.
The browser renders segments with textContent only (JD/email text is untrusted).
"""

from __future__ import annotations

import json
import shlex
import sqlite3
import textwrap

from job_agent.normalize import now_utc


class Out:
    def __init__(self):
        self.lines: list[list[dict]] = []
        self.client: dict = {}            # instructions for the browser (open url, clear, task)

    def line(self, *segs):
        row = []
        for s in segs:
            if isinstance(s, str):
                row.append({"t": s})
            elif isinstance(s, dict):
                row.append(s)
            else:
                t, c, *extra = s
                seg = {"t": t, "c": c}
                if extra and extra[0]:
                    seg.update(extra[0])
                row.append(seg)
        self.lines.append(row)
        return self

    def text(self, s: str, c: str = ""):
        for ln in (s or "").splitlines() or [""]:
            self.line((ln, c))
        return self

    def blank(self):
        self.lines.append([])
        return self

    def err(self, s: str):
        return self.line(("error: ", "r"), (s, ""))


DEC = {"referral_first": ("REF", "g"), "apply_direct": ("APP", "c"), "skip": ("SKP", "d")}
COMMANDS = {
    "help": "this list",
    "ls [ref|apply|skip|new|applied|all]": "list scored jobs (default: ref + apply)",
    "show <id>": "job details, facts, gaps, links, JD preview",
    "jd <id>": "full job description",
    "open <id>": "open the apply link in a new tab",
    "grep <text>": "search titles and companies",
    "applied <id> | unapply <id>": "mark / unmark as applied",
    "good <id> | bad <id> | skip <id>": "feedback (skip hides it from ls)",
    "people [company]": "referral contacts found on LinkedIn",
    "drafts": "outreach drafts (Lane A asks, Lane B notes)",
    "draft <id>": "full text of one draft (or its follow-up)",
    "sent <id> | replied <id> | referred <id>": "Lane A: you sent it / they replied / they referred you",
    "stats": "pipeline counts + last runs",
    "runs": "last 15 stage runs",
    "companies": "ATS watchlist",
    "run <discover|score|digest|track>": "run a pipeline stage (output streams here)",
    "clear": "clear screen (or Ctrl+L)",
}


def _pad(s, n):
    s = str(s or "")
    return (s[: n - 1] + "…") if len(s) > n else s.ljust(n)


def _job(conn, jid):
    return conn.execute(
        """SELECT j.*, coalesce(c.name,'?') AS company, s.score, s.decision, s.reasons_json, s.explanation,
                  s.facts_json, s.low_confidence, a.status AS app_status,
                  (SELECT verdict FROM feedback f WHERE f.job_id = j.id ORDER BY f.id DESC LIMIT 1) AS verdict
           FROM jobs j LEFT JOIN companies c ON c.id = j.company_id LEFT JOIN job_scores s ON s.job_id = j.id
           LEFT JOIN applications a ON a.job_id = j.id WHERE j.id = ?""", (jid,)).fetchone()


def _id(args, out) -> int | None:
    if not args or not args[0].lstrip("#").isdigit():
        out.err("expected a job id, e.g. show 63")
        return None
    return int(args[0].lstrip("#"))


def _row(out, r):
    tag, cls = DEC.get(r["decision"], ("---", "d"))
    flags = ("A" if r["app_status"] else " ") + {"good": "+", "bad": "-", "skip": "x"}.get(r["verdict"] or "", " ") \
        + ("?" if r["low_confidence"] else " ")
    out.line((f"{r['id']:>4} ", "y", {"cmd": f"show {r['id']}"}),
             (f"{(r['score'] or 0):>4.1f} ", "b" if (r["score"] or 0) >= 8 else ""),
             (tag + " ", cls), (flags + " ", "m"),
             (_pad(r["company"], 15) + " ", "c"), (_pad(r["title"], 52) + " ", ""),
             (_pad(r["location"], 26) + " ", "d"))
    url, kind = (r["apply_url"], "apply") if r["apply_url"] else (r["linkedin_job_url"], "linkedin")
    if url:
        out.lines[-1].append({"t": f"↗ {kind}", "c": "u", "href": url})


def cmd_ls(conn, args, out):
    which = (args[0] if args else "").lower()
    where = {"ref": "s.decision = 'referral_first'", "apply": "s.decision = 'apply_direct'",
             "skip": "s.decision = 'skip'", "applied": "a.status IS NOT NULL", "all": "1=1"}.get(
        which, "s.decision IN ('referral_first','apply_direct')")
    if which == "new":
        last = conn.execute("SELECT value FROM kv WHERE key='last_digest_at'").fetchone()
        where = f"s.decision != 'skip' AND j.first_seen_at > '{last['value'] if last else ''}'"
    hide = "" if which in ("all", "applied", "skip") else \
        " AND a.status IS NULL AND coalesce((SELECT verdict FROM feedback f WHERE f.job_id=j.id ORDER BY f.id DESC LIMIT 1),'') != 'skip'"
    rows = conn.execute(
        f"""SELECT j.id, j.title, j.location, j.apply_url, j.linkedin_job_url, coalesce(c.name,'?') AS company,
                   s.score, s.decision,
                   s.low_confidence, a.status AS app_status,
                   (SELECT verdict FROM feedback f WHERE f.job_id = j.id ORDER BY f.id DESC LIMIT 1) AS verdict
            FROM job_scores s JOIN jobs j ON j.id = s.job_id LEFT JOIN companies c ON c.id = j.company_id
            LEFT JOIN applications a ON a.job_id = j.id
            WHERE {where}{hide}
            ORDER BY CASE s.decision WHEN 'referral_first' THEN 0 WHEN 'apply_direct' THEN 1 ELSE 2 END,
                     s.low_confidence, s.score DESC""").fetchall()
    out.line(("  ID SCORE DEC  FL  COMPANY         TITLE" + " " * 48 + "LOCATION" + " " * 19 + "LINK", "d"))
    for r in rows:
        _row(out, r)
    out.line((f"-- {len(rows)} job(s). FL: A=applied +/-=feedback ?=no JD. click an id or `show <id>`", "d"))


def cmd_show(conn, args, out):
    jid = _id(args, out)
    if jid is None:
        return
    r = _job(conn, jid)
    if not r:
        return out.err(f"no job #{jid}")
    facts = json.loads(r["facts_json"] or "{}")
    tag, cls = DEC.get(r["decision"], ("unscored", "d"))
    out.line((f"#{r['id']} ", "y"), (r["title"], "b"))
    out.line((r["company"], "c"), (f"  ·  {r['location'] or '?'}  ·  {r['remote_type']}", "d"))
    out.line(("score ", "d"), (f"{r['score'] if r['score'] is not None else '-'}", "b"), ("  ", ""), (tag, cls),
             (f"   posted {(r['posted_at'] or r['alert_date'] or '?')[:10]}  first seen {r['first_seen_at'][:10]}", "d"))
    if r["app_status"]:
        out.line(("status: ", "d"), (r["app_status"], "g"))
    if facts:
        yrs = facts.get("years_required")
        out.line(("facts  ", "d"), (f"{facts.get('seniority','?')} · {facts.get('role_type','?')} · "
                                    f"{(str(yrs) + '+ yrs') if yrs is not None else 'yrs n/a'} · "
                                    f"{facts.get('domain') or '-'}", ""))
        if facts.get("must_have"):
            out.line(("must   ", "d"), (", ".join(facts["must_have"]), ""))
        if facts.get("nice_to_have"):
            out.line(("nice   ", "d"), (", ".join(facts["nice_to_have"]), "d"))
        comp = facts.get("_components") or {}
        if comp:
            out.line(("parts  ", "d"), ("  ".join(f"{k}={v}" for k, v in comp.items() if k != "boost"), "d"))
    for e in (r["explanation"] or "").splitlines():
        out.line(("  " + e, "g" if e.startswith("✓") else "r" if e.startswith("✗") else "y"))
    for x in json.loads(r["reasons_json"] or "[]"):
        out.line(("  ! " + x, "o"))
    for label, url in (("apply   ", r["apply_url"]), ("linkedin", r["linkedin_job_url"])):
        if url:
            out.line((label + " ", "d"), (url, "u", {"href": url}))
    if r["jd_text"]:
        out.blank().line(("--- JD preview ---", "d"))
        wrapped = []
        for para in r["jd_text"].splitlines():
            wrapped += textwrap.wrap(para, 110) or [""]
        for ln in wrapped[:14]:
            out.line((ln, "d"))
        if len(wrapped) > 14:
            out.line((f"… {len(wrapped) - 14} more lines: ", "d"), (f"jd {jid}", "y", {"cmd": f"jd {jid}"}))
    else:
        out.line(("no JD stored: open the LinkedIn link", "o"))


def cmd_jd(conn, args, out):
    jid = _id(args, out)
    if jid is None:
        return
    r = conn.execute("SELECT jd_text, title FROM jobs WHERE id = ?", (jid,)).fetchone()
    if not r or not r["jd_text"]:
        return out.err(f"no JD for #{jid}")
    out.line((f"#{jid} {r['title']}", "b"))
    for para in r["jd_text"].splitlines():
        for ln in textwrap.wrap(para, 110) or [""]:
            out.line((ln, ""))


def cmd_open(conn, args, out):
    jid = _id(args, out)
    if jid is None:
        return
    r = conn.execute("SELECT coalesce(apply_url, linkedin_job_url) AS u FROM jobs WHERE id = ?", (jid,)).fetchone()
    if not r or not r["u"]:
        return out.err(f"no link for #{jid}")
    out.client["open"] = r["u"]
    out.line(("opening ", "d"), (r["u"], "u", {"href": r["u"]}))


def cmd_grep(conn, args, out):
    if not args:
        return out.err("usage: grep <text>")
    q = f"%{' '.join(args)}%"
    rows = conn.execute(
        """SELECT j.id, j.title, j.location, j.apply_url, j.linkedin_job_url, coalesce(c.name,'?') AS company,
                  s.score, s.decision, s.low_confidence, a.status AS app_status,
                  (SELECT verdict FROM feedback f WHERE f.job_id = j.id ORDER BY f.id DESC LIMIT 1) AS verdict
           FROM jobs j LEFT JOIN companies c ON c.id = j.company_id LEFT JOIN job_scores s ON s.job_id = j.id
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE j.title LIKE ? OR c.name LIKE ? ORDER BY s.score DESC""", (q, q)).fetchall()
    for r in rows:
        _row(out, r)
    out.line((f"-- {len(rows)} match(es)", "d"))


def _set_status(conn, args, out, status):
    jid = _id(args, out)
    if jid is None:
        return
    if not conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone():
        return out.err(f"no job #{jid}")
    if status is None:
        conn.execute("DELETE FROM applications WHERE job_id = ?", (jid,))
        out.line((f"#{jid} unmarked", "y"))
    else:
        now = now_utc()
        conn.execute("""INSERT INTO applications(job_id, status, applied_at, updated_at) VALUES(?,?,?,?)
                        ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at""",
                     (jid, status, now, now))
        out.line((f"#{jid} marked {status}", "g"))
    conn.commit()


def _feedback(conn, args, out, verdict):
    jid = _id(args, out)
    if jid is None:
        return
    if not conn.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone():
        return out.err(f"no job #{jid}")
    conn.execute("INSERT INTO feedback(job_id, verdict) VALUES(?, ?)", (jid, verdict))
    conn.commit()
    out.line((f"#{jid} feedback: {verdict}", "g"))


def cmd_stats(conn, args, out):
    j = conn.execute("SELECT count(*) n, sum(jd_status='ok') jd, sum(linkedin_job_id IS NOT NULL) li FROM jobs").fetchone()
    out.line(("jobs      ", "d"), (f"{j['n']}", "b"), (f"  ({j['jd'] or 0} with JD, {j['li'] or 0} from LinkedIn alerts)", "d"))
    for d, n in conn.execute("SELECT decision, count(*) FROM job_scores GROUP BY decision"):
        tag, cls = DEC.get(d, (d, ""))
        out.line((f"{tag:<10}", cls), (str(n), "b"))
    a = conn.execute("SELECT count(*) FROM applications").fetchone()[0]
    out.line(("applied   ", "d"), (str(a), "b"))
    c = conn.execute("SELECT count(*) FROM companies WHERE watch = 1").fetchone()[0]
    out.line(("watching  ", "d"), (f"{c} companies", ""))
    out.blank().line(("last runs", "d"))
    for r in conn.execute("""SELECT stage, ok, max(started_at) AS t FROM runs GROUP BY stage ORDER BY t DESC"""):
        out.line((f"  {r['stage']:<16}", ""), ("ok  " if r["ok"] else "FAIL", "g" if r["ok"] else "r"),
                 (f" {r['t']} UTC", "d"))


def cmd_runs(conn, args, out):
    for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 15"):
        st = json.loads(r["stats_json"] or "{}")
        brief = ", ".join(f"{k}={v}" for k, v in st.items() if isinstance(v, (int, float, str)) and k != "report")[:80]
        out.line((f"{r['id']:>4} ", "y"), (f"{r['stage']:<15}", ""),
                 ("ok   " if r["ok"] else ("FAIL " if r["ok"] == 0 else "...  "), "g" if r["ok"] else "r"),
                 (f"{r['started_at']}  ", "d"), (brief or (r["error"] or "")[:80], "d"))


def cmd_companies(conn, args, out):
    for r in conn.execute("""SELECT c.name, c.ats, c.slug, c.watch, count(j.id) n FROM companies c
                             LEFT JOIN jobs j ON j.company_id = c.id WHERE c.ats IS NOT NULL
                             GROUP BY c.id ORDER BY c.watch DESC, n DESC"""):
        out.line(("● " if r["watch"] else "○ ", "g" if r["watch"] else "d"), (_pad(r["name"], 16), "c"),
                 (_pad(r["ats"], 11), "d"), (_pad(r["slug"], 40), "d"), (f"{r['n']} jobs", ""))


def cmd_people(conn, args, out):
    from job_agent.referrals.linkedin_people import people_for
    q = "SELECT DISTINCT c.id, c.name FROM referral_points r JOIN companies c ON c.id = r.company_id"
    params = ()
    if args:
        q += " WHERE c.name LIKE ?"
        params = (f"%{' '.join(args)}%",)
    rows = conn.execute(q + " ORDER BY c.name", params).fetchall()
    for c in rows:
        out.line((c["name"], "b"))
        for p in people_for(conn, c["id"]):
            tags = ",".join(sorted(p.tags))
            out.line((f"  {p.rank:>3} ", "y"), (_pad(p.name, 26) + " ", "c"), (_pad(p.degree or "-", 4) + " ", "d"),
                     (_pad(tags, 30) + " ", "g" if ("alum" in p.tags or "ex_colleague" in p.tags) else "m"),
                     (_pad(p.headline, 50) + " ", "d"), ("↗ profile", "u", {"href": p.profile_url}))
    if not rows:
        out.line(("no referral contacts yet: run `python -m job_agent referrals` in the LinkedIn window", "d"))


def cmd_drafts(conn, args, out):
    rows = conn.execute("""SELECT o.id, o.lane, o.status, o.note_text, o.low_quality, r.name, c.name AS company
                           FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
                           LEFT JOIN companies c ON c.id = r.company_id ORDER BY o.id""").fetchall()
    for r in rows:
        out.line((f"{r['id']:>4} ", "y", {"cmd": f"draft {r['id']}"}), (f"[{r['lane']}] ", "c"),
                 (_pad(r["status"], 9), "g" if r["status"] in ("sent", "accepted") else "d"),
                 (_pad(r["company"], 12) + " ", "c"), (_pad(r["name"], 24) + " ", ""),
                 (_pad(r["note_text"], 70), "d"), (" LOW" if r["low_quality"] else "", "o"))
    out.line((f"-- {len(rows)} draft(s). [A]=you send it  [B]=connection note (sender, dry-run for now)", "d"))


def cmd_draft(conn, args, out):
    did = _id(args, out)
    if did is None:
        return
    r = conn.execute("""SELECT o.*, r.name, r.profile_url, r.headline, r.tags_json FROM outreach o
                        JOIN referral_points r ON r.id = o.referral_point_id WHERE o.id = ?""", (did,)).fetchone()
    if not r:
        return out.err(f"no draft #{did}")
    out.line((f"#{did} [{r['lane']}] {r['status']}  ", "y"), (r["name"], "b"), (f"  {r['headline'] or ''}", "d"))
    out.line(("tags ", "d"), (", ".join(json.loads(r["tags_json"] or "[]")), "m"),
             ("   ", ""), ("↗ profile", "u", {"href": r["profile_url"]}))
    out.blank()
    out.text(r["note_text"] or "", "b")
    if r["followup_text"]:
        out.blank().line(("follow-up" + (" (sent)" if r["followed_up_at"] else " (to send)"), "y"))
        out.text(r["followup_text"], "b")
    out.blank().line((f"{len(r['note_text'] or '')} chars", "d"),
                     (f"   {r['error']}" if r["error"] else "", "o"))


def _outreach_action(conn, args, out, what):
    from job_agent.outreach import actions
    oid = _id(args, out)
    if oid is None:
        return
    msg = actions.mark_sent(conn, oid) if what == "sent" else actions.mark_replied(conn, oid, referred=what == "referred")
    out.line((msg, "g"))


def cmd_help(conn, args, out):
    for k, v in COMMANDS.items():
        out.line((f"  {k:<36}", "y"), (v, "d"))


HANDLERS = {
    "help": cmd_help, "ls": cmd_ls, "show": cmd_show, "jd": cmd_jd, "open": cmd_open, "grep": cmd_grep,
    "stats": cmd_stats, "runs": cmd_runs, "people": cmd_people, "drafts": cmd_drafts, "draft": cmd_draft, "companies": cmd_companies,
    "applied": lambda c, a, o: _set_status(c, a, o, "applied"),
    "unapply": lambda c, a, o: _set_status(c, a, o, None),
    "good": lambda c, a, o: _feedback(c, a, o, "good"),
    "bad": lambda c, a, o: _feedback(c, a, o, "bad"),
    "skip": lambda c, a, o: _feedback(c, a, o, "skip"),
    "sent": lambda c, a, o: _outreach_action(c, a, o, "sent"),
    "replied": lambda c, a, o: _outreach_action(c, a, o, "replied"),
    "referred": lambda c, a, o: _outreach_action(c, a, o, "referred"),
}
STAGES = {"discover", "score", "digest", "track"}


def execute(conn: sqlite3.Connection, line: str) -> Out:
    out = Out()
    try:
        parts = shlex.split(line)
    except ValueError as e:
        return out.err(str(e))
    if not parts:
        return out
    cmd, args = parts[0].lower(), parts[1:]
    if cmd in ("clear", "cls"):
        out.client["clear"] = True
    elif cmd == "run":
        if not args or args[0] not in STAGES:
            out.err(f"usage: run <{'|'.join(sorted(STAGES))}>")
        else:
            out.client["run"] = args[0]          # server starts it; browser polls output
    elif cmd in HANDLERS:
        try:
            HANDLERS[cmd](conn, args, out)
        except sqlite3.Error as e:
            out.err(f"db: {e}")
    else:
        out.err(f"command not found: {cmd}").line(("type ", "d"), ("help", "y", {"cmd": "help"}))
    return out
