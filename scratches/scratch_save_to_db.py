"""Live DB write: pull account summary + positions from IB paper, save to test DB, read back.

Run with:
    .venv/Scripts/python scratch_save_to_db.py

Prerequisites:
    - TWS or IB Gateway running on paper port 7497 with API enabled
    - config/config.toml with `mode = "paper"` (falls back to defaults otherwise)

What it writes:
    - 1 row in `daily_pnl` for today (account summary values + positions count)
    - N rows in `portfolio_snapshot` for today (one per held ticker)

DB location: `data/test_paper.db` -- separate from prod, gitignored, idempotent (rerun safe).
Inspect afterward with DB Browser for SQLite, DBeaver, or `sqlite3` CLI.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.config import load_config
from vibe_trade.db.engine import init_db
from vibe_trade.db.repository import (
    DailyPnLRepository,
    PortfolioSnapshotRepository,
)

TEST_DB_PATH = Path("data") / "test_paper.db"


async def main() -> None:
    config = load_config()

    if config.general.mode != "paper":
        print(f"ERROR: config says mode={config.general.mode!r}; refusing to run against live.")
        sys.exit(1)

    broker_config = config.broker.model_copy()
    broker_config.client_id = config.broker.client_id + 50

    broker = IBBroker(broker_config, mode="paper")

    # Initialize the test DB (creates file if missing; idempotent via save_snapshot + upsert).
    session_factory = init_db(str(TEST_DB_PATH))

    print(
        f"Connecting to IB paper at {broker_config.host}:{broker_config.get_port('paper')} "
        f"(client_id={broker_config.client_id})..."
    )
    await broker.connect()

    try:
        # ---------------------------------------------------------------- pull
        print("\n=== Pulling data from IB ===")
        account = await broker.get_account_summary()
        positions = await broker.get_positions()
        print(f"Account ID:        {account.account_id}")
        print(f"Net Liquidation:   ${account.net_liquidation:,.2f}")
        print(f"Total Cash:        ${account.total_cash:,.2f}")
        print(f"Realized P&L:      ${account.realized_pnl:,.2f}")
        print(f"Unrealized P&L:    ${account.unrealized_pnl:,.2f}")
        print(f"Open positions:    {len(positions)}")

        # ---------------------------------------------------------------- write
        today = date.today()
        session = session_factory()
        try:
            print(f"\n=== Writing to {TEST_DB_PATH} (date={today}) ===")

            daily_repo = DailyPnLRepository(session)
            daily_row = daily_repo.upsert_daily(
                today=today,
                realized_pnl=account.realized_pnl,
                unrealized_pnl=account.unrealized_pnl,
                trades_opened=0,  # placeholder -- populated by record/reconcile jobs later
                trades_closed=0,  # placeholder -- populated by record/reconcile jobs later
                account_value=account.net_liquidation,
                total_cash=account.total_cash,
                open_positions_count=len(positions),
            )
            print(f"[daily_pnl]          wrote 1 row (id={daily_row.id})")

            snap_repo = PortfolioSnapshotRepository(session)
            snap_rows = snap_repo.save_snapshot(
                today,
                [
                    {
                        "symbol": p.symbol,
                        "quantity": p.quantity,
                        "avg_cost": p.avg_cost,
                        "market_price": p.market_price,
                        "market_value": p.market_value,
                        "unrealized_pnl": p.unrealized_pnl,
                    }
                    for p in positions
                ],
            )
            print(f"[portfolio_snapshot] wrote {len(snap_rows)} rows")

            # ------------------------------------------------------- read back
            print(f"\n=== Reading back from {TEST_DB_PATH} ===")
            fetched_daily = (
                session.query(daily_row.__class__)
                .filter(daily_row.__class__.date == today)
                .first()
            )
            print("[daily_pnl row]")
            print(f"  date:                  {fetched_daily.date}")
            print(f"  realized_pnl:          ${fetched_daily.realized_pnl:,.2f}")
            print(f"  unrealized_pnl:        ${fetched_daily.unrealized_pnl:,.2f}")
            print(f"  account_value:         ${fetched_daily.account_value:,.2f}")
            print(f"  total_cash:            ${fetched_daily.total_cash:,.2f}")
            print(f"  open_positions_count:  {fetched_daily.open_positions_count}")
            print(f"  trades_opened:         {fetched_daily.trades_opened}")
            print(f"  trades_closed:         {fetched_daily.trades_closed}")
            print(f"  created_at:            {fetched_daily.created_at}")

            fetched_snaps = snap_repo.get_snapshot(today)
            print(f"\n[portfolio_snapshot rows -- {len(fetched_snaps)}]")
            header = (
                f"{'Symbol':<8} | {'Qty':>6} | {'Avg Cost':>10} | "
                f"{'Mkt Price':>10} | {'Mkt Value':>12} | {'Unreal P&L':>12}"
            )
            print(header)
            print("-" * len(header))
            for r in fetched_snaps:
                print(
                    f"{r.symbol:<8} | {r.quantity:>6} | "
                    f"${r.avg_cost:>9.2f} | ${r.market_price:>9.2f} | "
                    f"${r.market_value:>11.2f} | ${r.unrealized_pnl:>11.2f}"
                )
        finally:
            session.close()

    finally:
        await broker.disconnect()
        print(f"\nDisconnected cleanly. DB file: {TEST_DB_PATH.resolve()}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user -- disconnect should have run via try/finally.")
