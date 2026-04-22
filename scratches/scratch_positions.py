"""Live data-pull check: connect to IB paper, print account summary + open positions.

Run with:
    .venv/Scripts/python scratch_positions.py

Prerequisites:
    - TWS or IB Gateway running on paper port 7497 with API enabled
    - config/config.toml exists with `mode = "paper"` (falls back to defaults otherwise)

Safety:
    - Refuses to run if config says `mode = "live"`
    - Uses client_id = main config client_id + 50 (won't collide with a running bot)
    - try/finally + KeyboardInterrupt handling so we always disconnect cleanly —
      no ghost connections holding a client_id slot after a crash
"""

from __future__ import annotations

import asyncio
import sys

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.config import load_config


async def main() -> None:
    config = load_config()

    if config.general.mode != "paper":
        print(f"ERROR: config says mode={config.general.mode!r}; refusing to run against live.")
        sys.exit(1)

    # Bump client_id by +50 for scripts — avoids colliding with a running bot.
    broker_config = config.broker.model_copy()
    broker_config.client_id = config.broker.client_id + 50

    broker = IBBroker(broker_config, mode="paper")

    print(
        f"Connecting to IB paper at {broker_config.host}:{broker_config.get_port('paper')} "
        f"(client_id={broker_config.client_id})..."
    )
    await broker.connect()

    try:
        print("\n=== Account Summary ===")
        account = await broker.get_account_summary()
        print(f"Account ID:       {account.account_id}")
        print(f"Net Liquidation:  ${account.net_liquidation:,.2f}")
        print(f"Total Cash:       ${account.total_cash:,.2f}")
        print(f"Unrealized P&L:   ${account.unrealized_pnl:,.2f}")
        print(f"Realized P&L:     ${account.realized_pnl:,.2f}")

        print("\n=== Positions (via broker.get_positions) ===")
        positions = await broker.get_positions()
        if not positions:
            print("(no open positions — buy something in TWS first to see a populated list)")
        else:
            header = (
                f"{'Symbol':<8} | {'Qty':>6} | {'Avg Cost':>10} | "
                f"{'Mkt Price':>10} | {'Mkt Value':>12} | {'Unreal P&L':>12}"
            )
            print(header)
            print("-" * len(header))
            total_unreal = 0.0
            for p in positions:
                total_unreal += p.unrealized_pnl
                print(
                    f"{p.symbol:<8} | {p.quantity:>6} | "
                    f"${p.avg_cost:>9.2f} | ${p.market_price:>9.2f} | "
                    f"${p.market_value:>11.2f} | ${p.unrealized_pnl:>11.2f}"
                )
            print("-" * len(header))
            print(f"{'TOTAL UNREALIZED P&L:':>60} | ${total_unreal:>11.2f}")
            print(f"{'(account summary says:':>60} | ${account.unrealized_pnl:>11.2f})")

    finally:
        await broker.disconnect()
        print("\nDisconnected cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user — disconnect should have run via try/finally.")
