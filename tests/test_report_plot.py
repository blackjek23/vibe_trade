"""Tests for vibe_trade.reports.plot.save_report_plot.

Verifies the weekly dashboard PNG is generated across data states (full,
empty, holdings-but-no-closed-trades) and that the filename encodes the
date + period. No network, no IB, no DB.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

# Plotting is an optional dependency (the `plot` extra). Skip cleanly when absent.
pytest.importorskip("matplotlib", reason="matplotlib not installed (optional 'plot' extra)")

from vibe_trade.reports.data import DailyRow, HoldingRow
from vibe_trade.reports.metrics import (
    compute_closed_trade_stats,
    compute_metrics,
)


def _row(d: date, av: float, pos: int | None = 50) -> DailyRow:
    return DailyRow(date=d, realized_pnl=0.0, unrealized_pnl=0.0,
                    account_value=av, open_positions_count=pos)


def _holding(symbol: str, unrealized: float) -> HoldingRow:
    return HoldingRow(
        symbol=symbol, quantity=10, avg_cost=100.0,
        market_price=100.0 + unrealized / 10, market_value=1000.0 + unrealized,
        unrealized_pnl=unrealized,
    )


def _call(tmp_path: Path, *, daily_rows, holdings, closed, today, outliers=None):
    from vibe_trade.reports.plot import save_report_plot

    return save_report_plot(
        metrics=compute_metrics(daily_rows),
        daily_rows=daily_rows,
        holdings=holdings,
        holdings_as_of=today if holdings else None,
        activity={today: len(holdings)},
        closed_stats=compute_closed_trade_stats(closed),
        outliers=outliers or set(),
        window_days=7,
        today=today,
        output_path=tmp_path,
        period_label="Weekly",
    )


class TestReportPlot:
    def test_png_created_full_data(self, tmp_path: Path):
        today = date(2026, 6, 6)
        rows = [_row(date(2026, 6, d), 100_000.0 + d * 200) for d in (1, 2, 3, 4, 5)]
        holdings = [_holding("AAPL", 350.0), _holding("MSFT", -120.0)]
        png = _call(tmp_path, daily_rows=rows, holdings=holdings, closed=[], today=today)
        assert png.exists()
        assert png.suffix == ".png"
        assert png.stat().st_size > 1000  # not a trivially empty file

    def test_filename_encodes_date_and_period(self, tmp_path: Path):
        today = date(2026, 6, 6)
        rows = [_row(date(2026, 6, d), 100_000.0) for d in (4, 5)]
        png = _call(tmp_path, daily_rows=rows, holdings=[], closed=[], today=today)
        assert png.name == "2026-06-06-weekly.png"

    def test_empty_window_still_produces_sentinel_png(self, tmp_path: Path):
        today = date(2026, 6, 6)
        png = _call(tmp_path, daily_rows=[], holdings=[], closed=[], today=today)
        assert png.exists()
        assert png.stat().st_size > 1000

    def test_holdings_without_closed_trades_renders(self, tmp_path: Path):
        today = date(2026, 6, 6)
        rows = [_row(date(2026, 6, d), 100_000.0 + d * 100) for d in (1, 2, 3, 4, 5)]
        holdings = [_holding("AAPL", 200.0), _holding("NVDA", -50.0)]
        png = _call(tmp_path, daily_rows=rows, holdings=holdings, closed=[], today=today)
        assert png.exists()

    def test_single_point_window_renders_without_crash(self, tmp_path: Path):
        # <2 equity points -> "insufficient data" branch must not raise.
        today = date(2026, 6, 6)
        rows = [_row(date(2026, 6, 5), 100_000.0)]
        png = _call(tmp_path, daily_rows=rows, holdings=[], closed=[], today=today)
        assert png.exists()

    def test_creates_output_dir_if_missing(self, tmp_path: Path):
        today = date(2026, 6, 6)
        rows = [_row(date(2026, 6, d), 100_000.0) for d in (4, 5)]
        nested = tmp_path / "reports" / "sub"
        png = _call(nested, daily_rows=rows, holdings=[], closed=[], today=today)
        assert png.exists()
        assert png.parent == nested
