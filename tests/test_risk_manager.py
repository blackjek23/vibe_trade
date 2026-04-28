"""Tests for V2 risk manager — position-cap + already-holding checks."""

from __future__ import annotations

from vibe_trade.broker.models import Position
from vibe_trade.config import RiskConfig
from vibe_trade.risk.manager import RiskManager
from vibe_trade.strategy.base import SignalResult, SignalType


def _pos(symbol: str, qty: float = 10.0) -> Position:
    return Position(
        symbol=symbol,
        quantity=qty,
        avg_cost=100.0,
        market_price=110.0,
        market_value=qty * 110.0,
        unrealized_pnl=qty * 10.0,
    )


def _signal(symbol: str = "AAPL") -> SignalResult:
    return SignalResult(
        signal=SignalType.BUY,
        symbol=symbol,
        strategy_name="test",
        confidence=0.7,
        metadata={"price": 150.0},
    )


class TestCanOpenNewPosition:
    def test_under_cap_approves(self):
        rm = RiskManager(RiskConfig(max_open_positions=50))
        positions = [_pos(f"S{i}") for i in range(10)]
        d = rm.can_open_new_position(positions)
        assert d.approved is True

    def test_at_cap_rejects(self):
        rm = RiskManager(RiskConfig(max_open_positions=50))
        positions = [_pos(f"S{i}") for i in range(50)]
        d = rm.can_open_new_position(positions)
        assert d.approved is False
        assert "cap" in d.reason.lower() or "50/50" in d.reason

    def test_over_cap_rejects(self):
        # Defensive: should never happen but verify safety
        rm = RiskManager(RiskConfig(max_open_positions=50))
        positions = [_pos(f"S{i}") for i in range(75)]
        d = rm.can_open_new_position(positions)
        assert d.approved is False

    def test_zero_qty_positions_dont_count(self):
        # Closed positions sometimes linger with qty=0
        rm = RiskManager(RiskConfig(max_open_positions=3))
        positions = [
            _pos("A", qty=10),
            _pos("B", qty=0),  # closed
            _pos("C", qty=5),
        ]
        d = rm.can_open_new_position(positions)
        assert d.approved is True  # only 2 real positions

    def test_empty_positions_approves(self):
        rm = RiskManager(RiskConfig(max_open_positions=50))
        d = rm.can_open_new_position([])
        assert d.approved is True


class TestCanTradeSymbol:
    def test_new_symbol_approved(self):
        rm = RiskManager(RiskConfig())
        d = rm.can_trade_symbol(_signal("AAPL"), [_pos("MSFT")])
        assert d.approved is True

    def test_already_holding_rejected(self):
        rm = RiskManager(RiskConfig())
        d = rm.can_trade_symbol(_signal("AAPL"), [_pos("AAPL")])
        assert d.approved is False
        assert "AAPL" in d.reason

    def test_zero_qty_holding_doesnt_block(self):
        # Position lingering with qty=0 should not block a new BUY
        rm = RiskManager(RiskConfig())
        d = rm.can_trade_symbol(_signal("AAPL"), [_pos("AAPL", qty=0)])
        assert d.approved is True

    def test_empty_positions_approved(self):
        rm = RiskManager(RiskConfig())
        d = rm.can_trade_symbol(_signal("AAPL"), [])
        assert d.approved is True
