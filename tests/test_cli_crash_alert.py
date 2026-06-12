"""Tests for `cli._run_with_crash_alert` -- Bug #6 crash-alert wrapper.

Verifies that any uncaught exception inside a job's async body:
  1. Triggers a [CRITICAL] Telegram alert
  2. Re-raises so the process exits non-zero (cron picks this up)
  3. Does NOT swallow the real error even if notifier construction fails

Pure unit tests against the wrapper -- no IB, no DB, no real Telegram.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vibe_trade.cli import _run_with_crash_alert


class _SyncConfig:
    """Just a plain object _get_notifier can call ``.telegram.enabled`` on."""

    def __init__(self, telegram_enabled: bool = True):
        self.telegram = MagicMock(enabled=telegram_enabled)


# --------------------------------------------------------------------- helpers
async def _coro_that_raises(config, exc: Exception) -> None:
    raise exc


def _coro_factory_raising(exc: Exception):
    """Build a coro_factory(config) that always raises `exc`."""
    async def factory(config):
        raise exc
    return factory


def _coro_factory_ok():
    async def factory(config):
        return None
    return factory


# --------------------------------------------------------------------- tests
class TestCriticalAlertOnUncaughtException:
    def test_connection_refused_triggers_critical_alert(self):
        """Bug #6 main case: Gateway down -> ConnectionRefusedError.
        Wrapper must alert + re-raise.
        """
        notifier = MagicMock()
        notifier.notify_error = AsyncMock()
        config = _SyncConfig()

        with patch("vibe_trade.cli._get_notifier", return_value=notifier):
            with pytest.raises(ConnectionRefusedError):
                _run_with_crash_alert(
                    "submit", config,
                    _coro_factory_raising(ConnectionRefusedError("port 4002 down")),
                )

        # Exactly one alert, containing the [CRITICAL] tag + exception type + message
        notifier.notify_error.assert_called_once()
        msg = notifier.notify_error.call_args.args[0]
        assert "[CRITICAL]" in msg
        assert "submit" in msg
        assert "ConnectionRefusedError" in msg
        assert "port 4002 down" in msg

    def test_generic_exception_also_triggers_alert(self):
        """Wrapper is type-agnostic -- ANY uncaught exception alerts."""
        notifier = MagicMock()
        notifier.notify_error = AsyncMock()
        config = _SyncConfig()

        with patch("vibe_trade.cli._get_notifier", return_value=notifier):
            with pytest.raises(RuntimeError):
                _run_with_crash_alert(
                    "reconcile", config,
                    _coro_factory_raising(RuntimeError("DB constraint violation")),
                )

        notifier.notify_error.assert_called_once()
        msg = notifier.notify_error.call_args.args[0]
        assert "[CRITICAL]" in msg
        assert "reconcile" in msg
        assert "RuntimeError" in msg


class TestSuccessfulRunSendsNoAlert:
    def test_success_path_no_critical_alert(self):
        """Happy path: coro returns cleanly, no alert fired."""
        notifier = MagicMock()
        notifier.notify_error = AsyncMock()
        config = _SyncConfig()

        with patch("vibe_trade.cli._get_notifier", return_value=notifier):
            _run_with_crash_alert("submit", config, _coro_factory_ok())

        notifier.notify_error.assert_not_called()


class TestLiveModeBanner:
    """Every job announces live (real-money) mode loudly; paper stays quiet."""

    def test_live_mode_logs_warning(self, caplog):
        import logging

        from vibe_trade.config import AppConfig, GeneralConfig

        config = AppConfig(general=GeneralConfig(mode="live"))
        with caplog.at_level(logging.WARNING, logger="vibe_trade.cli"):
            _run_with_crash_alert("submit", config, _coro_factory_ok())
        assert any("LIVE TRADING MODE" in r.message for r in caplog.records)

    def test_paper_mode_no_banner(self, caplog):
        import logging

        from vibe_trade.config import AppConfig

        config = AppConfig()  # mode defaults to paper
        with caplog.at_level(logging.WARNING, logger="vibe_trade.cli"):
            _run_with_crash_alert("submit", config, _coro_factory_ok())
        assert not any("LIVE TRADING MODE" in r.message for r in caplog.records)


class TestNotifierFailureDoesNotSwallowRealError:
    def test_notifier_construction_failure_propagates_real_error(self):
        """If _get_notifier itself raises during the alert path, we still
        re-raise the ORIGINAL exception (not the notifier's), and the user
        sees the real crash via the logger.
        """
        config = _SyncConfig()

        real_error = ValueError("real broker bug")

        # Patch _get_notifier to raise -- simulating misconfigured Telegram creds
        # or a network blip at exactly the wrong moment.
        with patch(
            "vibe_trade.cli._get_notifier",
            side_effect=RuntimeError("notifier ctor crashed"),
        ):
            # Should still raise the ORIGINAL ValueError, not the RuntimeError.
            with pytest.raises(ValueError, match="real broker bug"):
                _run_with_crash_alert(
                    "submit", config, _coro_factory_raising(real_error),
                )

    def test_notify_error_send_failure_still_propagates_real_error(self):
        """notify_error() itself failing (e.g. Telegram API down) must not
        shadow the real exception."""
        notifier = MagicMock()
        notifier.notify_error = AsyncMock(side_effect=Exception("telegram 503"))
        config = _SyncConfig()

        real_error = ConnectionRefusedError("port 4002")

        with patch("vibe_trade.cli._get_notifier", return_value=notifier):
            with pytest.raises(ConnectionRefusedError, match="port 4002"):
                _run_with_crash_alert(
                    "record", config, _coro_factory_raising(real_error),
                )

        notifier.notify_error.assert_called_once()  # we tried
