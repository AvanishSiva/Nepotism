"""Connections.csv referral matching (spec §7 source 1). No browsing.

LinkedIn export (Settings → Data privacy → Get a copy of your data → Connections) gives a CSV
with a few "Notes:" lines, then: First Name,Last Name,URL,Email Address,Company,Position,Connected On.
Only connections at companies with referral-first jobs are stored in referral_points
(degree 1st, tag "connection"); the rest of your network never enters the DB.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path

from rapidfuzz import fuzz

from job_agent.config import Config
from job_agent.normalize import norm_company

log = logging.getLogger(__name__)
DEFAULT_PATH = "data/Connections.csv"


def csv_path(cfg: Config) -> Path:
    return cfg.path((cfg.referrals or {}).get("connections_csv", DEFAULT_PATH))


def install(src: Path, dest: Path) -> Path:
    """Copy an export (Connections.csv or the full .zip) to data/Connections.csv (mode 600)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            name = next((n for n in z.namelist() if n.lower().endswith("connections.csv")), None)
            if not name:
                raise ValueError(f"no Connections.csv inside {src}")
            dest.write_bytes(z.read(name))
    else:
        shutil.copyfile(src, dest)
    os.chmod(dest, 0o600)
    return dest


def read_rows(text: str) -> list[dict]:
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.lstrip("﻿").startswith("First Name")), None)
    if start is None:
        raise ValueError("Connections.csv header (First Name,Last Name,URL,...) not found")
    rows = []
    for r in csv.DictReader(io.StringIO("\n".join(lines[start:]).lstrip("﻿"))):
        name = f"{(r.get('First Name') or '').strip()} {(r.get('Last Name') or '').strip()}".strip()
        if name:
            rows.append({"name": name, "url": (r.get("URL") or "").strip(), "company": (r.get("Company") or "").strip(),
                         "position": (r.get("Position") or "").strip(), "connected_on": (r.get("Connected On") or "").strip()})
    return rows


def company_matches(row_company: str, name: str, aliases: list[str]) -> bool:
    rc = norm_company(row_company)
    if not rc:
        return False
    for cand in [name, *aliases]:
        c = norm_company(cand)
        if c and (rc == c or fuzz.ratio(rc, c) >= 92):
            return True
    return False


def _slug(url: str) -> str | None:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", url)
    return m.group(1).lower() if m else None


def import_matches(conn: sqlite3.Connection, cfg: Config, *, only: str | None = None) -> dict:
    """Store 1st-degree connections at referral-first companies. Returns stats."""
    import json

    from job_agent.referrals import linkedin_people as lp

    path = csv_path(cfg)
    stats = {"file": str(path), "rows": 0, "matched": 0, "no_url": 0, "per_company": {}}
    if not path.exists():
        stats["missing"] = True
        return stats
    rows = read_rows(path.read_text(encoding="utf-8", errors="replace"))
    stats["rows"] = len(rows)
    hooks = lp.Hooks(cfg)
    for c in lp.target_companies(conn, only):
        aliases = json.loads(c["aliases_json"] or "[]")
        for r in rows:
            if not company_matches(r["company"], c["name"], aliases):
                continue
            slug = _slug(r["url"])
            if not slug:
                stats["no_url"] += 1                     # hidden URL in export: can't key the person
                continue
            p = lp.Person(name=r["name"], headline=f"{r['position']} at {r['company']}".strip(),
                          profile_url=lp.profile_url(slug), degree="1st", mutuals=0,
                          tags={"connection"}, source="connections_csv")
            if lp._RECRUITER.search(r["position"]):
                p.tags.add("recruiter")
            elif lp._ENGINEER.search(r["position"]) and not lp._NOT_ENGINEER.search(r["position"]):
                p.tags.add("engineer")
            p.tags |= hooks.tag_from_headline(p.headline)
            lp.save_person(conn, c["id"], p)
            conn.execute("UPDATE referral_points SET found_at = datetime('now') WHERE profile_url = ?",
                         (p.profile_url,))
            stats["matched"] += 1
            stats["per_company"][c["name"]] = stats["per_company"].get(c["name"], 0) + 1
    conn.commit()
    return stats
