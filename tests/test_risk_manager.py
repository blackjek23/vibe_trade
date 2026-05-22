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


def _pos_with_pnl(symbol: str, pnl: float, qty: int = 10) -> Position:
    """Helper: a Position with a specific unrealized $ P&L."""
    return Position(
        symbol=symbol,
        quantity=qty,
        avg_cost=100.0,
        market_price=100.0 + (pnl / qty if qty else 0.0),
        market_value=qty * 100.0 + pnl,
        unrealized_pnl=pnl,
    )


class TestSelectForceTrimCandidates:
    """Enhancement #1 -- force-trim over-cap positions ranked by lowest $ P&L."""

    def test_below_cap_returns_empty(self):
        """Held=50, max=50 -> no trim needed."""
        positions = [_pos_with_pnl(f"S{i}", pnl=10.0 * i) for i in range(50)]
        result = RiskManager.select_force_trim_candidates(positions, max_positions=50)
        assert result == []

    def test_at_cap_returns_empty(self):
        """Held=30, max=50 -> well below cap, nothing to trim."""
        positions = [_pos_with_pnl(f"S{i}", pnl=10.0) for i in range(30)]
        result = RiskManager.select_force_trim_candidates(positions, max_positions=50)
        assert result == []

    def test_over_cap_returns_worst_by_dollar_pnl(self):
        """Held=60 with mixed P&L, max=50 -> exactly 10 trim signals,
        on the 10 most-negative unrealized_pnl symbols."""
        positions = []
        # 50 winners with positive P&L
        for i in range(50):
            positions.append(_pos_with_pnl(f"WIN{i:02d}", pnl=100.0 + i))
        # 10 losers with negative P&L of varying magnitudes
        for i in range(10):
            positions.append(_pos_with_pnl(f"LOSS{i:02d}", pnl=-50.0 * (i + 1)))

        result = RiskManager.select_force_trim_candidates(positions, max_positions=50)

        assert len(result) == 10
        # All 10 losers must be in the trim list (they're the most negative).
        loser_names = {f"LOSS{i:02d}" for i in range(10)}
        assert set(result) == loser_names
        # No winners trimmed.
        for r in result:
            assert not r.startswith("WIN")

    def test_donchian_exits_count_toward_cap_relief(self):
        """Held=55, max=50, Donchian already chose 5 exits -> 0 additional trim."""
        positions = [_pos_with_pnl(f"S{i:02d}", pnl=-100.0 * i) for i in range(55)]
        # Donchian picks 5 arbitrary names to exit (doesn't matter which).
        already_exiting = {f"S{i:02d}" for i in range(5)}

        result = RiskManager.select_force_trim_candidates(
            positions, max_positions=50, already_exiting=already_exiting,
        )
        assert result == []  # 55 - 5 already-exiting = 50 = max, no trim

    def test_donchian_exits_excluded_from_trim_candidates(self):
        """Held=60, Donchian exits 3, max=50 -> trim 7 (10 over - 3 already exiting),
        and the trim list MUST NOT overlap with the Donchian set."""
        # 60 positions with strictly negative P&L (everyone is a candidate).
        positions = [_pos_with_pnl(f"S{i:02d}", pnl=-100.0 * (i + 1)) for i in range(60)]
        # Donchian picks 3 of the WORST performers to exit.
        already_exiting = {"S57", "S58", "S59"}  # i=57,58,59 -> most negative

        result = RiskManager.select_force_trim_candidates(
            positions, max_positions=50, already_exiting=already_exiting,
        )

        # 60 - 3 already-exiting = 57 -> over by 7.
        assert len(result) == 7
        # None of the Donchian symbols may appear in the trim list.
        assert not (set(result) & already_exiting)
        # The 7 trimmed should be the next 7 worst after the 3 already excluded:
        # S56, S55, S54, S53, S52, S51, S50 (most negative remaining).
        assert set(result) == {f"S{i:02d}" for i in range(50, 57)}

    def test_zero_quantity_positions_ignored(self):
        """A position lingering with qty=0 must not count toward over-cap math."""
        positions = [
            _pos_with_pnl("A", pnl=-1000.0, qty=0),   # closed; ignore
            _pos_with_pnl("B", pnl=5.0, qty=10),
            _pos_with_pnl("C", pnl=10.0, qty=10),
        ]
        result = RiskManager.select_force_trim_candidates(positions, max_positions=2)
        assert result == []  # only 2 real positions, at cap
