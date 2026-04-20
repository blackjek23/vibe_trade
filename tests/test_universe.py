"""Universe loader tests — enforces the 'one order per ticker per day' invariant."""

from __future__ import annotations

from vibe_trade.config import UniverseConfig
from vibe_trade.data.universe import SP500_SYMBOLS, load_universe


class TestUniverseUniqueness:
    """The scanner loops `for symbol in universe` once per scan. If the
    universe contains duplicates, a single scan can produce multiple orders
    for the same ticker — violating the 'one order per ticker per day'
    invariant. These tests lock that contract in place.
    """

    def test_sp500_list_has_no_duplicates(self):
        assert len(SP500_SYMBOLS) == len(set(SP500_SYMBOLS)), (
            f"SP500_SYMBOLS contains duplicates: "
            f"{sorted({s for s in SP500_SYMBOLS if SP500_SYMBOLS.count(s) > 1})}"
        )

    def test_load_universe_sp500_is_unique(self):
        symbols = load_universe(UniverseConfig(source="sp500"))
        assert len(symbols) == len(set(symbols))

    def test_load_universe_custom_dedupes_input(self):
        # Even if the user passes duplicates in config, load_universe must
        # strip them — otherwise the scanner would place multiple orders.
        cfg = UniverseConfig(source="custom", custom_symbols=["AAPL", "MSFT", "AAPL", "msft"])
        symbols = load_universe(cfg)
        assert len(symbols) == len(set(symbols))
        assert set(symbols) == {"AAPL", "MSFT"}

    def test_load_universe_custom_empty(self):
        cfg = UniverseConfig(source="custom", custom_symbols=[])
        assert load_universe(cfg) == []
