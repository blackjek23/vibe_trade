"""Live order placement: submit one or more market orders via `broker.place_market_order`.

Run with:
    .venv/Scripts/python scratches/scratch_place_order.py

Prerequisites:
    - TWS or IB Gateway running on paper port 7497 with API enabled
    - config/config.toml with `mode = "paper"` (falls back to defaults otherwise)
    - Market is open (Asia/Jerusalem 16:30–23:00) -- outside hours orders queue, don't fill

Edit the ORDERS list below. Each entry is (SIDE, TICKER, QTY):

    ("BUY",  "F",  1)       -> buy 1 share of F
    ("SELL", "F",  1)       -> sell 1 share of F
    ("SELL", None, 1)       -> sell 1 share of whichever long you hold the most of
                              (auto-pick only valid for SELL; errors otherwise)

Orders are placed in sequence. A single 3-second banner shows all planned orders
before anything goes out, so Ctrl+C bails cleanly.

A BUY + SELL of the same ticker (both market orders) fills in milliseconds each
and nets to ~0 position change minus a tiny commission/spread -- fine for smoke-
testing both paths in one run, not for real position management.

Per-order failures don't abort the rest; the loop continues and prints a summary.
"""

from __future__ import annotations

import asyncio
import sys

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.broker.models import OrderRequest, OrderResult
from vibe_trade.config import load_config

# ---------------------------------------------------------------------------
# Edit this list to choose what to place. (SIDE, TICKER_or_None, QTY)
# ---------------------------------------------------------------------------
ORDERS: list[tuple[str, str | None, int]] = [
    ("BUY",  "T",  1),
    ("SELL", None, 1),  # auto-pick biggest long holding
]
# ---------------------------------------------------------------------------


def _validate_orders() -> None:
    for i, (side, ticker, qty) in enumerate(ORDERS):
        if side not in ("BUY", "SELL"):
            print(f"ERROR: ORDERS[{i}] side must be 'BUY' or 'SELL', got {side!r}.")
            sys.exit(1)
        if qty <= 0:
            print(f"ERROR: ORDERS[{i}] qty must be positive, got {qty}.")
            sys.exit(1)
        if ticker is None and side != "SELL":
            print(f"ERROR: ORDERS[{i}] TICKER=None is only valid for SIDE=SELL.")
            sys.exit(1)


async def _resolve_ticker(broker: IBBroker, ticker: str | None, side: str) -> str | None:
    """If ticker is None, pick the long with the most shares. Returns None if none exists."""
    if ticker is not None:
        return ticker
    # Caller already validated side == "SELL" when ticker is None.
    positions = await broker.get_positions()
    longs = [p for p in positions if p.quantity > 0]
    if not longs:
        return None
    biggest = max(longs, key=lambda p: p.quantity)
    print(f"  auto-picked SELL target: {biggest.symbol} (held qty={biggest.quantity})")
    return biggest.symbol


def _print_result(idx: int, side: str, qty: int, symbol: str, result: OrderResult) -> None:
    print(f"\n  === OrderResult #{idx} ({side} {qty} x {symbol}) ===")
    print(f"    order_id:     {result.order_id}")
    print(f"    symbol:       {result.symbol}")
    print(f"    side:         {result.side}")
    print(f"    quantity:     {result.quantity}")
    print(f"    status:       {result.status}")
    print(f"    fill_price:   {result.fill_price}")
    print(f"    fill_time:    {result.fill_time}")
    print(f"    error:        {result.error_message}")


async def main() -> None:
    _validate_orders()

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
        # Pre-resolve tickers (so the abort banner shows real symbols, not "auto").
        planned: list[tuple[str, str | None, int]] = []  # (side, resolved_symbol_or_None, qty)
        for side, ticker, qty in ORDERS:
            resolved = await _resolve_ticker(broker, ticker, side)
            planned.append((side, resolved, qty))

        print()
        print("=" * 64)
        print(f"  About to place {len(planned)} order(s) against PAPER:")
        for i, (side, resolved, qty) in enumerate(planned, start=1):
            if resolved is None:
                print(f"    #{i}: {side} {qty} of <auto-pick failed: no longs held> -- will SKIP")
            else:
                print(f"    #{i}: {side} {qty} x {resolved} (market)")
        print("  Ctrl+C within 3 seconds to abort.")
        print("=" * 64)
        await asyncio.sleep(3.0)

        placed = 0
        skipped = 0
        failed = 0
        for i, (side, resolved, qty) in enumerate(planned, start=1):
            if resolved is None:
                print(f"\n[{i}] SKIP -- no long positions to SELL.")
                skipped += 1
                continue
            print(f"\n[{i}] Submitting {side} {qty} x {resolved}...")
            try:
                result = await broker.place_market_order(
                    OrderRequest(symbol=resolved, side=side, quantity=qty)
                )
                _print_result(i, side, qty, resolved, result)
                placed += 1
            except Exception as exc:
                print(f"  FAILED: {exc!r}")
                failed += 1

        print("\n--- summary ---")
        print(f"  placed:  {placed}")
        print(f"  skipped: {skipped}")
        print(f"  failed:  {failed}")

    finally:
        await broker.disconnect()
        print("\nDisconnected cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAborted by user -- disconnect should have run via try/finally.")
