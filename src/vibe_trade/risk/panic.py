"""Panic button — close all positions immediately.

The single most destructive path in the repository: cancels every working
order, then market-sells (or market-buys, for a short) every held position.
Nothing else in the CLI does this much damage in one call, which is exactly
why its own correctness matters more than most -- see PROJECT_EVALUATION.md's
test-suite section: this module had zero test coverage before that audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from vibe_trade.broker.base import BaseBroker
from vibe_trade.broker.models import PLACEMENT_FAILURE_STATUSES, OrderRequest

logger = logging.getLogger(__name__)

# Distinct from every scheduled job's client id (submit=1, record=2,
# reconcile=3, override=4) so panic can always connect -- including the
# exact scenario an operator reaches for it in: another client id already
# stuck/hung. Panic doesn't need to reuse another client's id the way
# cancel-pending does (that's specifically to call cancelOrder on an order
# *that* client placed); cancel_all_orders uses reqGlobalCancel, which is
# account-wide regardless of which client issues it.
PANIC_CLIENT_ID: int = 5


@dataclass
class PanicResult:
    cancelled_orders: int = 0
    closed: int = 0    # positions successfully closed (order accepted by IB)
    failed: int = 0    # positions that errored or were rejected
    details: list[dict] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        """True only if every close actually succeeded.

        ``len(details)`` (the previous "PANIC: Closed N positions" log line)
        counts attempts, not outcomes -- it reported N closed whether or not
        a single order was accepted. Callers should gate on this, not on
        message text or attempt counts.
        """
        return self.failed == 0


async def panic_close_all(broker: BaseBroker) -> PanicResult:
    """Cancel every working order, then close every open position.

    Returns a `PanicResult` with true success/failure counts -- the caller
    (the `panic` CLI command) must not report success without checking
    `result.all_succeeded`.
    """
    result = PanicResult()

    cancelled = await broker.cancel_all_orders()
    result.cancelled_orders = cancelled
    logger.warning(f"PANIC: Cancelled {cancelled} open orders")

    positions = await broker.get_positions()
    for pos in positions:
        if pos.quantity == 0:
            continue

        side = "SELL" if pos.quantity > 0 else "BUY"
        qty = abs(pos.quantity)

        logger.warning(f"PANIC: Closing {pos.symbol} — {side} {qty} shares")
        try:
            order_result = await broker.place_market_order(
                OrderRequest(symbol=pos.symbol, side=side, quantity=qty)
            )
            ok = order_result.status not in PLACEMENT_FAILURE_STATUSES
            if ok:
                result.closed += 1
            else:
                result.failed += 1
                logger.error(
                    f"PANIC: {pos.symbol} order status={order_result.status} "
                    f"-- NOT confirmed closed"
                )
            result.details.append({
                "symbol": pos.symbol,
                "side": side,
                "quantity": qty,
                "status": order_result.status,
                "ok": ok,
                "fill_price": order_result.fill_price,
            })
        except Exception as e:  # noqa: BLE001 -- one bad symbol must not abort the rest
            result.failed += 1
            logger.error(f"PANIC: Failed to close {pos.symbol}: {e}")
            result.details.append({
                "symbol": pos.symbol,
                "side": side,
                "quantity": qty,
                "status": "ERROR",
                "ok": False,
                "error": str(e),
            })

    attempted = result.closed + result.failed
    if result.failed:
        logger.warning(
            f"PANIC: {result.closed}/{attempted} position(s) closed -- "
            f"{result.failed} FAILED, check the account immediately"
        )
    else:
        logger.warning(f"PANIC: {result.closed}/{attempted} position(s) closed")

    return result
