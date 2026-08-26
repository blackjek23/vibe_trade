"""Tests for the preflight job (`vibe_trade.jobs.preflight.run_preflight`).

Preflight exists because on 10 trading days between 2026-05 and 2026-07 nothing
ran — IB Gateway wasn't up. The crash alert did fire, but at 16:00 when the
window had already opened. These tests pin the two behaviours that matter:

- every check runs even when an earlier one fails, so the report is complete
- a *connected but not-yet-populated* Gateway (net_liq = 0) reads as NOT READY,
  because that is the mid-login state that would silently break submit
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from vibe_trade.broker.models import AccountSummary, Position
from vibe_trade.config import RiskConfig, StrategyConfig
from vibe_trade.jobs.preflight import run_preflight
from vibe_trade.strategy.registry import build_strategies


class MockBroker:
    def __init__(self, *, account=None, positions=None,
                 account_exc=None, positions_exc=None):
        self._account = account or AccountSummary(
            account_id="DU000001", net_liquidation=100_000.0, total_cash=40_000.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
        )
        self._positions = positions or []
        self._account_exc = account_exc
        self._positions_exc = positions_exc

    async def get_account_summary(self):
        if self._account_exc:
            raise self._account_exc
        return self._account

    async def get_positions(self):
        if self._positions_exc:
            raise self._positions_exc
        return list(self._positions)


def _strategies():
    return build_strategies(
        [StrategyConfig(id="donchian", enabled=True, params={"period": 20})],
        RiskConfig().pct_per_position,
    )


def _pos(symbol: str, qty: int = 5) -> Position:
    return Position(symbol, qty, 100.0, 110.0, 110.0 * qty, 10.0 * qty)


async def _run(broker, *, universe=None, strategies=None, max_positions=50, **kw):
    return await run_preflight(
        broker=broker,
        universe=universe if universe is not None else ["AAPL", "MSFT"],
        strategies=_strategies() if strategies is None else strategies,
        max_positions=max_positions,
        **kw,
    )


class TestHappyPath:
    async def test_all_checks_pass(self):
        result = await _run(MockBroker(positions=[_pos("AAPL")]))
        assert result.ok
        assert result.failures == []
        assert result.account_id == "DU000001"
        assert result.net_liquidation == pytest.approx(100_000.0)
        assert result.held_count == 1
        assert result.universe_size == 2
        assert result.strategy_names == ["donchian"]

    async def test_shorts_and_zero_rows_excluded_from_held(self):
        broker = MockBroker(positions=[_pos("AAPL", 5), _pos("F", 0), _pos("X", -3)])
        result = await _run(broker)
        assert result.held_count == 1
        assert result.ok


class TestGatewayNotReady:
    async def test_zero_net_liq_is_not_ready(self):
        """Connected but no account loaded — Gateway mid-login."""
        broker = MockBroker(account=AccountSummary(
            account_id="", net_liquidation=0.0, total_cash=0.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
        ))
        result = await _run(broker)
        assert not result.ok
        names = [c.name for c in result.failures]
        assert "ib_account" in names
        assert "logging in" in dict((c.name, c.detail) for c in result.failures)["ib_account"]

    async def test_account_read_exception_is_captured_not_raised(self):
        broker = MockBroker(account_exc=TimeoutError("accountSummary timed out"))
        result = await _run(broker)
        assert not result.ok
        assert any(c.name == "ib_account" and "TimeoutError" in c.detail
                   for c in result.failures)

    async def test_all_checks_still_run_after_an_early_failure(self):
        """A complete report beats a fast one — we want every problem at once."""
        broker = MockBroker(account_exc=RuntimeError("boom"),
                            positions_exc=RuntimeError("also boom"))
        result = await _run(broker, universe=[])
        names = [c.name for c in result.checks]
        assert "ib_account" in names
        assert "ib_positions" in names
        assert "universe" in names
        assert "strategies" in names

    async def test_custom_min_net_liq_threshold(self):
        broker = MockBroker(account=AccountSummary(
            account_id="DU1", net_liquidation=500.0, total_cash=0.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
        ))
        assert (await _run(broker)).ok  # default threshold is 1.0
        strict = await _run(broker, min_net_liquidation=1000.0)
        assert not strict.ok


class TestConfigProblems:
    async def test_empty_universe_fails(self):
        result = await _run(MockBroker(), universe=[])
        assert not result.ok
        assert any(c.name == "universe" and "EMPTY" in c.detail
                   for c in result.failures)

    async def test_no_strategies_fails(self):
        result = await _run(MockBroker(), strategies=[])
        assert not result.ok
        assert any(c.name == "strategies" for c in result.failures)


class TestPositionCap:
    async def test_over_cap_is_flagged(self):
        broker = MockBroker(positions=[_pos(f"S{i}") for i in range(52)])
        result = await _run(broker, max_positions=50)
        assert not result.ok
        cap = next(c for c in result.failures if c.name == "position_cap")
        assert "force-trim 2" in cap.detail

    async def test_at_cap_exactly_is_ok(self):
        broker = MockBroker(positions=[_pos(f"S{i}") for i in range(50)])
        result = await _run(broker, max_positions=50)
        assert result.ok


class TestMarketSessionCheck:
    """H-4: preflight reports today's US market-calendar status, but never
    fails on it -- a holiday is a valid day to do nothing, not a problem.
    """

    async def test_trading_day_reports_ok(self):
        now = datetime(2026, 3, 9, 10, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))  # Monday
        result = await _run(MockBroker(), now=now)
        assert result.ok
        check = next(c for c in result.checks if c.name == "market_session")
        assert check.ok
        assert "2026-03-09" in check.detail
        assert "open" in check.detail

    async def test_holiday_still_reports_ok_but_flags_closed(self):
        now = datetime(2026, 11, 26, 10, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))  # Thanksgiving
        result = await _run(MockBroker(), now=now)
        assert result.ok  # informational only -- must not fail preflight
        check = next(c for c in result.checks if c.name == "market_session")
        assert check.ok
        assert "CLOSED" in check.detail


class TestAccountModeMatch:
    """SEC-2: flag when the account IB Gateway is serving doesn't match the
    configured mode. See PROJECT_EVALUATION.md.
    """

    async def test_paper_mode_with_paper_account_passes(self):
        result = await _run(MockBroker(), mode="paper")
        assert result.ok
        names = [c.name for c in result.checks]
        assert "account_mode_match" in names

    async def test_paper_mode_with_live_account_fails(self):
        broker = MockBroker(account=AccountSummary(
            account_id="U1234567", net_liquidation=100_000.0, total_cash=100_000.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
        ))
        result = await _run(broker, mode="paper")
        assert not result.ok
        mismatch = next(c for c in result.failures if c.name == "account_mode_match")
        assert "live" in mismatch.detail

    async def test_live_mode_with_paper_account_fails(self):
        result = await _run(MockBroker(), mode="live")
        assert not result.ok
        mismatch = next(c for c in result.failures if c.name == "account_mode_match")
        assert "paper" in mismatch.detail

    async def test_live_mode_with_live_account_passes(self):
        broker = MockBroker(account=AccountSummary(
            account_id="U1234567", net_liquidation=100_000.0, total_cash=100_000.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
        ))
        result = await _run(broker, mode="live")
        assert result.ok

    async def test_empty_account_id_skips_the_check_not_fails_it(self):
        """Gateway mid-login (empty id) is already caught by ib_account --
        this check must not pile on a confusing second failure for it.
        """
        broker = MockBroker(account=AccountSummary(
            account_id="", net_liquidation=0.0, total_cash=0.0,
            unrealized_pnl=0.0, realized_pnl=0.0,
        ))
        result = await _run(broker)
        names = [c.name for c in result.checks]
        assert "account_mode_match" not in names
