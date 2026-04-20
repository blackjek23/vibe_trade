"""Data provider for fetching historical OHLCV data via yfinance."""

from __future__ import annotations

import asyncio
import logging

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Maps our timeframe strings to yfinance interval format.
TIMEFRAME_MAP = {
    "1h": "1h",
    "4h": "1h",  # yfinance has no 4h — caller can resample if needed
    "1d": "1d",
}


class DataProvider:
    """Fetches OHLCV candles from Yahoo Finance (yfinance).

    yfinance is a free, no-auth data source. Ideal for swing trading:
    daily/hourly bars, no IB rate limits, no TWS dependency.
    """

    async def get_candles(
        self,
        symbol: str,
        timeframe: str = "1h",
        lookback_days: int = 60,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles as a pandas DataFrame.

        Returns a DataFrame with columns: open, high, low, close, volume
        indexed by timestamp. Empty DataFrame if no data available.
        """
        interval = TIMEFRAME_MAP.get(timeframe, "1h")
        period = f"{lookback_days}d"

        # yfinance is synchronous — run in a thread so we don't block the event loop.
        df = await asyncio.to_thread(
            self._download,
            symbol=symbol,
            period=period,
            interval=interval,
        )

        if df is None or df.empty:
            logger.warning(f"No bars returned for {symbol}")
            return pd.DataFrame()

        # yfinance returns columns: Open, High, Low, Close, Adj Close, Volume
        df = df.rename(
            columns={
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            }
        )
        df = df[["open", "high", "low", "close", "volume"]]
        df.index.name = "timestamp"
        return df

    @staticmethod
    def _download(symbol: str, period: str, interval: str) -> pd.DataFrame:
        """Synchronous yfinance call — wrapped in asyncio.to_thread."""
        return yf.download(
            tickers=symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
            multi_level_index=False,
        )
