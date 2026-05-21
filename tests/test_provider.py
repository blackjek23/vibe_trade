"""Tests for DataProvider — yfinance retry + bounded-concurrency batch fetch.

Hygiene #1 (retry transient yfinance failures) and Hygiene #4 (parallel
universe scan) from docs/SESSION_H_FINDINGS.md. yfinance is fully mocked via
the static `_download` hook — no network.
"""

from __future__ import annotations

import asyncio

import pytest

from vibe_trade.data import provider as provider_mod
from vibe_trade.data.provider import DataProvider


def _yf_frame(rows: int = 30):
    """A yfinance-shaped OHLCV DataFrame (columns Open/High/Low/Close/Volume)."""
    import pandas as pd

    idx = pd.date_range("2026-01-01", periods=rows, freq="D")
    return pd.DataFrame(
        {
            "Open": [100.0] * rows,
            "High": [101.0] * rows,
            "Low": [99.0] * rows,
            "Close": [100.5] * rows,
            "Volume": [1000] * rows,
        },
        index=idx,
    )


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Skip the real retry sleep so the suite stays fast."""
    monkeypatch.setattr(provider_mod, "RETRY_BACKOFF_SECONDS", 0.0)


class TestRetry:
    """A single retry recovers transient yfinance failures (curl timeout, 503,
    momentary empty result) — ~6% of the universe flakes per run."""

    async def test_retries_then_succeeds(self, monkeypatch):
        import pandas as pd

        calls: list[str] = []

        def fake(symbol, period, interval):
            calls.append(symbol)
            return pd.DataFrame() if len(calls) == 1 else _yf_frame()

        monkeypatch.setattr(DataProvider, "_download", staticmethod(fake))
        df = await DataProvider().get_candles("AAPL", "1d", 60)
        assert len(calls) == 2  # first empty, retry succeeded
        assert not df.empty
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]

    async def test_empty_after_retries_exhausted(self, monkeypatch):
        import pandas as pd

        calls: list[str] = []

        def fake(symbol, period, interval):
            calls.append(symbol)
            return pd.DataFrame()

        monkeypatch.setattr(DataProvider, "_download", staticmethod(fake))
        df = await DataProvider().get_candles("AAPL", "1d", 60)
        assert len(calls) == 2  # 1 initial + 1 retry, then give up
        assert df.empty

    async def test_retries_on_exception(self, monkeypatch):
        calls: list[str] = []

        def fake(symbol, period, interval):
            calls.append(symbol)
            if len(calls) == 1:
                raise RuntimeError("curl timeout")
            return _yf_frame()

        monkeypatch.setattr(DataProvider, "_download", staticmethod(fake))
        df = await DataProvider().get_candles("AAPL", "1d", 60)
        assert len(calls) == 2
        assert not df.empty


class TestBatch:
    """get_candles_batch fetches the whole universe concurrently with bounded
    parallelism — replaces the ~5-min sequential scan."""

    async def test_returns_every_symbol(self, monkeypatch):
        monkeypatch.setattr(
            DataProvider, "_download", staticmethod(lambda symbol, period, interval: _yf_frame())
        )
        result = await DataProvider().get_candles_batch(["AAPL", "MSFT", "GOOG"])
        assert set(result) == {"AAPL", "MSFT", "GOOG"}
        assert all(not df.empty for df in result.values())

    async def test_isolates_per_symbol_failure(self, monkeypatch):
        def fake(symbol, period, interval):
            if symbol == "BAD":
                raise RuntimeError("yfinance flaked")
            return _yf_frame()

        monkeypatch.setattr(DataProvider, "_download", staticmethod(fake))
        result = await DataProvider().get_candles_batch(["BAD", "AAPL"])
        assert result["BAD"].empty       # failure -> empty df, not an exception
        assert not result["AAPL"].empty  # neighbour unaffected

    async def test_concurrency_is_bounded(self, monkeypatch):
        active = 0
        peak = 0

        async def fake_get_candles(self, symbol, timeframe="1h", lookback_days=60):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _yf_frame()

        monkeypatch.setattr(DataProvider, "get_candles", fake_get_candles)
        symbols = [f"S{i}" for i in range(25)]
        await DataProvider().get_candles_batch(symbols, max_concurrency=5)
        assert peak <= 5
