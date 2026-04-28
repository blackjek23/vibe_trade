"""V2 position sizing — fixed % of net liquidation, hard cap on open positions.

Locked spec (see project_v2_next_sessions.md memory):
- Basis: account.net_liquidation
- Per-position target: 1.8% of net_liq
- Max open positions: 50
- Share rounding: floor (whole shares only)
- Skip signal if 1 share already exceeds the target
- Skip BUY entirely if at the position cap
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_PCT_PER_POSITION: float = 0.018
DEFAULT_MAX_POSITIONS: int = 50


def size_position(
    net_liquidation: float,
    price: float,
    current_position_count: int,
    *,
    pct_per_position: float = DEFAULT_PCT_PER_POSITION,
    max_positions: int = DEFAULT_MAX_POSITIONS,
) -> int:
    """Return the number of shares to buy, or 0 if the signal should be skipped.

    Returns 0 in any of these cases:
    - position cap reached (`current_position_count >= max_positions`)
    - 1 share already exceeds the target (price > net_liq * pct)
    - non-positive net_liquidation or price (defensive, shouldn't happen in prod)

    Rounds DOWN to whole shares. Internal arithmetic is in integer cents to avoid
    float imprecision (e.g. `100_000 * 0.018` evaluates to `1799.9999...` not
    `1800` exactly under IEEE 754).
    """
    if net_liquidation <= 0 or price <= 0:
        logger.warning(
            "Invalid sizing inputs: net_liq=%.2f price=%.2f", net_liquidation, price
        )
        return 0

    if current_position_count >= max_positions:
        logger.info(
            "At position cap (%d/%d) -- skipping BUY", current_position_count, max_positions
        )
        return 0

    target_cents = round(net_liquidation * pct_per_position * 100)
    price_cents = round(price * 100)

    if price_cents <= 0:
        return 0

    if price_cents > target_cents:
        logger.info(
            "1 share of price=$%.2f exceeds target $%.2f (%.2f%% of $%.2f) -- skipping",
            price, target_cents / 100, pct_per_position * 100, net_liquidation,
        )
        return 0

    return target_cents // price_cents
