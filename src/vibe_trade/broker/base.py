"""Abstract broker interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from vibe_trade.broker.models import (
    AccountSummary,
    OpenOrder,
    OrderRequest,
    OrderResult,
    Position,
)


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

    @abstractmethod
    async def get_open_orders(self) -> list[OpenOrder]:
        """Return all working (unfilled) orders currently live at the broker."""

    async def get_today_order_refs(self) -> set[str]:
        """orderRefs seen at the broker today (working orders + fills).

        Powers submit's double-run guard: if a strategy ref is already present
        at IB, submit ran today and re-running would duplicate orders.
        Non-abstract on purpose -- brokers/mocks without ref visibility inherit
        this empty default, which turns the guard into a no-op.
        """
        return set()

    @abstractmethod
    async def cancel_orders_for_symbol(self, symbol: str) -> list[OpenOrder]:
        """Cancel every working order for `symbol`. Returns what was cancelled."""
