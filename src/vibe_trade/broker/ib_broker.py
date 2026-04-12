"""Interactive Brokers implementation via ib_async."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ib_async import IB, MarketOrder, Stock

from vibe_trade.broker.base import BaseBroker
from vibe_trade.broker.models import AccountSummary, OrderRequest, OrderResult, Position
from vibe_trade.config import BrokerConfig

logger = logging.getLogger(__name__)


class IBBroker(BaseBroker):
    def __init__(self, config: BrokerConfig, mode: str = "paper"):
        self.config = config
        self.mode = mode
        self.ib = IB()

    async def connect(self) -> None:
        port = self.config.get_port(self.mode)
        logger.info(f"Connecting to IB on {self.config.host}:{port} (mode={self.mode})")
        await self.ib.connectAsync(
            host=self.config.host,
            port=port,
            clientId=self.config.client_id,
            timeout=self.config.timeout,
            account=self.config.account or "",
        )
        logger.info("Connected to IB")

    async def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB")

    async def get_account_summary(self) -> AccountSummary:
        account_values = self.ib.accountSummary()
        values: dict[str, float] = {}
        account_id = ""
        for av in account_values:
            account_id = av.account
            if av.tag in (
                "NetLiquidation",
                "BuyingPower",
                "TotalCashValue",
                "UnrealizedPnL",
                "RealizedPnL",
            ):
                try:
                    values[av.tag] = float(av.value)
                except (ValueError, TypeError):
                    values[av.tag] = 0.0

        return AccountSummary(
            account_id=account_id,
            net_liquidation=values.get("NetLiquidation", 0.0),
            buying_power=values.get("BuyingPower", 0.0),
            total_cash=values.get("TotalCashValue", 0.0),
            unrealized_pnl=values.get("UnrealizedPnL", 0.0),
            realized_pnl=values.get("RealizedPnL", 0.0),
        )

    async def get_positions(self) -> list[Position]:
        ib_positions = self.ib.positions()
        positions = []
        for pos in ib_positions:
            contract = pos.contract
            positions.append(
                Position(
                    symbol=contract.symbol,
                    quantity=int(pos.position),
                    avg_cost=pos.avgCost,
                    market_price=pos.avgCost,  # updated below if market data available
                    market_value=pos.position * pos.avgCost,
                    unrealized_pnl=0.0,
                )
            )
        return positions

    async def place_market_order(self, request: OrderRequest) -> OrderResult:
        contract = Stock(request.symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)

        order = MarketOrder(request.side, request.quantity)
        trade = self.ib.placeOrder(contract, order)

        # Wait briefly for fill
        for _ in range(10):
            await asyncio.sleep(0.5)
            if trade.isDone():
                break

        if trade.fills:
            fill = trade.fills[0]
            return OrderResult(
                order_id=trade.order.orderId,
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                status="FILLED",
                fill_price=fill.execution.price,
                fill_time=datetime.now(),
            )

        status = trade.orderStatus.status if trade.orderStatus else "SUBMITTED"
        return OrderResult(
            order_id=trade.order.orderId,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            status=status,
        )

    async def get_market_price(self, symbol: str) -> float:
        contract = Stock(symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)

        ticker = self.ib.reqMktData(contract, snapshot=True)
        for _ in range(10):
            await asyncio.sleep(0.5)
            if ticker.last and ticker.last > 0:
                self.ib.cancelMktData(contract)
                return ticker.last
            if ticker.close and ticker.close > 0:
                self.ib.cancelMktData(contract)
                return ticker.close

        self.ib.cancelMktData(contract)
        raise ValueError(f"Could not get market price for {symbol}")

    async def cancel_all_orders(self) -> int:
        open_orders = self.ib.openOrders()
        count = len(open_orders)
        if count > 0:
            self.ib.reqGlobalCancel()
            logger.info(f"Cancelled {count} open orders")
        return count

    async def get_historical_bars(
        self,
        symbol: str,
        duration: str = "60 D",
        bar_size: str = "1 hour",
    ) -> list:
        """Fetch historical OHLCV bars from IB."""
        contract = Stock(symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)

        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        return bars
