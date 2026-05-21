"""Universe loader tests — enforces the 'one order per ticker per day' invariant."""

from __future__ import annotations

from vibe_trade.config import UniverseConfig
from vibe_trade.data.universe import SP500_SYMBOLS, load_universe, normalize_symbol


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


class TestSymbolNormalization:
    """Hygiene #1: yfinance expects class shares with a hyphen (BRK-B), not a
    dot (BRK.B). A dotted symbol silently returns no bars. normalize_symbol
    converts the form; the static list is already curated to the hyphen form.
    """

    def test_normalize_converts_dot_to_hyphen(self):
        assert normalize_symbol("BRK.B") == "BRK-B"
        assert normalize_symbol("BF.B") == "BF-B"

    def test_normalize_uppercases_and_strips(self):
        assert normalize_symbol(" brk.b ") == "BRK-B"

    def test_normalize_leaves_plain_symbol_untouched(self):
        assert normalize_symbol("AAPL") == "AAPL"

    def test_sp500_list_uses_yfinance_hyphen_form(self):
        dotted = [s for s in SP500_SYMBOLS if "." in s]
        assert dotted == [], f"dotted tickers will fail yfinance: {dotted}"

    def test_sp500_list_excludes_confirmed_delistings(self):
        delisted = {
            "ATVI", "FRC", "SIVB", "FBHS", "CDAY", "CTLT", "DFS", "PARA",
            "PEAK", "WRK", "PXD", "FLT", "PKI", "RE", "DISH", "ANSS",
        }
        still_present = delisted & set(SP500_SYMBOLS)
        assert still_present == set(), f"delisted tickers still listed: {still_present}"

    def test_load_universe_normalizes_dotted_custom_symbol(self):
        cfg = UniverseConfig(source="custom", custom_symbols=["BRK.B", "AAPL"])
        assert load_universe(cfg) == ["BRK-B", "AAPL"]
