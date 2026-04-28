"""Donchian Channel breakout strategy.

Locked spec (V2 first iteration, see project_v2_next_sessions.md memory):
- Period N = 20 days (classic Turtle setting)
- Symmetric: same N for entries and exits
- Excluding the bar being evaluated: yesterday's close is compared against
  the band built from the *prior* 20 days

Signal logic (evaluating yesterday's closed daily bar `df.iloc[-1]`):
- BUY  when close[-1] > max(high[-21:-1])  (close above the prior 20-day high)
- SELL when close[-1] < min(low[-21:-1])   (close below the prior 20-day low)
- HOLD otherwise (including ties at the band)
"""

from __future__ import annotations

import pandas as pd

from vibe_trade.strategy.base import BaseStrategy, SignalResult, SignalType


class DonchianStrategy(BaseStrategy):
    def __init__(self, period: int = 20):
        if period < 1:
            raise ValueError(f"period must be >= 1, got {period}")
        self.period = period

    @property
    def name(self) -> str:
        return "donchian"

    @property
    def required_candles(self) -> int:
        # N prior bars to build the band + 1 bar being evaluated.
        return self.period + 1

    def evaluate(self, symbol: str, candles: pd.DataFrame) -> SignalResult:
        if len(candles) < self.required_candles:
            return SignalResult(
                signal=SignalType.HOLD,
                symbol=symbol,
                strategy_name=self.name,
                metadata={"reason": "insufficient data", "have": len(candles), "need": self.required_candles},
            )

        # Prior N bars (excluding the bar being evaluated at index -1).
        prior = candles.iloc[-self.period - 1:-1]
        upper = float(prior["high"].max())
        lower = float(prior["low"].min())
        close = float(candles["close"].iloc[-1])

        metadata = {
            "upper": round(upper, 2),
            "lower": round(lower, 2),
            "close": round(close, 2),
            "period": self.period,
        }

        if close > upper:
            return SignalResult(
                signal=SignalType.BUY,
                symbol=symbol,
                strategy_name=self.name,
                confidence=0.7,
                metadata=metadata,
            )

        if close < lower:
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
