"""Run bookkeeping (the `runs` table) and log-file setup."""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import traceback
from contextlib import contextmanager
from datetime import date

from job_agent.db import PROJECT_ROOT


def setup_logging(verbose: bool = False) -> None:
    logs = PROJECT_ROOT / "logs"
    logs.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    fh = logging.FileHandler(logs / f"{date.today():%Y-%m-%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@contextmanager
def track_run(conn: sqlite3.Connection, stage: str, dry_run: bool = False):
    """Record a stage run. Yields a dict the stage fills with stats."""
    run_id = conn.execute("INSERT INTO runs(stage, dry_run) VALUES(?, ?)",
                          (stage, int(dry_run))).lastrowid
    conn.commit()
    stats: dict = {}
    try:
        yield stats
    except BaseException as e:
        conn.rollback()
        conn.execute(
            "UPDATE runs SET finished_at = datetime('now'), ok = 0, stats_json = ?, error = ? WHERE id = ?",
            (json.dumps(stats, default=str), "".join(traceback.format_exception_only(e)).strip()[:2000], run_id),
        )
        conn.commit()
        raise
    conn.execute(
        "UPDATE runs SET finished_at = datetime('now'), ok = 1, stats_json = ? WHERE id = ?",
        (json.dumps(stats, default=str), run_id),
    )
    conn.commit()
