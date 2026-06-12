"""Tests for vibe_trade.backtest.data — bar cache + market-cap cache.

All tests use injected fetchers (no network), tmp_path fixtures (no shared state).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from vibe_trade.backtest.data import (
    MARKET_CAP_FILENAME,
    fetch_and_cache_bars,
    get_top_n_by_mcap,
    load_bars,
)


def _synth_bars(start: date, end: date) -> pd.DataFrame:
    """Build a tiny daily-bar DataFrame for testing."""
    idx = pd.date_range(start, end, freq="D", inclusive="left")
    return pd.DataFrame(
        {
            "open": [100.0] * len(idx),
            "high": [101.0] * len(idx),
            "low": [99.0] * len(idx),
            "close": [100.5] * len(idx),
            "volume": [1000] * len(idx),
        },
        index=idx,
    ).rename_axis("date")


class TestFetchAndCacheBars:
    def test_fetches_and_writes_per_symbol(self, tmp_path: Path):
        calls = []

        def fake_fetcher(symbol, start, end):
            calls.append(symbol)
            return _synth_bars(start, end)

        paths = fetch_and_cache_bars(
            ["AAPL", "MSFT"],
            start=date(2024, 1, 1), end=date(2024, 1, 5),
            cache_dir=tmp_path, fetcher=fake_fetcher,
        )
        assert set(paths.keys()) == {"AAPL", "MSFT"}
        assert (tmp_path / "AAPL.csv").exists()
        assert (tmp_path / "MSFT.csv").exists()
        assert calls == ["AAPL", "MSFT"]

    def test_reuses_cache_on_subsequent_call(self, tmp_path: Path):
        calls = []

        def fake_fetcher(symbol, start, end):
            calls.append(symbol)
            return _synth_bars(start, end)

        fetch_and_cache_bars(
            ["AAPL"], start=date(2024, 1, 1), end=date(2024, 1, 5),
            cache_dir=tmp_path, fetcher=fake_fetcher,
        )
        # Second call: nothing new should be fetched.
        fetch_and_cache_bars(
            ["AAPL"], start=date(2024, 1, 1), end=date(2024, 1, 5),
            cache_dir=tmp_path, fetcher=fake_fetcher,
        )
        assert calls == ["AAPL"]  # only the first run hit the fetcher

    def test_force_refresh_re_fetches(self, tmp_path: Path):
        calls = []

        def fake_fetcher(symbol, start, end):
            calls.append(symbol)
            return _synth_bars(start, end)

        for _ in range(2):
            fetch_and_cache_bars(
                ["AAPL"], start=date(2024, 1, 1), end=date(2024, 1, 5),
                cache_dir=tmp_path, fetcher=fake_fetcher, force_refresh=True,
            )
        assert calls == ["AAPL", "AAPL"]

    def test_empty_data_skips_cache_write(self, tmp_path: Path):
        def empty_fetcher(symbol, start, end):
            return pd.DataFrame()

        paths = fetch_and_cache_bars(
            ["DELISTED"], start=date(2024, 1, 1), end=date(2024, 1, 5),
            cache_dir=tmp_path, fetcher=empty_fetcher,
        )
        assert paths == {}
        assert not (tmp_path / "DELISTED.csv").exists()


class TestLoadBars:
    def test_loads_cached_bars(self, tmp_path: Path):
        def fake_fetcher(symbol, start, end):
            return _synth_bars(start, end)

        fetch_and_cache_bars(
            ["AAPL"], start=date(2024, 1, 1), end=date(2024, 1, 11),
            cache_dir=tmp_path, fetcher=fake_fetcher,
        )
        df = load_bars("AAPL", cache_dir=tmp_path)
        assert len(df) == 10
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df.index.name == "date"

    def test_missing_returns_empty(self, tmp_path: Path):
        df = load_bars("NOPE", cache_dir=tmp_path)
        assert df.empty

    def test_slice_by_date_range(self, tmp_path: Path):
        def fake_fetcher(symbol, start, end):
            return _synth_bars(start, end)

        fetch_and_cache_bars(
            ["AAPL"], start=date(2024, 1, 1), end=date(2024, 1, 11),
            cache_dir=tmp_path, fetcher=fake_fetcher,
        )
        df = load_bars(
            "AAPL", cache_dir=tmp_path,
            start=date(2024, 1, 5), end=date(2024, 1, 8),
        )
        # Inclusive start, exclusive end -- 3 days: 5, 6, 7.
        assert len(df) == 3


class TestTopNByMcap:
    def test_ranks_and_caches(self, tmp_path: Path):
        caps = {"AAPL": 3_000_000, "MSFT": 2_500_000, "GOOG": 1_500_000, "X": 100_000}
        calls: list[str] = []

        def fake_fetcher(symbol):
            calls.append(symbol)
            return caps.get(symbol)

        top = get_top_n_by_mcap(
            list(caps.keys()), n=3, cache_dir=tmp_path, fetcher=fake_fetcher,
        )
        assert top == ["AAPL", "MSFT", "GOOG"]
        assert (tmp_path / MARKET_CAP_FILENAME).exists()

        # Second call: cache hit, no new fetches.
        calls.clear()
        top2 = get_top_n_by_mcap(
            list(caps.keys()), n=3, cache_dir=tmp_path, fetcher=fake_fetcher,
        )
        assert top2 == top
        assert calls == []

    def test_excludes_symbols_with_no_mcap(self, tmp_path: Path):
        caps = {"AAPL": 3_000_000, "DELISTED": None, "MSFT": 2_500_000}

        def fake_fetcher(symbol):
            return caps.get(symbol)

        top = get_top_n_by_mcap(
            list(caps.keys()), n=10, cache_dir=tmp_path, fetcher=fake_fetcher,
        )
        # DELISTED dropped (no mcap); only 2 ranked.
        assert top == ["AAPL", "MSFT"]

    def test_force_refresh_re_fetches_all(self, tmp_path: Path):
        caps_old = {"AAPL": 1_000_000}
        caps_new = {"AAPL": 5_000_000}
        state = {"caps": caps_old}

        def fake_fetcher(symbol):
            return state["caps"].get(symbol)

        get_top_n_by_mcap(["AAPL"], n=1, cache_dir=tmp_path, fetcher=fake_fetcher)
        state["caps"] = caps_new
        get_top_n_by_mcap(
            ["AAPL"], n=1, cache_dir=tmp_path,
            fetcher=fake_fetcher, force_refresh=True,
        )

        import json
        cache = json.loads((tmp_path / MARKET_CAP_FILENAME).read_text())
        assert cache["AAPL"] == 5_000_000

    def test_top_n_smaller_than_universe(self, tmp_path: Path):
        caps = {f"S{i}": 1000 - i for i in range(50)}

        def fake_fetcher(symbol):
            return caps.get(symbol)

        top5 = get_top_n_by_mcap(
            list(caps.keys()), n=5, cache_dir=tmp_path, fetcher=fake_fetcher,
        )
        assert top5 == ["S0", "S1", "S2", "S3", "S4"]
