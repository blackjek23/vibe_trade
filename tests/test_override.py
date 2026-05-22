"""Tests for the Session J manual override jobs (`vibe_trade.jobs.override`).

Two operator commands:
- `run_close_position` -- market-SELL the full IB position for one symbol.
- `run_cancel_pending` -- list working orders, or cancel all orders for a symbol.

Broker is a fake; these functions never touch IB or the DB directly.
"""

from __future__ import annotations

from vibe_trade.broker.models import OpenOrder, OrderRequest, OrderResult, Position
from vibe_trade.jobs.override import run_cancel_pending, run_close_position


# ----------------------------------------------------------------- fakes
class FakeBroker:
    """BaseBroker stand-in. Canned positions + working orders; records calls."""

    def __init__(
        self,
        positions: list[Position] | None = None,
        open_orders: list[OpenOrder] | None = None,
    ):
        self._positions = positions or []
        self._open_orders = open_orders or []
        self.orders_placed: list[OrderRequest] = []
        self.cancel_calls: list[str] = []

    async def get_positions(self) -> list[Position]:
        return list(self._positions)

    async def place_market_order(self, request: OrderRequest) -> OrderResult:
        self.orders_placed.append(request)
        return OrderResult(
            order_id=999,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            status="FILLED",
            fill_price=100.0,
        )

    async def get_open_orders(self) -> list[OpenOrder]:
        return list(self._open_orders)

    async def cancel_orders_for_symbol(self, symbol: str) -> list[OpenOrder]:
        self.cancel_calls.append(symbol)
        return [o for o in self._open_orders if o.symbol == symbol]


def _pos(symbol: str, qty: int) -> Position:
    return Position(
        symbol=symbol,
        quantity=qty,
        avg_cost=100.0,
        market_price=100.0,
        market_value=100.0 * qty,
        unrealized_pnl=0.0,
    )


def _order(symbol: str, side: str = "BUY", qty: int = 10, perm_id: int = 1) -> OpenOrder:
    return OpenOrder(
        symbol=symbol, side=side, quantity=qty, perm_id=perm_id, status="PreSubmitted"
    )


# --------------------------------------------------------- close_position
class TestClosePosition:
    async def test_places_full_position_sell(self):
        broker = FakeBroker(positions=[_pos("AAPL", 137)])

        result = await run_close_position(broker=broker, symbol="AAPL")

        assert result.found is True
        assert result.quantity == 137
        assert len(broker.orders_placed) == 1
        req = broker.orders_placed[0]
        assert req.symbol == "AAPL"
        assert req.side == "SELL"
        assert req.quantity == 137

    async def test_tags_order_ref_manual(self):
        broker = FakeBroker(positions=[_pos("AAPL", 50)])

        await run_close_position(broker=broker, symbol="AAPL")

        assert broker.orders_placed[0].order_ref == "manual"

    async def test_symbol_not_held_returns_not_found(self):
        broker = FakeBroker(positions=[_pos("MSFT", 10)])

        result = await run_close_position(broker=broker, symbol="AAPL")

        assert result.found is False
        assert broker.orders_placed == []

    async def test_zero_quantity_treated_as_not_held(self):
        broker = FakeBroker(positions=[_pos("AAPL", 0)])

        result = await run_close_position(broker=broker, symbol="AAPL")

        assert result.found is False
        assert broker.orders_placed == []

    async def test_declined_confirmation_aborts_without_order(self):
        broker = FakeBroker(positions=[_pos("AAPL", 137)])

        result = await run_close_position(
            broker=broker, symbol="AAPL", confirm=lambda sym, qty: False
        )

        assert result.found is True
        assert result.aborted is True
        assert broker.orders_placed == []

    async def test_confirm_callback_receives_symbol_and_quantity(self):
        broker = FakeBroker(positions=[_pos("AAPL", 137)])
        seen: list[tuple[str, int]] = []

        await run_close_position(
            broker=broker,
            symbol="AAPL",
            confirm=lambda sym, qty: seen.append((sym, qty)) or True,
        )

        assert seen == [("AAPL", 137)]


# --------------------------------------------------------- cancel_pending
class TestCancelPending:
    async def test_no_symbol_lists_without_cancelling(self):
        orders = [_order("AAPL", perm_id=1), _order("MSFT", perm_id=2)]
        broker = FakeBroker(open_orders=orders)

        result = await run_cancel_pending(broker=broker, symbol=None)

        assert result.listing == orders
        assert result.cancelled == []
        assert broker.cancel_calls == []

    async def test_with_symbol_cancels_matching_order(self):
        broker = FakeBroker(open_orders=[_order("AAPL", perm_id=1), _order("MSFT", perm_id=2)])

        result = await run_cancel_pending(broker=broker, symbol="AAPL")

        assert result.matched is True
        assert [o.symbol for o in result.cancelled] == ["AAPL"]
        assert broker.cancel_calls == ["AAPL"]

    async def test_symbol_with_no_working_order_reports_no_match(self):
        broker = FakeBroker(open_orders=[_order("MSFT", perm_id=2)])

        result = await run_cancel_pending(broker=broker, symbol="AAPL")

        assert result.matched is False
        assert result.cancelled == []
        assert broker.cancel_calls == []

    async def test_cancels_multiple_orders_for_one_symbol(self):
        broker = FakeBroker(
            open_orders=[
                _order("AAPL", perm_id=1),
                _order("AAPL", side="SELL", perm_id=2),
                _order("MSFT", perm_id=3),
            ]
        )

        result = await run_cancel_pending(broker=broker, symbol="AAPL")

        assert len(result.cancelled) == 2
        assert all(o.symbol == "AAPL" for o in result.cancelled)
