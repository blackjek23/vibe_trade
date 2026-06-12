"""Interactive Brokers implementation via ib_async."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from ib_async import IB, Contract, MarketOrder, Stock

from vibe_trade.broker.base import BaseBroker
from vibe_trade.broker.models import (
    AccountSummary,
    OpenOrder,
    OrderRequest,
    OrderResult,
    Position,
)
from vibe_trade.config import BrokerConfig

logger = logging.getLogger(__name__)


class IBBroker(BaseBroker):
    def __init__(self, config: BrokerConfig, mode: str = "paper"):
        self.config = config
        self.mode = mode
        self.ib = IB()
        self._contract_cache: dict[str, Contract] = {}

    async def connect(self) -> None:
        port = self.config.get_port(self.mode)
        max_attempts = self.config.connect_retries + 1
        backoff = self.config.retry_backoff_seconds

        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    f"Connecting to IB on {self.config.host}:{port} "
                    f"(mode={self.mode}, attempt {attempt}/{max_attempts})"
                )
                await self.ib.connectAsync(
                    host=self.config.host,
                    port=port,
                    clientId=self.config.client_id,
                    timeout=self.config.timeout,
                    account=self.config.account or "",
                )
                logger.info("Connected to IB")
                return
            except Exception as e:
                if attempt >= max_attempts:
                    logger.error(f"Failed to connect to IB after {max_attempts} attempts: {e}")
                    raise
                wait = backoff * (2 ** (attempt - 1))
                logger.warning(
                    f"Connect attempt {attempt}/{max_attempts} failed ({e!s}); "
                    f"retrying in {wait:.1f}s..."
                )
                await asyncio.sleep(wait)

    async def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
            logger.info("Disconnected from IB")
        self._contract_cache.clear()

    async def get_account_summary(self) -> AccountSummary:
        account_values = await self.ib.accountSummaryAsync()
        values: dict[str, float] = {}
        account_id = ""
        for av in account_values:
            account_id = av.account
            if av.tag in (
                "NetLiquidation",
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
            total_cash=values.get("TotalCashValue", 0.0),
            unrealized_pnl=values.get("UnrealizedPnL", 0.0),
            realized_pnl=values.get("RealizedPnL", 0.0),
        )

    async def get_positions(self) -> list[Position]:
        # Use portfolio() instead of positions() — it includes live market data
        # (marketPrice, marketValue, unrealizedPNL) populated by IB's account update stream.
        portfolio_items = self.ib.portfolio()
        positions = []
        for item in portfolio_items:
            positions.append(
                Position(
                    symbol=item.contract.symbol,
                    quantity=int(item.position),
                    avg_cost=item.averageCost,
                    market_price=item.marketPrice,
                    market_value=item.marketValue,
                    unrealized_pnl=item.unrealizedPNL,
                )
            )
        return positions

    async def _pace(self) -> None:
        """Sleep between IB-hitting calls to stay under pacing limits."""
        if self.config.order_pacing_seconds > 0:
            await asyncio.sleep(self.config.order_pacing_seconds)

    async def _get_qualified_contract(self, symbol: str) -> Contract:
        cached = self._contract_cache.get(symbol)
        if cached is not None:
            return cached
        contract = Stock(symbol, "SMART", "USD")
        await self.ib.qualifyContractsAsync(contract)
        await self._pace()
        self._contract_cache[symbol] = contract
        return contract

    async def place_market_order(self, request: OrderRequest) -> OrderResult:
        contract = await self._get_qualified_contract(request.symbol)

        order = MarketOrder(request.side, request.quantity)
        if request.order_ref:
            order.orderRef = request.order_ref
        trade = self.ib.placeOrder(contract, order)
        await self._pace()

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

    async def cancel_all_orders(self) -> int:
        open_orders = self.ib.openOrders()
        count = len(open_orders)
        if count > 0:
            self.ib.reqGlobalCancel()
            logger.info(f"Cancelled {count} open orders")
        return count

    @staticmethod
    def _to_open_order(trade) -> OpenOrder:
        """Map an ib_async Trade to our OpenOrder model."""
        return OpenOrder(
            symbol=trade.contract.symbol,
            side=trade.order.action,
            quantity=int(trade.order.totalQuantity),
            perm_id=trade.order.permId,
            status=trade.orderStatus.status if trade.orderStatus else "",
        )

    async def get_open_orders(self) -> list[OpenOrder]:
        # reqAllOpenOrders pulls orders from EVERY API client; without it
        # ib.openTrades() is scoped to this connection, so an override command
        # (client 4) cannot see orders placed by submit (client 1).
        await self.ib.reqAllOpenOrdersAsync()
        # openTrades() carries contract + order + orderStatus together;
        # openOrders() alone loses the contract symbol.
        return [self._to_open_order(t) for t in self.ib.openTrades()]

    async def get_today_order_refs(self) -> set[str]:
        """orderRefs of today's fills + working orders, across all clients.

        ib.fills() is account-wide (verified live: record/client 2 sees
        submit/client 1 fills); reqAllOpenOrders covers cross-client working
        orders. Feeds submit's double-run guard.
        """
        await self.ib.reqAllOpenOrdersAsync()
        refs: set[str] = set()
        for t in self.ib.openTrades():
            ref = (getattr(t.order, "orderRef", "") or "").strip()
            if ref:
                refs.add(ref)
        for f in self.ib.fills():
            ref = (getattr(f.execution, "orderRef", "") or "").strip()
            if ref:
                refs.add(ref)
        return refs

    async def cancel_orders_for_symbol(self, symbol: str) -> list[OpenOrder]:
        # Refresh across clients first (see get_open_orders) so a cross-client
        # order is visible to cancel.
        await self.ib.reqAllOpenOrdersAsync()
        cancelled: list[OpenOrder] = []
        for trade in self.ib.openTrades():
            if trade.contract.symbol != symbol:
                continue
            self.ib.cancelOrder(trade.order)
            cancelled.append(self._to_open_order(trade))
        if cancelled:
            logger.info(f"Cancelled {len(cancelled)} order(s) for {symbol}")
        return cancelled
