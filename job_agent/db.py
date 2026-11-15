"""SQLite schema and connection helpers.

All state lives in one file: data/job_agent.db. The schema is applied with
CREATE ... IF NOT EXISTS, so running it again on an existing DB is safe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "job_agent.db"

# Bump when the schema changes; stored in PRAGMA user_version.
SCHEMA_VERSION = 2

SCHEMA = """
-- Companies we know about: ATS watchlist + companies seen in alerts.
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL UNIQUE,          -- normalised name, for matching
    aliases_json    TEXT NOT NULL DEFAULT '[]',
    ats             TEXT CHECK (ats IN ('greenhouse','lever','ashby','workday') OR ats IS NULL),
    slug            TEXT,                          -- workday: "tenant/wdN/site"
    region          TEXT,                          -- lever: 'us' | 'eu'
    careers_url     TEXT,
    linkedin_slug   TEXT,
    watch           INTEGER NOT NULL DEFAULT 0,    -- 1 = poll its ATS daily
    cooldown_until  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY,
    company_id       INTEGER REFERENCES companies(id),
    source           TEXT NOT NULL,                -- gmail_linkedin|gmail_indeed|greenhouse|lever|ashby|workday
    title            TEXT NOT NULL,
    location         TEXT,
    remote_type      TEXT CHECK (remote_type IN ('remote','hybrid','onsite','unknown')) DEFAULT 'unknown',
    posted_at        TEXT,
    alert_date       TEXT,                         -- date of the alert email, if from Gmail
    apply_url        TEXT,
    linkedin_job_url TEXT,
    linkedin_job_id  TEXT,
    jd_text          TEXT,
    jd_status        TEXT NOT NULL DEFAULT 'missing' CHECK (jd_status IN ('ok','missing')),
    salary_min       REAL,
    salary_max       REAL,
    currency         TEXT,
    ats              TEXT,
    ats_job_id       TEXT,
    dedupe_key       TEXT NOT NULL UNIQUE,
    first_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_company     ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_li_job_id   ON jobs(linkedin_job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_ats_job     ON jobs(ats, ats_job_id);

CREATE TABLE IF NOT EXISTS job_scores (
    job_id          INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    facts_json      TEXT,
    score           REAL,
    decision        TEXT CHECK (decision IN ('referral_first','apply_direct','skip')),
    reasons_json    TEXT,
    explanation     TEXT,
    low_confidence  INTEGER NOT NULL DEFAULT 0,
    possible_repost INTEGER NOT NULL DEFAULT 0,
    scored_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scores_decision ON job_scores(decision);

CREATE TABLE IF NOT EXISTS referral_points (
    id          INTEGER PRIMARY KEY,
    company_id  INTEGER REFERENCES companies(id),
    name        TEXT NOT NULL,
    headline    TEXT,
    profile_url TEXT UNIQUE,
    degree      TEXT,                              -- '1st' | '2nd' | '3rd' | NULL
    tags_json   TEXT NOT NULL DEFAULT '[]',        -- e.g. ["alum","hiring_team"]
    mutuals     INTEGER,
    source      TEXT,                              -- connections_csv|job_page|people_tab|people_search
    found_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_refpoints_company ON referral_points(company_id);

CREATE TABLE IF NOT EXISTS outreach (
    id                INTEGER PRIMARY KEY,
    referral_point_id INTEGER NOT NULL REFERENCES referral_points(id),
    job_ids_json      TEXT NOT NULL DEFAULT '[]',
    lane              TEXT NOT NULL CHECK (lane IN ('A','B')),
    note_text         TEXT,
    low_quality       INTEGER NOT NULL DEFAULT 0,  -- drafted by Ollama fallback
    status            TEXT NOT NULL DEFAULT 'drafted' CHECK (status IN (
                        'drafted','queued','sent','accepted','asked','followed_up',
                        'replied','referred','withdrawn','aborted')),
    error             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at           TEXT,
    accepted_at       TEXT,
    asked_at          TEXT,
    followed_up_at    TEXT,
    replied_at        TEXT,
    followup_text     TEXT                         -- one gentle follow-up (Lane A), drafted after 5 days
);
CREATE INDEX IF NOT EXISTS idx_outreach_person ON outreach(referral_point_id);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach(status);

CREATE TABLE IF NOT EXISTS applications (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs(id),
    status      TEXT NOT NULL CHECK (status IN ('applied','interview','offer','rejected','ghosted')),
    applied_at  TEXT,
    referred_by INTEGER REFERENCES referral_points(id),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    verdict    TEXT NOT NULL CHECK (verdict IN ('good','bad','skip')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    stage       TEXT NOT NULL,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    ok          INTEGER,
    stats_json  TEXT,
    error       TEXT
);

-- One row per local day.
CREATE TABLE IF NOT EXISTS safety_counters (
    day           TEXT PRIMARY KEY,                -- YYYY-MM-DD
    page_views    INTEGER NOT NULL DEFAULT 0,
    requests_sent INTEGER NOT NULL DEFAULT 0,
    withdrawals   INTEGER NOT NULL DEFAULT 0
);

-- Global state that isn't per-day: paused_until, warmup_started_on, etc.
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cache of raw HTTP responses per run (ATS feeds), to avoid re-fetching.
CREATE TABLE IF NOT EXISTS http_cache (
    url        TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Gmail messages already processed (alerts, acceptance emails).
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id   TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,                    -- job_alert | accepted
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create the DB file and all tables. Safe to run more than once."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"DB schema version {current} is newer than this code ({SCHEMA_VERSION})."
        )
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for DBs created by older code (CREATE IF NOT EXISTS won't add columns)."""
    if "followup_text" not in _columns(conn, "outreach"):          # v2
        conn.execute("ALTER TABLE outreach ADD COLUMN followup_text TEXT")


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def kv_set(conn: sqlite3.Connection, key: str, value: str | None, *, commit: bool = True) -> None:
    if value is None:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))
    else:
        conn.execute("INSERT INTO kv(key, value, updated_at) VALUES(?, ?, datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (key, value))
    if commit:
        conn.commit()
