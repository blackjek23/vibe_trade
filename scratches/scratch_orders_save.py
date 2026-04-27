"""Live DB write: pull today's orders from IB, save to test DB, read back.

Mirrors what the V2 `record` job will do at 16:25 every trading day.

Run with:
    .venv/Scripts/python scratch_orders_save.py

Prerequisites:
    - TWS or IB Gateway running on paper port 7497 with API enabled
    - config/config.toml with `mode = "paper"` (falls back to defaults otherwise)

What it does:
    - Pulls `broker.ib.trades()` -- all orders the client has seen today
    - For each BUY:   inserts a `trades` row with status=SUBMITTED
    - For each SELL:  finds the matching OPEN trade in the DB, flips to PENDING_CLOSE
    - Reads back and prints every trade row

Idempotency:
    - Re-running is safe. Dedup by `ib_order_id` (BUYs) and `exit_ib_order_id` (SELLs).

Known limitation (by design, not a bug):
    The first run will save BUYs but skip every SELL, because there's no matching
    OPEN trade in the DB yet. Real life: `record` runs daily; a BUY saved today
    becomes OPEN after `reconcile` at 23:30. Tomorrow's SELL on that position
    then has a match to flip.

DB location: `data/test_paper.db` -- shared with `scratch_save_to_db.py`, gitignored.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.config import load_config
from vibe_trade.db.engine import init_db
from vibe_trade.db.models import Trade
from vibe_trade.db.repository import TradeRepository

TEST_DB_PATH = Path("data") / "test_paper.db"


async def main() -> None:
    config = load_config()

    if config.general.mode != "paper":
        print(f"ERROR: config says mode={config.general.mode!r}; refusing to run against live.")
        sys.exit(1)

    broker_config = config.broker.model_copy()
    broker_config.client_id = config.broker.client_id + 50

    broker = IBBroker(broker_config, mode="paper")
    session_factory = init_db(str(TEST_DB_PATH))

    print(
        f"Connecting to IB paper at {broker_config.host}:{broker_config.get_port('paper')} "
        f"(client_id={broker_config.client_id})..."
    )
    await broker.connect()

    try:
        # Give ib_async's account-update stream a moment to hydrate trade cache.
        await asyncio.sleep(1.0)

        print("\n=== Pulling today's orders from IB ===")
        ib_trades = list(broker.ib.trades())
        print(f"IB returned {len(ib_trades)} trade record(s)")

        session = session_factory()
        try:
            repo = TradeRepository(session)

            buys_inserted = 0
            buys_skipped_dup = 0
            sells_flipped = 0
            sells_skipped_no_match = 0
            sells_skipped_dup = 0

            print(f"\n=== Writing to {TEST_DB_PATH} ===")

            for t in ib_trades:
                order_id = t.order.orderId
                symbol = t.contract.symbol
                action = t.order.action  # "BUY" or "SELL"
                qty = int(t.order.totalQuantity)

                if action == "BUY":
                    # Dedup by ib_order_id -- rerun safe.
                    existing = (
                        session.query(Trade)
                        .filter(Trade.ib_order_id == order_id)
                        .first()
                    )
                    if existing is not None:
                        buys_skipped_dup += 1
                        print(
                            f"  [skip BUY  ] {symbol:<6s} order_id={order_id} "
                            f"already in DB as trade_id={existing.id}"
                        )
                        continue
                    new_trade = repo.create_submitted_buy(
                        symbol=symbol,
                        strategy_name="manual",  # placeholder -- real record uses signal's strategy
                        requested_quantity=qty,
                        ib_order_id=order_id,
                        submitted_at=datetime.now(),
                    )
                    buys_inserted += 1
                    print(
                        f"  [BUY      ] {symbol:<6s} order_id={order_id} req_qty={qty} "
                        f"-> trade_id={new_trade.id} status=SUBMITTED"
                    )

                elif action == "SELL":
                    # Dedup: if a row already tracks this SELL order, skip.
                    dup = (
                        session.query(Trade)
                        .filter(Trade.exit_ib_order_id == order_id)
                        .first()
                    )
                    if dup is not None:
                        sells_skipped_dup += 1
                        print(
                            f"  [skip SELL ] {symbol:<6s} order_id={order_id} "
                            f"already tracked by trade_id={dup.id}"
                        )
                        continue

                    # Find a matching OPEN trade for this symbol.
                    open_matches = [
                        ot for ot in repo.get_open_trades() if ot.symbol == symbol
                    ]
                    if not open_matches:
                        sells_skipped_no_match += 1
                        print(
                            f"  [skip SELL ] {symbol:<6s} order_id={order_id} "
                            f"-- no OPEN trade to flip to PENDING_CLOSE"
                        )
                        continue

                    open_trade = open_matches[0]  # earliest match
                    repo.mark_pending_close(
                        trade_id=open_trade.id,
                        exit_ib_order_id=order_id,
                        exit_submitted_at=datetime.now(),
                    )
                    sells_flipped += 1
                    print(
                        f"  [SELL     ] {symbol:<6s} order_id={order_id} "
                        f"-> trade_id={open_trade.id} OPEN -> PENDING_CLOSE"
                    )

                else:
                    print(f"  [skip ???  ] {symbol:<6s} unknown action={action!r}")

            print("\n--- summary ---")
            print(f"  BUYs inserted:        {buys_inserted}")
            print(f"  BUYs skipped (dup):   {buys_skipped_dup}")
            print(f"  SELLs flipped:        {sells_flipped}")
            print(f"  SELLs skipped (dup):  {sells_skipped_dup}")
            print(f"  SELLs skipped (no match in DB): {sells_skipped_no_match}")

            # ------------------------------------------------------- read back
            print(f"\n=== Reading back all trade rows from {TEST_DB_PATH} ===")
            all_trades = (
                session.query(Trade).order_by(Trade.created_at.desc()).all()
            )
            print(f"Total rows: {len(all_trades)}")
            if all_trades:
                header = (
                    f"{'id':>3} | {'symbol':<8} | {'side':<4} | "
                    f"{'req_qty':>7} | {'filled':>6} | "
                    f"{'status':<14} | {'ib_buy':>7} | {'ib_sell':>7} | "
                    f"{'submitted_at':<19}"
                )
                print(header)
                print("-" * len(header))
                for tr in all_trades:
                    submitted = (
                        tr.submitted_at.strftime("%Y-%m-%d %H:%M:%S")
                        if tr.submitted_at
                        else "--"
                    )
                    print(
                        f"{tr.id:>3} | {tr.symbol:<8} | {tr.side:<4} | "
                        f"{tr.requested_quantity or 0:>7d} | "
                        f"{tr.filled_quantity if tr.filled_quantity is not None else 0:>6d} | "
                        f"{tr.status:<14} | "
                        f"{tr.ib_order_id if tr.ib_order_id is not None else '--':>7} | "
                        f"{tr.exit_ib_order_id if tr.exit_ib_order_id is not None else '--':>7} | "
                        f"{submitted:<19}"
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
