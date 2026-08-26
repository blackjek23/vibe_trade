"""Tests for the V2 submit job (`vibe_trade.jobs.submit.run_submit`).

Strategy: real DonchianStrategy. Broker + data provider: mocks. Risk manager:
real instance with default V2 config.

Donchian signal recipe used throughout (see test_donchian.py for the spec):
- 20 bars at high=100, low=99, close=99.5
- Then 1 evaluation bar at close=X
  - X=101 -> BUY (close > prior 20-day high 100)
  - X=98  -> SELL (close < prior 20-day low 99)
  - X=99.5 -> HOLD
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from vibe_trade.broker.models import (
    AccountSummary,
    OrderRequest,
    OrderResult,
    Position,
)
from vibe_trade.config import RiskConfig
from vibe_trade.data.provider import DataProvider
from vibe_trade.jobs.submit import _last_bar_is_closed, run_submit
from vibe_trade.risk.manager import RiskManager
from vibe_trade.strategy.base import BaseStrategy, SignalResult, SignalType
from vibe_trade.strategy.examples.donchian import DonchianStrategy
from vibe_trade.strategy.registry import BuiltStrategy


def _strats(strategy=None, pct: float = 0.018) -> list[BuiltStrategy]:
    """Wrap one strategy (default Donchian) as the priority-ordered list the
    new run_submit expects. Keeps the single-strategy tests concise.
    """
    return [BuiltStrategy(strategy=strategy or DonchianStrategy(), pct_per_position=pct)]


# ----------------------------------------------------------------- fixtures
def _candles_for(close: float, period: int = 20) -> pd.DataFrame:
    """Build OHLC where prior `period` bars sit in [99, 100] and the last
    bar closes at the given price.
    """
    n = period + 1
    return pd.DataFrame(
        {
            "open": [99.5] * period + [close],
            "high": [100.0] * period + [max(close, 100.0)],
            "low": [99.0] * period + [min(close, 99.0)],
            "close": [99.5] * period + [close],
            "volume": [1000] * n,
        },
        index=pd.date_range("2026-01-01", periods=n, freq="D"),
    )


class MockDataProvider(DataProvider):
    """yfinance stand-in. Per-symbol canned candles; missing symbols -> empty."""

    def __init__(self, candles_by_symbol: dict[str, pd.DataFrame] | None = None):
        self.candles_by_symbol = candles_by_symbol or {}
        self.calls: list[str] = []

    async def get_candles(self, symbol, timeframe="1h", lookback_days=60):
        self.calls.append(symbol)
        return self.candles_by_symbol.get(symbol, pd.DataFrame())


class MockBroker:
    """BaseBroker stand-in. Records every order_request and returns canned results."""

    def __init__(
        self,
        account: AccountSummary,
        positions: list[Position],
        place_result_factory=None,
        today_order_refs: set[str] | None = None,
    ):
        self.account = account
        self._positions = positions
        self.orders_placed: list[OrderRequest] = []
        # orderRefs already at the broker today (double-run guard input).
        self._today_order_refs = today_order_refs or set()
        # Default: every order returns FILLED
        self._place_result_factory = place_result_factory or (
            lambda req, idx: OrderResult(
                order_id=1000 + idx,
                symbol=req.symbol,
                side=req.side,
                quantity=req.quantity,
                status="FILLED",
                fill_price=100.0,
            )
        )

    async def get_account_summary(self) -> AccountSummary:
        return self.account

    async def get_positions(self) -> list[Position]:
        return list(self._positions)

    async def get_today_order_refs(self) -> set[str]:
        return set(self._today_order_refs)

    async def place_market_order(self, order_request: OrderRequest) -> OrderResult:
        idx = len(self.orders_placed)
        self.orders_placed.append(order_request)
        return self._place_result_factory(order_request, idx)


def _account(
    net_liq: float = 100_000.0, total_cash: float | None = None,
    account_id: str = "DU000001",
) -> AccountSummary:
    """Default total_cash == net_liq: a flat account (no positions) should
    hold ~all its equity as cash (C-1's empty-position guard relies on this).
    Tests exercising that guard, or a mix of held positions and cash, pass
    total_cash explicitly.
    """
    return AccountSummary(
        account_id=account_id,
        net_liquidation=net_liq,
        total_cash=net_liq if total_cash is None else total_cash,
        unrealized_pnl=0.0,
        realized_pnl=0.0,
    )


def _position(symbol: str, qty: int = 10) -> Position:
    return Position(
        symbol=symbol,
        quantity=qty,
        avg_cost=100.0,
        market_price=99.5,
        market_value=qty * 99.5,
        unrealized_pnl=0.0,
    )


def _risk_mgr(max_positions: int = 50) -> RiskManager:
    return RiskManager(RiskConfig(max_open_positions=max_positions))


# ============================================================== tests


class TestEmpty:
    async def test_no_positions_no_universe(self):
        broker = MockBroker(_account(), [])
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
        )
        assert result.exits_evaluated == 0
        assert result.entries_evaluated == 0
        assert result.exits_placed == 0
        assert result.entries_placed == 0
        assert broker.orders_placed == []


class TestDoubleRunGuard:
    """Submit has no DB state, so IB is the dedup source: strategy orderRefs
    already present today mean submit ran -- a re-run (cron retry, manual
    re-invocation) must abort instead of duplicating every order.
    """

    async def test_aborts_when_strategy_ref_already_at_ib(self):
        broker = MockBroker(
            _account(), [_position("AAPL", qty=12)],
            today_order_refs={"donchian"},
        )
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})  # would SELL
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["AAPL"],
        )
        assert result.aborted_duplicate_run is True
        assert broker.orders_placed == []          # nothing duplicated
        assert any("already ran today" in e for e in result.errors)

    async def test_trim_ref_alone_also_aborts(self):
        broker = MockBroker(_account(), [], today_order_refs={"trim"})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
        )
        assert result.aborted_duplicate_run is True

    async def test_unrelated_refs_do_not_abort(self):
        """Manual override orders (order_ref='manual') must not block submit."""
        broker = MockBroker(_account(), [], today_order_refs={"manual"})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
        )
        assert result.aborted_duplicate_run is False

    async def test_force_bypasses_guard(self):
        broker = MockBroker(
            _account(), [_position("AAPL", qty=12)],
            today_order_refs={"donchian"},
        )
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})  # SELL
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=[],
            force=True,
        )
        assert result.aborted_duplicate_run is False
        assert result.exits_placed == 1  # proceeded normally


class TestExitsPhase:
    async def test_sell_signal_places_sell_at_held_quantity(self):
        broker = MockBroker(_account(), [_position("AAPL", qty=12)])
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})  # SELL signal
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=[],  # only exit
        )
        assert result.exits_evaluated == 1
        assert result.exits_signaled == 1
        assert result.exits_placed == 1
        assert len(broker.orders_placed) == 1
        order = broker.orders_placed[0]
        assert order.symbol == "AAPL"
        assert order.side == "SELL"
        assert order.quantity == 12  # full position size

    async def test_hold_signal_does_not_place_order(self):
        broker = MockBroker(_account(), [_position("AAPL", qty=12)])
        dp = MockDataProvider({"AAPL": _candles_for(close=99.5)})  # HOLD
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=[],
        )
        assert result.exits_evaluated == 1
        assert result.exits_signaled == 0
        assert result.exits_placed == 0
        assert broker.orders_placed == []

    async def test_buy_signal_on_held_position_does_not_double_buy(self):
        # Strategy returning BUY on something we already own: entries phase
        # skips held tickers, exits phase ignores BUY signals -> nothing placed.
        broker = MockBroker(_account(), [_position("AAPL", qty=12)])
        dp = MockDataProvider({"AAPL": _candles_for(close=101.0)})  # BUY
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["AAPL"],  # in universe but already held
        )
        assert result.exits_signaled == 0
        assert result.entries_signaled == 0  # AAPL skipped in entries (held)
        assert broker.orders_placed == []

    async def test_short_position_skipped(self):
        # qty <= 0 not in the long-position set; exits phase doesn't run on it.
        broker = MockBroker(_account(), [_position("AAPL", qty=-10)])
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=[],
        )
        assert result.held_count == 0
        assert result.exits_evaluated == 0


class TestEntriesPhase:
    async def test_buy_signal_sized_and_placed(self):
        # $100K * 1.8% = $1,800 target. Last close = $101. 1800/101 = 17 shares.
        broker = MockBroker(_account(net_liq=100_000.0), [])
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["GOOG"],
        )
        assert result.entries_signaled == 1
        assert result.entries_placed == 1
        assert len(broker.orders_placed) == 1
        order = broker.orders_placed[0]
        assert order.symbol == "GOOG"
        assert order.side == "BUY"
        # cents-based: 100_000 * 0.018 * 100 = 180000 cents target, price 10100 cents
        # 180000 // 10100 = 17
        assert order.quantity == 17

    async def test_sell_signal_in_entries_phase_ignored(self):
        # Strategy returns SELL on a universe (not held) ticker -> no BUY placed.
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"GOOG": _candles_for(close=98.0)})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["GOOG"],
        )
        assert result.entries_evaluated == 1
        assert result.entries_signaled == 0
        assert broker.orders_placed == []

    async def test_held_tickers_skipped(self):
        broker = MockBroker(_account(), [_position("AAPL", qty=10)])
        dp = MockDataProvider(
            {
                "AAPL": _candles_for(close=99.5),  # HOLD, anyway irrelevant
                "GOOG": _candles_for(close=101.0),  # BUY
            }
        )
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["AAPL", "GOOG"],
        )
        # AAPL skipped (held), GOOG evaluated
        assert result.entries_evaluated == 1
        assert result.entries_placed == 1
        assert broker.orders_placed[0].symbol == "GOOG"

    async def test_sizer_skips_oversized_share(self):
        # Tiny net_liq -> 1 share already exceeds the per-position target.
        # net_liq=$1,000, pct=1.8% -> target $18; price $101 > $18 -> skip.
        broker = MockBroker(_account(net_liq=1_000.0), [])
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["GOOG"],
        )
        assert result.entries_signaled == 1
        assert result.entries_skipped_sizing == 1
        assert result.entries_placed == 0
        assert broker.orders_placed == []


class TestPositionCap:
    async def test_at_cap_skips_entries_phase_entirely(self):
        # 50 positions held, max=50 -> entries phase short-circuits.
        positions = [_position(f"S{i}") for i in range(50)]
        broker = MockBroker(_account(), positions)
        # Provide BUY-signal candles for one universe ticker; should NEVER be evaluated.
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(max_positions=50),
            universe=["GOOG"],
            max_positions=50,
        )
        assert result.entries_phase_skipped is True
        assert "cap" in result.cap_reason.lower() or "50/50" in result.cap_reason
        assert result.entries_evaluated == 0
        assert "GOOG" not in dp.calls  # never even fetched candles

    async def test_buys_during_run_count_toward_cap(self):
        # Hold 49 positions, max=50. Universe has 3 BUY-signal tickers.
        # Only the first should fill; the next two get skipped by sizer (count >= 50).
        positions = [_position(f"S{i}") for i in range(49)]
        broker = MockBroker(_account(), positions)
        dp = MockDataProvider(
            {
                "GOOG": _candles_for(close=101.0),
                "META": _candles_for(close=101.0),
                "NVDA": _candles_for(close=101.0),
            }
        )
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(max_positions=50),
            universe=["GOOG", "META", "NVDA"],
            max_positions=50,
        )
        assert result.entries_signaled == 3
        assert result.entries_placed == 1   # only the first BUY made it
        assert result.entries_skipped_sizing == 2
        assert len(broker.orders_placed) == 1


class TestPerSymbolErrors:
    async def test_data_fetch_failure_doesnt_abort(self):
        # MockDataProvider returns empty for unknown symbols; strategy short-circuits to HOLD.
        # This isn't an error per se, just no signal. Verify we still process the next ticker.
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})  # META has no data
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["META", "GOOG"],
        )
        assert result.entries_evaluated == 2
        assert result.entries_placed == 1  # GOOG fired
        assert broker.orders_placed[0].symbol == "GOOG"

    async def test_flaky_fetch_isolated_as_data_unavailable(self):
        # Hygiene #1/#4: a yfinance exception for one ticker is isolated by the
        # batch fetch -> that ticker counts as data_unavailable, run continues.
        class FlakyProvider(MockDataProvider):
            async def get_candles(self, symbol, timeframe="1h", lookback_days=60):
                if symbol == "BAD":
                    raise RuntimeError("yfinance flaked")
                return await super().get_candles(symbol, timeframe, lookback_days)

        broker = MockBroker(_account(), [])
        dp = FlakyProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["BAD", "GOOG"],
        )
        assert result.data_unavailable == 1
        assert result.entries_failed == 0
        assert result.entries_placed == 1
        assert broker.orders_placed[0].symbol == "GOOG"

    async def test_data_unavailable_counted_for_empty_candles(self):
        # Hygiene #1 bonus: universe tickers yfinance returns no bars for are
        # tallied in result.data_unavailable (drives the skip-summary log line).
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})  # META, NVDA empty
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["META", "GOOG", "NVDA"],
        )
        assert result.data_unavailable == 2
        assert result.entries_placed == 1


class TestPlacementStatuses:
    """Bug #1 -- PreSubmitted is a successful placement, not a failure.

    A market order placed pre-RTH (16:00 IDT cron, 30 min before US open) sits
    in IB's PreSubmitted state until the market opens. Before this fix, submit
    counted it as failed, producing misleading 'N failed' Telegram alerts.
    """

    async def test_presubmitted_entry_counts_as_placed(self):
        """Single BUY signal, broker returns PreSubmitted -> placed=1 failed=0."""
        def factory(req, idx):
            return OrderResult(
                order_id=idx + 100, symbol=req.symbol, side=req.side,
                quantity=req.quantity, status="PreSubmitted",
            )
        broker = MockBroker(_account(), [], place_result_factory=factory)
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})

        result = await run_submit(
            broker=broker, strategies=_strats(),
            data_provider=dp, risk_manager=_risk_mgr(),
            universe=["GOOG"],
        )

        assert result.entries_placed == 1
        assert result.entries_failed == 0
        assert result.errors == []

    async def test_presubmitted_exit_counts_as_placed(self):
        """SELL on a held position also accepts PreSubmitted."""
        def factory(req, idx):
            return OrderResult(
                order_id=idx + 100, symbol=req.symbol, side=req.side,
                quantity=req.quantity, status="PreSubmitted",
            )
        broker = MockBroker(_account(), [_position("AAPL", qty=12)],
                            place_result_factory=factory)
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})  # SELL

        result = await run_submit(
            broker=broker, strategies=_strats(),
            data_provider=dp, risk_manager=_risk_mgr(),
            universe=[],
        )

        assert result.exits_placed == 1
        assert result.exits_failed == 0

    async def test_all_signals_presubmitted_reports_zero_failed(self):
        """Reproduces the exact Monday-2026-05-11 scenario: 9 entries, all
        PreSubmitted. Before the fix this reported 0 placed, 9 failed.
        """
        def factory(req, idx):
            return OrderResult(
                order_id=idx + 3500, symbol=req.symbol, side=req.side,
                quantity=req.quantity, status="PreSubmitted",
            )
        broker = MockBroker(_account(net_liq=100_000.0), [],
                            place_result_factory=factory)
        symbols = ["BA", "BLK", "CBOE", "EXPD", "FTNT", "GOOGL", "HPE", "NTAP", "NWS"]
        # Every symbol gets a BUY-signal candle set.
        dp = MockDataProvider({s: _candles_for(close=101.0) for s in symbols})

        result = await run_submit(
            broker=broker, strategies=_strats(),
            data_provider=dp, risk_manager=_risk_mgr(),
            universe=symbols,
        )

        assert result.entries_signaled == 9
        assert result.entries_placed == 9
        assert result.entries_failed == 0
        assert result.errors == []


class TestForceTrim:
    """Enhancement #1 -- when we hold more than max_positions, force-sell the
    worst performers (lowest unrealized $ P&L) until we're back at the cap.
    Trim orders carry orderRef="trim" so analytics can distinguish them.
    """

    def _losers_and_winners(self, n_losers: int, n_winners: int) -> list[Position]:
        positions = []
        for i in range(n_winners):
            positions.append(Position(
                symbol=f"WIN{i:02d}", quantity=10, avg_cost=100.0,
                market_price=110.0, market_value=1100.0, unrealized_pnl=100.0 + i,
            ))
        for i in range(n_losers):
            positions.append(Position(
                symbol=f"LOSS{i:02d}", quantity=10, avg_cost=100.0,
                market_price=90.0, market_value=900.0,
                unrealized_pnl=-50.0 * (i + 1),
            ))
        return positions

    async def test_over_cap_trims_to_max(self):
        """Held=60 (50 winners + 10 losers), max=50 -> 10 trim SELLs placed,
        all carrying orderRef='trim' and targeting the 10 losers."""
        positions = self._losers_and_winners(n_losers=10, n_winners=50)
        broker = MockBroker(_account(), positions)
        # Empty universe + HOLD candles for held: no Donchian exits, no entries.
        dp = MockDataProvider()  # no candles -> strategy short-circuits
        result = await run_submit(
            broker=broker, strategies=_strats(),
            data_provider=dp, risk_manager=_risk_mgr(max_positions=50),
            universe=[],
        )
        assert result.exits_placed == 0
        assert result.trim_signaled == 10
        assert result.trim_placed == 10
        assert result.trim_failed == 0
        # All 10 trim orders are SELLs with order_ref="trim".
        trim_orders = [o for o in broker.orders_placed if o.order_ref == "trim"]
        assert len(trim_orders) == 10
        assert all(o.side == "SELL" for o in trim_orders)
        # Symbols should be exactly the 10 losers.
        assert {o.symbol for o in trim_orders} == {f"LOSS{i:02d}" for i in range(10)}

    async def test_at_cap_no_trim(self):
        """Held=50, max=50 -> no trim."""
        positions = self._losers_and_winners(n_losers=5, n_winners=45)
        broker = MockBroker(_account(), positions)
        dp = MockDataProvider()
        result = await run_submit(
            broker=broker, strategies=_strats(),
            data_provider=dp, risk_manager=_risk_mgr(max_positions=50),
            universe=[],
        )
        assert result.trim_signaled == 0
        assert result.trim_placed == 0
        assert not any(o.order_ref == "trim" for o in broker.orders_placed)

    async def test_donchian_exits_relieve_cap_no_trim(self):
        """Held=55, max=50, Donchian signals 5 SELLs -> no force-trim needed."""
        # 5 positions get a SELL signal, 50 hold.
        positions = []
        candle_map = {}
        for i in range(5):
            sym = f"EX{i:02d}"
            positions.append(Position(
                symbol=sym, quantity=10, avg_cost=100.0,
                market_price=95.0, market_value=950.0, unrealized_pnl=-50.0,
            ))
            candle_map[sym] = _candles_for(close=98.0)  # SELL
        for i in range(50):
            sym = f"HOLD{i:02d}"
            positions.append(Position(
                symbol=sym, quantity=10, avg_cost=100.0,
                market_price=110.0, market_value=1100.0, unrealized_pnl=100.0,
            ))
            candle_map[sym] = _candles_for(close=99.5)  # HOLD

        broker = MockBroker(_account(), positions)
        dp = MockDataProvider(candle_map)
        result = await run_submit(
            broker=broker, strategies=_strats(),
            data_provider=dp, risk_manager=_risk_mgr(max_positions=50),
            universe=[],
        )
        assert result.exits_placed == 5      # Donchian got its 5
        assert result.trim_signaled == 0     # nothing left to trim
        assert result.trim_placed == 0

    async def test_donchian_exits_excluded_from_trim(self):
        """Held=60, Donchian SELLs 3 of the worst, max=50 -> trim 7 OTHER names.
        Trim list must not overlap with Donchian exits."""
        positions = []
        candle_map = {}
        # 3 worst performers; Donchian decides to exit them.
        for i in range(3):
            sym = f"DONCH{i:02d}"
            positions.append(Position(
                symbol=sym, quantity=10, avg_cost=100.0,
                market_price=70.0, market_value=700.0,
                unrealized_pnl=-300.0 - i,  # the 3 most negative
            ))
            candle_map[sym] = _candles_for(close=98.0)  # SELL
        # 57 others with varying P&L; force-trim should pick 7 worst of these.
        for i in range(57):
            sym = f"OTH{i:02d}"
            pnl = -100.0 + i  # ranges from -100 to -44 (then -43..+...)
            positions.append(Position(
                symbol=sym, quantity=10, avg_cost=100.0,
                market_price=100.0 + pnl / 10, market_value=1000.0 + pnl,
                unrealized_pnl=pnl,
            ))
            candle_map[sym] = _candles_for(close=99.5)  # HOLD

        broker = MockBroker(_account(), positions)
        dp = MockDataProvider(candle_map)
        result = await run_submit(
            broker=broker, strategies=_strats(),
            data_provider=dp, risk_manager=_risk_mgr(max_positions=50),
            universe=[],
        )
        assert result.exits_placed == 3
        assert result.trim_signaled == 7
        assert result.trim_placed == 7
        trim_orders = [o for o in broker.orders_placed if o.order_ref == "trim"]
        donchian_orders = [
            o for o in broker.orders_placed
            if o.side == "SELL" and o.order_ref != "trim"
        ]
        assert len(trim_orders) == 7
        assert len(donchian_orders) == 3
        # Trim and Donchian sets must be disjoint.
        trim_symbols = {o.symbol for o in trim_orders}
        donchian_symbols = {o.symbol for o in donchian_orders}
        assert not (trim_symbols & donchian_symbols)
        # Trim should hit the 7 most-negative OTHers: OTH00..OTH06.
        assert trim_symbols == {f"OTH{i:02d}" for i in range(7)}

    async def test_trim_orders_tagged_orderref_trim(self):
        """Single over-cap trim: verify order_ref='trim' on the placed order."""
        positions = self._losers_and_winners(n_losers=1, n_winners=50)
        broker = MockBroker(_account(), positions)
        dp = MockDataProvider()
        result = await run_submit(
            broker=broker, strategies=_strats(),
            data_provider=dp, risk_manager=_risk_mgr(max_positions=50),
            universe=[],
        )
        assert result.trim_placed == 1
        trim_orders = [o for o in broker.orders_placed if o.order_ref == "trim"]
        assert len(trim_orders) == 1
        assert trim_orders[0].symbol == "LOSS00"
        assert trim_orders[0].side == "SELL"


class _Fake(BaseStrategy):
    """Strategy with a controllable signal, for testing orchestration logic."""

    def __init__(self, name, signal=SignalType.HOLD, signal_map=None, required=1):
        self._name = name
        self._sig = signal
        self._map = signal_map or {}
        self._req = required

    @property
    def name(self):
        return self._name

    @property
    def required_candles(self):
        return self._req

    def evaluate(self, symbol, candles):
        return SignalResult(
            signal=self._map.get(symbol, self._sig),
            symbol=symbol,
            strategy_name=self._name,
        )


def _built(name, signal=SignalType.HOLD, pct=0.018, signal_map=None):
    return BuiltStrategy(
        strategy=_Fake(name, signal=signal, signal_map=signal_map),
        pct_per_position=pct,
    )


class TestMultiStrategyEntries:
    async def test_priority_first_buy_wins(self):
        # Two strategies both BUY GOOG -> highest priority ("a") claims it once.
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategies=[_built("a", SignalType.BUY), _built("b", SignalType.BUY)],
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["GOOG"],
        )
        assert result.entries_signaled == 1
        assert result.entries_placed == 1
        assert len(broker.orders_placed) == 1
        assert broker.orders_placed[0].order_ref == "a"
        assert result.entries_placed_by_strategy == {"a": 1}

    async def test_lower_priority_wins_when_higher_holds(self):
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategies=[_built("a", SignalType.HOLD), _built("b", SignalType.BUY)],
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["GOOG"],
        )
        assert result.entries_placed == 1
        assert broker.orders_placed[0].order_ref == "b"

    async def test_buy_order_ref_set_for_real_donchian(self):
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),  # real Donchian
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["GOOG"],
        )
        assert result.entries_placed == 1
        assert broker.orders_placed[0].order_ref == "donchian"

    async def test_per_strategy_pct_override_sizes_differently(self):
        # pct=0.009 -> target $900, price $101 -> 8 shares (vs 17 at 1.8%).
        broker = MockBroker(_account(net_liq=100_000.0), [])
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategies=[_built("small", SignalType.BUY, pct=0.009)],
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["GOOG"],
        )
        assert result.entries_placed == 1
        assert broker.orders_placed[0].quantity == 8


class TestMultiStrategyExits:
    async def test_only_owner_strategy_evaluates(self):
        # AAPL owned by "b" (which HOLDs). "a" would SELL, but it's not the owner
        # -> no exit. Proves exits are strategy-scoped.
        broker = MockBroker(_account(), [_position("AAPL", qty=10)])
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})
        result = await run_submit(
            broker=broker,
            strategies=[_built("a", SignalType.SELL), _built("b", SignalType.HOLD)],
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=[],
            position_strategies={"AAPL": "b"},
        )
        assert result.exits_placed == 0
        assert broker.orders_placed == []

    async def test_owner_strategy_sell_places_tagged_exit(self):
        broker = MockBroker(_account(), [_position("AAPL", qty=10)])
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})
        result = await run_submit(
            broker=broker,
            strategies=[_built("a", SignalType.HOLD), _built("b", SignalType.SELL)],
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=[],
            position_strategies={"AAPL": "b"},
        )
        assert result.exits_placed == 1
        assert broker.orders_placed[0].order_ref == "b"
        assert result.exits_placed_by_strategy == {"b": 1}

    async def test_orphan_position_uses_highest_priority(self):
        # No owner recorded -> highest-priority strategy ("a") evaluates it.
        broker = MockBroker(_account(), [_position("AAPL", qty=10)])
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})
        result = await run_submit(
            broker=broker,
            strategies=[_built("a", SignalType.SELL), _built("b", SignalType.HOLD)],
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=[],
            position_strategies={},  # orphan
        )
        assert result.exits_placed == 1
        assert broker.orders_placed[0].order_ref == "a"

    async def test_unknown_owner_falls_back_to_default(self):
        # Owner id no longer in the active set -> falls back to strategies[0].
        broker = MockBroker(_account(), [_position("AAPL", qty=10)])
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})
        result = await run_submit(
            broker=broker,
            strategies=[_built("a", SignalType.SELL), _built("b", SignalType.HOLD)],
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=[],
            position_strategies={"AAPL": "ghost"},
        )
        assert result.exits_placed == 1
        assert broker.orders_placed[0].order_ref == "a"


class TestLookbackSizing:
    def test_short_strategy_keeps_floor(self):
        from vibe_trade.jobs.submit import _required_lookback_days

        # Donchian required=21 -> ceil(21*1.6)+15=49 -> floored at 60.
        assert _required_lookback_days(_strats()) == 60

    def test_long_strategy_expands_window(self):
        from vibe_trade.jobs.submit import _required_lookback_days
        from vibe_trade.strategy.examples.sma_crossover import SMACrossoverStrategy

        built = [BuiltStrategy(strategy=SMACrossoverStrategy(), pct_per_position=0.018)]
        # required=50 -> ceil(80)+15 = 95.
        assert _required_lookback_days(built) == 95

    async def test_empty_strategies_rejected(self):
        broker = MockBroker(_account(), [])
        with pytest.raises(ValueError):
            await run_submit(
                broker=broker,
                strategies=[],
                data_provider=MockDataProvider(),
                risk_manager=_risk_mgr(),
                universe=[],
            )


class TestNoDbWrites:
    """Regression guard for V2 invariant: submit must not touch the DB.

    We don't have a DB session passed in -- but verify the function signature
    doesn't accept one, so this can't accidentally regress.
    """

    def test_run_submit_signature_has_no_db_param(self):
        import inspect
        params = set(inspect.signature(run_submit).parameters.keys())
        assert "session" not in params
        assert "db_session" not in params
        assert "session_factory" not in params
        assert "db" not in params


def _candles_ending(last_date: str, close: float, period: int = 20) -> pd.DataFrame:
    """Like `_candles_for`, but the last bar lands on a specific calendar
    date -- needed to control whether it looks "closed" relative to a given
    `now` (H-1 bar-freshness guard).
    """
    n = period + 1
    index = pd.date_range(end=last_date, periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": [99.5] * period + [close],
            "high": [100.0] * period + [max(close, 100.0)],
            "low": [99.0] * period + [min(close, 99.0)],
            "close": [99.5] * period + [close],
            "volume": [1000] * n,
        },
        index=index,
    )


class TestLastBarIsClosed:
    """H-1: distinguish a closed prior-day bar from a live intraday quote
    that lands as the final row during the Israel/US DST mismatch.
    """

    def test_prior_day_bar_is_closed(self):
        candles = _candles_ending("2026-03-05", close=101.0)  # Thursday
        now = datetime(2026, 3, 6, 16, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))  # Friday
        assert _last_bar_is_closed(candles, now=now) is True

    def test_same_us_day_bar_is_not_closed(self):
        """The DST-gap scenario: last bar is today's (in-progress) US session."""
        candles = _candles_ending("2026-03-09", close=101.0)  # Monday
        now = datetime(2026, 3, 9, 16, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        assert _last_bar_is_closed(candles, now=now) is False

    def test_empty_dataframe_is_not_closed(self):
        assert _last_bar_is_closed(pd.DataFrame(), now=datetime.now(ZoneInfo("UTC"))) is False

    def test_naive_now_is_rejected(self):
        candles = _candles_ending("2026-03-05", close=101.0)
        with pytest.raises(ValueError, match="timezone-aware"):
            _last_bar_is_closed(candles, now=datetime(2026, 3, 6, 16, 0))


class TestBarFreshnessGuard:
    """H-1 integration: run_submit must not evaluate a stale (same-US-day) bar
    as though it were a closed prior day, on either the exit or entry side.
    """

    async def test_exit_holds_on_stale_bar_instead_of_evaluating(self):
        broker = MockBroker(_account(), [_position("AAPL", qty=12)])
        # Would SELL if evaluated (close < prior 20-day low), but the bar is
        # today's US date per `now` -- must be held, not acted on.
        dp = MockDataProvider({"AAPL": _candles_ending("2026-03-09", close=98.0)})
        now = datetime(2026, 3, 9, 16, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))

        result = await run_submit(
            broker=broker, strategies=_strats(), data_provider=dp,
            risk_manager=_risk_mgr(), universe=[], now=now,
        )

        assert result.stale_bars_skipped == 1
        assert result.exits_signaled == 0
        assert broker.orders_placed == []

    async def test_entry_skipped_on_stale_bar_instead_of_evaluating(self):
        broker = MockBroker(_account(), [])
        # Would BUY if evaluated (close > prior 20-day high).
        dp = MockDataProvider({"AAPL": _candles_ending("2026-03-09", close=101.0)})
        now = datetime(2026, 3, 9, 16, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))

        result = await run_submit(
            broker=broker, strategies=_strats(), data_provider=dp,
            risk_manager=_risk_mgr(), universe=["AAPL"], now=now,
        )

        assert result.stale_bars_skipped == 1
        assert result.entries_signaled == 0
        assert broker.orders_placed == []

    async def test_closed_bar_with_explicit_now_still_evaluates_normally(self):
        """Sanity check: passing `now` explicitly doesn't itself break the
        ordinary (bar-is-closed) path."""
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"AAPL": _candles_ending("2026-03-05", close=101.0)})
        now = datetime(2026, 3, 6, 16, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))

        result = await run_submit(
            broker=broker, strategies=_strats(), data_provider=dp,
            risk_manager=_risk_mgr(), universe=["AAPL"], now=now,
        )

        assert result.stale_bars_skipped == 0
        assert result.entries_placed == 1


class TestNonTradingDayGuard:
    """H-4: submit must not evaluate anything on a non-trading day. The CLI
    computes `is_trading_day` from the real market calendar before even
    connecting to IB; run_submit takes it as an explicit parameter rather
    than computing it from wall-clock time itself, so this stays
    deterministic regardless of which day the test suite runs on.
    """

    async def test_non_trading_day_aborts_before_anything_else(self):
        broker = MockBroker(
            _account(), [_position("AAPL", qty=12)],
            today_order_refs={"donchian"},  # would also trip the double-run guard
        )
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})  # would SELL
        result = await run_submit(
            broker=broker, strategies=_strats(), data_provider=dp,
            risk_manager=_risk_mgr(), universe=["AAPL"], is_trading_day=False,
        )

        assert result.aborted_non_trading_day is True
        assert result.aborted_duplicate_run is False  # never got that far
        assert broker.orders_placed == []

    async def test_trading_day_true_is_the_default_and_proceeds_normally(self):
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"AAPL": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker, strategies=_strats(), data_provider=dp,
            risk_manager=_risk_mgr(), universe=["AAPL"],
        )

        assert result.aborted_non_trading_day is False
        assert result.entries_placed == 1


class TestPlacementStatusPolarity:
    """H-5: submit must whitelist *failure* statuses, not success statuses.

    The previous approach (PLACEMENT_SUCCESS_STATUSES) omitted PendingSubmit,
    ApiPending and the empty string -- all legitimate for 5 seconds after
    placeOrder() under load -- so a busy open counted live orders as failed
    and breached the position cap. See PROJECT_EVALUATION.md.
    """

    @staticmethod
    def _factory_with_status(status: str):
        def factory(req, idx):
            return OrderResult(
                order_id=idx + 100, symbol=req.symbol, side=req.side,
                quantity=req.quantity, status=status,
            )
        return factory

    async def test_pending_submit_counts_as_placed(self):
        broker = MockBroker(
            _account(), [], place_result_factory=self._factory_with_status("PendingSubmit"),
        )
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker, strategies=_strats(), data_provider=dp,
            risk_manager=_risk_mgr(), universe=["GOOG"],
        )
        assert result.entries_placed == 1
        assert result.entries_failed == 0

    async def test_api_pending_counts_as_placed(self):
        broker = MockBroker(
            _account(), [], place_result_factory=self._factory_with_status("ApiPending"),
        )
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker, strategies=_strats(), data_provider=dp,
            risk_manager=_risk_mgr(), universe=["GOOG"],
        )
        assert result.entries_placed == 1
        assert result.entries_failed == 0

    async def test_empty_status_counts_as_placed(self):
        broker = MockBroker(
            _account(), [], place_result_factory=self._factory_with_status(""),
        )
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker, strategies=_strats(), data_provider=dp,
            risk_manager=_risk_mgr(), universe=["GOOG"],
        )
        assert result.entries_placed == 1
        assert result.entries_failed == 0

    async def test_cancelled_still_counts_as_failed(self):
        broker = MockBroker(
            _account(), [], place_result_factory=self._factory_with_status("Cancelled"),
        )
        dp = MockDataProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker, strategies=_strats(), data_provider=dp,
            risk_manager=_risk_mgr(), universe=["GOOG"],
        )
        assert result.entries_placed == 0
        assert result.entries_failed == 1
        assert result.errors  # order status surfaced for the operator


class TestEmptyPositionGuard:
    """C-1: an empty `get_positions()` read must not be trusted as a genuinely
    flat account unless the cash balance agrees. See PROJECT_EVALUATION.md.
    """

    async def test_empty_positions_with_deployed_cash_aborts(self):
        """Cash well below net_liq with 0 positions read -> suspected failed
        read. Nothing should be placed, exits included.
        """
        broker = MockBroker(_account(total_cash=40_000.0), [])
        dp = MockDataProvider({"AAPL": _candles_for(close=101.0)})  # would BUY
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["AAPL"],
        )
        assert result.aborted_empty_position_read is True
        assert broker.orders_placed == []
        assert any("failed/racing position read" in e for e in result.errors)

    async def test_empty_positions_with_flat_cash_proceeds(self):
        """Cash ~= net_liq with 0 positions read -> genuinely flat, proceed."""
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"AAPL": _candles_for(close=101.0)})  # BUY
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["AAPL"],
        )
        assert result.aborted_empty_position_read is False
        assert result.entries_placed == 1

    async def test_held_positions_skip_the_guard_regardless_of_cash(self):
        """Guard only applies when positions comes back empty."""
        broker = MockBroker(
            _account(total_cash=1_000.0), [_position("AAPL", qty=12)],
        )
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
        )
        assert result.aborted_empty_position_read is False

    async def test_db_reports_open_positions_ib_shows_none_aborts(self):
        """C-1's other half: even with cash that looks perfectly flat, the
        DB reporting OPEN rows IB doesn't see at all is independently
        suspicious -- mirrors reconcile._sweep_open_rows's own instinct.
        """
        broker = MockBroker(_account(), [])  # cash looks fine (flat)
        dp = MockDataProvider({"AAPL": _candles_for(close=101.0)})  # would BUY
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["AAPL"],
            position_strategies={"AAPL": "donchian"},  # DB thinks this is OPEN
        )
        assert result.aborted_empty_position_read is True
        assert broker.orders_placed == []
        assert any("DB reports" in e for e in result.errors)

    async def test_both_signals_present_mentions_both_in_message(self):
        broker = MockBroker(_account(total_cash=40_000.0), [])
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
            position_strategies={"AAPL": "donchian", "MSFT": "sma"},
        )
        assert result.aborted_empty_position_read is True
        msg = result.errors[0]
        assert "total_cash" in msg
        assert "DB reports" in msg
        assert "2 OPEN position" in msg

    async def test_orphan_empty_position_strategies_does_not_trip_the_guard(self):
        """An empty (not None) position_strategies map -- the CLI's genuine
        "no open trades in the DB" case -- must not be mistaken for a
        non-empty one."""
        broker = MockBroker(_account(), [])
        dp = MockDataProvider({"AAPL": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["AAPL"],
            position_strategies={},
        )
        assert result.aborted_empty_position_read is False
        assert result.entries_placed == 1


class TestModeMismatchGuard:
    """SEC-2: config `mode` must match the account IB Gateway is actually
    serving before any order is placed. See PROJECT_EVALUATION.md.
    """

    async def test_paper_config_with_live_account_aborts(self):
        broker = MockBroker(_account(account_id="U1234567"), [])
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
            mode="paper",
        )
        assert result.aborted_mode_mismatch is True
        assert broker.orders_placed == []
        assert any("config mode='paper'" in e for e in result.errors)

    async def test_live_config_with_paper_account_aborts(self):
        broker = MockBroker(_account(account_id="DU000001"), [])
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
            mode="live",
        )
        assert result.aborted_mode_mismatch is True
        assert broker.orders_placed == []

    async def test_live_config_with_live_account_proceeds(self):
        broker = MockBroker(_account(account_id="U1234567"), [])
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
            mode="live",
        )
        assert result.aborted_mode_mismatch is False

    async def test_empty_account_id_does_not_trip_the_guard(self):
        """An unpopulated account_id is preflight's job to catch (Gateway
        mid-login); this guard must not misfire on it as a false mismatch.
        """
        broker = MockBroker(_account(account_id=""), [])
        result = await run_submit(
            broker=broker,
            strategies=_strats(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
        )
        assert result.aborted_mode_mismatch is False
