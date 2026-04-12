"""Order executor — translates signals into market orders."""

from __future__ import annotations

import logging

from vibe_trade.broker.base import BaseBroker
from vibe_trade.broker.models import AccountSummary, OrderRequest, OrderResult
from vibe_trade.config import RiskConfig
from vibe_trade.db.repository import TradeRepository
from vibe_trade.risk.position_sizer import calculate_position_size
from vibe_trade.strategy.base import SignalResult, SignalType

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(
        self,
        broker: BaseBroker,
        trade_repo: TradeRepository,
        risk_config: RiskConfig,
    ):
        self.broker = broker
        self.trade_repo = trade_repo
        self.risk_config = risk_config

    async def execute_signal(
        self,
        signal: SignalResult,
        account: AccountSummary,
    ) -> OrderResult | None:
        """Execute a trading signal by placing a market order."""
        if signal.signal == SignalType.HOLD:
            return None

        entry_price = signal.metadata.get("price", 0)
        if entry_price <= 0:
            logger.error(f"No valid price for {signal.symbol}")
            return None

        if signal.signal == SignalType.BUY:
            if not signal.trailing_stop_price:
                logger.error(f"No trailing stop price for BUY signal on {signal.symbol}")
                return None

            quantity = calculate_position_size(
                account_value=account.net_liquidation,
                risk_per_trade_pct=self.risk_config.max_risk_per_trade_pct,
                entry_price=entry_price,
                trailing_stop_price=signal.trailing_stop_price,
            )

            if quantity <= 0:
                logger.warning(f"Position size is 0 for {signal.symbol}, skipping")
                return None

            request = OrderRequest(
                symbol=signal.symbol,
                side="BUY",
                quantity=quantity,
            )

        elif signal.signal == SignalType.SELL:
            # For sell signals, close the existing position
            existing = self.trade_repo.get_open_trade_for_symbol(signal.symbol)
            if not existing:
                logger.info(f"No open position to sell for {signal.symbol}")
                return None

            request = OrderRequest(
                symbol=signal.symbol,
                side="SELL",
                quantity=existing.quantity,
            )
        else:
            return None

        # Place the market order
        logger.info(f"Placing {request.side} order: {request.quantity} x {signal.symbol}")
        result = await self.broker.place_market_order(request)

        # Record in database
        if result.status == "FILLED" and signal.signal == SignalType.BUY:
            self.trade_repo.create_trade(
                symbol=signal.symbol,
                side="BUY",
                strategy_name=signal.strategy_name,
                entry_price=result.fill_price or entry_price,
                quantity=quantity,
                trailing_stop=signal.trailing_stop_price,
                ib_order_id=result.order_id,
            )
        elif result.status == "FILLED" and signal.signal == SignalType.SELL:
            existing = self.trade_repo.get_open_trade_for_symbol(signal.symbol)
            if existing:
                self.trade_repo.close_trade(existing.id, result.fill_price or entry_price)

        return result
