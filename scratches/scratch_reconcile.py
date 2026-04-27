"""Live reconcile: mirror what `vibe-trade reconcile` will do at 23:30.

Run with:
    .venv/Scripts/python scratch_reconcile.py

Prerequisites:
    - TWS or IB Gateway running on paper port 7497 with API enabled
    - config/config.toml with `mode = "paper"` (falls back to defaults otherwise)
    - `scratch_orders_save.py` has run earlier today so there are pending trades to reconcile

What it does (per V2 architecture):
    1. Pulls `ib.trades()` (for orderStatus) and `ib.fills()` (for execution details)
    2. Loads today's pending trades from DB (SUBMITTED BUYs + PENDING_CLOSE SELLs)
    3. For each pending trade:
        - SUBMITTED BUY: aggregate fills by `ib_order_id`
            * fully filled -> confirm_buy_fill(status=OPEN)
            * partial     -> confirm_buy_fill(status=PARTIALLY_FILLED)
            * no fills + order Cancelled -> mark_cancelled
            * no fills + still working -> skip (log only)
        - PENDING_CLOSE: aggregate fills by `exit_ib_order_id`
            * fully filled -> confirm_close_fill(status=CLOSED, pnl from IB fills)
            * partial     -> confirm_close_fill(status=PARTIALLY_FILLED)
            * no fills + cancelled -> confirm_close_fill(status=CANCELLED) reverts to OPEN
            * no fills + still working -> skip
    4. Saves PortfolioSnapshot rows (one per held ticker)
    5. Upserts DailyPnL with real `trades_opened` / `trades_closed` counts

Idempotent: rerunning is safe. Already-reconciled trades (status OPEN/CLOSED/etc.)
are not in `get_pending_orders_for_today` so they're skipped. Snapshot delete-then-insert.

DB location: `data/test_paper.db` -- same as `scratch_save_to_db.py`, gitignored.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.config import load_config
from vibe_trade.db.engine import init_db
from vibe_trade.db.models import Trade
from vibe_trade.db.repository import (
    DailyPnLRepository,
    PortfolioSnapshotRepository,
    TradeRepository,
)

TEST_DB_PATH = Path("data") / "test_paper.db"

# IB reports order-level terminal states as these strings:
TERMINAL_CANCELLED = {"Cancelled", "Inactive", "ApiCancelled"}


def _aggregate_fills(fills: list):
    """Sum shares, weighted avg price, latest fill time, realized P&L across fills."""
    total_shares = sum(f.execution.shares for f in fills)
    if total_shares <= 0:
        return 0.0, 0.0, None, 0.0
    weighted_price = (
        sum(f.execution.price * f.execution.shares for f in fills) / total_shares
    )
    latest_time = max(f.time for f in fills)
    realized_pnl = sum(
        (f.commissionReport.realizedPNL or 0.0) if f.commissionReport else 0.0
        for f in fills
    )
    return total_shares, weighted_price, latest_time, realized_pnl


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
        # Give ib_async's account-update stream a moment to hydrate.
        await asyncio.sleep(1.0)

        # ----------------------------------------------------------------- pull
        print("\n=== Pulling from IB ===")
        account = await broker.get_account_summary()
        positions = await broker.get_positions()
        ib_trades = list(broker.ib.trades())
        ib_fills = list(broker.ib.fills())
        print(f"account:   net_liq=${account.net_liquidation:,.2f} "
              f"realized=${account.realized_pnl:,.2f} unrealized=${account.unrealized_pnl:,.2f}")
        print(f"positions: {len(positions)}")
        print(f"trades:    {len(ib_trades)}")
        print(f"fills:     {len(ib_fills)}")

        trades_by_order_id = {t.order.orderId: t for t in ib_trades}
        fills_by_order_id: dict[int, list] = defaultdict(list)
        for f in ib_fills:
            fills_by_order_id[f.execution.orderId].append(f)

        # ---------------------------------------------------------------- reconcile
        today = date.today()
        session = session_factory()
        try:
            trade_repo = TradeRepository(session)
            snap_repo = PortfolioSnapshotRepository(session)
            daily_repo = DailyPnLRepository(session)

            pending = trade_repo.get_pending_orders_for_today(today)
            print(f"\n=== Reconciling {len(pending)} pending trade(s) from DB ===")

            opened_count = 0   # SUBMITTED -> OPEN or PARTIALLY_FILLED
            closed_count = 0   # PENDING_CLOSE -> CLOSED or PARTIALLY_FILLED
            cancelled_count = 0
            skipped_count = 0

            for trade in pending:
                if trade.status == "SUBMITTED":
                    oid = trade.ib_order_id
                    fills = fills_by_order_id.get(oid, [])
                    ib_trade = trades_by_order_id.get(oid)
                    order_status = (
                        ib_trade.orderStatus.status if ib_trade and ib_trade.orderStatus else "?"
                    )

                    if not fills:
                        if order_status in TERMINAL_CANCELLED:
                            trade_repo.mark_cancelled(
                                trade.id,
                                f"BUY order {oid} {order_status} with no fills",
                            )
                            cancelled_count += 1
                            print(
                                f"  [CANCEL BUY] trade_id={trade.id} {trade.symbol} "
                                f"order_id={oid} status={order_status}"
                            )
                        else:
                            skipped_count += 1
                            print(
                                f"  [skip  BUY] trade_id={trade.id} {trade.symbol} "
                                f"order_id={oid} status={order_status} (still working, no fills)"
                            )
                        continue

                    total_shares, avg_px, latest_time, _realized = _aggregate_fills(fills)
                    filled_qty = int(total_shares)
                    new_status = (
                        "OPEN" if filled_qty == trade.requested_quantity else "PARTIALLY_FILLED"
                    )
                    trade_repo.confirm_buy_fill(
                        trade_id=trade.id,
                        entry_price=avg_px,
                        filled_quantity=filled_qty,
                        entry_time=latest_time or datetime.now(),
                        status=new_status,
                    )
                    opened_count += 1
                    print(
                        f"  [FILL  BUY] trade_id={trade.id} {trade.symbol:<6s} "
                        f"order_id={oid} req={trade.requested_quantity} filled={filled_qty} "
                        f"avg=${avg_px:.2f} -> {new_status}"
                    )

                elif trade.status == "PENDING_CLOSE":
                    oid = trade.exit_ib_order_id
                    fills = fills_by_order_id.get(oid, [])
                    ib_trade = trades_by_order_id.get(oid)
                    order_status = (
                        ib_trade.orderStatus.status if ib_trade and ib_trade.orderStatus else "?"
                    )

                    if not fills:
                        if order_status in TERMINAL_CANCELLED:
                            # Status=CANCELLED reverts trade to OPEN (position still held).
                            trade_repo.confirm_close_fill(
                                trade_id=trade.id,
                                exit_price=0.0,
                                filled_quantity=0,
                                exit_time=datetime.now(),
                                pnl=0.0,
                                pnl_pct=0.0,
                                status="CANCELLED",
                            )
                            cancelled_count += 1
                            print(
                                f"  [CANCEL SELL] trade_id={trade.id} {trade.symbol} "
                                f"order_id={oid} status={order_status} -> reverted to OPEN"
                            )
                        else:
                            skipped_count += 1
                            print(
                                f"  [skip  SELL] trade_id={trade.id} {trade.symbol} "
                                f"order_id={oid} status={order_status} (still working, no fills)"
                            )
                        continue

                    total_shares, avg_px, latest_time, realized_pnl = _aggregate_fills(fills)
                    filled_qty = int(total_shares)
                    # pnl_pct vs. entry basis; if entry didn't record, fall back to 0.0
                    basis = (trade.entry_price or 0.0) * (trade.filled_quantity or 0)
                    pnl_pct = (realized_pnl / basis * 100.0) if basis > 0 else 0.0
                    # If BUY was partially filled, exit can still be fully the partial amount.
                    new_status = (
                        "CLOSED"
                        if filled_qty == (trade.filled_quantity or trade.requested_quantity)
                        else "PARTIALLY_FILLED"
                    )
                    trade_repo.confirm_close_fill(
                        trade_id=trade.id,
                        exit_price=avg_px,
                        filled_quantity=filled_qty,
                        exit_time=latest_time or datetime.now(),
                        pnl=realized_pnl,
                        pnl_pct=pnl_pct,
                        status=new_status,
                    )
                    closed_count += 1
                    print(
                        f"  [FILL SELL] trade_id={trade.id} {trade.symbol:<6s} "
                        f"order_id={oid} filled={filled_qty} avg=${avg_px:.2f} "
                        f"pnl=${realized_pnl:+.2f} ({pnl_pct:+.2f}%) -> {new_status}"
                    )

                else:
                    skipped_count += 1
                    print(
                        f"  [skip  ???] trade_id={trade.id} unexpected status={trade.status}"
                    )

            print("\n--- reconcile summary ---")
            print(f"  opened (SUBMITTED->OPEN/PARTIAL):      {opened_count}")
            print(f"  closed (PENDING_CLOSE->CLOSED/PARTIAL):{closed_count}")
            print(f"  cancelled:                            {cancelled_count}")
            print(f"  skipped (still working):              {skipped_count}")

            # ---------------------------------------------------- portfolio snapshot
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
            print(f"\n[portfolio_snapshot] wrote {len(snap_rows)} rows for {today}")

            # ---------------------------------------------------- daily pnl
            daily_row = daily_repo.upsert_daily(
                today=today,
                realized_pnl=account.realized_pnl,
                unrealized_pnl=account.unrealized_pnl,
                trades_opened=opened_count,
                trades_closed=closed_count,
                account_value=account.net_liquidation,
                total_cash=account.total_cash,
                open_positions_count=len(positions),
            )
            print(
                f"[daily_pnl] upserted row id={daily_row.id} "
                f"opened={opened_count} closed={closed_count} "
                f"net_liq=${account.net_liquidation:,.2f}"
            )

            # ---------------------------------------------------- read-back
            print(f"\n=== Read-back: today's trades from {TEST_DB_PATH} ===")
            todays_trades = (
                session.query(Trade)
                .filter(
                    (Trade.submitted_at >= datetime.combine(today, datetime.min.time()))
                    | (Trade.exit_submitted_at >= datetime.combine(today, datetime.min.time()))
                )
                .order_by(Trade.id)
                .all()
            )
            print(f"Total rows touched today: {len(todays_trades)}")
            if todays_trades:
                header = (
                    f"{'id':>3} | {'symbol':<8} | {'side':<4} | "
                    f"{'req':>4} | {'filled':>6} | "
                    f"{'status':<16} | {'entry':>8} | {'exit':>8} | "
                    f"{'pnl':>10} | {'pnl_pct':>8}"
                )
                print(header)
                print("-" * len(header))
                for tr in todays_trades:
                    entry_s = f"${tr.entry_price:.2f}" if tr.entry_price else "--"
                    exit_s = f"${tr.exit_price:.2f}" if tr.exit_price else "--"
                    pnl_s = f"${tr.pnl:+.2f}" if tr.pnl is not None else "--"
                    pct_s = f"{tr.pnl_pct:+.2f}%" if tr.pnl_pct is not None else "--"
                    print(
                        f"{tr.id:>3} | {tr.symbol:<8} | {tr.side:<4} | "
                        f"{tr.requested_quantity:>4d} | "
                        f"{tr.filled_quantity if tr.filled_quantity is not None else 0:>6d} | "
                        f"{tr.status:<16} | {entry_s:>8} | {exit_s:>8} | "
                        f"{pnl_s:>10} | {pct_s:>8}"
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
