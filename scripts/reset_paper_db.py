"""Archive and wipe the vibe_trade DB for a clean paper-trading run.

Deliberately narrow: this touches **only the SQLite file**. It never connects to
IB and never closes a position. Flattening the account is a separate, explicit
step (`vibe-trade panic`) so that "reset my records" can never silently become
"liquidate my portfolio". See docs/playbooks/paper-reset.md for the full order.

Safety rails, in order of how much they matter:

1. **Refuses unless `general.mode == "paper"`.** A live-mode config aborts, full
   stop, no override flag. There is no legitimate reason to wipe live history.
2. **Archives before deleting.** Timestamped copy + `.sha256`, same convention as
   `export_db.sh`. Uses SQLite's online `.backup` so the copy is consistent even
   mid-write. Refuses to continue if the archive fails verification.
3. **Prints what it will destroy and requires `--yes`.** A dry run is the default.

Usage:
    python scripts/reset_paper_db.py --config config/config.toml            # dry run
    python scripts/reset_paper_db.py --config config/config.toml --yes      # do it
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vibe_trade.config import load_config  # noqa: E402

ARCHIVE_DIR = Path("backups")
COUNTED_TABLES = ("trades", "daily_pnl", "portfolio_snapshot", "signals", "scan_log")


def _counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        present = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for t in COUNTED_TABLES if t in present
        }
    finally:
        con.close()


def _archive(db: Path, archive_dir: Path) -> Path:
    """Consistent online backup + checksum. Raises if the copy is not sound."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = archive_dir / f"vibe_trade-prewipe-{stamp}.db"

    src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(out))
        try:
            src.backup(dst)          # online .backup — safe mid-write / WAL
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
        raise RuntimeError(f"archive failed integrity_check: {integrity}")

    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.with_suffix(out.suffix + ".sha256")).write_text(
        f"{digest}  {out.name}\n", encoding="utf-8"
    )

    # The archive must contain what the original did, or we are not safe to wipe.
    if _counts(db) != _counts(out):
        raise RuntimeError("archive row counts differ from source -- refusing to wipe")

    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config/config.toml")
    ap.add_argument("--yes", action="store_true", help="actually perform the wipe")
    ap.add_argument("--archive-dir", default=str(ARCHIVE_DIR))
    args = ap.parse_args(argv)

    config = load_config(args.config)
    mode = config.general.mode
    db = Path(config.general.db_path)

    print(f"config:  {args.config}")
    print(f"mode:    {mode}")
    print(f"db_path: {db}")

    # --- rail 1: paper only, no override
    if mode != "paper":
        print(
            f"\nREFUSING: mode is '{mode}', not 'paper'. This script will not wipe "
            f"a live-mode database under any flag.",
            file=sys.stderr,
        )
        return 2

    if not db.is_file():
        print(f"\nNothing to do: {db} does not exist (already clean).")
        return 0

    counts = _counts(db)
    total = sum(counts.values())
    print(f"\nWill DESTROY {total:,} row(s) across {len(counts)} table(s):")
    for t, n in counts.items():
        print(f"  {t:<20} {n:>8,}")

    # --- rail 3: dry run by default
    if not args.yes:
        print(
            "\nDRY RUN — nothing changed. Re-run with --yes to archive and wipe."
        )
        return 0

    # --- rail 2: archive, verify, only then destroy
    print("\nArchiving...")
    archive = _archive(db, Path(args.archive_dir))
    print(f"  {archive}  ({archive.stat().st_size:,} bytes)")
    print(f"  {archive.name}.sha256")
    print("  integrity_check: ok, row counts match source")

    # Move rather than unlink: recoverable for as long as the .wiped file survives,
    # and it makes an interrupted run obvious instead of silently half-done.
    wiped = db.with_suffix(db.suffix + ".wiped")
    shutil.move(str(db), str(wiped))
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.exists():
            side.unlink()

    print(f"\nWiped. Old file moved to {wiped}")
    print("Recreate the empty schema with any CLI command, e.g.:")
    print("  python -m vibe_trade status --config " + args.config)
    print(f"\nArchive kept at: {archive}")
    print("Delete the .wiped file once you are happy with the reset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
