"""Tests for vibe-trade report (reports/ module + CLI command)."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from vibe_trade.reports.data import ClosedTrade, DailyRow, HoldingRow
from vibe_trade.reports.metrics import (
    ClosedTradeStats,
    ReportMetrics,
    compute_closed_trade_stats,
    compute_metrics,
)


# ============================================================ compute_metrics


def _row(d: date, av: float | None, real: float = 0.0, unr: float = 0.0,
         pos: int | None = 50) -> DailyRow:
    return DailyRow(date=d, realized_pnl=real, unrealized_pnl=unr,
                    account_value=av, open_positions_count=pos)


def test_compute_metrics_empty_returns_zeroed_dataclass():
    m = compute_metrics([])
    assert m.sample_size == 0
    assert m.start_value is None
    assert m.end_value is None
    assert m.total_return_pct == 0.0
    assert m.cagr_pct is None
    assert m.sharpe == 0.0
    assert m.max_drawdown_pct == 0.0
    assert m.best_day_pnl == 0.0
    assert m.worst_day_pnl == 0.0


def test_compute_metrics_single_row_has_zero_return_no_nan():
    m = compute_metrics([_row(date(2026, 5, 1), 100_000.0)])
    assert m.sample_size == 1
    assert m.start_value == 100_000.0
    assert m.end_value == 100_000.0
    assert m.total_return_pct == 0.0
    assert m.cagr_pct is None  # span < 1 day
    assert m.sharpe == 0.0
    assert m.max_drawdown_pct == 0.0
    assert not math.isnan(m.sharpe)


def test_compute_metrics_flat_account_value_gives_zero_sharpe():
    rows = [_row(date(2026, 5, d), 100_000.0) for d in (1, 2, 3, 4, 5)]
    m = compute_metrics(rows)
    assert m.sharpe == 0.0
    assert m.total_return_pct == 0.0
    assert m.max_drawdown_pct == 0.0


def test_compute_metrics_drops_rows_with_null_account_value():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), None),  # should be skipped
        _row(date(2026, 5, 3), 101_000.0),
    ]
    m = compute_metrics(rows)
    assert m.sample_size == 2
    assert m.start_value == 100_000.0
    assert m.end_value == 101_000.0


def test_compute_metrics_monotonically_increasing_positive_sharpe_no_dd():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), 101_000.0),
        _row(date(2026, 5, 3), 102_000.0),
        _row(date(2026, 5, 4), 103_000.0),
        _row(date(2026, 5, 5), 104_000.0),
    ]
    m = compute_metrics(rows)
    assert m.total_return_pct > 0
    assert m.sharpe > 0
    assert m.max_drawdown_pct == 0.0
    assert m.max_dd_peak_date is None
    assert m.max_dd_trough_date is None


def test_compute_metrics_monotonically_decreasing_negative_return_and_dd():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), 99_000.0),
        _row(date(2026, 5, 3), 98_000.0),
        _row(date(2026, 5, 4), 97_000.0),
    ]
    m = compute_metrics(rows)
    assert m.total_return_pct < 0
    assert m.max_drawdown_pct < 0
    assert m.max_dd_peak_date == date(2026, 5, 1)
    assert m.max_dd_trough_date == date(2026, 5, 4)


def test_compute_metrics_drawdown_identifies_correct_peak_and_trough():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), 105_000.0),  # peak
        _row(date(2026, 5, 3), 102_000.0),
        _row(date(2026, 5, 4), 95_000.0),   # trough (worst from 105k peak)
        _row(date(2026, 5, 5), 98_000.0),
        _row(date(2026, 5, 6), 110_000.0),  # new high, drawdown resets
    ]
    m = compute_metrics(rows)
    # peak is the 105k bar on 5/2; trough is the 95k bar on 5/4
    assert m.max_dd_peak_date == date(2026, 5, 2)
    assert m.max_dd_trough_date == date(2026, 5, 4)
    assert m.max_drawdown_pct == pytest.approx((95_000 / 105_000 - 1) * 100, abs=1e-6)


def test_compute_metrics_best_worst_day_pnl_from_realized_plus_unrealized():
    rows = [
        _row(date(2026, 5, 1), 100_000.0, real=0.0, unr=500.0),   # +500
        _row(date(2026, 5, 2), 101_000.0, real=100.0, unr=900.0), # +1000 BEST
        _row(date(2026, 5, 3), 100_500.0, real=-50.0, unr=-450.0),# -500 WORST
        _row(date(2026, 5, 4), 100_800.0, real=0.0, unr=300.0),   # +300
    ]
    m = compute_metrics(rows)
    assert m.best_day_pnl == 1000.0
    assert m.worst_day_pnl == -500.0


def test_compute_metrics_known_fixture_sharpe_and_drawdown():
    # Hand-computed: account_value [100, 102, 101, 103, 105]
    # daily returns: 0.02, -0.00980392, 0.01980198, 0.01941748
    # mean = 0.01235389
    # variance (sample, N-1=3) = 2.182e-4 -> std = 0.014773
    # sharpe = mean/std * sqrt(252) = 0.8362 * 15.8745 ~= 13.27
    rows = [
        _row(date(2026, 5, 1), 100.0),
        _row(date(2026, 5, 2), 102.0),
        _row(date(2026, 5, 3), 101.0),
        _row(date(2026, 5, 4), 103.0),
        _row(date(2026, 5, 5), 105.0),
    ]
    m = compute_metrics(rows)
    assert m.sharpe == pytest.approx(13.27, abs=0.05)
    # drawdown: 101 vs peak 102 -> -0.98%
    assert m.max_drawdown_pct == pytest.approx(-0.9803921, abs=1e-4)
    # CAGR over 4 days, factor 1.05 -- huge annualized number
    assert m.cagr_pct is not None and m.cagr_pct > 100.0


# ============================================================ compute_closed_trade_stats


def _ct(symbol: str, entry: date, exit_: date, pnl: float) -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        entry_time=datetime.combine(entry, datetime.min.time()),
        exit_time=datetime.combine(exit_, datetime.min.time()),
        pnl=pnl,
        pnl_pct=None,
    )


def test_compute_closed_trade_stats_empty_returns_zeros_no_nan():
    s = compute_closed_trade_stats([])
    assert s.n == 0
    assert s.win_rate == 0.0
    assert s.avg_win == 0.0
    assert s.avg_loss == 0.0
    assert s.profit_factor == 0.0
    assert s.avg_holding_days == 0.0


def test_compute_closed_trade_stats_mixed_wins_and_losses():
    trades = [
        _ct("AAPL", date(2026, 5, 1), date(2026, 5, 11), pnl=+200.0),   # 10d
        _ct("MSFT", date(2026, 5, 2), date(2026, 5, 22), pnl=+400.0),   # 20d
        _ct("GOOG", date(2026, 5, 3), date(2026, 5, 18), pnl=-100.0),   # 15d
        _ct("AMZN", date(2026, 5, 4), date(2026, 5, 19), pnl=-300.0),   # 15d
    ]
    s = compute_closed_trade_stats(trades)
    assert s.n == 4
    assert s.win_rate == 0.5
    assert s.avg_win == 300.0           # (200+400)/2
    assert s.avg_loss == -200.0         # (-100 + -300)/2
    assert s.profit_factor == pytest.approx(600.0 / 400.0)
    assert s.avg_holding_days == pytest.approx((10 + 20 + 15 + 15) / 4)


def test_compute_closed_trade_stats_all_wins_profit_factor_inf():
    trades = [
        _ct("AAPL", date(2026, 5, 1), date(2026, 5, 5), pnl=+100.0),
        _ct("MSFT", date(2026, 5, 2), date(2026, 5, 6), pnl=+200.0),
    ]
    s = compute_closed_trade_stats(trades)
    assert s.n == 2
    assert s.win_rate == 1.0
    assert s.profit_factor == float("inf")
