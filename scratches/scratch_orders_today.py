"""Live data-pull: show today's orders known to IB (filled, submitted, cancelled).

Run with:
    .venv/Scripts/python scratch_orders_today.py

Prerequisites:
    - TWS or IB Gateway running on paper port 7497 with API enabled
    - config/config.toml with `mode = "paper"` (falls back to defaults otherwise)

What it does:
    - Connects to IB paper
    - Pulls `ib.trades()` — all trade records the client knows about (today + session lifetime)
    - Pulls `ib.openOrders()` — subset that are still working / pending
    - Prints both so you can see the raw shape `record` job will consume

Empty output is valid: if no orders exist for this session, both lists are empty.
"""

from __future__ import annotations

import asyncio
import sys

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.config import load_config


def _fmt_trade_row(t) -> str:
    """Render one `ib.trades()` entry as a single compact line."""
    order = t.order
    status = t.orderStatus
    contract = t.contract
    avg_fill = status.avgFillPrice if status and status.avgFillPrice else 0.0
    filled = int(status.filled) if status else 0
    return (
        f"  order_id={order.orderId:>6d} | "
        f"{contract.symbol:<8} | "
        f"{order.action:<4} | "
        f"req_qty={int(order.totalQuantity):>4d} | "
        f"filled={filled:>4d} | "
        f"status={status.status if status else 'n/a':<12} | "
        f"avg_fill=${avg_fill:>8.2f} | "
        f"type={order.orderType}"
    )


async def main() -> None:
    config = load_config()

    if config.general.mode != "paper":
        print(f"ERROR: config says mode={config.general.mode!r}; refusing to run against live.")
        sys.exit(1)

    broker_config = config.broker.model_copy()
    broker_config.client_id = config.broker.client_id + 50

    broker = IBBroker(broker_config, mode="paper")

    print(
        f"Connecting to IB paper at {broker_config.host}:{broker_config.get_port('paper')} "
        f"(client_id={broker_config.client_id})..."
    )
    await broker.connect()

    try:
        # ib_async populates trades/orders via the account-update stream; give
        # it a beat to hydrate before we read.
        await asyncio.sleep(1.0)

        print("\n=== broker.ib.trades() — all trade records this client knows about ===")
        trades = list(broker.ib.trades())
        print(f"Count: {len(trades)}")
        if trades:
            for t in trades:
                print(_fmt_trade_row(t))
        else:
            print("  (none — no orders submitted in this session yet)")

        print("\n=== broker.ib.openOrders() — subset still pending / working ===")
        open_orders = list(broker.ib.openOrders())
        print(f"Count: {len(open_orders)}")
        for o in open_orders:
            print(
                f"  order_id={o.orderId:>6d} | "
                f"{o.action:<4} | "
                f"req_qty={int(o.totalQuantity):>4d} | "
                f"type={o.orderType}"
            )
        if not open_orders:
            print("  (none — nothing pending)")

    finally:
        await broker.disconnect()
        print("\nDisconnected cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user — disconnect should have run via try/finally.")
