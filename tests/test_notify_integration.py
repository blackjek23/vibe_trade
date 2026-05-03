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


def test_setup_logging_stream_handler_is_plain_text(tmp_path):
    from vibe_trade.cli import _setup_logging

    log_file = tmp_path / "vibe_trade.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    _setup_logging("INFO", str(log_file))

    stream_handlers = [
        h for h in root.handlers if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]
    assert len(stream_handlers) == 1
    fmt_str = stream_handlers[0].formatter._fmt
    assert "%(asctime)s" in fmt_str
    assert "%(message)s" in fmt_str
    assert "{" not in fmt_str

    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)


def test_setup_logging_file_handler_is_rotating(tmp_path):
    from vibe_trade.cli import _setup_logging

    log_file = tmp_path / "vibe_trade.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    _setup_logging("INFO", str(log_file))

    file_handlers = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert handler.when == "MIDNIGHT"
    assert handler.backupCount == 7

    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)


def test_setup_logging_file_handler_emits_json(tmp_path):
    import json
    from vibe_trade.cli import _setup_logging

    log_file = tmp_path / "vibe_trade.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    _setup_logging("INFO", str(log_file))
    logging.getLogger("test").info("hello world")

    for h in list(root.handlers):
        h.flush()
        h.close()
        root.removeHandler(h)

    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["message"] == "hello world"
    assert "time" in payload


def test_setup_logging_no_file_when_log_file_none():
    from vibe_trade.cli import _setup_logging

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    _setup_logging("INFO", None)

    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers == []

    for h in list(root.handlers):
        h.close()
        root.removeHandler(h)
