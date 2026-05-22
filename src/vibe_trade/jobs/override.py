"""Session J manual override jobs -- operator-driven, off the cron cycle.

Two commands, both ad-hoc and run by a human watching the terminal:

- `run_close_position` -- market-SELL the full IB position for one symbol.
  Used to exit a ticker outside the 16:00 submit window.
- `run_cancel_pending` -- list working orders, or cancel every working order
  for one symbol.

Both follow submit's invariant: **no DB writes**. The next `record` / `reconcile`
run picks up the resulting fills via `ib.fills()` (orphan back-fill, Bug #5).

Client IDs:
- `close-position` connects as `OVERRIDE_CLIENT_ID` (4) -- distinct from
  submit=1 / record=2 / reconcile=3 / notifier=8. It places its own order and
  reads account-wide positions, so it needs no special id.
- `cancel-pending` connects as **submit's** id (1). IB only honours a
  `cancelOrder` from the client that placed the order (or the master client);
  `ib.openTrades()` is likewise client-scoped. Since submit is the only thing
  that places working orders, cancel-pending must act *as* submit to see and
  cancel them. submit is short-lived (done seconds after 16:00) so an ad-hoc
  cancel-pending will not collide; if it does, the connect fails loudly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from vibe_trade.broker.base import BaseBroker
from vibe_trade.broker.models import OpenOrder, OrderRequest

logger = logging.getLogger(__name__)

OVERRIDE_CLIENT_ID = 4

# orderRef tag stamped on manual close-position SELLs, so the fill is
# distinguishable from strategy exits ("donchian") and force-trims ("trim").
MANUAL_ORDER_REF = "manual"


@dataclass
class ClosePositionResult:
    symbol: str
    found: bool                       # was the symbol held with non-zero qty?
    quantity: int = 0                 # shares closed (abs of position)
    aborted: bool = False             # operator declined the confirmation
    status: str = ""                  # OrderResult.status, when an order was placed
    fill_price: float | None = None


@dataclass
class CancelPendingResult:
    listing: list[OpenOrder]          # every working order seen (always populated)
    symbol: str | None = None         # the targeted symbol, or None for list-only
    matched: bool = True              # False when `symbol` matched no working order
    cancelled: list[OpenOrder] = field(default_factory=list)


async def run_close_position(
    *,
    broker: BaseBroker,
    symbol: str,
    confirm: Callable[[str, int], bool] | None = None,
) -> ClosePositionResult:
    """Market-close the full IB position for `symbol`.

    `confirm` is called with `(symbol, quantity)` before the order is placed;
    return False to abort. Defaults to auto-confirm (used by tests and `--yes`).
    Caller manages the broker connection.
    """
    confirm = confirm or (lambda _sym, _qty: True)

    positions = await broker.get_positions()
    match = next(
        (p for p in positions if p.symbol == symbol and p.quantity != 0), None
    )
    if match is None:
        logger.warning("close-position: %s not held (or zero qty)", symbol)
        return ClosePositionResult(symbol=symbol, found=False)

    qty = abs(match.quantity)
    # V2 has no shorts, but stay correct if a short ever appears.
    side = "SELL" if match.quantity > 0 else "BUY"

    if not confirm(symbol, qty):
        logger.info("close-position: %s aborted by operator", symbol)
        return ClosePositionResult(symbol=symbol, found=True, quantity=qty, aborted=True)

    logger.warning("close-position: %s %d shares of %s", side, qty, symbol)
    result = await broker.place_market_order(
        OrderRequest(
            symbol=symbol, side=side, quantity=qty, order_ref=MANUAL_ORDER_REF
        )
    )
    return ClosePositionResult(
        symbol=symbol,
        found=True,
        quantity=qty,
        status=result.status,
        fill_price=result.fill_price,
    )


async def run_cancel_pending(
    *,
    broker: BaseBroker,
    symbol: str | None = None,
) -> CancelPendingResult:
    """List working orders (`symbol=None`) or cancel all orders for one symbol.

    Cancels every working order matching `symbol` -- in practice the
    one-order-per-ticker-per-day invariant means there is at most one.
    Caller manages the broker connection.
    """
    listing = await broker.get_open_orders()

    if symbol is None:
        logger.info("cancel-pending: listing %d working order(s)", len(listing))
        return CancelPendingResult(listing=listing)

    matches = [o for o in listing if o.symbol == symbol]
    if not matches:
        logger.warning("cancel-pending: no working order for %s", symbol)
        return CancelPendingResult(listing=listing, symbol=symbol, matched=False)

    cancelled = await broker.cancel_orders_for_symbol(symbol)
    logger.warning("cancel-pending: cancelled %d order(s) for %s", len(cancelled), symbol)
    return CancelPendingResult(listing=listing, symbol=symbol, cancelled=cancelled)
