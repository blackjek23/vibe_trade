"""RSI Mean Reversion strategy."""

from __future__ import annotations

import pandas as pd

from vibe_trade.strategy.base import BaseStrategy, SignalResult, SignalType
from vibe_trade.strategy.indicators import compute_indicators


class RSIMeanRevertStrategy(BaseStrategy):
    def __init__(self, rsi_period: int = 14, oversold: int = 30, overbought: int = 70):
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought

    @property
    def name(self) -> str:
        return "rsi_mean_revert"

    @property
    def required_candles(self) -> int:
        return self.rsi_period + 10

    def evaluate(self, symbol: str, candles: pd.DataFrame) -> SignalResult:
        if len(candles) < self.required_candles:
            return SignalResult(
                signal=SignalType.HOLD,
                symbol=symbol,
                strategy_name=self.name,
                metadata={"reason": "insufficient data"},
            )

        # Compute all needed indicators in one backtrader pass
        ind = compute_indicators(candles, rsi_period=self.rsi_period, atr_period=14)
        rsi_values = ind.rsi
        current_rsi = rsi_values.iloc[-1]
        prev_rsi = rsi_values.iloc[-2]
        current_atr = ind.atr.iloc[-1]
        current_price = candles["close"].iloc[-1]

        metadata = {
            "rsi": round(current_rsi, 2),
            "prev_rsi": round(prev_rsi, 2),
            "atr": round(current_atr, 2),
            "price": round(current_price, 2),
        }

        # Buy when RSI crosses back above oversold (mean reversion)
        if prev_rsi <= self.oversold and current_rsi > self.oversold:
            trailing_stop = current_price - (current_atr * 2)
            return SignalResult(
                signal=SignalType.BUY,
                symbol=symbol,
                strategy_name=self.name,
                confidence=min(1.0, (self.oversold - prev_rsi) / 10 + 0.5),
                trailing_stop_price=trailing_stop,
                metadata=metadata,
            )

        # Sell when RSI crosses back below overbought
        if prev_rsi >= self.overbought and current_rsi < self.overbought:
            return SignalResult(
                signal=SignalType.SELL,
                symbol=symbol,
                strategy_name=self.name,
                confidence=min(1.0, (prev_rsi - self.overbought) / 10 + 0.5),
                metadata=metadata,
            )

        return SignalResult(
            signal=SignalType.HOLD,
            symbol=symbol,
            strategy_name=self.name,
            metadata=metadata,
        )
