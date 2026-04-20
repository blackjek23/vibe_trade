"""Quick script to show the market_price / unrealized_pnl bug in get_positions().

Run with: .venv/Scripts/python scratch_positions.py
Prerequisite: TWS or IB Gateway running on port 7497 with API enabled.
"""

from __future__ import annotations

import asyncio

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.config import BrokerConfig


async def main() -> None:
    # Use a dedicated client_id for scripts so we don't clash with a running bot.
    config = BrokerConfig(host="127.0.0.1", client_id=51)
    broker = IBBroker(config, mode="paper")

    print("Connecting to IB paper...")
    await broker.connect()

    try:
        print("\n=== Account Summary ===")
        account = await broker.get_account_summary()
        print(f"Account ID:       {account.account_id}")
        print(f"Net Liquidation:  ${account.net_liquidation:,.2f}")
        print(f"Total Cash:       ${account.total_cash:,.2f}")
        print(f"Unrealized P&L:   ${account.unrealized_pnl:,.2f}")
        print(f"Realized P&L:     ${account.realized_pnl:,.2f}")

        print("\n=== Positions (via our get_positions) ===")
        positions = await broker.get_positions()
        if not positions:
            print("(no open positions — buy something in TWS first to see the bug)")
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
        print("\nDisconnected.")


if __name__ == "__main__":
    asyncio.run(main())
