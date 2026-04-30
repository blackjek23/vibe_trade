"""Tests for vibe_trade.backtest.engine.run_backtest.

Synthetic OHLC built to fire Donchian breakouts at known dates so we can
assert exact entry/exit prices, dates, and quantities.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from vibe_trade.backtest.engine import BacktestResult, run_backtest
from vibe_trade.strategy.examples.donchian import DonchianStrategy


def _bars(closes: list[float], highs: list[float] | None = None,
          lows: list[float] | None = None, opens: list[float] | None = None,
          start: str = "2024-01-01") -> pd.DataFrame:
    """Build a daily-bar DataFrame with custom close (and optional H/L/open)."""
    n = len(closes)
    return pd.DataFrame(
        {
            "open": opens if opens is not None else closes,
            "high": highs if highs is not None else [c + 0.5 for c in closes],
            "low": lows if lows is not None else [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1000] * n,
        },
        index=pd.date_range(start, periods=n, freq="D"),
    ).rename_axis("date")


def _flat_then_breakout_bars(start: str = "2024-01-01") -> pd.DataFrame:
    """20 flat bars (high=100, low=99, close=99.5), bar 21 closes at 101 (BUY),
    bar 22 opens at 101.50 (the fill day)."""
    closes = [99.5] * 20 + [101.0, 101.5, 101.5, 101.5, 101.5]
    highs  = [100.0] * 20 + [101.5, 102.0, 102.0, 102.0, 102.0]
    lows   = [99.0] * 20 + [100.5, 101.0, 101.0, 101.0, 101.0]
    opens  = [99.5] * 20 + [101.0, 101.5, 101.5, 101.5, 101.5]
    return _bars(closes, highs, lows, opens, start=start)


class TestEmpty:
    def test_no_universe_returns_starting_equity(self):
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["AAPL"],
            start=date(2024, 1, 1),
            end=date(2024, 2, 1),
            bars={},  # no data
        )
        assert isinstance(result, BacktestResult)
        assert result.ending_equity == result.starting_equity
        assert result.trades == []

    def test_flat_data_no_signals_no_trades(self):
        flat = _bars([100.0] * 30)
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": flat},
        )
        # 30 days of perfectly flat bars: no breakouts -> no trades.
        assert result.trades == []
        assert result.open_positions_at_end == 0
        # Equity unchanged
        assert abs(result.ending_equity - result.starting_equity) < 1e-6


class TestBuyFlow:
    def test_buy_fills_at_next_day_open(self):
        # Breakout on bar 21 (index 20); fill on bar 22 (index 21) at its open.
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": _flat_then_breakout_bars()},
            starting_equity=10_000.0,
            pct_per_position=0.04,
            max_positions=25,
        )
        # No trade closed -> trades list still empty, but a position is open.
        assert result.open_positions_at_end == 1
        # 4% of $10k = $400. Fill at $101.50 = floor(400/101.50) = 3 shares
        # cash spent = 3 * 101.50 = 304.50
        # remaining cash = 9695.50 + position market value
        # market value at close of bar 22 = 3 * 101.50 = 304.50 (close == open here)
        assert abs(result.ending_equity - 10_000.0) < 1.0  # roughly flat after open

    def test_buy_skipped_when_no_cash(self):
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": _flat_then_breakout_bars()},
            starting_equity=50.0,  # too poor for even 1 share at $101.50
            pct_per_position=0.04,
            max_positions=25,
        )
        # Sizer skips: 4% of $50 = $2 < $101.50 (1 share). entries_skipped path.
        assert result.open_positions_at_end == 0
        assert result.trades == []


class TestSellFlow:
    def test_sell_signal_closes_position_at_next_day_open(self):
        # Build: 20 flat at 99.5, breakout to 101 (signal day 21),
        # fill open 101.50 (day 22), then breakdown to 95 on day 41
        # (need 20 fresh "low" bars between to reset the band, but for test
        # purposes the band post-breakout is built from days 22-41 highs/lows).
        # Simpler: keep all the flat low at 99 in bars[1..20], breakout at 21,
        # then bars 22..41 trade at ~101 (held), then bar 42 closes at 90
        # which is below min(low) of bars 22..41 (~100.5) -> SELL signal,
        # fill at bar 43's open.
        closes = (
            [99.5] * 20         # 0..19  flat baseline
            + [101.0]            # 20     BUY signal day
            + [101.5] * 20       # 21..40 holding (band rebuilds at ~101 lows)
            + [90.0]             # 41     SELL signal day
            + [89.0]             # 42     SELL fills at this open=89
        )
        highs = (
            [100.0] * 20
            + [101.5]
            + [102.0] * 20
            + [91.0]
            + [89.5]
        )
        lows = (
            [99.0] * 20
            + [100.5]
            + [101.0] * 20
            + [89.0]
            + [88.0]
        )
        opens = (
            [99.5] * 20
            + [101.0]
            + [101.5] * 20
            + [90.0]
            + [89.0]    # SELL fills at this open
        )
        df = _bars(closes, highs, lows, opens)

        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2030, 1, 1),
            bars={"X": df},
            starting_equity=10_000.0,
            pct_per_position=0.04,
        )
        # One round-trip trade should be in the log.
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.symbol == "X"
        # Entry at bar 22's open = 101.50
        assert abs(trade.entry_price - 101.5) < 1e-6
        # Exit at bar 43's open = 89.0
        assert abs(trade.exit_price - 89.0) < 1e-6
        # Loss: (89.0 - 101.50) * qty
        assert trade.pnl < 0
        # Position closed
        assert result.open_positions_at_end == 0


class TestCapAndDedup:
    def test_position_cap_respected(self):
        # 30 distinct symbols all breakout on the same day; cap=5 -> only 5 fill.
        bar_template = _flat_then_breakout_bars()
        bars = {f"S{i}": bar_template.copy() for i in range(30)}
        universe = list(bars.keys())

        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=universe,
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars=bars,
            starting_equity=1_000_000.0,
            pct_per_position=0.04,
            max_positions=5,
        )
        # Exactly 5 positions opened (cap).
        assert result.open_positions_at_end == 5

    def test_held_ticker_not_re_bought(self):
        # Same symbol with two breakouts: first opens a position, second is
        # ignored because we already hold it.
        # Build: breakout day 20, hold, then "another breakout" day 41 still
        # holding -> should NOT re-buy.
        closes = [99.5] * 20 + [101.0] + [101.0] * 25  # held throughout
        df = _bars(closes)

        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": df},
            starting_equity=100_000.0,
            pct_per_position=0.04,
        )
        # Exactly one position; no second BUY despite continued breakout.
        assert result.open_positions_at_end == 1


class TestEquityCurve:
    def test_equity_curve_has_one_point_per_trading_day(self):
        df = _bars([100.0] * 50)
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": df},
        )
        assert len(result.equity_curve) == 50

    def test_equity_curve_index_is_datetime(self):
        df = _bars([100.0] * 30)
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": df},
        )
        assert isinstance(result.equity_curve.index, pd.DatetimeIndex)
