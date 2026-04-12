"""Trailing stop management."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vibe_trade.config import TrailingStopConfig
from vibe_trade.db.models import Trade

logger = logging.getLogger(__name__)


@dataclass
class TrailingStopUpdate:
    trade_id: int
    symbol: str
    should_close: bool
    new_stop: float | None = None
    reason: str = ""


def evaluate_trailing_stop(
    trade: Trade,
    current_price: float,
    current_atr: float | None,
    config: TrailingStopConfig,
) -> TrailingStopUpdate:
    """Evaluate whether to tighten the trailing stop or close the position.

    The trailing stop only moves up (for long positions), never down.
    """
    if trade.trailing_stop is None:
        return TrailingStopUpdate(
            trade_id=trade.id,
            symbol=trade.symbol,
            should_close=False,
            reason="No trailing stop set",
        )

    # Check if price has hit the trailing stop
    if trade.side == "BUY" and current_price <= trade.trailing_stop:
        return TrailingStopUpdate(
            trade_id=trade.id,
            symbol=trade.symbol,
            should_close=True,
            reason=f"Price {current_price:.2f} hit trailing stop {trade.trailing_stop:.2f}",
        )

    # Calculate new stop level
    if config.method == "atr" and current_atr and current_atr > 0:
        new_stop = current_price - (current_atr * config.atr_multiplier)
    else:
        new_stop = current_price * (1 - config.percentage / 100)

    # Only tighten (move up for long positions)
    if trade.side == "BUY" and new_stop > trade.trailing_stop:
        logger.info(
            f"{trade.symbol}: Tightening trailing stop from {trade.trailing_stop:.2f} to {new_stop:.2f}"
        )
        return TrailingStopUpdate(
            trade_id=trade.id,
            symbol=trade.symbol,
            should_close=False,
            new_stop=new_stop,
            reason=f"Stop tightened to {new_stop:.2f}",
        )

    return TrailingStopUpdate(
        trade_id=trade.id,
        symbol=trade.symbol,
        should_close=False,
        reason="No change needed",
    )
