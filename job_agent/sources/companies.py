"""ATS watchlist: careers-URL → (ats, slug) detection and YAML → DB sync."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from job_agent.normalize import norm_company

ATS_TYPES = {"greenhouse", "lever", "ashby", "workday"}


def parse_careers_url(url: str) -> dict:
    """Return {'ats', 'slug', 'region'} for a supported careers URL, else raise ValueError."""
    u = urlsplit(url.strip() if "://" in url else "https://" + url.strip())
    host = u.netloc.lower()
    parts = [p for p in u.path.split("/") if p]

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io",
                "job-boards.eu.greenhouse.io"} and parts:
        if parts[0] == "embed":  # boards.greenhouse.io/embed/job_board?for=slug
            m = re.search(r"(?:^|&)for=([^&]+)", u.query)
            if m:
                return {"ats": "greenhouse", "slug": m.group(1), "region": None}
        else:
            return {"ats": "greenhouse", "slug": parts[0], "region": None}

    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
        return {"ats": "lever", "slug": parts[0], "region": "eu" if ".eu." in host else "us"}

    if host == "jobs.ashbyhq.com" and parts:
        return {"ats": "ashby", "slug": parts[0], "region": None}

    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", host)
    if m:
        site_parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]  # drop "en-US"
        if site_parts:
            return {"ats": "workday", "slug": f"{m.group(1)}/{m.group(2)}/{site_parts[0]}", "region": None}

    raise ValueError(f"Unsupported or unrecognised careers URL: {url}")


def load_watchlist(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    entries = []
    for i, e in enumerate(raw.get("companies") or []):
        if not isinstance(e, dict) or not e.get("name"):
            raise ValueError(f"{path}: entry #{i + 1} needs a 'name'")
        entry = {
            "name": str(e["name"]).strip(),
            "aliases": [str(a) for a in e.get("aliases") or []],
            "careers_url": e.get("careers_url"),
            "linkedin_slug": e.get("linkedin_slug"),
            "watch": bool(e.get("watch", True)),
            "ats": e.get("ats"),
            "slug": e.get("slug"),
            "region": e.get("region"),
        }
        if entry["careers_url"] and not entry["ats"]:
            entry.update({k: v for k, v in parse_careers_url(entry["careers_url"]).items()
                          if v is not None or k != "region"})
        if entry["ats"] and entry["ats"] not in ATS_TYPES:
            raise ValueError(f"{path}: {entry['name']}: unknown ats '{entry['ats']}'")
        if entry["ats"] and not entry["slug"]:
            raise ValueError(f"{path}: {entry['name']}: ats set but no slug/careers_url")
        entries.append(entry)
    return entries


def upsert_company(conn: sqlite3.Connection, name: str, **fields) -> int:
    """Insert or update a company by normalised name. Returns its id."""
    key = norm_company(name)
    row = conn.execute("SELECT id FROM companies WHERE name_norm = ?", (key,)).fetchone()
    cols = {k: v for k, v in fields.items() if k in {
        "aliases_json", "ats", "slug", "region", "careers_url", "linkedin_slug", "watch"}}
    if row:
        if cols:
            sets = ", ".join(f"{k} = ?" for k in cols)
            conn.execute(f"UPDATE companies SET {sets}, updated_at = datetime('now') WHERE id = ?",
                         (*cols.values(), row["id"]))
        return row["id"]
    cols = {"name": name, "name_norm": key, **cols}
    cur = conn.execute(
        f"INSERT INTO companies({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )
    return cur.lastrowid


def sync_watchlist(conn: sqlite3.Connection, entries: list[dict]) -> dict:
    """Make the companies table reflect the YAML watchlist."""
    watched_ids = []
    for e in entries:
        cid = upsert_company(
            conn, e["name"],
            aliases_json=json.dumps(e["aliases"]),
            ats=e["ats"], slug=e["slug"], region=e["region"],
            careers_url=e["careers_url"], linkedin_slug=e["linkedin_slug"],
            watch=1 if (e["watch"] and e["ats"]) else 0,
        )
        if e["watch"] and e["ats"]:
            watched_ids.append(cid)
    # Anything no longer in the YAML stops being polled (rows are kept: jobs reference them).
    placeholders = ",".join("?" * len(watched_ids)) or "NULL"
    unwatched = conn.execute(
        f"UPDATE companies SET watch = 0, updated_at = datetime('now') "
        f"WHERE watch = 1 AND id NOT IN ({placeholders})", watched_ids,
    ).rowcount
    conn.commit()
    return {"watched": len(watched_ids), "unwatched": unwatched}
