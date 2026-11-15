"""LinkedIn safety (spec §9): activity window, daily caps, kill switch, pause/resume.

Every LinkedIn page view goes through Safety.before_view() / after_view().
"""

from __future__ import annotations

import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

PAUSE_DAYS = 7
MANUAL_ONLY_DAYS = 7          # after a restriction lifts: no automation for 7 days ...
REDUCED_DAYS = 7              # ... then 25% of caps for 7 more days
PENDING_STOP, PENDING_RESUME = 200, 100
MIN_SENT_FOR_RATE = 10        # acceptance-rate rule needs a sample first

# Page text phrases that mean LinkedIn is restricting/challenging the account.
# Specific phrases only: bare "verify" or "restricted" appear on normal pages
# (e.g. "Verified" badges).
_RESTRICT_PHRASES = [
    "unusual activity", "your account has been restricted", "account has been temporarily restricted",
    "temporarily restricted", "we've restricted your account", "verify your identity",
    "security verification", "quick security check", "complete this security check",
    "are you a human", "prove you're not a robot", "captcha",
    "weekly invitation limit", "reached the weekly limit", "too many requests",
    "you've reached the commercial use limit", "commercial use limit",
]
_RESTRICT_RE = re.compile("|".join(re.escape(p) for p in _RESTRICT_PHRASES), re.I)
_RESTRICT_URL = re.compile(r"/checkpoint/|/challenge|captcha", re.I)
_LOGIN_URL = re.compile(r"/login|/authwall|/uas/|/signup|/start/join", re.I)


class SafetyStop(Exception):
    """Stop all LinkedIn activity for this run."""


def detect(url: str, text: str) -> tuple[str, str] | None:
    """Kill-switch check on a loaded page. Returns (kind, evidence) or None.

    kind = 'restricted' (→ 7-day pause) or 'logged_out' (→ stop run, ask user to log in).
    """
    if _RESTRICT_URL.search(url or ""):
        return "restricted", f"url {url}"
    m = _RESTRICT_RE.search(text or "")
    if m:
        return "restricted", f"page says '{m.group(0)}'"
    if _LOGIN_URL.search(url or ""):
        return "logged_out", f"url {url}"
    head = (text or "")[:2000].lower()
    if "join now" in head and "sign in" in head and "messaging" not in head:
        return "logged_out", "sign-in page"
    return None


class Safety:
    def __init__(self, conn: sqlite3.Connection, cfg, *, ignore_window: bool = False, notify=None):
        self.conn = conn
        li = cfg.linkedin or {}
        self.caps = {"page_views": 15, "requests": 10, "withdrawals": 5, **(li.get("caps") or {})}
        self.window = {"start": "08:00", "end": "10:30", "weekdays_only": True, **(li.get("window") or {})}
        pacing = li.get("pacing") or {}
        self.view_gap = tuple(pacing.get("view_gap_s", (20, 60)))
        self.settle = tuple(pacing.get("settle_s", (3, 8)))
        self.send_gap = tuple(pacing.get("send_gap_s", (60, 180)))
        self.warmup = list(li.get("warmup") or [3, 6])
        self._last_send: float | None = None
        self.ignore_window = ignore_window
        self.notify = notify
        self._last_view: float | None = None

    # -- state ---------------------------------------------------------------
    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def counters(self) -> sqlite3.Row:
        self.conn.execute("INSERT OR IGNORE INTO safety_counters(day) VALUES(?)", (self._today(),))
        return self.conn.execute("SELECT * FROM safety_counters WHERE day = ?", (self._today(),)).fetchone()

    def paused_until(self) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = 'paused_until'").fetchone()
        if row and row["value"] and row["value"] > datetime.now().isoformat(timespec="seconds"):
            return row["value"]
        return None

    def _kv(self, key: str, value: str | None):
        if value is None:
            self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        else:
            self.conn.execute("INSERT INTO kv(key, value, updated_at) VALUES(?, ?, datetime('now')) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                              (key, value))
        self.conn.commit()

    def _kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def pause(self, reason: str, days: int = PAUSE_DAYS, *, restriction: bool = False) -> str:
        until = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
        self._kv("paused_until", until)
        self._kv("paused_reason", reason)
        if restriction:
            self._kv("last_restriction_at", datetime.now().isoformat(timespec="seconds"))
        log.error("LinkedIn PAUSED until %s: %s", until, reason)
        if self.notify:
            try:
                self.notify(f"🛑 LinkedIn activity PAUSED until {until[:16]}.\nReason: {reason}\n"
                            f"Check your account, then resume with /resume (Telegram) or `python -m job_agent safety resume`.")
            except Exception as e:
                log.warning("pause notification failed: %s", e)
        return until

    def resume(self) -> None:
        self._kv("paused_until", None)
        self._kv("paused_reason", None)
        self._kv("resumed_at", datetime.now().isoformat(timespec="seconds"))

    # -- gates ---------------------------------------------------------------
    def in_window(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if self.window.get("weekdays_only", True) and now.weekday() >= 5:
            return False
        return self.window["start"] <= now.strftime("%H:%M") < self.window["end"]

    def restriction_phase(self, now: datetime | None = None) -> str | None:
        """None | 'manual_only' (7 days after resuming from a restriction) | 'reduced' (25% caps, 7 more days)."""
        last, resumed = self._kv_get("last_restriction_at"), self._kv_get("resumed_at")
        if not last or not resumed or resumed < last:
            return None
        since = (now or datetime.now()) - datetime.fromisoformat(resumed)
        if since < timedelta(days=MANUAL_ONLY_DAYS):
            return "manual_only"
        if since < timedelta(days=MANUAL_ONLY_DAYS + REDUCED_DAYS):
            return "reduced"
        return None

    def _scaled(self, cap: int) -> int:
        return max(1, int(cap * 0.25)) if self.restriction_phase() == "reduced" else int(cap)

    def views_left(self) -> int:
        return max(0, self._scaled(self.caps["page_views"]) - int(self.counters()["page_views"]))

    # -- connection requests -------------------------------------------------
    def acceptance_rate(self) -> tuple[float | None, int]:
        """Rolling 30 days over Lane B requests sent >= 7 days ago (younger ones haven't had time)."""
        r = self.conn.execute(
            """SELECT count(*) AS n, sum(accepted_at IS NOT NULL) AS acc FROM outreach
               WHERE lane = 'B' AND sent_at IS NOT NULL AND sent_at >= datetime('now', '-30 days')
                 AND sent_at <= datetime('now', '-7 days')""").fetchone()
        n = r["n"] or 0
        return ((r["acc"] or 0) / n if n else None), n

    def pending_count(self) -> int:
        return self.conn.execute("SELECT count(*) FROM outreach WHERE lane = 'B' AND status = 'sent'").fetchone()[0]

    def requests_cap(self, today: datetime | None = None) -> tuple[int, list[str]]:
        """Today's connection-request cap after warm-up, post-restriction and acceptance-rate rules."""
        why = []
        cap = int(self.caps["requests"])
        start = self._kv_get("warmup_started_on")
        week = 0 if not start else ((today or datetime.now()).date() - datetime.fromisoformat(start).date()).days // 7
        if week < len(self.warmup):
            cap = min(cap, int(self.warmup[week]))
            why.append(f"warm-up week {week + 1}: max {cap}/day")
        if self.restriction_phase() == "reduced":
            cap = max(1, int(cap * 0.25))
            why.append("25% caps after a restriction")
        rate, n = self.acceptance_rate()
        if rate is not None and n >= MIN_SENT_FOR_RATE and rate < 0.25:
            cap = max(1, cap // 2)
            why.append(f"acceptance rate {rate:.0%} < 25% over {n} requests: cap halved")
        return cap, why

    def requests_left(self) -> int:
        return max(0, self.requests_cap()[0] - int(self.counters()["requests_sent"]))

    def check_can_send(self) -> None:
        """Gate before each connection request (on top of check_can_browse)."""
        self.check_can_browse()
        pending = self.pending_count()
        blocked = self._kv_get("pending_block") == "1"
        if pending > PENDING_STOP or (blocked and pending >= PENDING_RESUME):
            if not blocked:
                self._kv("pending_block", "1")
            raise SafetyStop(f"{pending} invitations pending: no new requests until below {PENDING_RESUME}")
        if blocked:
            self._kv("pending_block", None)
        if self.requests_left() <= 0:
            cap, why = self.requests_cap()
            raise SafetyStop(f"daily connection-request cap reached ({cap}{'; ' + '; '.join(why) if why else ''})")

    def before_send(self) -> None:
        """Random 60-180 s between sends (the profile view before it already waited its own gap)."""
        if self._last_send is not None:
            wait = random.uniform(*self.send_gap) - (time.monotonic() - self._last_send)
            if wait > 0:
                log.info("pacing: waiting %.0fs before next connection request", wait)
                time.sleep(wait)

    def count_request(self) -> None:
        self._last_send = time.monotonic()
        if not self._kv_get("warmup_started_on"):
            self._kv("warmup_started_on", datetime.now().date().isoformat())
        self.conn.execute("UPDATE safety_counters SET requests_sent = requests_sent + 1 WHERE day = ?", (self._today(),))
        self.conn.commit()

    def withdrawals_left(self) -> int:
        return max(0, self._scaled(self.caps["withdrawals"]) - int(self.counters()["withdrawals"]))

    def count_withdrawal(self) -> None:
        self.conn.execute("UPDATE safety_counters SET withdrawals = withdrawals + 1 WHERE day = ?", (self._today(),))
        self.conn.commit()

    def check_can_browse(self) -> None:
        """Raise SafetyStop if LinkedIn must not be touched now."""
        if (p := self.paused_until()):
            raise SafetyStop(f"paused until {p}")
        phase = self.restriction_phase()
        if phase == "manual_only":
            raise SafetyStop(f"manual-only week after a LinkedIn restriction (resumed {self._kv_get('resumed_at')[:10]}); "
                             f"no automated LinkedIn activity until {MANUAL_ONLY_DAYS} days after resume")
        if not self.ignore_window and not self.in_window():
            raise SafetyStop(f"outside LinkedIn window ({self.window['start']}–{self.window['end']}"
                             f"{', weekdays' if self.window.get('weekdays_only', True) else ''})")
        if self.views_left() <= 0:
            raise SafetyStop(f"daily page-view cap reached ({self.caps['page_views']})")

    def before_view(self) -> None:
        self.check_can_browse()
        if self._last_view is not None:
            gap = random.uniform(*self.view_gap)
            wait = gap - (time.monotonic() - self._last_view)
            if wait > 0:
                log.info("pacing: waiting %.0fs before next page view", wait)
                time.sleep(wait)

    def after_view(self, url: str, text: str) -> None:
        """Count the view and run the kill switch. Raises SafetyStop on trouble."""
        self._last_view = time.monotonic()
        self.conn.execute("UPDATE safety_counters SET page_views = page_views + 1 WHERE day = ?", (self._today(),))
        self.conn.commit()
        self.check_page(url, text)

    def check_page(self, url: str, text: str) -> None:
        """Kill switch on the current page state (also after clicks/dialogs). Doesn't count a view."""
        hit = detect(url, text)
        if hit is None:
            return
        kind, evidence = hit
        if kind == "restricted":
            self.pause(f"restriction/verification signal: {evidence}", restriction=True)
            raise SafetyStop(f"kill switch: {evidence}")
        raise SafetyStop(f"not logged in to LinkedIn ({evidence}). Log in in the job-agent Chrome window.")

    def settle_sleep(self) -> None:
        time.sleep(random.uniform(*self.settle))
