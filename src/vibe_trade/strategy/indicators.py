"""Technical indicator computations using backtrader's indicator engine.

Wraps backtrader indicators so they can be called with pandas DataFrames.
Backtrader requires a Cerebro run to compute indicators, so we run a
lightweight extraction pass under the hood.
"""

from __future__ import annotations

from dataclasses import dataclass

import backtrader as bt
import pandas as pd


@dataclass
class IndicatorSet:
    """All computed indicator values aligned to the input DataFrame index."""
    sma: pd.Series
    ema: pd.Series
    rsi: pd.Series
    atr: pd.Series
    macd_line: pd.Series
    macd_signal: pd.Series
    macd_hist: pd.Series
    bb_upper: pd.Series
    bb_middle: pd.Series
    bb_lower: pd.Series


class _Extractor(bt.Strategy):
    """Internal strategy that collects indicator values each bar."""

    params = dict(
        sma_period=20,
        ema_period=20,
        rsi_period=14,
        atr_period=14,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        bb_period=20,
        bb_devfactor=2.0,
    )

    def __init__(self):
        self.sma_ind = bt.indicators.SMA(self.data.close, period=self.p.sma_period)
        self.ema_ind = bt.indicators.EMA(self.data.close, period=self.p.ema_period)
        self.rsi_ind = bt.indicators.RSI(self.data.close, period=self.p.rsi_period)
        self.atr_ind = bt.indicators.ATR(self.data, period=self.p.atr_period)
        self.macd_ind = bt.indicators.MACD(
            self.data.close,
            period_me1=self.p.macd_fast,
            period_me2=self.p.macd_slow,
            period_signal=self.p.macd_signal,
        )
        self.bb_ind = bt.indicators.BollingerBands(
            self.data.close,
            period=self.p.bb_period,
            devfactor=self.p.bb_devfactor,
        )

        self._results: dict[str, list[float]] = {
            "sma": [], "ema": [], "rsi": [], "atr": [],
            "macd_line": [], "macd_signal": [], "macd_hist": [],
            "bb_upper": [], "bb_middle": [], "bb_lower": [],
        }
        self._dates: list = []

    def next(self):
        self._dates.append(self.data.datetime.datetime())
        self._results["sma"].append(self.sma_ind[0])
        self._results["ema"].append(self.ema_ind[0])
        self._results["rsi"].append(self.rsi_ind[0])
        self._results["atr"].append(self.atr_ind[0])
        self._results["macd_line"].append(self.macd_ind.macd[0])
        self._results["macd_signal"].append(self.macd_ind.signal[0])
        self._results["macd_hist"].append(self.macd_ind.macd[0] - self.macd_ind.signal[0])
        self._results["bb_upper"].append(self.bb_ind.top[0])
        self._results["bb_middle"].append(self.bb_ind.mid[0])
        self._results["bb_lower"].append(self.bb_ind.bot[0])


def compute_indicators(
    candles: pd.DataFrame,
    sma_period: int = 20,
    ema_period: int = 20,
    rsi_period: int = 14,
    atr_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    bb_period: int = 20,
    bb_devfactor: float = 2.0,
) -> IndicatorSet:
    """Compute all indicators for a candles DataFrame using backtrader.

    Args:
        candles: DataFrame with columns: open, high, low, close, volume.
                 Index should be DatetimeIndex.

    Returns:
        IndicatorSet with all indicator values as pandas Series,
        aligned to the tail of the input index (backtrader drops
        the warmup bars).
    """
    cerebro = bt.Cerebro()
    bt_data = bt.feeds.PandasData(dataname=candles)
    cerebro.adddata(bt_data)
    cerebro.addstrategy(
        _Extractor,
        sma_period=sma_period,
        ema_period=ema_period,
        rsi_period=rsi_period,
        atr_period=atr_period,
        macd_fast=macd_fast,
        macd_slow=macd_slow,
        macd_signal=macd_signal,
        bb_period=bb_period,
        bb_devfactor=bb_devfactor,
    )
    result = cerebro.run()
    strat = result[0]

    index = pd.DatetimeIndex(strat._dates)

    return IndicatorSet(
        sma=pd.Series(strat._results["sma"], index=index, name="sma"),
        ema=pd.Series(strat._results["ema"], index=index, name="ema"),
        rsi=pd.Series(strat._results["rsi"], index=index, name="rsi"),
        atr=pd.Series(strat._results["atr"], index=index, name="atr"),
        macd_line=pd.Series(strat._results["macd_line"], index=index, name="macd_line"),
        macd_signal=pd.Series(strat._results["macd_signal"], index=index, name="macd_signal"),
        macd_hist=pd.Series(strat._results["macd_hist"], index=index, name="macd_hist"),
        bb_upper=pd.Series(strat._results["bb_upper"], index=index, name="bb_upper"),
        bb_middle=pd.Series(strat._results["bb_middle"], index=index, name="bb_middle"),
        bb_lower=pd.Series(strat._results["bb_lower"], index=index, name="bb_lower"),
    )


# --- Standalone convenience functions (thin wrappers) ---
# These run a full cerebro under the hood each time.
# For multiple indicators on the same data, prefer compute_indicators().

def sma(candles: pd.DataFrame, period: int = 20) -> pd.Series:
    """Simple Moving Average of close prices."""
    return compute_indicators(candles, sma_period=period).sma


def ema(candles: pd.DataFrame, period: int = 20) -> pd.Series:
    """Exponential Moving Average of close prices."""
    return compute_indicators(candles, ema_period=period).ema


def rsi(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    return compute_indicators(candles, rsi_period=period).rsi


def atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    return compute_indicators(candles, atr_period=period).atr


def macd(
    candles: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD line, signal line, and histogram."""
    ind = compute_indicators(candles, macd_fast=fast, macd_slow=slow, macd_signal=signal)
    return ind.macd_line, ind.macd_signal, ind.macd_hist


def bollinger_bands(
    candles: pd.DataFrame,
    period: int = 20,
    devfactor: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands: upper, middle, lower."""
    ind = compute_indicators(candles, bb_period=period, bb_devfactor=devfactor)
    return ind.bb_upper, ind.bb_middle, ind.bb_lower
