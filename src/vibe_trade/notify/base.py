"""Abstract notifier interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


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

    async def notify_report_image(self, image_path: Path, caption: str = "") -> None:
        """Send a report image (e.g. the weekly PNG).

        Concrete default no-op so existing notifiers keep working; only
        notifiers that can deliver files (Telegram) override this.
        """
