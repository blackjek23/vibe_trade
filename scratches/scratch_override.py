"""Live verification for the Session J override broker methods.

Run with:
    .venv/Scripts/python scratches/scratch_override.py            # list only (safe)
    .venv/Scripts/python scratches/scratch_override.py AAPL       # cancel AAPL's orders

Prerequisites:
    - TWS or IB Gateway running on paper port 7497 with API enabled
    - config/config.toml with `mode = "paper"`

What it does:
    - Connects to IB paper (client_id = main + 50, per the test convention)
    - Calls `broker.get_open_orders()` -- the new Session J method backing
      `cancel-pending` -- and prints each OpenOrder so you can confirm the
      shape mapped out of `ib.openTrades()` matches reality
    - If a symbol is passed as argv[1], calls `cancel_orders_for_symbol(SYMBOL)`
      after an explicit y/N confirmation, then re-lists to show the effect

`close-position` is NOT exercised here -- it places a real market SELL; run
the actual `vibe-trade close-position` CLI against paper for that. This script
covers only the genuinely-new, untested IB glue: open-order read + cancel.

Empty output is valid: with no working orders, get_open_orders() returns [].
To get a working order to look at, place a limit-ish order in TWS first, or
run `vibe-trade submit` on a day with BUY signals just before market open.
"""

from __future__ import annotations

import asyncio
import sys

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.config import load_config


def _print_open_orders(orders) -> None:
    """Render a list[OpenOrder] as a compact table."""
    print(f"Count: {len(orders)}")
    if not orders:
        print("  (none -- nothing working)")
        return
    print(
        f"  {'symbol':<8} | {'side':<4} | {'qty':>5} | {'permId':>12} | status"
    )
    for o in orders:
        print(
            f"  {o.symbol:<8} | {o.side:<4} | {o.quantity:>5d} | "
            f"{o.perm_id:>12d} | {o.status}"
        )


async def main() -> None:
    config = load_config()

    if config.general.mode != "paper":
        print(f"ERROR: config says mode={config.general.mode!r}; refusing to run against live.")
        sys.exit(1)

    target_symbol = sys.argv[1].upper() if len(sys.argv) > 1 else None

    broker_config = config.broker.model_copy()
    broker_config.client_id = config.broker.client_id + 50

    broker = IBBroker(broker_config, mode="paper")

    print(
        f"Connecting to IB paper at {broker_config.host}:{broker_config.get_port('paper')} "
        f"(client_id={broker_config.client_id})..."
    )
    await broker.connect()

    try:
        # ib_async hydrates open orders via the account-update stream; wait a beat.
        await asyncio.sleep(1.0)

        print("\n=== broker.get_open_orders() -- backs `cancel-pending` listing ===")
        orders = await broker.get_open_orders()
        _print_open_orders(orders)

        if target_symbol is None:
            print("\n(list-only run -- pass a symbol as argv[1] to cancel its orders)")
            return

        matches = [o for o in orders if o.symbol == target_symbol]
        print(f"\n=== cancel_orders_for_symbol({target_symbol!r}) ===")
        if not matches:
            print(f"  no working order for {target_symbol} -- nothing to cancel")
            return

        print(f"  {len(matches)} working order(s) match {target_symbol}:")
        _print_open_orders(matches)
        answer = input(f"  Cancel all {len(matches)} order(s) for {target_symbol}? [y/N] ")
        if answer.strip().lower() != "y":
            print("  aborted -- nothing cancelled")
            return

        cancelled = await broker.cancel_orders_for_symbol(target_symbol)
        print(f"  cancelOrder issued for {len(cancelled)} order(s)")

        # Give IB a moment to process, then re-list to show the effect.
        await asyncio.sleep(2.0)
        print("\n=== broker.get_open_orders() after cancel ===")
        _print_open_orders(await broker.get_open_orders())

    finally:
        await broker.disconnect()
        print("\nDisconnected cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user -- disconnect should have run via try/finally.")
