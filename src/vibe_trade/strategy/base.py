"""Abstract base strategy and signal types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class SignalResult:
    signal: SignalType
    symbol: str
    strategy_name: str
    confidence: float = 0.0  # 0 to 1
    metadata: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this strategy."""

    @property
    @abstractmethod
    def required_candles(self) -> int:
        """Minimum number of candles needed to generate a signal."""

    @abstractmethod
    def evaluate(self, symbol: str, candles: pd.DataFrame) -> SignalResult:
        """Evaluate the strategy on the given candles.

        Args:
            symbol: The stock ticker.
            candles: DataFrame with columns: open, high, low, close, volume.

        Returns:
            A SignalResult with the trading signal.
        """
