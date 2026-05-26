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
