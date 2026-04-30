"""Historical-data caching for the backtester.

Two caches under `data/historical/`:
- `<SYMBOL>.csv`         -- daily OHLCV bars per symbol (one file per ticker)
- `_market_caps.json`    -- snapshot of {symbol: marketCap} for top-N selection

CSV chosen over parquet so we don't add a pyarrow dependency. Footprint is
small enough that parse speed doesn't matter (8y x 100 symbols ~= 200K rows).

The data fetchers accept a `fetcher` callable for dependency injection so tests
can run offline. Default: yfinance.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data") / "historical"
MARKET_CAP_FILENAME = "_market_caps.json"


# --------------------------------------------------------- yfinance fetchers
def _yf_download_bars(symbol: str, start: date, end: date) -> pd.DataFrame:
    """Default fetcher: yfinance daily OHLCV. Returns empty DataFrame on failure."""
    import yfinance as yf

    df = yf.download(
        tickers=symbol,
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        progress=False,
        auto_adjust=True,
        multi_level_index=False,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(
        columns={
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        }
    )[["open", "high", "low", "close", "volume"]]
    df.index.name = "date"
    return df


def _yf_fetch_market_cap(symbol: str) -> int | None:
    """Default fetcher: yfinance Ticker.info['marketCap']. None on failure."""
    import yfinance as yf

    try:
        info = yf.Ticker(symbol).info
        mcap = info.get("marketCap")
        return int(mcap) if mcap else None
    except Exception as exc:  # noqa: BLE001 -- network failures are common
        logger.warning("market cap fetch failed for %s: %r", symbol, exc)
        return None


# --------------------------------------------------------- bar cache
def fetch_and_cache_bars(
    symbols: list[str],
    start: date,
    end: date,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    fetcher: Callable[[str, date, date], pd.DataFrame] = _yf_download_bars,
    force_refresh: bool = False,
) -> dict[str, Path]:
    """Ensure cached bars exist for `symbols` covering [start, end).

    Returns {symbol: cache_path}. Symbols that yielded no data are omitted.
    Existing cache files are reused unless `force_refresh=True`. We don't
    extend partial caches -- if cache exists, we trust it covers the range.
    Refresh manually (force_refresh=True) when extending the date window.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    for symbol in symbols:
        path = cache_dir / f"{symbol}.csv"
        if path.exists() and not force_refresh:
            paths[symbol] = path
            continue
        df = fetcher(symbol, start, end)
        if df.empty:
            logger.warning("no bars returned for %s; skipping", symbol)
            continue
        df.to_csv(path, index=True)
        paths[symbol] = path
        logger.info("cached %d bars for %s -> %s", len(df), symbol, path.name)

    return paths


def load_bars(
    symbol: str,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Load cached bars for one symbol, optionally sliced to [start, end).

    Returns empty DataFrame if cache doesn't exist.
    """
    path = cache_dir / f"{symbol}.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, index_col="date", parse_dates=["date"])
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index < pd.Timestamp(end)]
    return df


# --------------------------------------------------------- market-cap cache
def get_top_n_by_mcap(
    symbols: list[str],
    n: int,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    fetcher: Callable[[str], int | None] = _yf_fetch_market_cap,
    force_refresh: bool = False,
) -> list[str]:
    """Return the top-N symbols by market cap.

    Cached to `<cache_dir>/_market_caps.json` as {symbol: mcap}. On first call
    (or when force_refresh=True) fetches fresh values for every symbol.
    Symbols that returned no market cap are excluded from ranking.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / MARKET_CAP_FILENAME

    cap_map: dict[str, int] = {}
    if cache_path.exists() and not force_refresh:
        cap_map = json.loads(cache_path.read_text())

    missing = [s for s in symbols if s not in cap_map]
    if missing or force_refresh:
        target = symbols if force_refresh else missing
        for sym in target:
            mcap = fetcher(sym)
            if mcap is not None:
                cap_map[sym] = mcap
        cache_path.write_text(json.dumps(cap_map, indent=2, sort_keys=True))

    # Rank: descending mcap, take top n. Filter to symbols actually in input.
    ranked = sorted(
        ((s, cap_map[s]) for s in symbols if s in cap_map),
        key=lambda t: t[1],
        reverse=True,
    )
    return [s for s, _ in ranked[:n]]
