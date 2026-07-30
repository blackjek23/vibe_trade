"""Read-only drift audit for a vibe_trade DB. Reports; never mutates.

Answers the four questions that `reconcile`'s new drift sweep now prevents going
forward, but which still need resolving for rows written before it existed:

1. OPEN rows the latest portfolio_snapshot does not back  (phantom positions)
2. symbols carrying more than one OPEN row                (duplicate lots)
3. CLOSED rows whose `pnl` contradicts their own entry/exit prices
4. `daily_pnl.realized_pnl` vs `sum(trades.pnl)`          (two ledgers disagreeing)

Plus a calendar gap check, since missed job runs are what produce 1 and 2.

Exit code is 1 if any issue is found, 0 if clean — so it doubles as the
regression assertion for the Phase 2 cleanup.

Usage:
    python scripts/audit_drift.py scripts/vibe_trade-20260730-055751.db
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from pathlib import Path

# Round-trip commission on a 2-leg equity trade at IB is ~$2. Anything beyond
# this much unexplained on top of it is a basis mismatch, not fees.
PNL_TOLERANCE_USD = 5.0

# US market holidays that fall on weekdays, for the calendar gap check. Extend as
# needed — an unlisted holiday shows up as a false "missed run" and is harmless.
MARKET_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def _q(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    return con.execute(sql, args).fetchall()


def audit(path: Path) -> int:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    issues = 0
    try:
        print(f"file: {path}  ({path.stat().st_size:,} bytes)\n")

        last_date = _q(con, "SELECT MAX(date) FROM portfolio_snapshot")[0][0]
        if last_date is None:
            # A freshly-reset DB has no snapshot yet. That is a clean state, not a
            # failure -- exiting 1 here would make the post-wipe verification in
            # playbooks/paper-reset.md look broken on a correct reset.
            n_trades = _q(con, "SELECT COUNT(*) FROM trades")[0][0]
            if n_trades == 0:
                print("empty database (0 trades, 0 snapshots) - nothing to audit.")
                print("\n" + "=" * 60)
                print("CLEAN")
                return 0
            print(f"{n_trades} trade(s) but no portfolio_snapshot rows - "
                  f"cannot audit position drift. Run reconcile first.")
            return 1
        held = {r[0] for r in _q(
            con, "SELECT symbol FROM portfolio_snapshot WHERE date = ?", (last_date,)
        )}
        print(f"latest snapshot {last_date}: {len(held)} symbol(s) held at IB")

        # --- 1. phantom OPEN rows -------------------------------------------
        open_rows = _q(
            con,
            "SELECT id, symbol, filled_quantity, entry_price, date(entry_time) "
            "FROM trades WHERE status = 'OPEN' ORDER BY symbol, entry_time",
        )
        phantoms = [r for r in open_rows if r[1] not in held]
        print(f"\n[1] OPEN rows: {len(open_rows)}  "
              f"({len({r[1] for r in open_rows})} distinct symbols)")
        if phantoms:
            issues += len(phantoms)
            print(f"    !! {len(phantoms)} phantom row(s) - OPEN in DB, not held at IB:")
            for tid, sym, qty, px, when in phantoms:
                print(f"       id={tid:<5} {sym:<6} {qty} sh @ {px}  entered {when}")
        else:
            print("    ok — every OPEN row is backed by a held position")

        # --- 2. duplicate OPEN rows -----------------------------------------
        dupes = _q(
            con,
            "SELECT symbol, COUNT(*) n FROM trades WHERE status = 'OPEN' "
            "GROUP BY symbol HAVING n > 1 ORDER BY n DESC, symbol",
        )
        print(f"\n[2] symbols with >1 OPEN row: {len(dupes)}")
        if dupes:
            issues += len(dupes)
            print("    !! " + ", ".join(f"{s}x{n}" for s, n in dupes))
        else:
            print("    ok — one OPEN row per symbol")

        # --- 3. pnl vs own prices -------------------------------------------
        closed = _q(
            con,
            "SELECT id, symbol, entry_price, exit_price, filled_quantity, pnl "
            "FROM trades WHERE status = 'CLOSED' AND entry_price IS NOT NULL "
            "AND exit_price IS NOT NULL AND pnl IS NOT NULL",
        )
        bad = []
        for tid, sym, entry, exit_, qty, pnl in closed:
            expected = (exit_ - entry) * (qty or 0)
            if abs(expected - pnl) > PNL_TOLERANCE_USD:
                bad.append((tid, sym, entry, exit_, qty, pnl, expected))
        print(f"\n[3] CLOSED rows checked: {len(closed)}")
        if bad:
            issues += len(bad)
            print(f"    !! {len(bad)} row(s) whose pnl contradicts their own prices:")
            for tid, sym, entry, exit_, qty, pnl, exp in sorted(
                bad, key=lambda r: -abs(r[6] - r[5])
            ):
                print(f"       id={tid:<5} {sym:<6} {entry} -> {exit_} x{qty}  "
                      f"stored {pnl:+.2f}  implied {exp:+.2f}  "
                      f"delta {exp - pnl:+.2f}")
        else:
            print("    ok — stored pnl matches entry/exit prices within tolerance")

        # --- 4. ledger agreement --------------------------------------------
        trades_sum = _q(
            con, "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE pnl IS NOT NULL"
        )[0][0]
        daily_sum = _q(
            con,
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM daily_pnl "
            "WHERE realized_pnl IS NOT NULL",
        )[0][0]
        print(f"\n[4] sum(trades.pnl)          = {trades_sum:>12,.2f}")
        print(f"    sum(daily_pnl.realized)  = {daily_sum:>12,.2f}")
        if abs(trades_sum - daily_sum) > PNL_TOLERANCE_USD:
            issues += 1
            print(f"    !! ledgers disagree by {trades_sum - daily_sum:+,.2f}")
        else:
            print("    ok — ledgers agree within tolerance")

        # --- calendar gaps ---------------------------------------------------
        days = [r[0] for r in _q(con, "SELECT date FROM daily_pnl ORDER BY date")]
        if days:
            have = {dt.date.fromisoformat(d) for d in days}
            d, end = min(have), max(have)
            missing = []
            while d <= end:
                if (d.weekday() < 5
                        and d not in have
                        and d.isoformat() not in MARKET_HOLIDAYS):
                    missing.append(d.isoformat())
                d += dt.timedelta(days=1)
            print(f"\n[5] daily_pnl rows: {len(days)}  ({days[0]} -> {days[-1]})")
            if missing:
                issues += len(missing)
                print(f"    !! {len(missing)} trading day(s) with no row "
                      f"(missed job runs):")
                print("       " + ", ".join(missing))
            else:
                print("    ok — no missing trading days")

        print(f"\n{'=' * 60}")
        print("CLEAN" if issues == 0 else f"{issues} issue(s) found")
    finally:
        con.close()
    return 0 if issues == 0 else 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/audit_drift.py <path-to.db>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"ERROR: not a file: {path}", file=sys.stderr)
        return 2
    return audit(path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
