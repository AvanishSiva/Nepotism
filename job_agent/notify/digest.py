"""Daily digest data (spec §10): one query used by both Telegram and the HTML report."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DigestJob:
    id: int
    company: str
    title: str
    location: str | None
    score: float
    decision: str
    url: str | None
    linkedin_url: str | None
    explanation: list[str]
    reasons: list[str]
    low_confidence: bool
    is_new: bool
    posted: str | None
    facts: dict = field(default_factory=dict)


@dataclass
class Digest:
    date: datetime
    jobs: list[DigestJob]
    counts: dict
    new_count: int
    skipped_reasons: dict
    linkedin_status: str = ""
    closed: list[str] = field(default_factory=list)
    outreach: list[dict] = field(default_factory=list)

    @property
    def referral(self):
        return [j for j in self.jobs if j.decision == "referral_first"]

    @property
    def apply(self):
        return [j for j in self.jobs if j.decision == "apply_direct"]


def build(conn: sqlite3.Connection, *, since: str | None) -> Digest:
    """since: UTC timestamp of the previous digest; jobs first seen after it are 'new'."""
    rows = conn.execute(
        """SELECT j.id, coalesce(c.name, '?') AS company, j.title, j.location, j.apply_url,
                  j.linkedin_job_url, j.first_seen_at, coalesce(j.posted_at, j.alert_date) AS posted,
                  s.score, s.decision, s.explanation, s.reasons_json, s.low_confidence, s.facts_json
           FROM job_scores s JOIN jobs j ON j.id = s.job_id LEFT JOIN companies c ON c.id = j.company_id
           WHERE j.closed_at IS NULL
           ORDER BY CASE s.decision WHEN 'referral_first' THEN 0 WHEN 'apply_direct' THEN 1 ELSE 2 END,
                    s.low_confidence, s.score DESC"""
    ).fetchall()
    jobs, skipped = [], {}
    counts = {"referral_first": 0, "apply_direct": 0, "skip": 0}
    new_count = 0
    for r in rows:
        is_new = since is None or r["first_seen_at"] > since
        new_count += is_new
        counts[r["decision"]] = counts.get(r["decision"], 0) + 1
        reasons = json.loads(r["reasons_json"] or "[]")
        if r["decision"] == "skip":
            key = (reasons[0] if reasons else "?").split("(")[0].split("'")[0].strip()
            if key.startswith("score"):
                key = "score below threshold"
            elif key.startswith("posted"):
                key = "stale listing"
            skipped[key] = skipped.get(key, 0) + 1
            continue
        facts = json.loads(r["facts_json"] or "{}")
        jobs.append(DigestJob(
            id=r["id"], company=r["company"], title=r["title"], location=r["location"],
            score=r["score"], decision=r["decision"], url=r["apply_url"] or r["linkedin_job_url"],
            linkedin_url=r["linkedin_job_url"], explanation=(r["explanation"] or "").splitlines(),
            reasons=[x for x in reasons if "no JD" not in x], low_confidence=bool(r["low_confidence"]),
            is_new=is_new, posted=(r["posted"] or "")[:10] or None, facts=facts,
        ))
    return Digest(date=datetime.now(), jobs=jobs, counts=counts, new_count=new_count,
                  skipped_reasons=dict(sorted(skipped.items(), key=lambda kv: -kv[1])),
                  outreach=outreach_items(conn), closed=closed_since(conn, since))


def closed_since(conn: sqlite3.Connection, since: str | None) -> list[str]:
    """Jobs worth acting on (ref/apply, not applied) that closed since the last digest."""
    rows = conn.execute(
        """SELECT coalesce(c.name, '?') AS company, j.title FROM jobs j JOIN job_scores s ON s.job_id = j.id
           LEFT JOIN companies c ON c.id = j.company_id LEFT JOIN applications a ON a.job_id = j.id
           WHERE j.closed_at IS NOT NULL AND j.closed_at > ? AND a.job_id IS NULL
             AND s.decision IN ('referral_first', 'apply_direct')
           ORDER BY s.score DESC""", (since or "",)).fetchall()
    return [f"{r['company']}: {r['title']}" for r in rows]


def outreach_items(conn: sqlite3.Connection) -> list[dict]:
    """Open outreach for the digest/report: what's waiting on you and what the sender is doing."""
    rows = conn.execute(
        """SELECT o.id, o.lane, o.status, o.note_text, o.followup_text, o.followed_up_at, o.low_quality, o.error,
                  o.job_ids_json, r.name, r.degree, r.tags_json, r.profile_url, c.name AS company
           FROM outreach o JOIN referral_points r ON r.id = o.referral_point_id
           LEFT JOIN companies c ON c.id = r.company_id
           WHERE o.status IN ('drafted', 'queued', 'sent', 'accepted')
              OR (o.status = 'asked' AND o.followup_text IS NOT NULL AND o.followed_up_at IS NULL)
           ORDER BY o.lane, o.id""").fetchall()
    out = []
    for r in rows:
        followup = r["status"] == "asked"
        out.append({"id": r["id"], "lane": r["lane"], "status": r["status"], "name": r["name"],
                    "company": r["company"], "degree": r["degree"], "url": r["profile_url"],
                    "tags": [t for t in json.loads(r["tags_json"] or "[]") if not t.startswith("maybe_")],
                    "text": r["followup_text"] if followup else r["note_text"], "followup": followup,
                    "low_quality": bool(r["low_quality"]), "note": r["error"],
                    "job_ids": json.loads(r["job_ids_json"] or "[]")})
    return out


def telegram_text(d: Digest, report_path: str, *, max_apply: int = 12) -> str:
    from job_agent.notify.telegram import esc

    lines = [f"<b>Job Agent: {d.date:%a %d %b}</b>",
             f"{d.new_count} new | {d.counts['referral_first']} referral-first | "
             f"{d.counts['apply_direct']} apply | {d.counts['skip']} skipped"]

    def block(i, j):
        new = " 🆕" if j.is_new else ""
        link = f' <a href="{esc(j.url)}">[open]</a>' if j.url else ""
        out = [f"{i}. <b>{esc(j.company)}</b>: {esc(j.title)}  <b>{j.score:g}</b>{link}{new}"]
        for e in j.explanation:
            out.append(f"   {esc(e)}")
        for r in j.reasons:
            out.append(f"   ⚠ {esc(r)}")
        return "\n".join(out)

    n = 0
    if d.referral:
        lines.append("\n<b>Referral first</b>")
        for j in d.referral:
            n += 1
            lines.append(block(n, j))
    with_jd = [j for j in d.apply if not j.low_confidence]
    no_jd = [j for j in d.apply if j.low_confidence]
    if with_jd:
        lines.append("\n<b>Apply directly</b>")
        for j in with_jd[:max_apply]:
            n += 1
            lines.append(block(n, j))
        if len(with_jd) > max_apply:
            lines.append(f"   …and {len(with_jd) - max_apply} more in the report")
    if no_jd:
        lines.append("\n<b>From alerts, no JD (open manually)</b>")
        for j in no_jd:
            link = f' <a href="{esc(j.url)}">[LinkedIn]</a>' if j.url else ""
            lines.append(f"• {esc(j.company)}: {esc(j.title)}{link}")
    if d.outreach:
        lines.append("\n<b>Outreach</b>")
        for o in d.outreach:
            tags = f" ({', '.join(o['tags'])}{', ' + o['degree'] if o['degree'] else ''})" if o["tags"] or o["degree"] else ""
            who = f"{esc(o['name'])}{esc(tags)}, {esc(o['company'])}"
            if o["lane"] == "A":
                what = "follow-up ready" if o["followup"] else "referral ask ready"
                lines.append(f"A) {who}: {what} → /draft {o['id']}")
            elif o["status"] == "queued":
                dry = " (dry run: would send)" if (o["note"] or "").startswith("dry-run") else ""
                lines.append(f"B) {who}: request queued{dry}")
            elif o["status"] == "sent":
                lines.append(f"B) {who}: request pending")
    if d.closed:
        lines.append(f"\n<b>Closed since last digest</b> (dropped from referrals)")
        lines += [f"✕ {esc(x)}" for x in d.closed[:10]]
        if len(d.closed) > 10:
            lines.append(f"   …and {len(d.closed) - 10} more")
    if d.linkedin_status:
        lines.append(f"\n<i>{esc(d.linkedin_status)}</i>")
    lines.append(f"Full report: <code>{esc(report_path)}</code>")
    return "\n".join(lines)
