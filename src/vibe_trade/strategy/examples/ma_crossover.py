"""Moving Average Crossover strategy."""

from __future__ import annotations

import pandas as pd

from vibe_trade.strategy.base import BaseStrategy, SignalResult, SignalType
from vibe_trade.strategy.indicators import compute_indicators


class MACrossoverStrategy(BaseStrategy):
    def __init__(self, fast_period: int = 20, slow_period: int = 50):
        self.fast_period = fast_period
        self.slow_period = slow_period

    @property
    def name(self) -> str:
        return "ma_crossover"

    @property
    def required_candles(self) -> int:
        return self.slow_period + 5

    def evaluate(self, symbol: str, candles: pd.DataFrame) -> SignalResult:
        if len(candles) < self.required_candles:
            return SignalResult(
                signal=SignalType.HOLD,
                symbol=symbol,
                strategy_name=self.name,
                metadata={"reason": "insufficient data"},
            )

        # Compute all needed indicators in one backtrader pass
        ind = compute_indicators(
            candles,
            sma_period=self.fast_period,
            ema_period=self.slow_period,  # use ema slot for slow MA
            atr_period=14,
        )
        fast_sma = ind.sma
        slow_sma = ind.ema  # using EMA slot for the slow period
        current_atr = ind.atr.iloc[-1]

        # Current and previous crossover state
        fast_now = fast_sma.iloc[-1]
        fast_prev = fast_sma.iloc[-2]
        slow_now = slow_sma.iloc[-1]
        slow_prev = slow_sma.iloc[-2]
        current_price = candles["close"].iloc[-1]

        metadata = {
            "fast_sma": round(fast_now, 2),
            "slow_sma": round(slow_now, 2),
            "atr": round(current_atr, 2),
            "price": round(current_price, 2),
        }

        # Bullish crossover: fast crosses above slow
        if fast_prev <= slow_prev and fast_now > slow_now and current_price > fast_now:
            trailing_stop = current_price - (current_atr * 2)
            return SignalResult(
                signal=SignalType.BUY,
                symbol=symbol,
                strategy_name=self.name,
                confidence=0.7,
                trailing_stop_price=trailing_stop,
                metadata=metadata,
            )

        # Bearish crossover: fast crosses below slow
        if fast_prev >= slow_prev and fast_now < slow_now and current_price < fast_now:
            return SignalResult(
                signal=SignalType.SELL,
                symbol=symbol,
                strategy_name=self.name,
                confidence=0.7,
                metadata=metadata,
            )

        return SignalResult(
            signal=SignalType.HOLD,
            symbol=symbol,
            strategy_name=self.name,
            metadata=metadata,
        )
