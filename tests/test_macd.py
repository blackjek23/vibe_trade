"""Tests for the MACD crossover strategy.

Regime/state semantics (Session L):
- BUY  when MACD line > signal line
- SELL when MACD line < signal line
- HOLD on an exact tie or with insufficient data
"""

from __future__ import annotations

import pandas as pd
import pytest

from vibe_trade.strategy.base import SignalType
from vibe_trade.strategy.examples.macd_crossover import MACDCrossoverStrategy


def _candles(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1000] * n,
        },
        index=pd.date_range("2026-01-01", periods=n, freq="D"),
    )


class TestNameAndRequired:
    def test_name(self):
        assert MACDCrossoverStrategy().name == "macd"

    def test_required_candles(self):
        # 2 * slow + signal = 2*26 + 9
        assert MACDCrossoverStrategy().required_candles == 61
        assert MACDCrossoverStrategy(fast=3, slow=6, signal=2).required_candles == 14

    def test_period_validation(self):
        with pytest.raises(ValueError):
            MACDCrossoverStrategy(fast=26, slow=12)
        with pytest.raises(ValueError):
            MACDCrossoverStrategy(signal=0)


class TestSignals:
    def test_uptrend_macd_above_signal_is_buy(self):
        candles = _candles([100.0 + i for i in range(80)])
        result = MACDCrossoverStrategy().evaluate("AAPL", candles)
        assert result.signal == SignalType.BUY
        assert result.strategy_name == "macd"
        assert result.metadata["macd"] > result.metadata["signal"]

    def test_downtrend_macd_below_signal_is_sell(self):
        candles = _candles([200.0 - i for i in range(80)])
        result = MACDCrossoverStrategy().evaluate("AAPL", candles)
        assert result.signal == SignalType.SELL
        assert result.metadata["macd"] < result.metadata["signal"]

    def test_flat_series_is_hold(self):
        # Constant closes -> MACD line and signal both 0 -> tie -> HOLD.
        result = MACDCrossoverStrategy().evaluate("X", _candles([100.0] * 70))
        assert result.signal == SignalType.HOLD
        assert result.metadata["macd"] == 0.0
        assert result.metadata["signal"] == 0.0


class TestInsufficientData:
    def test_too_few_bars_is_hold(self):
        result = MACDCrossoverStrategy().evaluate("X", _candles([100.0] * 30))
        assert result.signal == SignalType.HOLD
        assert "insufficient" in result.metadata["reason"]
        assert result.metadata["need"] == 61
