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


from datetime import date


def test_format_submit_msg_normal():
    from vibe_trade.cli import _format_submit_msg
    from vibe_trade.jobs.submit import SubmitResult

    result = SubmitResult(
        universe_size=100, held_count=5,
        exits_evaluated=5, exits_signaled=2, exits_placed=2, exits_failed=0,
        entries_evaluated=95, entries_signaled=3, entries_placed=3,
        entries_skipped_sizing=0, entries_failed=0,
    )
    msg = _format_submit_msg(result, date(2026, 5, 3))
    assert "[SUBMIT] 2026-05-03" in msg
    assert "Exits:   2 placed, 0 failed" in msg
    assert "Entries: 3 placed, 0 failed" in msg
    assert "error" not in msg.lower()


def test_format_submit_msg_with_errors():
    from vibe_trade.cli import _format_submit_msg
    from vibe_trade.jobs.submit import SubmitResult

    result = SubmitResult(
        exits_placed=2, exits_failed=1, entries_placed=3,
        errors=["exit AAPL: TimeoutError(...)", "exit MSFT: ValueError(...)"],
    )
    msg = _format_submit_msg(result, date(2026, 5, 3))
    assert "2 error(s):" in msg
    assert "exit AAPL: TimeoutError" in msg
    assert "exit MSFT: ValueError" in msg


def test_format_submit_msg_entries_skipped():
    from vibe_trade.cli import _format_submit_msg
    from vibe_trade.jobs.submit import SubmitResult

    result = SubmitResult(
        exits_evaluated=3, exits_placed=1,
        entries_phase_skipped=True, cap_reason="At max positions (50)",
    )
    msg = _format_submit_msg(result, date(2026, 5, 3))
    assert "Entries phase skipped: At max positions (50)" in msg


def test_format_record_msg():
    from vibe_trade.cli import _format_record_msg
    from vibe_trade.jobs.record import RecordResult

    result = RecordResult(buys_inserted=3, sells_flipped=2)
    msg = _format_record_msg(result, date(2026, 5, 3))
    assert "[RECORD] 2026-05-03" in msg
    assert "3 BUYs recorded, 2 SELLs flipped" in msg


def test_format_record_msg_with_errors():
    from vibe_trade.cli import _format_record_msg
    from vibe_trade.jobs.record import RecordResult

    result = RecordResult(buys_inserted=1, errors=["perm_id=42: ValueError(...)"])
    msg = _format_record_msg(result, date(2026, 5, 3))
    assert "1 error(s):" in msg
    assert "perm_id=42" in msg


def _make_trade(symbol, side, qty, **kwargs):
    """Lightweight builder: a stand-in shaped like vibe_trade.db.models.Trade."""
    from types import SimpleNamespace
    return SimpleNamespace(symbol=symbol, side=side, filled_quantity=qty, **kwargs)


def test_format_reconcile_msg_with_trades():
    from types import SimpleNamespace
    from vibe_trade.cli import _format_reconcile_msg
    from vibe_trade.jobs.reconcile import ReconcileResult

    opened = [
        _make_trade("AAPL", "BUY", 10, pnl=None),
        _make_trade("MSFT", "BUY", 5, pnl=None),
    ]
    closed = [
        _make_trade("GOOGL", "BUY", 3, pnl=142.50),
        _make_trade("NVDA", "BUY", 2, pnl=-18.20),
    ]
    pnl = SimpleNamespace(realized_pnl=124.30, account_value=102_450.0)
    result = ReconcileResult(opened=2, closed=2)

    msg = _format_reconcile_msg(result, opened, closed, pnl, date(2026, 5, 3))
    assert "[DAILY SUMMARY] 2026-05-03" in msg
    assert "Opened: 2" in msg
    assert "Closed: 2" in msg
    assert "```" in msg
    assert "AAPL" in msg and "BUY" in msg
    assert "GOOGL" in msg and "+$142.50" in msg
    assert "NVDA" in msg and "-$18.20" in msg
    assert "Realized P&L: +$124.30" in msg
    assert "Account:" in msg and "$102,450.00" in msg


def test_format_reconcile_msg_no_trades():
    from vibe_trade.cli import _format_reconcile_msg
    from vibe_trade.jobs.reconcile import ReconcileResult

    result = ReconcileResult()
    msg = _format_reconcile_msg(result, [], [], None, date(2026, 5, 3))
    assert "No trades today." in msg


def test_format_reconcile_msg_errors():
    from vibe_trade.cli import _format_reconcile_msg
    from vibe_trade.jobs.reconcile import ReconcileResult

    result = ReconcileResult(errors=["perm_id=99: SomeError(...)"])
    msg = _format_reconcile_msg(result, [], [], None, date(2026, 5, 3))
    assert "1 error(s):" in msg
    assert "perm_id=99" in msg


def test_format_reconcile_msg_no_pnl_row():
    """When DailyPnL row is missing, omit totals lines but still render trades."""
    from vibe_trade.cli import _format_reconcile_msg
    from vibe_trade.jobs.reconcile import ReconcileResult

    closed = [_make_trade("X", "BUY", 1, pnl=10.0)]
    result = ReconcileResult(closed=1)
    msg = _format_reconcile_msg(result, [], closed, None, date(2026, 5, 3))
    assert "X" in msg
    assert "Realized P&L:" not in msg
    assert "Account:" not in msg


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
