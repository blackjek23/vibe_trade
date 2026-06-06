"""Shared base for moving-average crossover strategies (SMA / EMA).

Regime/state semantics (Session L): the signal is the *current* relationship
between the fast and slow moving average at the bar being evaluated
(``df.iloc[-1]``), not the single bar on which they cross. Re-evaluated daily;
submit's held-position dedup prevents re-buying. This makes the strategy robust
to missed trading days (a missed Gateway-outage day doesn't lose the signal).

- BUY  when fast MA > slow MA
- SELL when fast MA < slow MA
- HOLD on an exact tie or with insufficient data

Exit is the reverse of entry, computed purely from the candle series, so the
strategy fits the stateless ``evaluate(symbol, candles)`` interface and supports
strategy-scoped exits (its SELL only closes positions it opened).
"""

from __future__ import annotations

from abc import abstractmethod

import pandas as pd

from vibe_trade.strategy.base import BaseStrategy, SignalResult, SignalType


class _CrossoverStrategy(BaseStrategy):
    """Fast-vs-slow moving-average regime strategy. Subclass defines the MA."""

    def __init__(self, fast: int, slow: int):
        if fast < 1 or slow < 1:
            raise ValueError(f"periods must be >= 1, got fast={fast} slow={slow}")
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be < slow ({slow})")
        self.fast = fast
        self.slow = slow

    @abstractmethod
    def _moving_average(self, series: pd.Series, period: int) -> pd.Series:
        """Return the moving average of ``series`` with the given period."""

    def evaluate(self, symbol: str, candles: pd.DataFrame) -> SignalResult:
        if len(candles) < self.required_candles:
            return SignalResult(
                signal=SignalType.HOLD,
                symbol=symbol,
                strategy_name=self.name,
                metadata={
                    "reason": "insufficient data",
                    "have": len(candles),
                    "need": self.required_candles,
                },
            )

        close = candles["close"].astype(float)
        fast_ma = float(self._moving_average(close, self.fast).iloc[-1])
        slow_ma = float(self._moving_average(close, self.slow).iloc[-1])

        metadata = {
            "fast": round(fast_ma, 4),
            "slow": round(slow_ma, 4),
            "fast_period": self.fast,
            "slow_period": self.slow,
        }

        if fast_ma > slow_ma:
            return SignalResult(
                signal=SignalType.BUY,
                symbol=symbol,
                strategy_name=self.name,
                confidence=0.6,
                metadata=metadata,
            )

        if fast_ma < slow_ma:
            return SignalResult(
                signal=SignalType.SELL,
                symbol=symbol,
                strategy_name=self.name,
                confidence=0.6,
                metadata=metadata,
            )

        return SignalResult(
            signal=SignalType.HOLD,
            symbol=symbol,
            strategy_name=self.name,
            metadata=metadata,
        )
