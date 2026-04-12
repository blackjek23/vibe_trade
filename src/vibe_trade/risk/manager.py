"""Risk manager — pre-trade and portfolio-level checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vibe_trade.broker.models import AccountSummary, Position
from vibe_trade.config import RiskConfig
from vibe_trade.strategy.base import SignalResult

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    approved: bool
    reason: str = ""


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config

    def check_portfolio_limits(
        self,
        account: AccountSummary,
        positions: list[Position],
    ) -> RiskDecision:
        """Check portfolio-level risk gates before scanning for new entries."""
        # Max open positions
        if len(positions) >= self.config.max_open_positions:
            return RiskDecision(
                approved=False,
                reason=f"Max positions reached ({len(positions)}/{self.config.max_open_positions})",
            )

        # Max portfolio exposure
        total_exposure = sum(abs(p.market_value) for p in positions)
        exposure_pct = (total_exposure / account.net_liquidation * 100) if account.net_liquidation > 0 else 0
        if exposure_pct >= self.config.max_portfolio_exposure_pct:
            return RiskDecision(
                approved=False,
                reason=f"Portfolio exposure at {exposure_pct:.1f}% (max {self.config.max_portfolio_exposure_pct}%)",
            )

        return RiskDecision(approved=True)

    def check_trade(
        self,
        signal: SignalResult,
        account: AccountSummary,
        positions: list[Position],
    ) -> RiskDecision:
        """Check if a specific trade is allowed."""
        # Check if already holding this symbol
        for pos in positions:
            if pos.symbol == signal.symbol and pos.quantity != 0:
                return RiskDecision(
                    approved=False,
                    reason=f"Already holding position in {signal.symbol}",
                )

        # Single stock concentration limit
        if signal.trailing_stop_price and account.net_liquidation > 0:
            from vibe_trade.risk.position_sizer import calculate_position_size

            entry_price = signal.metadata.get("price", 0)
            if entry_price > 0:
                shares = calculate_position_size(
                    account.net_liquidation,
                    self.config.max_risk_per_trade_pct,
                    entry_price,
                    signal.trailing_stop_price,
                )
                position_value = shares * entry_price
                concentration = position_value / account.net_liquidation * 100
                if concentration > self.config.max_single_stock_pct:
                    return RiskDecision(
                        approved=False,
                        reason=f"{signal.symbol} would be {concentration:.1f}% of portfolio (max {self.config.max_single_stock_pct}%)",
                    )

        return RiskDecision(approved=True)
