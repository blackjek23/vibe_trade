"""Tests for the `backtest --strategy` selector (Session L).

The selector resolves the strategy via the registry before any bar fetch, so an
unknown id fails fast with no network access.
"""

from __future__ import annotations

from typer.testing import CliRunner

from vibe_trade.cli import app

runner = CliRunner()


def test_backtest_rejects_unknown_strategy():
    result = runner.invoke(
        app,
        ["backtest", "--start", "2024-01-01", "--end", "2024-02-01",
         "--strategy", "bogus"],
    )
    assert result.exit_code != 0
    combined = result.output + str(result.exception)
    assert "Unknown strategy id" in combined


def test_backtest_accepts_known_strategy_ids():
    # The registry must know every Session L strategy the selector advertises.
    from vibe_trade.strategy.registry import STRATEGY_FACTORIES

    for sid in ("donchian", "sma", "ema", "macd"):
        assert sid in STRATEGY_FACTORIES
