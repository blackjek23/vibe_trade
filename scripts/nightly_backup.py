"""Nightly DB backup — consistent online `.backup` + integrity check + sha256
sidecar + retention pruning.

Replaces the plain `cp` in `deploy/crontab.example`, which the audit flagged
as the weakest copy mechanism in a codebase that gets this right twice
elsewhere (`export_db.sh`, `reset_paper_db.py`). A plain `cp` mid-write (or
mid-WAL-checkpoint) can copy a torn, inconsistent file that `integrity_check`
would reject -- silently, and it would sit there for up to 14 days until it
rotated out with nobody the wiser. This script uses the same safe idiom as
`reset_paper_db.py._archive`: SQLite's own online `.backup` API (safe even if
a job is mid-write), verified before it's trusted, plus a `.sha256` sidecar
so `docs/playbooks/data-recovery.md`'s documented `sha256sum -c` restore step
actually has a file to check against (previously it had none).

Pure stdlib -- no extra packages, so it runs in a bare `python:3.11-slim`
container with no `apk add`/`apt-get install` step (and therefore no network
dependency) at 23:45.

Usage (see deploy/crontab.example for the actual cron invocation):
    python nightly_backup.py --db /data/vibe_trade.db --out /backup --keep-days 14
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import sqlite3
import sys
from pathlib import Path


def backup_once(db: Path, out_dir: Path) -> Path:
    """Consistent online backup + checksum. Raises if the copy is not sound."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    out = out_dir / f"vibe_trade-{stamp}.db"

    src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(out))
        try:
            src.backup(dst)  # online .backup -- safe mid-write / WAL
        finally:
            dst.close()
    finally:
        src.close()

    check = sqlite3.connect(f"file:{out.as_posix()}?mode=ro", uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check;").fetchone()[0]
    finally:
        check.close()
    if integrity != "ok":
        # Leave the bad file in place for forensics, but make its sidecar
        # absence obvious rather than shipping a checksum for a bad backup.
        raise RuntimeError(f"backup failed integrity_check: {integrity}")

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_suffix(out.suffix + ".sha256").write_text(
        f"{digest}  {out.name}\n", encoding="utf-8"
    )
    return out


def prune_old(out_dir: Path, keep_days: int) -> list[Path]:
    """Remove backups (and their sidecars) older than `keep_days`."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)
    removed = []
    for f in sorted(out_dir.glob("vibe_trade-*.db")):
        mtime = dt.datetime.fromtimestamp(f.stat().st_mtime, tz=dt.timezone.utc)
        if mtime < cutoff:
            f.unlink()
            sidecar = f.with_suffix(f.suffix + ".sha256")
            if sidecar.exists():
                sidecar.unlink()
            removed.append(f)
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path, help="path to the live SQLite DB")
    ap.add_argument("--out", required=True, type=Path, help="directory to write the backup into")
    ap.add_argument("--keep-days", type=int, default=14, help="retention window (default 14)")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"ERROR: source DB not found: {args.db}", file=sys.stderr)
        return 1

    try:
        archive = backup_once(args.db, args.out)
    except Exception as exc:  # noqa: BLE001 -- report and exit non-zero for cron
        print(f"ERROR: backup failed: {exc}", file=sys.stderr)
        return 1

    print(f"OK: {archive.name} (integrity_check passed, {archive.name}.sha256 written)")

    for removed in prune_old(args.out, args.keep_days):
        print(f"pruned: {removed.name} (older than {args.keep_days} days)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
