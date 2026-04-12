"""Panic button — close all positions immediately."""

from __future__ import annotations

import logging

from vibe_trade.broker.base import BaseBroker
from vibe_trade.broker.models import OrderRequest

logger = logging.getLogger(__name__)


async def panic_close_all(broker: BaseBroker) -> list[dict]:
    """Close all open positions via market orders.

    Returns a list of results for each position closed.
    """
    results = []

    # Cancel all pending orders first
    cancelled = await broker.cancel_all_orders()
    logger.warning(f"PANIC: Cancelled {cancelled} open orders")

    # Get all positions and close them
    positions = await broker.get_positions()
    for pos in positions:
        if pos.quantity == 0:
            continue

        side = "SELL" if pos.quantity > 0 else "BUY"
        qty = abs(pos.quantity)

        logger.warning(f"PANIC: Closing {pos.symbol} — {side} {qty} shares")
        try:
            result = await broker.place_market_order(
                OrderRequest(
                    symbol=pos.symbol,
                    side=side,
                    quantity=qty,
                )
            )
            results.append({
                "symbol": pos.symbol,
                "side": side,
                "quantity": qty,
                "status": result.status,
                "fill_price": result.fill_price,
            })
        except Exception as e:
            logger.error(f"PANIC: Failed to close {pos.symbol}: {e}")
            results.append({
                "symbol": pos.symbol,
                "side": side,
                "quantity": qty,
                "status": "ERROR",
                "error": str(e),
            })

    logger.warning(f"PANIC: Closed {len(results)} positions")
    return results
