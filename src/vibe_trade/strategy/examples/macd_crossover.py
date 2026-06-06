"""MACD crossover strategy (regime/state semantics).

MACD line = EMA(fast) - EMA(slow); signal line = EMA(MACD, signal).
- BUY  when MACD line > signal line
- SELL when MACD line < signal line
- HOLD on an exact tie or with insufficient data

Defaults: fast=12, slow=26, signal=9 (standard MACD). Regime semantics (current
relationship at ``df.iloc[-1]``, not the crossing bar) keep it robust to missed
days and consistent with the SMA/EMA crossover strategies. Exit is the reverse of
entry, computed purely from the candle series (stateless / strategy-scoped).
"""

from __future__ import annotations

import pandas as pd

from vibe_trade.strategy.base import BaseStrategy, SignalResult, SignalType


class MACDCrossoverStrategy(BaseStrategy):
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        if fast < 1 or slow < 1 or signal < 1:
            raise ValueError(
                f"periods must be >= 1, got fast={fast} slow={slow} signal={signal}"
            )
        if fast >= slow:
            raise ValueError(f"fast ({fast}) must be < slow ({slow})")
        self.fast = fast
        self.slow = slow
        self.signal = signal

    @property
    def name(self) -> str:
        return "macd"

    @property
    def required_candles(self) -> int:
        # Warmup for the slow EMA plus the signal EMA computed on top of it.
        return 2 * self.slow + self.signal

    def _ema(self, series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()

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
        macd_line = self._ema(close, self.fast) - self._ema(close, self.slow)
        signal_line = self._ema(macd_line, self.signal)

        macd = float(macd_line.iloc[-1])
        sig = float(signal_line.iloc[-1])

        metadata = {
            "macd": round(macd, 4),
            "signal": round(sig, 4),
            "fast_period": self.fast,
            "slow_period": self.slow,
            "signal_period": self.signal,
        }

        if macd > sig:
            return SignalResult(
                signal=SignalType.BUY,
                symbol=symbol,
                strategy_name=self.name,
                confidence=0.6,
                metadata=metadata,
            )

        if macd < sig:
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
