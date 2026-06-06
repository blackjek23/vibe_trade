"""Exponential-moving-average crossover strategy (regime/state semantics).

BUY when EMA(fast) > EMA(slow), SELL when EMA(fast) < EMA(slow), HOLD on a tie.
Defaults: fast=12, slow=26. See ``_crossover.py`` for the shared logic.

``required_candles = 2 * slow`` gives the EMA enough history to converge before
the strategy is allowed to signal (``ewm`` with ``adjust=False`` is defined from
the first bar but is unreliable until several spans of warmup).
"""

from __future__ import annotations

import pandas as pd

from vibe_trade.strategy.examples._crossover import _CrossoverStrategy


class EMACrossoverStrategy(_CrossoverStrategy):
    def __init__(self, fast: int = 12, slow: int = 26):
        super().__init__(fast=fast, slow=slow)

    @property
    def name(self) -> str:
        return "ema"

    @property
    def required_candles(self) -> int:
        return 2 * self.slow

    def _moving_average(self, series: pd.Series, period: int) -> pd.Series:
        return series.ewm(span=period, adjust=False).mean()
