"""Tests for Donchian Channel breakout strategy.

Locked spec (V2 first iteration):
- N=20, symmetric, EXCLUDING the bar being evaluated
- BUY when close[-1] > max(high[-21:-1])
- SELL when close[-1] < min(low[-21:-1])
- HOLD on ties or inside the band
"""

from __future__ import annotations

import pandas as pd
import pytest

from vibe_trade.strategy.base import SignalType
from vibe_trade.strategy.examples.donchian import DonchianStrategy


def _candles(highs: list[float], lows: list[float], closes: list[float]) -> pd.DataFrame:
    """Build a minimal OHLC DataFrame for tests. open + volume are filler."""
    n = len(closes)
    assert len(highs) == n and len(lows) == n
    return pd.DataFrame(
        {
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000] * n,
        },
        index=pd.date_range("2026-01-01", periods=n, freq="D"),
    )


def _flat_then_breakout(period: int, breakout_close: float) -> pd.DataFrame:
    """N flat bars at $100 high / $99 low, then 1 evaluation bar with given close."""
    highs = [100.0] * period + [breakout_close]
    lows = [99.0] * period + [breakout_close]
    closes = [99.5] * period + [breakout_close]
    return _candles(highs, lows, closes)


class TestNameAndRequired:
    def test_name(self):
        assert DonchianStrategy().name == "donchian"

    def test_default_required_candles_is_21(self):
        # period 20 + 1 evaluation bar
        assert DonchianStrategy().required_candles == 21

    def test_custom_period_required(self):
        assert DonchianStrategy(period=10).required_candles == 11

    def test_period_must_be_positive(self):
        with pytest.raises(ValueError):
            DonchianStrategy(period=0)


class TestBuySignal:
    def test_breakout_above_prior_high(self):
        # Prior 20 highs all 100. Close at 101 > 100 -> BUY.
        candles = _flat_then_breakout(period=20, breakout_close=101.0)
        result = DonchianStrategy().evaluate("AAPL", candles)
        assert result.signal == SignalType.BUY
        assert result.symbol == "AAPL"
        assert result.strategy_name == "donchian"
        assert result.metadata["upper"] == 100.0
        assert result.metadata["close"] == 101.0

    def test_breakout_just_above_band(self):
        # 100.01 > 100.00 -> BUY (strict >, not >=).
        candles = _flat_then_breakout(period=20, breakout_close=100.01)
        assert DonchianStrategy().evaluate("X", candles).signal == SignalType.BUY


class TestSellSignal:
    def test_breakdown_below_prior_low(self):
        # Prior 20 lows all 99. Close at 98 < 99 -> SELL.
        candles = _flat_then_breakout(period=20, breakout_close=98.0)
        result = DonchianStrategy().evaluate("AAPL", candles)
        assert result.signal == SignalType.SELL
        assert result.metadata["lower"] == 99.0
        assert result.metadata["close"] == 98.0

    def test_breakdown_just_below_band(self):
        candles = _flat_then_breakout(period=20, breakout_close=98.99)
        assert DonchianStrategy().evaluate("X", candles).signal == SignalType.SELL


class TestHoldSignal:
    def test_inside_band(self):
        # Close 99.5 sits between 99 (lower) and 100 (upper) -> HOLD.
        candles = _flat_then_breakout(period=20, breakout_close=99.5)
        assert DonchianStrategy().evaluate("X", candles).signal == SignalType.HOLD

    def test_close_equal_to_upper_is_hold(self):
        # Strict > for BUY: close == upper means HOLD, not BUY.
        candles = _flat_then_breakout(period=20, breakout_close=100.0)
        assert DonchianStrategy().evaluate("X", candles).signal == SignalType.HOLD

    def test_close_equal_to_lower_is_hold(self):
        # Strict < for SELL: close == lower means HOLD, not SELL.
        candles = _flat_then_breakout(period=20, breakout_close=99.0)
        assert DonchianStrategy().evaluate("X", candles).signal == SignalType.HOLD


class TestExcludesEvaluationBar:
    """The locked spec says the band is built from PRIOR bars only, not the
    bar being evaluated. Tests that today's high/low don't pollute the band."""

    def test_buy_uses_prior_high_not_current_bar_high(self):
        # Prior 20 highs all 100. Today's BAR has high=200, low=50, close=101.
        # If the strategy mistakenly included today's bar in the band:
        #   upper would be 200 -> close 101 < 200 -> NO BUY.
        # With correct exclusion:
        #   upper is 100 (from prior 20) -> close 101 > 100 -> BUY.
        highs = [100.0] * 20 + [200.0]
        lows = [99.0] * 20 + [50.0]
        closes = [99.5] * 20 + [101.0]
        candles = _candles(highs, lows, closes)
        result = DonchianStrategy().evaluate("X", candles)
        assert result.signal == SignalType.BUY
        assert result.metadata["upper"] == 100.0  # NOT 200

    def test_sell_uses_prior_low_not_current_bar_low(self):
        # Mirror: today's bar has a fake low of 1; band built from prior 20 only.
        highs = [100.0] * 20 + [200.0]
        lows = [99.0] * 20 + [1.0]
        closes = [99.5] * 20 + [98.0]
        candles = _candles(highs, lows, closes)
        result = DonchianStrategy().evaluate("X", candles)
        assert result.signal == SignalType.SELL
        assert result.metadata["lower"] == 99.0  # NOT 1.0


class TestInsufficientData:
    def test_too_few_bars_returns_hold(self):
        # 20 bars -- need 21.
        candles = _candles([100.0] * 20, [99.0] * 20, [99.5] * 20)
        result = DonchianStrategy().evaluate("X", candles)
        assert result.signal == SignalType.HOLD
        assert "insufficient" in result.metadata["reason"]
        assert result.metadata["have"] == 20
        assert result.metadata["need"] == 21

    def test_empty_dataframe_returns_hold(self):
        candles = _candles([], [], [])
        result = DonchianStrategy().evaluate("X", candles)
        assert result.signal == SignalType.HOLD


class TestRealisticScenario:
    def test_uses_sample_candles_fixture(self, sample_candles):
        # Just verify the strategy runs cleanly on the shared 60-day fixture
        # without raising. Signal can be anything depending on RNG.
        result = DonchianStrategy().evaluate("AAPL", sample_candles)
        assert result.signal in (SignalType.BUY, SignalType.SELL, SignalType.HOLD)
        assert result.symbol == "AAPL"
        assert "upper" in result.metadata
        assert "lower" in result.metadata
