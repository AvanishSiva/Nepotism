"""Lane B connection-request sender (spec §8, §9).

Mode comes from config `linkedin.send_mode`: "dry_run" (default) or "live".
Live mode is refused (falls back to dry-run) until there are >= `linkedin.min_dry_run_days`
days of completed dry runs, so you always review a week of "would send" logs first.

Per queued note:
  1. open the profile (counted page view, random wait, kill switch), scroll a little
  2. verify the page name matches the target (fuzzy >= 90), else abort
  3. detect the connection state: Connect visible / under "More" / Pending / connected
  4. (live) click Connect → "Add a note" → type the note with per-character random delays →
     verify the textarea equals the draft → Send
  5. (live) confirm the Pending state → log to `outreach`. Any unexpected state → abort, log,
     notify. A person is never retried (one outreach row per person, ever).
It also reads Education to confirm "maybe_alum" → "alum" and redrafts the note with the
verified link. Live mode also withdraws Lane B requests pending > 21 days (capped).
Dry-run never clicks or types: everything it WOULD send goes to logs/send_dryrun.jsonl.
"""

from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime

from rapidfuzz import fuzz

from job_agent.config import Config
from job_agent.db import PROJECT_ROOT
from job_agent.normalize import norm_text
from job_agent.referrals.linkedin_people import Hooks, people_for

log = logging.getLogger(__name__)
DRYRUN_LOG = PROJECT_ROOT / "logs" / "send_dryrun.jsonl"
SEND_LOG = PROJECT_ROOT / "logs" / "send_live.jsonl"
NAME_MIN = 90
WITHDRAW_AFTER_DAYS = 21

# Dialog text that means "don't send to this person" (not an account restriction).
_ABORT_DIALOG = re.compile(r"enter (their|his|her) email|know .{0,40} personally|how do you know|"
                           r"personalized invitations|out of personalized|upgrade to premium", re.I)


class SendAbort(Exception):
    """Abort this one person (unexpected page state); the run continues."""


def name_matches(target: str, page_name: str) -> bool:
    a, b = norm_text(target), norm_text(page_name)
    return bool(a and b) and (fuzz.token_set_ratio(a, b) >= NAME_MIN or fuzz.ratio(a, b) >= NAME_MIN)


def connection_state(labels: list[str], name: str, degree: str | None = None) -> str:
    """connect | connect_in_more | pending | connected | follow_only | unknown

    "Message" alone does NOT mean connected: with Premium it is shown for non-connections
    (InMail). The degree badge in the top card is the reliable signal.
    """
    if degree == "1st":
        return "connected"
    first = norm_text(name).split()[0] if name else ""
    low = [l.lower() for l in labels]
    if any(l.startswith("pending") or "withdraw invitation" in l for l in low):
        return "pending"
    if any(l == "connect" or (l.startswith("invite") and "connect" in l) for l in low):
        return "connect"
    if any(l.startswith("remove connection") for l in low):
        return "connected"
    has_message = any(l == "message" or (l.startswith("message") and first in l) for l in low)
    has_more = any(l.startswith("more") for l in low)
    if has_more:
        return "connect_in_more"           # Connect is usually inside the More menu then
    if has_message and degree is None:
        return "unknown"                   # can't tell InMail from a 1st-degree Message
    if any(l.startswith("follow") for l in low):
        return "follow_only"
    return "unknown"


def education_confirms(education: str, schools: list[str]) -> str | None:
    edu = norm_text(education)
    for s in schools:
        if s and norm_text(s) in edu:
            return s
    return None


def dry_run_days(conn: sqlite3.Connection) -> int:
    """Distinct days with a completed dry-run send that actually checked someone."""
    return conn.execute(
        """SELECT count(DISTINCT date(started_at, 'localtime')) FROM runs
           WHERE stage = 'send' AND dry_run = 1 AND ok = 1
             AND coalesce(json_extract(stats_json, '$.checked'), 0) > 0""").fetchone()[0]


def resolve_mode(conn: sqlite3.Connection, cfg: Config, force_dry: bool = False) -> tuple[str, str | None]:
    """(mode, reason-if-live-was-refused)."""
    li = cfg.linkedin or {}
    wanted = "dry_run" if force_dry else str(li.get("send_mode", "dry_run"))
    if wanted != "live":
        return "dry_run", None
    need = int(li.get("min_dry_run_days", 5))
    have = dry_run_days(conn)
    if have < need:
        return "dry_run", f"live sending needs {need} days of dry runs first (have {have})"
    return "live", None


def _log(entry: dict, live: bool) -> None:
    path = SEND_LOG if live else DRYRUN_LOG
    path.parent.mkdir(exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# -- live browser actions -----------------------------------------------------------

def _human_pause(a: float = 0.6, b: float = 1.6) -> None:
    time.sleep(random.uniform(a, b))


def _invite_re(name: str) -> re.Pattern:
    first = re.escape(name.split()[0]) if name.split() else ""
    return re.compile(rf"^invite\s+.*{first}.*\s+to connect$", re.I)


def _visible(locator):
    for i in range(min(locator.count(), 6)):
        el = locator.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            continue
    return None


def _find_connect(page, name: str):
    """Connect control for THIS person only (aria-label names them), in the top card first."""
    rx = _invite_re(name)
    top = page.locator("main section").first
    for scope in (top, page.locator("main")):
        for role in ("button", "link", "menuitem"):
            el = _visible(scope.get_by_role(role, name=rx))
            if el:
                return el
    return None


def _open_connect(page, name: str, state: str):
    el = _find_connect(page, name)
    if el is None and state in ("connect_in_more", "connect"):
        more = _visible(page.locator("main section").first.get_by_role("button", name=re.compile(r"^more", re.I)))
        if more is None:
            raise SendAbort("no Connect and no More button")
        more.click()
        _human_pause()
        el = _find_connect(page, name)
        if el is None:
            page.keyboard.press("Escape")
            raise SendAbort("Connect not found under More")
    if el is None:
        raise SendAbort("Connect button not found")
    el.click()
    _human_pause(1.2, 2.5)


def _type_human(page, text: str) -> None:
    for i, ch in enumerate(text):
        page.keyboard.type(ch)
        time.sleep(random.uniform(0.04, 0.17))
        if ch in ".,;" or (i and i % random.randint(35, 70) == 0):
            time.sleep(random.uniform(0.3, 0.9))       # occasional thinking pause


def _close_dialog(page) -> None:
    try:
        page.keyboard.press("Escape")
        _human_pause(0.5, 1.0)
        discard = _visible(page.get_by_role("button", name=re.compile(r"^discard$", re.I)))
        if discard:
            discard.click()
    except Exception:
        pass


def send_invite(browser, safety, name: str, note: str, state: str) -> str:
    """Steps 3-5 on the already-open, name-verified profile. Returns the confirmation evidence.
    Raises SendAbort (this person) or SafetyStop (everything)."""
    page = browser.page
    _open_connect(page, name, state)
    safety.check_page(page.url, browser.body_text())
    dialog = page.get_by_role("dialog").last
    try:
        dialog.wait_for(state="visible", timeout=10000)
    except Exception:
        raise SendAbort("invitation dialog did not open")
    dtext = dialog.inner_text(timeout=5000)
    if _ABORT_DIALOG.search(dtext):
        _close_dialog(page)
        raise SendAbort(f"dialog asks for something unexpected: {dtext[:120]!r}")
    add_note = _visible(dialog.get_by_role("button", name=re.compile(r"add a (free )?note", re.I)))
    if add_note:
        add_note.click()
        _human_pause()
    box = dialog.locator("textarea").first
    try:
        box.wait_for(state="visible", timeout=8000)
    except Exception:
        _close_dialog(page)
        raise SendAbort("note box did not appear")
    box.click()
    _human_pause(0.4, 1.0)
    _type_human(page, note)
    _human_pause(1.0, 2.5)                                  # "re-read" before sending
    typed = box.input_value()
    if typed != note:
        _close_dialog(page)
        raise SendAbort(f"textarea mismatch ({len(typed)} vs {len(note)} chars); not sent")
    send = _visible(dialog.get_by_role("button", name=re.compile(r"^send( invitation)?$", re.I)))
    if send is None or not send.is_enabled():
        _close_dialog(page)
        raise SendAbort("Send button missing/disabled")
    send.click()
    _human_pause(2.0, 4.0)
    body = browser.body_text()
    safety.check_page(page.url, body)                       # e.g. weekly invitation limit dialog
    if re.search(r"invitation (was )?sent|invitation to .{1,80} sent", body, re.I):
        return "toast: invitation sent"
    from job_agent.referrals.browser import _PROFILE_JS
    info = page.evaluate(_PROFILE_JS)
    if connection_state(info.get("labels", []), name, info.get("degree")) == "pending":
        return "top card shows Pending"
    raise SendAbort("clicked Send but could not confirm Pending; check this profile manually")


def withdraw_invite(browser, safety, name: str) -> None:
    page = browser.page
    first = re.escape(name.split()[0])
    btn = _visible(page.locator("main section").first.get_by_role(
        "button", name=re.compile(rf"^pending.*{first}|withdraw invitation.*{first}", re.I)))
    if btn is None:
        raise SendAbort("Pending button not found")
    btn.click()
    _human_pause(1.0, 2.0)
    dialog = page.get_by_role("dialog").last
    try:
        dialog.wait_for(state="visible", timeout=8000)
    except Exception:
        raise SendAbort("withdraw dialog did not open")
    ok = _visible(dialog.get_by_role("button", name=re.compile(r"^withdraw$", re.I)))
    if ok is None:
        _close_dialog(page)
        raise SendAbort("Withdraw confirm button not found")
    ok.click()
    _human_pause(2.0, 3.5)
    safety.check_page(page.url, browser.body_text())


# -- stage ----------------------------------------------------------------------------

def _redraft_for_alum(conn, cfg, o, stats) -> str:
    from job_agent.outreach import drafts
    person = next(p for p in people_for(conn, o["company_id"]) if p.profile_url == o["profile_url"])
    company = conn.execute("SELECT name FROM companies WHERE id = ?", (o["company_id"],)).fetchone()["name"]
    ids = json.loads(o["job_ids_json"] or "[]")
    titles = [r["title"] for r in conn.execute(
        f"SELECT title FROM jobs WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
    ctx = drafts.Context(person, company, titles or ["software engineering"], ids, None, "B",
                         drafts.shared_link(person, cfg, company, titles[0] if titles else ""),
                         drafts.proof_point(cfg, None))
    note, low = drafts.write_note(ctx, cfg, stats)
    conn.execute("UPDATE outreach SET note_text = ?, low_quality = ? WHERE id = ?", (note, int(low), o["id"]))
    return note


def run(conn: sqlite3.Connection, cfg: Config, *, limit: int | None = None, ignore_window: bool = False,
        notify=None, force_dry: bool = False) -> dict:
    from job_agent.referrals.browser import LinkedInBrowser, chrome_available, inspect_profile
    from job_agent.safety import Safety, SafetyStop

    mode, refused = resolve_mode(conn, cfg, force_dry)
    live = mode == "live"
    stats = {"mode": mode, "checked": 0, "would_send": 0, "sent": 0, "aborted": 0, "redrafted": 0,
             "withdrawn": 0, "states": {}, "stopped": None}
    if refused:
        stats["live_refused"] = refused
    queue = conn.execute(
        """SELECT o.*, r.name, r.profile_url, r.company_id, r.tags_json, r.headline, r.degree
           FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
           WHERE o.lane = 'B' AND o.status = 'queued' ORDER BY o.id""").fetchall()
    stale = conn.execute(
        f"""SELECT o.*, r.name, r.profile_url FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
            WHERE o.lane = 'B' AND o.status = 'sent' AND o.sent_at <= datetime('now', '-{WITHDRAW_AFTER_DAYS} days')
            ORDER BY o.sent_at""").fetchall() if live else []
    if limit is not None:
        queue = queue[:limit]
    if not queue and not stale:
        return stats
    safety = Safety(conn, cfg, ignore_window=ignore_window, notify=notify)
    try:
        safety.check_can_browse()
        if live and queue:
            safety.check_can_send()
    except SafetyStop as e:
        stats["stopped"] = str(e)
        return stats
    if not chrome_available():
        stats["stopped"] = "Chrome not reachable on 127.0.0.1:9222 (run scripts/start_chrome.sh)"
        return stats
    if live:
        stats["requests_cap"], stats["cap_rules"] = safety.requests_cap()

    hooks = Hooks(cfg)
    try:
        with LinkedInBrowser(safety) as browser:
            for o in queue:
                if live:
                    safety.check_can_send()            # cap may be reached mid-run → SafetyStop
                info = inspect_profile(browser, o["profile_url"])
                stats["checked"] += 1
                now = datetime.now().isoformat(timespec="seconds")
                entry = {"at": now, "mode": mode, "outreach_id": o["id"], "target": o["name"],
                         "url": o["profile_url"], "page_name": info.get("name"),
                         "labels": info.get("labels", [])[:12], "education_found": bool(info.get("education"))}
                if not name_matches(o["name"], info.get("name", "")):
                    entry.update(result="abort: name mismatch")
                    conn.execute("UPDATE outreach SET error = ? WHERE id = ?",
                                 (f"{mode}: name mismatch (page shows {info.get('name')!r})", o["id"]))
                    stats["aborted"] += 1
                    _log(entry, live)
                    conn.commit()
                    continue

                state = connection_state(info.get("labels", []), o["name"], info.get("degree"))
                entry.update(degree=info.get("degree"), state=state)
                stats["states"][state] = stats["states"].get(state, 0) + 1

                tags = set(json.loads(o["tags_json"] or "[]"))
                school = education_confirms(info.get("education", ""), hooks.schools)
                note = o["note_text"]
                if school and "alum" not in tags:
                    tags = (tags - {"maybe_alum"}) | {"alum"}
                    conn.execute("UPDATE referral_points SET tags_json = ? WHERE id = ?",
                                 (json.dumps(sorted(tags)), o["referral_point_id"]))
                    note = _redraft_for_alum(conn, cfg, o, stats)
                    stats["redrafted"] += 1
                    entry["alum_confirmed"] = school
                elif "maybe_alum" in tags and info.get("education"):
                    tags.discard("maybe_alum")             # Education read, school not there
                    conn.execute("UPDATE referral_points SET tags_json = ? WHERE id = ?",
                                 (json.dumps(sorted(tags)), o["referral_point_id"]))

                if state in ("connect", "connect_in_more"):
                    entry["note"] = note
                    if not live:
                        entry["result"] = "WOULD SEND"
                        stats["would_send"] += 1
                        conn.execute("UPDATE outreach SET error = ? WHERE id = ?",
                                     (f"dry-run {now}: would send ({state})", o["id"]))
                    else:
                        safety.before_send()
                        try:
                            evidence = send_invite(browser, safety, o["name"], note, state)
                        except SendAbort as e:
                            entry["result"] = f"abort: {e}"
                            stats["aborted"] += 1
                            conn.execute("UPDATE outreach SET status = 'aborted', error = ? WHERE id = ?",
                                         (f"live {now}: {e}", o["id"]))
                            if notify:
                                notify(f"⚠ Connection request to {o['name']} aborted: {e}\n{o['profile_url']}")
                        else:
                            safety.count_request()
                            stats["sent"] += 1
                            entry["result"] = f"SENT ({evidence})"
                            conn.execute("UPDATE outreach SET status = 'sent', sent_at = datetime('now'), "
                                         "error = NULL WHERE id = ?", (o["id"],))
                elif state == "pending":
                    entry["result"] = "skip: invitation already pending"
                    conn.execute("UPDATE outreach SET status = 'sent', sent_at = coalesce(sent_at, datetime('now')), "
                                 "error = 'already pending' WHERE id = ?", (o["id"],))
                elif state == "connected":
                    entry["result"] = "skip: already connected → Lane A"
                    conn.execute("UPDATE outreach SET status = 'aborted', error = 'already connected: draft Lane A' "
                                 "WHERE id = ?", (o["id"],))
                    conn.execute("UPDATE referral_points SET degree = '1st', tags_json = ? WHERE id = ?",
                                 (json.dumps(sorted(tags | {"connection"})), o["referral_point_id"]))
                else:
                    entry["result"] = f"abort: unexpected state ({state})"
                    stats["aborted"] += 1
                    conn.execute("UPDATE outreach SET error = ? WHERE id = ?",
                                 (f"{mode}: unexpected state {state}", o["id"]))
                _log(entry, live)
                conn.commit()

            for o in stale:                                   # live only
                if safety.withdrawals_left() <= 0:
                    break
                info = inspect_profile(browser, o["profile_url"], scroll=False)
                entry = {"at": datetime.now().isoformat(timespec="seconds"), "mode": "live-withdraw",
                         "outreach_id": o["id"], "target": o["name"], "url": o["profile_url"]}
                state = connection_state(info.get("labels", []), o["name"], info.get("degree"))
                if not name_matches(o["name"], info.get("name", "")) or state != "pending":
                    if state == "connected":                  # accepted without an email we saw
                        conn.execute("UPDATE outreach SET status = 'accepted', accepted_at = datetime('now') "
                                     "WHERE id = ?", (o["id"],))
                        entry["result"] = "already accepted"
                    else:
                        entry["result"] = f"skip withdraw: state {state}"
                    _log(entry, True)
                    conn.commit()
                    continue
                try:
                    withdraw_invite(browser, safety, o["name"])
                except SendAbort as e:
                    entry["result"] = f"withdraw aborted: {e}"
                else:
                    safety.count_withdrawal()
                    stats["withdrawn"] += 1
                    entry["result"] = "WITHDRAWN"
                    conn.execute("UPDATE outreach SET status = 'withdrawn' WHERE id = ?", (o["id"],))
                _log(entry, True)
                conn.commit()
    except SafetyStop as e:
        stats["stopped"] = str(e)
    stats["views_left_today"] = safety.views_left()
    if live:
        stats["requests_left_today"] = safety.requests_left()
    return stats
