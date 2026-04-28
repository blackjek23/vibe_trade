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
from vibe_trade.jobs.submit import run_submit
from vibe_trade.risk.manager import RiskManager
from vibe_trade.strategy.examples.donchian import DonchianStrategy


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
    ):
        self.account = account
        self._positions = positions
        self.orders_placed: list[OrderRequest] = []
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

    async def place_market_order(self, order_request: OrderRequest) -> OrderResult:
        idx = len(self.orders_placed)
        self.orders_placed.append(order_request)
        return self._place_result_factory(order_request, idx)


def _account(net_liq: float = 100_000.0) -> AccountSummary:
    return AccountSummary(
        account_id="DU000001",
        net_liquidation=net_liq,
        total_cash=net_liq * 0.4,
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
            strategy=DonchianStrategy(),
            data_provider=MockDataProvider(),
            risk_manager=_risk_mgr(),
            universe=[],
        )
        assert result.exits_evaluated == 0
        assert result.entries_evaluated == 0
        assert result.exits_placed == 0
        assert result.entries_placed == 0
        assert broker.orders_placed == []


class TestExitsPhase:
    async def test_sell_signal_places_sell_at_held_quantity(self):
        broker = MockBroker(_account(), [_position("AAPL", qty=12)])
        dp = MockDataProvider({"AAPL": _candles_for(close=98.0)})  # SELL signal
        result = await run_submit(
            broker=broker,
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
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
            strategy=DonchianStrategy(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["META", "GOOG"],
        )
        assert result.entries_evaluated == 2
        assert result.entries_placed == 1  # GOOG fired
        assert broker.orders_placed[0].symbol == "GOOG"

    async def test_exception_in_one_ticker_doesnt_abort(self):
        class FlakyProvider(MockDataProvider):
            async def get_candles(self, symbol, timeframe="1h", lookback_days=60):
                if symbol == "BAD":
                    raise RuntimeError("yfinance flaked")
                return await super().get_candles(symbol, timeframe, lookback_days)

        broker = MockBroker(_account(), [])
        dp = FlakyProvider({"GOOG": _candles_for(close=101.0)})
        result = await run_submit(
            broker=broker,
            strategy=DonchianStrategy(),
            data_provider=dp,
            risk_manager=_risk_mgr(),
            universe=["BAD", "GOOG"],
        )
        assert result.entries_failed == 1
        assert result.entries_placed == 1
        assert any("BAD" in e for e in result.errors)
        assert broker.orders_placed[0].symbol == "GOOG"


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
