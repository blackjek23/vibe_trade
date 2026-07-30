"""Measure realized slippage: what the bot actually paid vs the modelled fill.

`backtest/engine.py` fills every order at the day's OPEN with **zero** frictions.
Production sends market orders that fill *at* the open, so the difference between
the two is measurable from live data — and that difference is the single biggest
unknown in the backtest.

**Why this is worth doing when a profitability read isn't.** Both are per-trade
averages, but their signal-to-noise differs by ~50x:

    edge:      mean ~$143/trade, sd ~$1,059  -> ~218 trades for a 2-SE read
    slippage:  mean ~few bps,    sd ~few bps -> a few dozen legs is plenty

So a 3-4 month paper run cannot tell you whether the strategy makes money, but it
can pin down the friction number precisely. Feed the result back into the backtest
(which has 8 years and 1,300+ trades) and *that* becomes the decision instrument.

Needs network (yfinance). Read-only: never writes to the DB.

Usage:
    python scripts/measure_slippage.py data/vibe_trade.db
    python scripts/measure_slippage.py <db> --csv out.csv
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics as st
import sys
from datetime import date, timedelta
from pathlib import Path

# IB tiered/fixed US equities: $0.005/share, $1.00 minimum per order.
IB_PER_SHARE = 0.005
IB_MIN_ORDER = 1.00


def _legs(db: Path, since: str | None = None) -> list[dict]:
    """One row per executed leg (BUY entry, SELL exit) with its fill price.

    `since` filters on fill date. Use it to exclude a setup/manual-testing period:
    slippage on orders placed by hand at arbitrary times of day says nothing about
    what the 16:00 cron will pay at the open, and those legs are large enough to
    drag the mean badly.
    """
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        out: list[dict] = []
        rows = con.execute(
            "SELECT symbol, entry_price, date(entry_time), exit_price, "
            "date(exit_time), filled_quantity, status FROM trades"
        ).fetchall()
        for sym, entry_px, entry_d, exit_px, exit_d, qty, status in rows:
            if since and entry_d and entry_d < since:
                entry_d = None
            if since and exit_d and exit_d < since:
                exit_d = None
            if entry_px and entry_d and qty:
                out.append({"symbol": sym, "side": "BUY", "date": entry_d,
                            "fill": float(entry_px), "qty": int(qty)})
            if exit_px and exit_d and qty and status in ("CLOSED", "PARTIALLY_FILLED"):
                out.append({"symbol": sym, "side": "SELL", "date": exit_d,
                            "fill": float(exit_px), "qty": int(qty)})
        return out
    finally:
        con.close()


def _fetch_opens(symbols: list[str], start: date, end: date) -> dict:
    """{(symbol, 'YYYY-MM-DD'): open_price} for the whole window in one request."""
    import yfinance as yf

    df = yf.download(
        symbols, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
        progress=False, auto_adjust=False, group_by="ticker", threads=True,
    )
    opens: dict = {}
    if df is None or df.empty:
        return opens
    multi = isinstance(df.columns, type(df.columns)) and getattr(df.columns, "nlevels", 1) > 1
    for sym in symbols:
        try:
            col = df[sym]["Open"] if multi else df["Open"]
        except (KeyError, TypeError):
            continue
        for ts, val in col.items():
            if val == val:  # skip NaN
                opens[(sym, ts.date().isoformat())] = float(val)
    return opens


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db")
    ap.add_argument("--csv", help="write per-leg detail to this path")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="ignore legs filled before this date (skip a setup period)")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"ERROR: not a file: {db}", file=sys.stderr)
        return 2

    legs = _legs(db, since=args.since)
    if not legs:
        print("No executed legs in this DB - nothing to measure.")
        return 0

    symbols = sorted({leg["symbol"] for leg in legs})
    dates = [date.fromisoformat(leg["date"]) for leg in legs]
    print(f"{len(legs)} leg(s) across {len(symbols)} symbol(s), "
          f"{min(dates)} -> {max(dates)}")
    print("fetching daily opens from yfinance...")
    opens = _fetch_opens(symbols, min(dates), max(dates))
    if not opens:
        print("ERROR: no price data returned.", file=sys.stderr)
        return 1

    measured, unmatched = [], 0
    for leg in legs:
        op = opens.get((leg["symbol"], leg["date"]))
        if not op or op <= 0:
            unmatched += 1
            continue
        # Positive bps = cost. A BUY filled above the open paid up; a SELL filled
        # below the open gave up value. Both are money lost to friction.
        diff = (leg["fill"] - op) if leg["side"] == "BUY" else (op - leg["fill"])
        leg["open"] = op
        leg["slip_bps"] = diff / op * 10_000
        leg["slip_usd"] = diff * leg["qty"]
        leg["commission"] = max(IB_MIN_ORDER, IB_PER_SHARE * leg["qty"])
        measured.append(leg)

    if not measured:
        print("No legs could be matched to a daily open.", file=sys.stderr)
        return 1

    def report(name: str, rows: list[dict]) -> None:
        if not rows:
            return
        bps = [r["slip_bps"] for r in rows]
        usd = [r["slip_usd"] for r in rows]
        print(f"\n{name}  (n={len(rows)})")
        print(f"  mean       {st.mean(bps):+8.2f} bps    ${st.mean(usd):+8.2f}/leg")
        print(f"  median     {st.median(bps):+8.2f} bps")
        if len(rows) > 1:
            sd = st.stdev(bps)
            print(f"  std dev    {sd:8.2f} bps")
            print(f"  95% CI     {st.mean(bps) - 2*sd/len(rows)**0.5:+.2f} to "
                  f"{st.mean(bps) + 2*sd/len(rows)**0.5:+.2f} bps")
        print(f"  worst      {max(bps):+8.2f} bps    best {min(bps):+8.2f} bps")
        print(f"  total      ${sum(usd):+,.2f}")

    report("ALL LEGS", measured)
    report("BUY legs", [r for r in measured if r["side"] == "BUY"])
    report("SELL legs", [r for r in measured if r["side"] == "SELL"])

    all_bps = [r["slip_bps"] for r in measured]
    mean_bps = st.mean(all_bps)
    comm = sum(r["commission"] for r in measured)
    slip = sum(r["slip_usd"] for r in measured)

    print("\n" + "=" * 62)
    print("COST SUMMARY")
    print(f"  slippage             ${slip:+,.2f}")
    print(f"  commission (est.)    ${comm:,.2f}   "
          f"(${IB_PER_SHARE}/sh, ${IB_MIN_ORDER:.2f} min)")
    print(f"  combined             ${slip + comm:,.2f} over {len(measured)} leg(s)")
    print(f"                       ${(slip + comm) / len(measured):,.2f} per leg")
    med_bps = st.median(all_bps)
    print("\nBACKTEST INPUT")
    print(f"  median {med_bps:5.1f} bps/leg -> round trip ~{2 * med_bps:.1f} bps"
          f"   <-- prefer this")
    print(f"  mean   {mean_bps:5.1f} bps/leg -> round trip ~{2 * mean_bps:.1f} bps")
    if mean_bps > med_bps * 1.5:
        print("  Right-skewed: the mean is pulled by a few bad fills, so the median")
        print("  is the better typical cost. Model the median, stress-test the mean.")
    if unmatched:
        print(f"\n({unmatched} leg(s) skipped - no daily open found for that date)")

    if args.csv:
        import csv as _csv
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=list(measured[0].keys()))
            w.writeheader()
            w.writerows(measured)
        print(f"\nper-leg detail -> {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
