"""Tests for Session F: notification wiring and logging upgrade.

These tests are unit-level and require no IB connection or DB.
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from vibe_trade.config import AppConfig, TelegramConfig
from vibe_trade.notify.console import ConsoleNotifier
from vibe_trade.notify.telegram import TelegramNotifier


def test_get_notifier_returns_console_when_disabled():
    from vibe_trade.cli import _get_notifier

    config = AppConfig()
    config.telegram.enabled = False
    notifier = _get_notifier(config)
    assert isinstance(notifier, ConsoleNotifier)


def test_get_notifier_returns_telegram_when_enabled():
    from vibe_trade.cli import _get_notifier

    config = AppConfig()
    config.telegram.enabled = True
    config.telegram.token = "FAKE_TOKEN"
    config.telegram.chat_id = "12345"
    notifier = _get_notifier(config)
    assert isinstance(notifier, TelegramNotifier)
