"""Abstract notifier interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseNotifier(ABC):
    @abstractmethod
    async def notify_trade(self, message: str) -> None:
        """Send a trade notification."""

    @abstractmethod
    async def notify_summary(self, message: str) -> None:
        """Send a scan summary."""

    @abstractmethod
    async def notify_error(self, message: str) -> None:
        """Send an error notification."""

    @abstractmethod
    async def notify_panic(self, message: str) -> None:
        """Send a panic button notification."""
