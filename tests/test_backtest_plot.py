"""Tests for vibe_trade.backtest.plot.save_backtest_plot.

Verifies PNG generation with and without benchmarks. No network, no IB.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


def _equity(values: list[float], start: str = "2024-01-01") -> pd.Series:
    return pd.Series(
        values,
        index=pd.date_range(start, periods=len(values), freq="D"),
        dtype=float,
        name="equity",
    )


def _bench_closes(values: list[float], start: str = "2024-01-01") -> pd.Series:
    return pd.Series(
        values,
        index=pd.date_range(start, periods=len(values), freq="D"),
        dtype=float,
    )


class TestPlot:
    def test_png_created(self, tmp_path: Path):
        from vibe_trade.backtest.plot import save_backtest_plot

        eq = _equity([100_000.0, 105_000.0, 110_000.0, 108_000.0, 112_000.0])
        benchmarks = {
            "SPY": _bench_closes([300.0, 305.0, 310.0, 308.0, 315.0]),
        }
        png = save_backtest_plot(eq, benchmarks, tmp_path, starting_equity=100_000.0)
        assert png.exists()
        assert png.suffix == ".png"
        assert png.stat().st_size > 1000  # not a trivially empty file

    def test_png_without_benchmarks(self, tmp_path: Path):
        from vibe_trade.backtest.plot import save_backtest_plot

        eq = _equity([100_000.0, 102_000.0, 104_000.0])
        png = save_backtest_plot(eq, {}, tmp_path, starting_equity=100_000.0)
        assert png.exists()

    def test_png_with_two_benchmarks(self, tmp_path: Path):
        from vibe_trade.backtest.plot import save_backtest_plot

        eq = _equity([100_000.0, 105_000.0, 110_000.0])
        benchmarks = {
            "SPY": _bench_closes([300.0, 305.0, 310.0]),
            "QQQ": _bench_closes([400.0, 410.0, 420.0]),
        }
        png = save_backtest_plot(eq, benchmarks, tmp_path, starting_equity=100_000.0)
        assert png.exists()
        assert png.stat().st_size > 1000
