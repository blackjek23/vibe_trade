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

# Transient yfinance failures (curl timeout, 503, momentary empty result) are
# common for ~6% of the universe per run — Hygiene #1 in SESSION_H_FINDINGS.md.
# One retry with a short backoff recovers most of them without slowing the run.
RETRY_ATTEMPTS: int = 2          # total tries = 1 initial + 1 retry
RETRY_BACKOFF_SECONDS: float = 1.5

# Bounded concurrency for batch fetches — keeps yfinance/Yahoo happy while
# cutting the universe scan from ~5 min sequential to ~30 s (Hygiene #4).
DEFAULT_BATCH_CONCURRENCY: int = 10


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
        indexed by timestamp. Empty DataFrame if no data available after
        retrying transient failures.
        """
        interval = TIMEFRAME_MAP.get(timeframe, "1h")
        period = f"{lookback_days}d"

        df: pd.DataFrame | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                # yfinance is synchronous — run in a thread, don't block the loop.
                df = await asyncio.to_thread(
                    self._download, symbol=symbol, period=period, interval=interval
                )
            except Exception as exc:  # noqa: BLE001 -- yfinance raises many types
                logger.warning(
                    "yfinance error for %s (attempt %d/%d): %r",
                    symbol, attempt, RETRY_ATTEMPTS, exc,
                )
                df = None

            if df is not None and not df.empty:
                break

            if attempt < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)

        if df is None or df.empty:
            logger.warning("No bars returned for %s", symbol)
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

    async def get_candles_batch(
        self,
        symbols: list[str],
        timeframe: str = "1d",
        lookback_days: int = 60,
        max_concurrency: int = DEFAULT_BATCH_CONCURRENCY,
    ) -> dict[str, pd.DataFrame]:
        """Fetch candles for many symbols concurrently.

        Returns ``{symbol: DataFrame}`` for every input symbol. A symbol whose
        fetch fails maps to an empty DataFrame — one bad ticker never aborts
        the batch (same isolation as the per-symbol submit loop).

        Concurrency is bounded by ``max_concurrency`` so we don't stampede
        Yahoo. Replaces the ~5-min sequential universe scan (Hygiene #4).
        """
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _fetch(sym: str) -> tuple[str, pd.DataFrame]:
            async with semaphore:
                try:
                    df = await self.get_candles(sym, timeframe, lookback_days)
                except Exception as exc:  # noqa: BLE001 -- isolate per ticker
                    logger.warning("batch fetch failed for %s: %r", sym, exc)
                    df = pd.DataFrame()
                return sym, df

        pairs = await asyncio.gather(*(_fetch(s) for s in symbols))
        return dict(pairs)

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
