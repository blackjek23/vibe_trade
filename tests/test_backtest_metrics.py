"""Tests for vibe_trade.backtest.metrics.compute_metrics.

Builds BacktestResult fixtures with hand-crafted equity curves + trade lists
and asserts each computed metric. No engine, no IB, no DB.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from vibe_trade.backtest.engine import BacktestResult, BacktestTrade
from vibe_trade.backtest.metrics import compute_metrics


def _curve(values: list[float], start: str = "2024-01-01") -> pd.Series:
    return pd.Series(
        values,
        index=pd.date_range(start, periods=len(values), freq="D"),
        dtype=float,
        name="equity",
    )


def _result(equity: list[float], trades: list[BacktestTrade] = None,
            *, starting: float | None = None,
            open_positions_at_end: int = 0) -> BacktestResult:
    eq = _curve(equity)
    start_eq = starting if starting is not None else (equity[0] if equity else 100_000.0)
    end_eq = equity[-1] if equity else start_eq
    return BacktestResult(
        starting_equity=start_eq,
        ending_equity=end_eq,
        equity_curve=eq,
        trades=trades or [],
        open_positions_at_end=open_positions_at_end,
    )


def _trade(symbol: str, entry: str, exit_: str, qty: int,
           entry_price: float, exit_price: float) -> BacktestTrade:
    return BacktestTrade(
        symbol=symbol,
        entry_date=date.fromisoformat(entry),
        entry_price=entry_price,
        exit_date=date.fromisoformat(exit_),
        exit_price=exit_price,
        qty=qty,
        pnl=(exit_price - entry_price) * qty,
    )


class TestEmpty:
    def test_empty_curve_returns_zeros(self):
        m = compute_metrics(_result([]))
        assert m.total_return_pct == 0.0
        assert m.n_trades == 0
        assert m.sharpe == 0.0


class TestReturns:
    def test_total_return_pct(self):
        # 100k -> 110k = 10% total return
        m = compute_metrics(_result([100_000.0, 105_000.0, 110_000.0]))
        assert abs(m.total_return_pct - 10.0) < 1e-6

    def test_cagr_for_one_year(self):
        # 100k -> 110k over ~365 days -> ~10% CAGR
        eq = [100_000.0] + [100_000.0] * 363 + [110_000.0]
        m = compute_metrics(_result(eq))
        assert abs(m.cagr_pct - 10.0) < 0.5  # close to 10%, allow rounding

    def test_negative_return(self):
        m = compute_metrics(_result([100_000.0, 90_000.0]))
        assert m.total_return_pct == pytest.approx(-10.0)


class TestDrawdown:
    def test_no_drawdown_when_monotonic_up(self):
        m = compute_metrics(_result([100.0, 110.0, 120.0, 130.0]))
        assert abs(m.max_drawdown_pct) < 1e-6

    def test_max_drawdown_finds_worst_dip(self):
        # 100 -> 120 (peak) -> 90 (max DD = -25% from peak) -> 110
        m = compute_metrics(_result([100.0, 120.0, 90.0, 110.0]))
        assert abs(m.max_drawdown_pct - (-25.0)) < 1e-6


class TestSharpe:
    def test_sharpe_zero_for_flat_curve(self):
        m = compute_metrics(_result([100.0] * 50))
        # std of returns = 0 -> sharpe forced to 0
        assert m.sharpe == 0.0

    def test_sharpe_positive_for_upward_curve(self):
        # Steady 0.1% daily growth -> very high sharpe
        eq = [100.0 * (1.001 ** i) for i in range(252)]
        m = compute_metrics(_result(eq))
        assert m.sharpe > 5.0

    def test_sharpe_negative_for_downward_curve(self):
        eq = [100.0 * (0.999 ** i) for i in range(252)]
        m = compute_metrics(_result(eq))
        assert m.sharpe < -5.0


class TestTrades:
    def test_win_rate(self):
        trades = [
            _trade("A", "2024-01-01", "2024-01-05", 10, 100.0, 110.0),  # +100 win
            _trade("B", "2024-01-02", "2024-01-06", 10, 100.0, 95.0),   # -50 loss
            _trade("C", "2024-01-03", "2024-01-07", 10, 100.0, 105.0),  # +50 win
        ]
        m = compute_metrics(_result([100_000.0, 100_000.0], trades=trades))
        assert m.n_trades == 3
        assert abs(m.win_rate - 2 / 3) < 1e-6

    def test_profit_factor(self):
        trades = [
            _trade("A", "2024-01-01", "2024-01-05", 10, 100.0, 110.0),  # gross_wins = 100
            _trade("B", "2024-01-02", "2024-01-06", 10, 100.0, 95.0),   # gross_losses = 50
        ]
        m = compute_metrics(_result([100_000.0, 100_000.0], trades=trades))
        assert abs(m.profit_factor - 2.0) < 1e-6

    def test_profit_factor_inf_when_no_losses(self):
        trades = [
            _trade("A", "2024-01-01", "2024-01-05", 10, 100.0, 110.0),
        ]
        m = compute_metrics(_result([100_000.0, 100_000.0], trades=trades))
        assert m.profit_factor == math.inf

    def test_avg_win_and_loss(self):
        trades = [
            _trade("A", "2024-01-01", "2024-01-05", 10, 100.0, 110.0),  # +100
            _trade("B", "2024-01-02", "2024-01-06", 10, 100.0, 120.0),  # +200
            _trade("C", "2024-01-03", "2024-01-07", 10, 100.0, 95.0),   # -50
        ]
        m = compute_metrics(_result([100_000.0, 100_000.0], trades=trades))
        assert abs(m.avg_win - 150.0) < 1e-6
        assert abs(m.avg_loss - (-50.0)) < 1e-6

    def test_avg_holding_days(self):
        trades = [
            _trade("A", "2024-01-01", "2024-01-11", 10, 100.0, 110.0),  # 10 days
            _trade("B", "2024-01-01", "2024-01-21", 10, 100.0, 120.0),  # 20 days
        ]
        m = compute_metrics(_result([100_000.0, 100_000.0], trades=trades))
        assert abs(m.avg_holding_days - 15.0) < 1e-6


class TestExposure:
    def test_open_position_at_end_counted(self):
        # No closed trades, but open_positions_at_end > 0 -> 100% exposure heuristic
        m = compute_metrics(_result(
            [100_000.0, 101_000.0, 102_000.0],
            open_positions_at_end=1,
        ))
        assert m.exposure_pct == 100.0

    def test_no_trades_no_open_no_exposure(self):
        m = compute_metrics(_result([100_000.0, 101_000.0]))
        assert m.exposure_pct == 0.0

    def test_partial_exposure(self):
        # 10-day curve. Trade open days 3-7 (5 days exposed out of 10).
        eq = [100_000.0] * 10
        trades = [
            _trade("A", "2024-01-03", "2024-01-07", 10, 100.0, 105.0),
        ]
        m = compute_metrics(_result(eq, trades=trades))
        # 5 days of overlap with the curve out of 10 -> 50%.
        assert abs(m.exposure_pct - 50.0) < 1e-6
