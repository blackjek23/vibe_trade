"""Console/logging notifier — always available as fallback."""

from __future__ import annotations

import logging

from vibe_trade.notify.base import BaseNotifier

logger = logging.getLogger(__name__)


class ConsoleNotifier(BaseNotifier):
    async def notify_trade(self, message: str) -> None:
        logger.info(f"[TRADE] {message}")

    async def notify_summary(self, message: str) -> None:
        logger.info(f"[SUMMARY] {message}")

    async def notify_error(self, message: str) -> None:
        logger.error(f"[ERROR] {message}")

    async def notify_panic(self, message: str) -> None:
        logger.warning(f"[PANIC] {message}")
