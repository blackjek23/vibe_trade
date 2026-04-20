"""Abstract broker interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from vibe_trade.broker.models import AccountSummary, OrderRequest, OrderResult, Position


class BaseBroker(ABC):
    @abstractmethod
    async def connect(self) -> None:
        """Connect to the broker."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the broker."""

    @abstractmethod
    async def get_account_summary(self) -> AccountSummary:
        """Get current account state."""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Get all open positions."""

    @abstractmethod
    async def place_market_order(self, request: OrderRequest) -> OrderResult:
        """Place a market order."""

    @abstractmethod
    async def cancel_all_orders(self) -> int:
        """Cancel all open orders. Returns number cancelled."""
