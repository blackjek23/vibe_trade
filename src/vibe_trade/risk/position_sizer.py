"""Position sizing based on account percentage and risk."""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


def calculate_position_size(
    account_value: float,
    risk_per_trade_pct: float,
    entry_price: float,
    trailing_stop_price: float,
) -> int:
    """Calculate number of shares based on risk-per-trade.

    Formula: shares = (account_value * risk_pct/100) / abs(entry_price - stop_price)

    Returns 0 if the calculation is invalid.
    """
    risk_amount = account_value * (risk_per_trade_pct / 100)
    risk_per_share = abs(entry_price - trailing_stop_price)

    if risk_per_share <= 0 or entry_price <= 0:
        logger.warning("Invalid prices for position sizing: entry=%.2f stop=%.2f", entry_price, trailing_stop_price)
        return 0

    shares = risk_amount / risk_per_share
    shares = math.floor(shares)  # Round down to whole shares

    # Sanity check: position value shouldn't exceed account
    if shares * entry_price > account_value:
        shares = math.floor(account_value / entry_price)

    return max(shares, 0)
