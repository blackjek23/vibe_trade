"""Simple-moving-average crossover strategy (regime/state semantics).

BUY when SMA(fast) > SMA(slow), SELL when SMA(fast) < SMA(slow), HOLD on a tie.
Defaults: fast=20, slow=50. See ``_crossover.py`` for the shared logic.
"""

from __future__ import annotations

import pandas as pd

from vibe_trade.strategy.examples._crossover import _CrossoverStrategy


class SMACrossoverStrategy(_CrossoverStrategy):
    def __init__(self, fast: int = 20, slow: int = 50):
        super().__init__(fast=fast, slow=slow)

    @property
    def name(self) -> str:
        return "sma"

    @property
    def required_candles(self) -> int:
        # Need ``slow`` bars for the slow SMA at the evaluated bar.
        return self.slow

    def _moving_average(self, series: pd.Series, period: int) -> pd.Series:
        return series.rolling(period).mean()
