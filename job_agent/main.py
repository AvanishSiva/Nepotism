"""CLI entrypoint: python -m job_agent <command>."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from job_agent.config import load_config
from job_agent.db import DEFAULT_DB_PATH, init_db
from job_agent.runs import setup_logging, track_run


def cmd_init_db(args, cfg) -> int:
    init_db(args.db).close()
    print(f"DB ready: {args.db}")
    return 0


def cmd_discover(args, cfg) -> int:
    from job_agent.sources.discover import discover_ats, discover_gmail
    from job_agent.sources.gmail_api import GmailAuthError

    conn = init_db(args.db)
    dry = " (dry run, nothing saved)" if args.dry_run else ""
    rc = 0
    if args.source in ("all", "ats"):
        with track_run(conn, "discover_ats", dry_run=args.dry_run) as st:
            st.update(discover_ats(conn, cfg, only=args.company, use_cache=not args.no_cache,
                                   write=not args.dry_run))
        print(f"\nATS: {st['companies']} companies, {st['seen']} postings scanned, "
              f"{st['kept']} matched, {st['new']} new, {st['updated']} refreshed{dry}")
        if st.get("closed"):
            print(f"  closed {len(st['closed'])} job(s): " + "; ".join(st["closed"][:8])
                  + (" …" if len(st["closed"]) > 8 else ""))
        if st.get("reopened"):
            print(f"  reopened {st['reopened']} job(s)")
        if st.get("outreach_dropped"):
            print(f"  dropped unsent outreach (role closed): {', '.join(st['outreach_dropped'])}")
        for err in st["errors"]:
            print(f"  ERROR {err}")
    if args.source in ("all", "gmail") and not args.company:
        try:
            with track_run(conn, "discover_gmail", dry_run=args.dry_run) as st:
                st.update(discover_gmail(conn, cfg, use_cache=not args.no_cache,
                                         write=not args.dry_run, reprocess=args.reprocess))
        except GmailAuthError as e:
            print(f"\nGmail: skipped: {e}")
            return 1
        print(f"\nGmail: {st['emails']} alert emails ({st['emails_skipped']} already done), "
              f"{st['jobs']} jobs, {st['new']} new, {st['updated']} updated{dry}")
        print(f"  parsed via: {st['parse_methods']}   JD match: {st['match']}")
        for bad in st["incomplete_emails"]:
            print(f"  INCOMPLETE {bad}")
    return rc


def cmd_gmail_auth(args, cfg) -> int:
    from job_agent.sources.gmail_api import authorize

    authorize()
    return 0


def cmd_gmail_peek(args, cfg) -> int:
    """Show recent LinkedIn emails (sender/subject) to verify the alert sender address."""
    from job_agent.sources import gmail_api
    from job_agent.sources.gmail_alerts import job_ids_in

    svc = gmail_api.service()
    for mid in gmail_api.search(svc, args.query, args.n):
        m = gmail_api.get(svc, mid)
        print(f"{m.date}  {m.sender[:45]:<45}  jobs={len(job_ids_in(m)):<3} {m.subject[:70]}")
        if args.save:
            out = cfg.path("data/samples")
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{mid}.txt").write_text(m.text)
            (out / f"{mid}.html").write_text(m.html)
    return 0


def cmd_score(args, cfg) -> int:
    from job_agent.scoring.stage import run

    conn = init_db(args.db)
    with track_run(conn, "score") as st:
        st.update(run(conn, cfg, job_ids=args.job, reextract=args.reextract, limit=args.limit,
                      use_claude=not args.no_claude))
    print(f"\nScore: {st['jobs']} jobs | hard-skipped {st['hard_skipped']} | LLM-extracted {st['extracted']} "
          f"(cached {st['cached']}, title-only {st['title_only']}) | decisions {st['decisions']}")
    print(f"  LLM calls: ollama={st['ollama_calls']} claude={st['claude_calls']}")
    for e in st["errors"]:
        print(f"  ERROR {e}")
    return cmd_ranked(args, cfg)


def cmd_ranked(args, cfg) -> int:
    import json as _json

    conn = init_db(args.db)
    rows = conn.execute(
        "SELECT j.id, c.name AS company, j.title, j.location, s.score, s.decision, s.explanation, "
        "s.low_confidence, s.reasons_json, coalesce(j.apply_url, j.linkedin_job_url) AS url "
        "FROM job_scores s JOIN jobs j ON j.id = s.job_id LEFT JOIN companies c ON c.id = j.company_id "
        "WHERE j.closed_at IS NULL "
        "ORDER BY CASE s.decision WHEN 'referral_first' THEN 0 WHEN 'apply_direct' THEN 1 ELSE 2 END, "
        "s.score DESC"
    ).fetchall()
    shown = 0
    for decision, label in (("referral_first", "REFERRAL FIRST (score >= 8)"),
                            ("apply_direct", "APPLY DIRECT (6 - 8)")):
        group = [r for r in rows if r["decision"] == decision]
        print(f"\n=== {label}: {len(group)} ===")
        for r in group:
            flag = "  [no JD - open manually]" if r["low_confidence"] else ""
            print(f"#{r['id']:<4} {r['score']:>4}  {r['company'] or '?'} - {r['title']}  ({r['location']}){flag}")
            for line in (r["explanation"] or "").splitlines():
                print(f"             {line}")
            notes = [x for x in _json.loads(r["reasons_json"] or "[]") if "no JD" not in x]
            if notes:
                print(f"             ! {'; '.join(notes)}")
            print(f"             {r['url']}")
            shown += 1
    skipped = [r for r in rows if r["decision"] == "skip"]
    print(f"\n=== SKIPPED: {len(skipped)} ===")
    if args.show_skipped:
        for r in skipped:
            print(f"#{r['id']:<4} {r['score']:>4}  {(r['company'] or '?')[:14]:<14} {r['title'][:50]:<50} "
                  f"{'; '.join(_json.loads(r['reasons_json'] or '[]'))[:90]}")
    else:
        from collections import Counter
        why = Counter()
        for r in skipped:
            first = (_json.loads(r["reasons_json"] or "[]") or ["?"])[0]
            why[first.split("(")[0].split("'")[0].strip()] += 1
        for k, n in why.most_common():
            print(f"  {n:>3}  {k}")
        print("  (use `ranked --show-skipped` for details)")
    return 0


def cmd_telegram_setup(args, cfg) -> int:
    from job_agent.notify import telegram as tg

    found = tg.find_chat_id()
    if not found:
        print("No messages to the bot yet. Open your bot in Telegram, press Start (or send 'hi'), then re-run.")
        return 1
    cid, name = found
    tg.save_chat_id(cid)
    tg.send(f"✅ Job Agent connected. Hi {tg.esc(name)}! Daily digests will arrive here.")
    print(f"Saved TELEGRAM_CHAT_ID for {name} to .env and sent a test message.")
    return 0


def cmd_digest(args, cfg) -> int:
    from job_agent.notify import digest as dg
    from job_agent.notify import report

    conn = init_db(args.db)
    row = conn.execute("SELECT value FROM kv WHERE key = 'last_digest_at'").fetchone()
    since = None if args.all_new else (row["value"] if row else None)
    with track_run(conn, "digest") as st:
        d = dg.build(conn, since=since)
        d.linkedin_status = _linkedin_status(conn, cfg, getattr(args, "status_notes", None) or [])
        path = report.write(d)
        rel = str(path.relative_to(path.parent.parent))
        text = dg.telegram_text(d, rel)
        st.update({"referral": d.counts["referral_first"], "apply": d.counts["apply_direct"],
                   "new": d.new_count, "report": rel})
        if args.no_send:
            print(text)
        else:
            from job_agent.notify import telegram as tg
            n = tg.send(text)
            if not args.no_attach:
                tg.send_document(path, caption="Full report (open in browser)")
            st["telegram_messages"] = n
            conn.execute("INSERT INTO kv(key, value, updated_at) VALUES('last_digest_at', datetime('now'), "
                         "datetime('now')) ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                         "updated_at = excluded.updated_at")
            conn.commit()
            print(f"Sent digest to Telegram ({n} message(s) + report file).")
    print(f"Report: {path}")
    return 0


def _linkedin_status(conn, cfg, notes: list[str]) -> str:
    from job_agent.outreach.sender import dry_run_days
    from job_agent.safety import Safety

    s = Safety(conn, cfg)
    c = s.counters()
    mode = (cfg.linkedin or {}).get("send_mode", "dry_run")
    parts = []
    if s.paused_until():
        parts.append(f"LinkedIn PAUSED until {s.paused_until()[:16]}")
    parts.append(f"LinkedIn today: {c['page_views']} page views, {c['requests_sent']} requests sent")
    parts.append(f"sender: {'LIVE' if mode == 'live' else f'dry run (day {dry_run_days(conn)})'}")
    return " | ".join(parts + notes)


def cmd_portal(args, cfg) -> int:
    from job_agent.portal.server import serve

    init_db(args.db).close()
    serve(port=args.port, open_browser=not args.no_browser, db_path=args.db)
    return 0


def _notifier():
    from job_agent.notify import telegram as tg
    try:
        tg.chat_id()
        return lambda text: tg.send(tg.esc(text))
    except tg.TelegramError:
        return None


def cmd_referrals(args, cfg) -> int:
    from job_agent.referrals import linkedin_people as lp

    conn = init_db(args.db)
    if args.plan:
        st = lp.run(conn, cfg, plan_only=True, only=args.company)
        print(f"Plan: {st['companies']} referral-first companies (max "
              f"{(cfg.referrals or {}).get('max_views_per_company', 3)} views each), nothing visited:")
        for name, steps in st["plan"].items():
            print(f"\n  {name}")
            for s_ in steps:
                print(f"    {s_}")
        return 0
    with track_run(conn, "referrals") as st:
        st.update(lp.run(conn, cfg, only=args.company, ignore_window=args.ignore_window, notify=_notifier()))
    print(f"\nReferrals: {st['views']} LinkedIn page views, {st['people_new']} people saved"
          + (f"  |  stopped: {st['stopped']}" if st["stopped"] else ""))
    for name, cs in st["per_company"].items():
        print(f"  {name:<14} views={cs['views']} found={cs['found']} good_total={cs['good_total']}"
              + (f"  ({cs['note']})" if cs.get("note") else ""))
    if "views_left_today" in st:
        print(f"  page views left today: {st['views_left_today']}")
    return 0


def cmd_draft(args, cfg) -> int:
    from job_agent.outreach import drafts

    conn = init_db(args.db)
    with track_run(conn, "draft") as st:
        st.update(drafts.run(conn, cfg, only=args.company))
    print(f"Drafted {st['drafted']} (Lane A {st['lane_A']}, Lane B {st['lane_B']}, low quality {st['low_quality']}); "
          f"claude calls {st['claude_calls']}")
    return cmd_drafts(args, cfg)


def cmd_drafts(args, cfg) -> int:
    conn = init_db(args.db)
    rows = conn.execute("""SELECT o.id, o.lane, o.status, o.note_text, o.low_quality, o.error, r.name, r.tags_json,
                                  r.profile_url, c.name AS company
                           FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
                           LEFT JOIN companies c ON c.id = r.company_id ORDER BY o.id""").fetchall()
    for r in rows:
        print(f"\n#{r['id']} [{r['lane']}] {r['status']:<8} {r['company']}: {r['name']} "
              f"({','.join(json.loads(r['tags_json'] or '[]'))}){'  LOW QUALITY' if r['low_quality'] else ''}")
        print(f"   {r['profile_url']}")
        print(f"   ({len(r['note_text'] or '')} chars) {r['note_text']}")
        if r["error"]:
            print(f"   note: {r['error']}")
    if not rows:
        print("No drafts yet. Run `python -m job_agent draft`.")
    return 0


def cmd_send(args, cfg) -> int:
    from job_agent.outreach import sender

    conn = init_db(args.db)
    mode, _ = sender.resolve_mode(conn, cfg, force_dry=args.dry_run)
    with track_run(conn, "send", dry_run=mode != "live") as st:
        st.update(sender.run(conn, cfg, limit=args.limit, ignore_window=args.ignore_window, notify=_notifier(),
                             force_dry=args.dry_run))
    if st["mode"] == "live":
        print(f"\nSend (LIVE): checked {st['checked']}, SENT {st['sent']}, aborted {st['aborted']}, "
              f"withdrawn {st['withdrawn']}, redrafted {st['redrafted']}, states {st['states']}")
        print(f"  cap today {st.get('requests_cap')} ({'; '.join(st.get('cap_rules') or []) or 'configured cap'}), "
              f"left {st.get('requests_left_today')}.  Details: logs/send_live.jsonl")
    else:
        print(f"\nSend (DRY RUN, nothing sent): checked {st['checked']}, would send {st['would_send']}, "
              f"aborted {st['aborted']}, redrafted {st['redrafted']}, states {st['states']}")
        print("  Details: logs/send_dryrun.jsonl  |  dry-run days so far:", sender.dry_run_days(conn))
    if st.get("live_refused"):
        print(f"  live mode refused: {st['live_refused']}")
    if st["stopped"]:
        print(f"  stopped: {st['stopped']}")
    return 0


def cmd_track(args, cfg) -> int:
    from job_agent.outreach import track

    conn = init_db(args.db)
    with track_run(conn, "track") as st:
        st.update(track.run(conn, cfg, notify=_notifier()))
    print(f"Track: {st.get('emails', 0)} new acceptance emails, {st.get('matched', 0)} matched your requests, "
          f"{st.get('drafted', 0)} Lane A drafts, {st['followups']} follow-ups"
          + (f"\n  Gmail: {st['gmail_error']}" if st.get("gmail_error") else ""))
    return 0


def cmd_bot(args, cfg) -> int:
    from job_agent.notify import bot

    conn = init_db(args.db)
    if args.loop:
        print("Telegram bot polling (Ctrl+C to stop)")
        bot.loop(conn, cfg)
    n = bot.poll_once(conn, cfg)
    print(f"handled {n} message(s)")
    return 0


def cmd_connections(args, cfg) -> int:
    from pathlib import Path

    from job_agent.referrals import connections_csv as cc

    conn = init_db(args.db)
    dest = cc.csv_path(cfg)
    if args.file:
        cc.install(Path(args.file).expanduser(), dest)
        print(f"Installed {dest}")
    st = cc.import_matches(conn, cfg)
    if st.get("missing"):
        print(f"No {dest}. Export it: LinkedIn → Settings → Data privacy → Get a copy of your data → "
              f"Connections, then `python -m job_agent connections --file ~/Downloads/<the zip or csv>`")
        return 1
    print(f"{st['rows']} connections read; {st['matched']} work at referral-first companies "
          f"({st['no_url']} skipped: no profile URL in export)")
    for name, n in st["per_company"].items():
        print(f"  {name}: {n}")
    print("Next: `python -m job_agent draft` writes Lane A referral asks for them.")
    return 0


def _stage(name: str, fn, notes: list[str]) -> bool:
    print(f"\n===== {name} =====", flush=True)
    try:
        rc = fn()
    except SystemExit as e:
        rc = e.code
    except Exception as e:
        logging.getLogger("job_agent").exception("%s failed", name)
        notes.append(f"{name} failed: {e}")
        return False
    if rc not in (0, None):
        notes.append(f"{name}: exit {rc}")
    return True


def _ensure_chrome(cfg, notes: list[str]) -> bool:
    import subprocess

    from job_agent.db import PROJECT_ROOT
    from job_agent.referrals.browser import chrome_available

    if chrome_available():
        return True
    if not (cfg.linkedin or {}).get("auto_start_chrome", False):
        notes.append("Chrome not running on :9222, LinkedIn steps skipped (scripts/start_chrome.sh)")
        return False
    subprocess.run([str(PROJECT_ROOT / "scripts" / "start_chrome.sh")], capture_output=True, timeout=60)
    if chrome_available():
        return True
    notes.append("Chrome could not be started, LinkedIn steps skipped")
    return False


def _sub(args, *argv):
    ns = build_parser().parse_args(["--db", str(args.db), *argv])
    return ns


def cmd_run_all(args, cfg) -> int:
    """discover → score → referrals → draft → digest. A failing stage never blocks the rest."""
    from job_agent.safety import Safety

    conn = init_db(args.db)
    notes: list[str] = []
    with track_run(conn, "run-all", dry_run=args.dry_run) as st:
        _stage("discover", lambda: cmd_discover(_sub(args, "discover"), cfg), notes)
        _stage("score", lambda: cmd_score(_sub(args, "score"), cfg), notes)
        s = Safety(conn, cfg)
        if s.paused_until():
            notes.append(f"LinkedIn paused until {s.paused_until()[:16]}")
        elif not s.in_window():
            notes.append("outside LinkedIn window: referral search skipped today")
        elif _ensure_chrome(cfg, notes):
            _stage("referrals", lambda: cmd_referrals(_sub(args, "referrals"), cfg), notes)
        _stage("draft", lambda: cmd_draft(_sub(args, "draft"), cfg), notes)
        if args.dry_run and s.in_window() and not s.paused_until() and _ensure_chrome(cfg, []):
            _stage("send (dry run)", lambda: cmd_send(_sub(args, "send", "--dry-run"), cfg), notes)
        digest_args = _sub(args, "digest", *(["--no-send"] if args.no_digest_send else []))
        digest_args.status_notes = notes
        _stage("digest", lambda: cmd_digest(digest_args, cfg), notes)
        st["notes"] = notes
    print("\nrun-all done" + ("".join(f"\n  - {n}" for n in notes) if notes else ""))
    return 0


def cmd_schedule(args, cfg) -> int:
    from job_agent import scheduler

    fn = {"install": scheduler.install, "uninstall": scheduler.uninstall, "status": scheduler.status}[args.action]
    for line in fn():
        print(line)
    if args.action == "install":
        print("\nLogs: logs/launchd-*.log   Check: python3 -m job_agent schedule status")
    return 0


def cmd_people(args, cfg) -> int:
    from job_agent.referrals import linkedin_people as lp

    conn = init_db(args.db)
    rows = conn.execute("SELECT DISTINCT c.id, c.name FROM referral_points r JOIN companies c ON c.id = r.company_id"
                        + (" WHERE c.name LIKE ?" if args.company else "") + " ORDER BY c.name",
                        ((f"%{args.company}%",) if args.company else ())).fetchall()
    for c in rows:
        print(f"\n{c['name']}")
        for p in lp.people_for(conn, c["id"]):
            print(f"  {p.rank:>3}  {p.name:<26} {(p.degree or '-'):<4} {','.join(sorted(p.tags)):<28} "
                  f"{p.headline[:60]}  {p.profile_url}")
    if not rows:
        print("No referral points yet. Run `python -m job_agent referrals`.")
    return 0


def cmd_safety(args, cfg) -> int:
    from job_agent.referrals.browser import chrome_available
    from job_agent.safety import Safety

    conn = init_db(args.db)
    s = Safety(conn, cfg)
    if args.action == "pause":
        print("paused until", s.pause("manual pause from CLI"))
    elif args.action == "resume":
        s.resume()
        print("resumed")
    c = s.counters()
    print(f"paused_until : {s.paused_until() or '-'}")
    print(f"window       : {s.window['start']}-{s.window['end']} "
          f"{'weekdays' if s.window.get('weekdays_only', True) else 'every day'}  (now {'IN' if s.in_window() else 'OUT of'} window)")
    print(f"today        : page_views {c['page_views']}/{s.caps['page_views']}, requests {c['requests_sent']}/"
          f"{s.caps['requests']}, withdrawals {c['withdrawals']}/{s.caps['withdrawals']}")
    print(f"chrome :9222 : {'reachable' if chrome_available() else 'NOT reachable (scripts/start_chrome.sh)'}")
    return 0


def cmd_companies(args, cfg) -> int:
    from job_agent.sources.companies import load_watchlist, sync_watchlist

    conn = init_db(args.db)
    if args.action == "sync":
        print(sync_watchlist(conn, load_watchlist(cfg.path(cfg.discover.watchlist_file))))
    rows = conn.execute(
        "SELECT c.name, c.ats, c.slug, c.watch, COUNT(j.id) AS jobs FROM companies c "
        "LEFT JOIN jobs j ON j.company_id = c.id GROUP BY c.id ORDER BY c.watch DESC, c.name"
    ).fetchall()
    for r in rows:
        print(f"{'*' if r['watch'] else ' '} {r['name']:<14} {r['ats'] or '-':<10} "
              f"{r['slug'] or '-':<40} jobs={r['jobs']}")
    return 0


def cmd_jobs(args, cfg) -> int:
    conn = init_db(args.db)
    where = "WHERE j.first_seen_at >= datetime('now', '-1 day')" if args.new else ""
    rows = conn.execute(
        f"SELECT j.id, c.name AS company, j.title, j.location, j.remote_type, j.jd_status, "
        f"substr(coalesce(j.posted_at, j.alert_date), 1, 10) AS posted FROM jobs j LEFT JOIN companies c ON c.id = j.company_id "
        f"{where} ORDER BY coalesce(j.posted_at, j.alert_date) DESC LIMIT ?", (args.limit,)
    ).fetchall()
    for r in rows:
        print(f"{r['id']:>5}  {r['posted'] or '?':<10}  {(r['company'] or '?')[:12]:<12}  "
              f"{r['title'][:55]:<55}  {(r['location'] or '')[:30]:<30} {r['remote_type']}")
    print(f"({len(rows)} shown)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="job_agent")
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--config", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create the database (safe to re-run)").set_defaults(fn=cmd_init_db)

    d = sub.add_parser("discover", help="collect jobs from ATS watchlist")
    d.add_argument("--company", help="only this watched company")
    d.add_argument("--no-cache", action="store_true", help="ignore cached HTTP responses")
    d.add_argument("--dry-run", action="store_true", help="fetch and print, don't save")
    d.add_argument("--source", choices=["all", "ats", "gmail"], default="all")
    d.add_argument("--reprocess", action="store_true", help="re-read alert emails already processed")
    d.set_defaults(fn=cmd_discover)

    sub.add_parser("gmail-auth", help="one-time Gmail login (opens browser)").set_defaults(fn=cmd_gmail_auth)
    g = sub.add_parser("gmail-peek", help="list recent LinkedIn emails to verify sender")
    g.add_argument("--query", default="from:linkedin.com newer_than:14d")
    g.add_argument("-n", type=int, default=25)
    g.add_argument("--save", action="store_true", help="save bodies to data/samples/ for debugging")
    g.set_defaults(fn=cmd_gmail_peek)

    c = sub.add_parser("companies", help="show / sync the ATS watchlist")
    c.add_argument("action", nargs="?", choices=["list", "sync"], default="list")
    c.set_defaults(fn=cmd_companies)

    sc = sub.add_parser("score", help="filter + extract facts + score + decide")
    sc.add_argument("--job", type=int, nargs="*", help="only these job ids")
    sc.add_argument("--reextract", action="store_true", help="ignore cached LLM facts")
    sc.add_argument("--limit", type=int, help="max new LLM extractions this run")
    sc.add_argument("--no-claude", action="store_true", help="never fall back to claude -p")
    sc.add_argument("--show-skipped", action="store_true")
    sc.set_defaults(fn=cmd_score)

    rk = sub.add_parser("ranked", help="show scored jobs by decision")
    rk.add_argument("--show-skipped", action="store_true")
    rk.set_defaults(fn=cmd_ranked)

    sub.add_parser("telegram-setup", help="find your chat id after you message the bot") \
        .set_defaults(fn=cmd_telegram_setup)
    dg = sub.add_parser("digest", help="write HTML report + send Telegram digest")
    dg.add_argument("--no-send", action="store_true", help="print instead of sending")
    dg.add_argument("--no-attach", action="store_true", help="don't attach the HTML report file")
    dg.add_argument("--all-new", action="store_true", help="mark every job as new")
    dg.set_defaults(fn=cmd_digest)

    pt = sub.add_parser("portal", help="terminal-style web portal on http://127.0.0.1:8765")
    pt.add_argument("--port", type=int, default=8765)
    pt.add_argument("--no-browser", action="store_true")
    pt.set_defaults(fn=cmd_portal)

    rf = sub.add_parser("referrals", help="find referral contacts on LinkedIn (read-only)")
    rf.add_argument("--plan", action="store_true", help="show which pages would be visited; visit nothing")
    rf.add_argument("--company", help="only this company")
    rf.add_argument("--ignore-window", action="store_true",
                    help="allow outside 08:00-10:30 weekdays (manual, supervised runs only)")
    rf.set_defaults(fn=cmd_referrals)

    dr = sub.add_parser("draft", help="draft Lane A asks / Lane B connection notes with Claude")
    dr.add_argument("--company")
    dr.set_defaults(fn=cmd_draft)
    sub.add_parser("drafts", help="show all drafts").set_defaults(fn=cmd_drafts)
    se = sub.add_parser("send", help="Lane B sender (config linkedin.send_mode: dry_run | live)")
    se.add_argument("--limit", type=int)
    se.add_argument("--dry-run", action="store_true", help="never click/send, whatever the config says")
    se.add_argument("--ignore-window", action="store_true", help="supervised manual runs only")
    se.set_defaults(fn=cmd_send)

    sub.add_parser("track", help="acceptance emails → Lane A drafts; follow-ups; Gmail check") \
        .set_defaults(fn=cmd_track)
    bt = sub.add_parser("bot", help="Telegram commands (/draft /sent /pause /status ...)")
    bt.add_argument("--loop", action="store_true", help="long-poll forever")
    bt.set_defaults(fn=cmd_bot)
    cn = sub.add_parser("connections", help="import LinkedIn Connections.csv export → 1st-degree contacts")
    cn.add_argument("--file", help="path to the export (.zip or Connections.csv); copied to data/")
    cn.set_defaults(fn=cmd_connections)
    ra = sub.add_parser("run-all", help="discover → score → referrals → draft → digest")
    ra.add_argument("--no-digest-send", action="store_true", help="print the digest instead of Telegram")
    ra.set_defaults(fn=cmd_run_all, dry_run=False)
    dr2 = sub.add_parser("dry-run", help="run-all + sender in dry-run: no LinkedIn writes at all")
    dr2.add_argument("--no-digest-send", action="store_true")
    dr2.set_defaults(fn=cmd_run_all, dry_run=True)
    sch = sub.add_parser("schedule", help="macOS launchd schedule: install | uninstall | status")
    sch.add_argument("action", choices=["install", "uninstall", "status"])
    sch.set_defaults(fn=cmd_schedule)

    pp = sub.add_parser("people", help="show saved referral contacts")
    pp.add_argument("company", nargs="?")
    pp.set_defaults(fn=cmd_people)

    sf = sub.add_parser("safety", help="LinkedIn safety status; `safety pause|resume`")
    sf.add_argument("action", nargs="?", choices=["status", "pause", "resume"], default="status")
    sf.set_defaults(fn=cmd_safety)

    j = sub.add_parser("jobs", help="list stored jobs")
    j.add_argument("--new", action="store_true", help="only first seen in the last 24h")
    j.add_argument("--limit", type=int, default=50)
    j.set_defaults(fn=cmd_jobs)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.config) if args.config else load_config()
    return args.fn(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
