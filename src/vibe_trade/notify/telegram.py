"""Telegram notifier — sends messages to a configured chat."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from telegram import Bot

from vibe_trade.config import TelegramConfig
from vibe_trade.notify.base import BaseNotifier

logger = logging.getLogger(__name__)


class TelegramNotifier(BaseNotifier):
    def __init__(self, config: TelegramConfig):
        self.config = config
        token = config.token or os.environ.get("VIBE_TRADE_TELEGRAM_TOKEN", "")
        chat_id = config.chat_id or os.environ.get("VIBE_TRADE_TELEGRAM_CHAT_ID", "")
        self.chat_id = chat_id
        self.bot = Bot(token=token) if token else None

    async def _send(self, message: str) -> None:
        if not self.bot or not self.chat_id:
            logger.warning("Telegram not configured, skipping notification")
            return
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=message)
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    async def notify_trade(self, message: str) -> None:
        if self.config.notify_on_trade:
            await self._send(f"[TRADE] {message}")

    async def notify_summary(self, message: str) -> None:
        if self.config.daily_summary:
            await self._send(message)

    async def notify_error(self, message: str) -> None:
        if self.config.notify_on_error:
            await self._send(f"[ERROR] {message}")

    async def notify_panic(self, message: str) -> None:
        await self._send(f"[PANIC] {message}")

    async def notify_report_image(self, image_path: Path, caption: str = "") -> None:
        if not self.bot or not self.chat_id:
            logger.warning("Telegram not configured, skipping report image")
            return
        try:
            with open(image_path, "rb") as photo:
                await self.bot.send_photo(
                    chat_id=self.chat_id, photo=photo, caption=caption or None,
                )
        except Exception as e:
            logger.error(f"Failed to send Telegram report image: {e}")
