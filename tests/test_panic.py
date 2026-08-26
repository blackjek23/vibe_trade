"""Tests for `risk/panic.py` (`panic_close_all`).

Zero coverage before PROJECT_EVALUATION.md's audit flagged it as "the single
most destructive path in the repository" with nothing verifying it. Its own
logging counted failures as closures -- `f"PANIC: Closed {len(results)}
positions"` reported N closed whether or not a single order was accepted.
These tests pin the corrected behavior: `PanicResult.all_succeeded` reflects
actual outcomes, not attempt counts.
"""

from __future__ import annotations

from vibe_trade.broker.models import AccountSummary, OrderRequest, OrderResult, Position
from vibe_trade.risk.panic import panic_close_all


class MockBroker:
    """BaseBroker stand-in. Records every order placed and returns canned
    results, optionally raising for a specific symbol.
    """

    def __init__(
        self,
        positions: list[Position],
        place_result_factory=None,
        cancelled_orders: int = 0,
        raise_for_symbol: str | None = None,
    ):
        self._positions = positions
        self.cancelled_orders = cancelled_orders
        self.orders_placed: list[OrderRequest] = []
        self._raise_for_symbol = raise_for_symbol
        self._place_result_factory = place_result_factory or (
            lambda req, idx: OrderResult(
                order_id=1000 + idx, symbol=req.symbol, side=req.side,
                quantity=req.quantity, status="FILLED", fill_price=100.0,
            )
        )

    async def get_account_summary(self) -> AccountSummary:
        raise NotImplementedError  # panic_close_all never needs this

    async def get_positions(self) -> list[Position]:
        return list(self._positions)

    async def cancel_all_orders(self) -> int:
        return self.cancelled_orders

    async def place_market_order(self, request: OrderRequest) -> OrderResult:
        if request.symbol == self._raise_for_symbol:
            raise ConnectionError("simulated IB disconnect mid-close")
        idx = len(self.orders_placed)
        self.orders_placed.append(request)
        return self._place_result_factory(request, idx)


def _pos(symbol: str, qty: int) -> Position:
    return Position(
        symbol=symbol, quantity=qty, avg_cost=100.0, market_price=99.0,
        market_value=qty * 99.0, unrealized_pnl=0.0,
    )


class TestCancelsOrdersFirst:
    async def test_cancel_count_recorded(self):
        broker = MockBroker([], cancelled_orders=7)
        result = await panic_close_all(broker)
        assert result.cancelled_orders == 7

    async def test_no_positions_no_orders_placed(self):
        broker = MockBroker([], cancelled_orders=0)
        result = await panic_close_all(broker)
        assert result.closed == 0
        assert result.failed == 0
        assert result.all_succeeded is True
        assert broker.orders_placed == []


class TestClosesEveryPosition:
    async def test_long_position_sells_full_quantity(self):
        broker = MockBroker([_pos("AAPL", 50)])
        result = await panic_close_all(broker)

        assert len(broker.orders_placed) == 1
        req = broker.orders_placed[0]
        assert req.symbol == "AAPL"
        assert req.side == "SELL"
        assert req.quantity == 50
        assert result.closed == 1
        assert result.all_succeeded is True

    async def test_short_position_buys_to_cover(self):
        """V2 doesn't place shorts, but panic must still be able to close
        one if it somehow exists -- BUY the absolute quantity."""
        broker = MockBroker([_pos("TSLA", -20)])
        result = await panic_close_all(broker)

        req = broker.orders_placed[0]
        assert req.side == "BUY"
        assert req.quantity == 20
        assert result.closed == 1

    async def test_zero_quantity_position_skipped(self):
        broker = MockBroker([_pos("DEAD", 0)])
        result = await panic_close_all(broker)

        assert broker.orders_placed == []
        assert result.closed == 0
        assert result.failed == 0

    async def test_multiple_positions_all_closed(self):
        broker = MockBroker([_pos("AAPL", 10), _pos("MSFT", 5), _pos("GOOG", -3)])
        result = await panic_close_all(broker)

        assert len(broker.orders_placed) == 3
        assert result.closed == 3
        assert result.all_succeeded is True


class TestFailureAccounting:
    """The core regression: outcomes, not attempts, drive success reporting."""

    def _rejected_factory(self, req, idx):
        return OrderResult(
            order_id=idx, symbol=req.symbol, side=req.side,
            quantity=req.quantity, status="Cancelled",
        )

    async def test_rejected_order_counts_as_failed_not_closed(self):
        broker = MockBroker(
            [_pos("AAPL", 10)], place_result_factory=self._rejected_factory,
        )
        result = await panic_close_all(broker)

        assert result.closed == 0
        assert result.failed == 1
        assert result.all_succeeded is False
        assert result.details[0]["ok"] is False
        assert result.details[0]["status"] == "Cancelled"

    async def test_exception_during_close_counts_as_failed(self):
        broker = MockBroker([_pos("AAPL", 10)], raise_for_symbol="AAPL")
        result = await panic_close_all(broker)

        assert result.closed == 0
        assert result.failed == 1
        assert result.all_succeeded is False
        assert result.details[0]["status"] == "ERROR"
        assert "simulated IB disconnect" in result.details[0]["error"]

    async def test_one_bad_symbol_does_not_abort_the_rest(self):
        """A crash on one position must not leave the others untouched."""
        broker = MockBroker(
            [_pos("AAPL", 10), _pos("MSFT", 5), _pos("GOOG", 3)],
            raise_for_symbol="MSFT",
        )
        result = await panic_close_all(broker)

        assert result.closed == 2   # AAPL, GOOG
        assert result.failed == 1   # MSFT
        assert result.all_succeeded is False
        symbols_attempted = {d["symbol"] for d in result.details}
        assert symbols_attempted == {"AAPL", "MSFT", "GOOG"}

    async def test_pending_submit_still_counts_as_closed(self):
        """H-5 consistency: a live-but-not-yet-filled order is a success,
        not a failure -- panic uses the same failure-status definition as
        submit, not a stricter one.
        """
        def factory(req, idx):
            return OrderResult(
                order_id=idx, symbol=req.symbol, side=req.side,
                quantity=req.quantity, status="PendingSubmit",
            )
        broker = MockBroker([_pos("AAPL", 10)], place_result_factory=factory)
        result = await panic_close_all(broker)

        assert result.closed == 1
        assert result.failed == 0
        assert result.all_succeeded is True


class TestAllSucceededProperty:
    """Regression guard for the exact bug named in the audit: the old log
    line reported success by counting attempts, not checking outcomes.
    """

    async def test_mixed_results_report_not_all_succeeded(self):
        broker = MockBroker(
            [_pos("AAPL", 10), _pos("MSFT", 5)], raise_for_symbol="MSFT",
        )
        result = await panic_close_all(broker)

        # The old code's signal ("N closed") would have said "2 closed" here
        # regardless of MSFT's failure -- len(details) == 2 either way.
        assert len(result.details) == 2
        assert result.all_succeeded is False
