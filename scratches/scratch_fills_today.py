"""Live data-pull: show today's fills (executions) known to IB.

Run with:
    .venv/Scripts/python scratch_fills_today.py

Prerequisites:
    - TWS or IB Gateway running on paper port 7497 with API enabled
    - config/config.toml with `mode = "paper"` (falls back to defaults otherwise)

What it does:
    - Connects to IB paper
    - Pulls `ib.fills()` — every execution the client has seen this session
    - Prints each fill's shape: order_id, symbol, side, shares, price, time, realized P&L, commission

This is the raw shape the `reconcile` job will consume at 23:30 to populate
`entry_price`, `filled_quantity`, `entry_time`, `exit_price`, `exit_time`, `pnl`, `pnl_pct`.

Note: `ib.fills()` is client-id scoped — it only shows fills for orders this client
submitted. Order placed by a different client_id or TWS UI won't appear.

Empty output is valid: if you haven't placed orders in this session, the list is empty.
"""

from __future__ import annotations

import asyncio
import sys

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.config import load_config


def _fmt_fill_row(f) -> str:
    """Render one `ib.fills()` entry as a single compact line."""
    ex = f.execution
    cr = f.commissionReport
    symbol = f.contract.symbol if f.contract else "?"
    realized = cr.realizedPNL if cr and cr.realizedPNL is not None else 0.0
    commission = cr.commission if cr and cr.commission is not None else 0.0
    # ex.side is "BOT" or "SLD" in IB parlance
    return (
        f"  order_id={ex.orderId:>6d} | "
        f"{symbol:<8} | "
        f"{ex.side:<3} | "
        f"shares={ex.shares:>6.2f} | "
        f"price=${ex.price:>8.2f} | "
        f"time={f.time} | "
        f"realized_pnl=${realized:>10.2f} | "
        f"commission=${commission:>6.2f} | "
        f"exec_id={ex.execId}"
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
        # ib_async populates fills via the account-update stream; give it a beat.
        await asyncio.sleep(1.0)

        print("\n=== broker.ib.fills() — all executions this client knows about ===")
        fills = list(broker.ib.fills())
        print(f"Count: {len(fills)}")
        if fills:
            for f in fills:
                print(_fmt_fill_row(f))
        else:
            print("  (none — no executions in this session yet)")

        # Also show fills grouped by order_id — this is how reconcile will aggregate them.
        if fills:
            from collections import defaultdict

            print("\n=== Grouped by order_id (how reconcile will aggregate) ===")
            by_order: dict[int, list] = defaultdict(list)
            for f in fills:
                by_order[f.execution.orderId].append(f)

            for order_id, grp in sorted(by_order.items()):
                total_shares = sum(fl.execution.shares for fl in grp)
                weighted_px = (
                    sum(fl.execution.price * fl.execution.shares for fl in grp) / total_shares
                    if total_shares else 0.0
                )
                total_realized = sum(
                    (fl.commissionReport.realizedPNL or 0.0) if fl.commissionReport else 0.0
                    for fl in grp
                )
                latest = max(fl.time for fl in grp)
                symbol = grp[0].contract.symbol if grp[0].contract else "?"
                side = grp[0].execution.side
                print(
                    f"  order_id={order_id:>6d} | {symbol:<8} | {side:<3} | "
                    f"n_fills={len(grp):>2d} | total_shares={total_shares:>6.2f} | "
                    f"avg_price=${weighted_px:>8.2f} | realized=${total_realized:>10.2f} | "
                    f"latest_time={latest}"
                )

    finally:
        await broker.disconnect()
        print("\nDisconnected cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user — disconnect should have run via try/finally.")
