"""Tests for SMA / EMA moving-average crossover strategies.

Regime/state semantics (Session L):
- BUY  when fast MA > slow MA
- SELL when fast MA < slow MA
- HOLD on an exact tie or with insufficient data
"""

from __future__ import annotations

import pandas as pd
import pytest

from vibe_trade.strategy.base import SignalType
from vibe_trade.strategy.examples.ema_crossover import EMACrossoverStrategy
from vibe_trade.strategy.examples.sma_crossover import SMACrossoverStrategy


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


def _rising(n: int) -> pd.DataFrame:
    return _candles([100.0 + i for i in range(n)])


def _falling(n: int) -> pd.DataFrame:
    return _candles([200.0 - i for i in range(n)])


def _flat(n: int) -> pd.DataFrame:
    return _candles([100.0] * n)


class TestSMA:
    def test_name(self):
        assert SMACrossoverStrategy().name == "sma"

    def test_required_candles_is_slow(self):
        assert SMACrossoverStrategy().required_candles == 50
        assert SMACrossoverStrategy(fast=5, slow=10).required_candles == 10

    def test_uptrend_is_buy(self):
        result = SMACrossoverStrategy().evaluate("AAPL", _rising(60))
        assert result.signal == SignalType.BUY
        assert result.strategy_name == "sma"
        assert result.metadata["fast"] > result.metadata["slow"]

    def test_downtrend_is_sell(self):
        result = SMACrossoverStrategy().evaluate("AAPL", _falling(60))
        assert result.signal == SignalType.SELL
        assert result.metadata["fast"] < result.metadata["slow"]

    def test_flat_is_hold_on_tie(self):
        result = SMACrossoverStrategy().evaluate("X", _flat(60))
        assert result.signal == SignalType.HOLD
        assert result.metadata["fast"] == result.metadata["slow"]

    def test_insufficient_data_is_hold(self):
        result = SMACrossoverStrategy().evaluate("X", _rising(40))
        assert result.signal == SignalType.HOLD
        assert "insufficient" in result.metadata["reason"]
        assert result.metadata["need"] == 50

    def test_custom_periods(self):
        result = SMACrossoverStrategy(fast=5, slow=10).evaluate("X", _rising(15))
        assert result.signal == SignalType.BUY
        assert result.metadata["fast_period"] == 5
        assert result.metadata["slow_period"] == 10


class TestEMA:
    def test_name(self):
        assert EMACrossoverStrategy().name == "ema"

    def test_required_candles_is_twice_slow(self):
        assert EMACrossoverStrategy().required_candles == 52
        assert EMACrossoverStrategy(fast=5, slow=10).required_candles == 20

    def test_uptrend_is_buy(self):
        result = EMACrossoverStrategy().evaluate("AAPL", _rising(60))
        assert result.signal == SignalType.BUY

    def test_downtrend_is_sell(self):
        result = EMACrossoverStrategy().evaluate("AAPL", _falling(60))
        assert result.signal == SignalType.SELL

    def test_flat_is_hold_on_tie(self):
        result = EMACrossoverStrategy().evaluate("X", _flat(60))
        assert result.signal == SignalType.HOLD

    def test_insufficient_data_is_hold(self):
        result = EMACrossoverStrategy().evaluate("X", _rising(40))
        assert result.signal == SignalType.HOLD
        assert result.metadata["need"] == 52


class TestValidation:
    def test_fast_must_be_less_than_slow(self):
        with pytest.raises(ValueError):
            SMACrossoverStrategy(fast=50, slow=20)
        with pytest.raises(ValueError):
            EMACrossoverStrategy(fast=26, slow=26)

    def test_periods_must_be_positive(self):
        with pytest.raises(ValueError):
            SMACrossoverStrategy(fast=0, slow=10)


class TestRealisticScenario:
    def test_runs_on_sample_fixture(self, sample_candles):
        # 60-bar fixture: SMA(20/50) has enough; EMA(2*26=52) has enough.
        for strat in (SMACrossoverStrategy(), EMACrossoverStrategy()):
            result = strat.evaluate("AAPL", sample_candles)
            assert result.signal in (SignalType.BUY, SignalType.SELL, SignalType.HOLD)
            assert "fast" in result.metadata
