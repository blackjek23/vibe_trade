"""Quick integrity + contents check for a vibe_trade SQLite DB file.

Opens the DB read-only, runs PRAGMA integrity_check, and prints the row count
of every user table. Exits non-zero if the integrity check does not return 'ok'.

Usage:
    python scripts/verify_db.py data/vibe_trade_prod_20260613.db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/verify_db.py <path-to.db>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.is_file():
        print(f"ERROR: not a file: {path}", file=sys.stderr)
        return 2

    # Read-only so we can never mutate the snapshot while inspecting it.
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        integrity = cur.execute("PRAGMA integrity_check;").fetchone()[0]
        print(f"file:            {path}  ({path.stat().st_size:,} bytes)")
        print(f"integrity_check: {integrity}")

        tables = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        if not tables:
            print("  (no user tables found)")
        for t in tables:
            n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"  {t:<22} {n:>8,} rows")
    finally:
        con.close()

    return 0 if integrity == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
