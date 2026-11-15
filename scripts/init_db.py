"""One-time setup: create data/job_agent.db and all tables.

Usage:
    python scripts/init_db.py              # create (safe to re-run; never drops data)
    python scripts/init_db.py --db other.db
    python scripts/init_db.py --reset      # DELETE the DB file and start fresh (asks first)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_agent.db import DEFAULT_DB_PATH, SCHEMA_VERSION, init_db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--reset", action="store_true", help="delete the existing DB first")
    args = ap.parse_args()

    db_path: Path = args.db
    if args.reset and db_path.exists():
        answer = input(f"Delete {db_path} and ALL its data? Type 'yes': ")
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

    existed = db_path.exists()
    conn = init_db(db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    conn.close()

    print(f"{'Updated' if existed else 'Created'} {db_path} (schema v{SCHEMA_VERSION})")
    print("Tables:", ", ".join(tables))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
