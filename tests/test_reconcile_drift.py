"""Tests for reconcile's OPEN-row drift handling.

Covers the two paths added to close the last hole in the status lifecycle:

- **orphan-SELL recovery** — a SELL filled at IB but `record` never ran, so no DB
  row carries its `exit_perm_id`. Matched by symbol (FIFO) while the fill is still
  in `ib.fills()`, because only the current session is returned and the exit is
  unrecoverable tomorrow.
- **`_sweep_open_rows`** — OPEN rows IB doesn't back. Nothing else in reconcile
  ever looked at an OPEN row: `get_pending_orders` returns only SUBMITTED +
  PENDING_CLOSE, and the orphan path only sees permIds in today's fills.

Same mocking approach as `test_reconcile.py` (SimpleNamespace fills, real DB
session via the shared `db_session` fixture).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from vibe_trade.broker.models import AccountSummary, Position
from vibe_trade.db.models import Trade
from vibe_trade.db.repository import (
    DailyPnLRepository,
    PortfolioSnapshotRepository,
    TradeRepository,
)
from vibe_trade.jobs.reconcile import run_reconcile


def _fill(perm_id: int, order_id: int, symbol: str, side: str, shares: int,
          price: float = 100.0, realized_pnl: float = 0.0):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        execution=SimpleNamespace(
            permId=perm_id, orderId=order_id, shares=float(shares),
            price=price, side=side,
        ),
        commissionReport=SimpleNamespace(realizedPNL=realized_pnl, commission=1.0),
        time=datetime(2026, 4, 28, 19, 30, tzinfo=timezone.utc),
    )


class MockBroker:
    def __init__(self, *, trades=None, fills=None, positions=None):
        self.ib = SimpleNamespace(
            trades=lambda: list(trades or []),
            fills=lambda: list(fills or []),
        )
        self._positions = positions or []

    async def get_account_summary(self):
        return AccountSummary(
            account_id="DU000001", net_liquidation=100_000.0, total_cash=40_000.0,
            unrealized_pnl=500.0, realized_pnl=200.0,
        )

    async def get_positions(self):
        return list(self._positions)


def _setup_repos(db_session: Session):
    return (
        TradeRepository(db_session),
        PortfolioSnapshotRepository(db_session),
        DailyPnLRepository(db_session),
    )


def _make_open_trade(repo: TradeRepository, *, symbol="T", perm_id=500, qty=10,
                     entry_price=100.0) -> Trade:
    """A trade sitting at OPEN -- the state the drift sweep inspects."""
    t = repo.create_submitted_buy(
        symbol=symbol, strategy_name="donchian", requested_quantity=qty,
        ib_order_id=7, submitted_at=datetime.now(), perm_id=perm_id,
    )
    repo.confirm_buy_fill(
        t.id, entry_price=entry_price, filled_quantity=qty,
        entry_time=datetime.now(), status="OPEN",
    )
    return t


def _pos(symbol: str, qty: int, avg: float = 100.0, px: float = 110.0) -> Position:
    return Position(symbol, qty, avg, px, px * qty, (px - avg) * qty)


# ============================================================== orphan SELL


class TestOrphanSellRecovery:
    async def test_orphan_sell_closes_matching_open_row(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        open_trade = _make_open_trade(trade_repo, symbol="CSCO", perm_id=600,
                                      qty=17, entry_price=95.96)
        # SELL fill at IB carrying a permId the DB has never seen.
        sell = _fill(perm_id=999, order_id=42, symbol="CSCO", side="SLD",
                     shares=17, price=115.13, realized_pnl=-42.67)
        broker = MockBroker(fills=[sell], positions=[_pos("AAPL", 3)])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.orphan_sells_recovered == 1
        assert result.orphan_sells_unmatched == 0
        row = db_session.get(Trade, open_trade.id)
        assert row.status == "CLOSED"
        assert row.exit_price == pytest.approx(115.13)
        assert row.exit_perm_id == 999
        # P&L from this row's own prices: (115.13 - 95.96) * 17 = +326.89.
        # This is the exact CSCO case that was stored as -42.67 in prod, because
        # IB's account-level basis was mixed into a per-order row.
        assert row.pnl == pytest.approx((115.13 - 95.96) * 17)
        assert row.pnl > 0
        assert "realizedPNL=-42.67" in row.notes

    async def test_recovered_sell_not_flagged_by_drift_sweep(self, db_session: Session):
        """Recovery runs before the sweep, so no row is handled twice."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        open_trade = _make_open_trade(trade_repo, symbol="INTC", perm_id=601, qty=5)
        sell = _fill(perm_id=998, order_id=43, symbol="INTC", side="SLD",
                     shares=5, price=110.0)
        broker = MockBroker(fills=[sell], positions=[_pos("AAPL", 3)])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.orphan_sells_recovered == 1
        assert result.open_rows_flagged == 0
        assert db_session.get(Trade, open_trade.id).status == "CLOSED"

    async def test_orphan_sell_with_no_open_row_still_warns(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        sell = _fill(perm_id=997, order_id=44, symbol="NOPE", side="SLD", shares=1)
        broker = MockBroker(fills=[sell], positions=[_pos("AAPL", 3)])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.orphan_sells_unmatched == 1
        assert result.orphan_sells_recovered == 0

    async def test_fifo_picks_oldest_open_row(self, db_session: Session):
        """With duplicate OPEN rows the oldest closes first (deterministic)."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        first = _make_open_trade(trade_repo, symbol="AMD", perm_id=610, qty=4)
        second = _make_open_trade(trade_repo, symbol="AMD", perm_id=611, qty=4)
        db_session.get(Trade, first.id).entry_time = datetime(2026, 5, 6, 16, 0)
        db_session.get(Trade, second.id).entry_time = datetime(2026, 6, 9, 16, 0)
        db_session.commit()

        sell = _fill(perm_id=996, order_id=45, symbol="AMD", side="SLD",
                     shares=4, price=120.0)
        broker = MockBroker(fills=[sell], positions=[_pos("AMD", 4)])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.orphan_sells_recovered == 1
        assert db_session.get(Trade, first.id).status == "CLOSED"
        assert db_session.get(Trade, second.id).status == "OPEN"

    async def test_partial_orphan_sell_marks_partially_filled(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        t = _make_open_trade(trade_repo, symbol="F", perm_id=612, qty=100)
        sell = _fill(perm_id=995, order_id=46, symbol="F", side="SLD",
                     shares=40, price=14.0)
        broker = MockBroker(fills=[sell], positions=[_pos("F", 60)])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.orphan_sells_recovered == 1
        assert db_session.get(Trade, t.id).status == "PARTIALLY_FILLED"


# ============================================================== drift sweep


class TestOpenRowDriftSweep:
    async def test_open_row_not_held_at_ib_is_flagged(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        phantom = _make_open_trade(trade_repo, symbol="TSLA", perm_id=700, qty=2)
        broker = MockBroker(positions=[_pos("AAPL", 5, 200.0, 210.0)])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.open_rows_flagged == 1
        row = db_session.get(Trade, phantom.id)
        assert row.status == "NEEDS_REVIEW"
        assert "not held at IB" in row.notes
        assert row.exit_price is None  # never invents an exit price

    async def test_held_row_is_left_alone(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        good = _make_open_trade(trade_repo, symbol="AAPL", perm_id=701, qty=5)
        broker = MockBroker(positions=[_pos("AAPL", 5, 200.0, 210.0)])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.open_rows_flagged == 0
        assert result.qty_divergences == 0
        assert db_session.get(Trade, good.id).status == "OPEN"

    async def test_duplicate_open_rows_report_qty_divergence(self, db_session: Session):
        """14 prod symbols had two OPEN rows each. Warn, never auto-mutate."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        a = _make_open_trade(trade_repo, symbol="AAPL", perm_id=702, qty=5)
        b = _make_open_trade(trade_repo, symbol="AAPL", perm_id=703, qty=5)
        broker = MockBroker(positions=[_pos("AAPL", 5, 200.0, 210.0)])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.qty_divergences == 1
        assert any("qty divergence AAPL" in e for e in result.errors)
        assert db_session.get(Trade, a.id).status == "OPEN"
        assert db_session.get(Trade, b.id).status == "OPEN"
        assert result.open_rows_flagged == 0

    async def test_empty_ib_positions_does_not_mass_flag(self, db_session: Session):
        """A failed position read must never wipe every OPEN row to NEEDS_REVIEW."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        rows = [
            _make_open_trade(trade_repo, symbol=s, perm_id=800 + i, qty=3)
            for i, s in enumerate(["AAPL", "MSFT", "NVDA"])
        ]
        broker = MockBroker(positions=[])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.open_rows_flagged == 0
        assert any("drift sweep skipped" in e for e in result.errors)
        for r in rows:
            assert db_session.get(Trade, r.id).status == "OPEN"

    async def test_sweep_noop_when_no_open_rows(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        broker = MockBroker(positions=[_pos("AAPL", 5, 200.0, 210.0)])
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.open_rows_flagged == 0
        assert result.qty_divergences == 0
        assert result.errors == []

    async def test_multiple_phantoms_all_flagged(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        phantoms = [
            _make_open_trade(trade_repo, symbol=s, perm_id=810 + i, qty=2)
            for i, s in enumerate(["CAT", "INTC", "TSLA"])
        ]
        broker = MockBroker(positions=[_pos("AAPL", 5, 200.0, 210.0)])

        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert result.open_rows_flagged == 3
        for p in phantoms:
            assert db_session.get(Trade, p.id).status == "NEEDS_REVIEW"


class TestNeedsReviewIsTerminal:
    async def test_flagged_row_leaves_both_scans(self, db_session: Session):
        """NEEDS_REVIEW must not re-enter the reconcile loop as an unknown status,
        and must stop counting as a live position."""
        trade_repo, _, _ = _setup_repos(db_session)
        t = _make_open_trade(trade_repo, symbol="ZZZ", perm_id=900, qty=1)
        trade_repo.mark_needs_review(t.id, "phantom")
        assert trade_repo.get_pending_orders() == []
        assert trade_repo.get_open_trades() == []

    async def test_sweep_is_idempotent(self, db_session: Session):
        """Re-running reconcile must not re-flag or double-count."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        _make_open_trade(trade_repo, symbol="TSLA", perm_id=901, qty=2)
        broker = MockBroker(positions=[_pos("AAPL", 5, 200.0, 210.0)])

        first = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        second = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )

        assert first.open_rows_flagged == 1
        assert second.open_rows_flagged == 0


class TestCloseFromOpenGuards:
    async def test_refuses_non_open_status(self, db_session: Session):
        trade_repo, _, _ = _setup_repos(db_session)
        t = trade_repo.create_submitted_buy(
            symbol="X", strategy_name="donchian", requested_quantity=5,
            ib_order_id=1, submitted_at=datetime.now(), perm_id=1234,
        )
        with pytest.raises(ValueError, match="cannot close from status=SUBMITTED"):
            trade_repo.close_from_open(
                t.id, exit_price=10.0, filled_quantity=5,
                exit_time=datetime.now(), exit_perm_id=1,
            )

    async def test_missing_trade_raises(self, db_session: Session):
        trade_repo, _, _ = _setup_repos(db_session)
        with pytest.raises(ValueError, match="not found"):
            trade_repo.close_from_open(
                9999, exit_price=10.0, filled_quantity=1,
                exit_time=datetime.now(), exit_perm_id=1,
            )


class TestResolveNeedsReview:
    """The other half of the sweep: a flagged row must be clearable."""

    async def test_resolve_closes_with_row_basis_pnl(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        t = _make_open_trade(trade_repo, symbol="TSLA", perm_id=910, qty=3,
                             entry_price=429.71)
        broker = MockBroker(positions=[_pos("AAPL", 5)])
        await run_reconcile(broker=broker, trade_repo=trade_repo,
                            snap_repo=snap_repo, daily_repo=daily_repo)
        assert db_session.get(Trade, t.id).status == "NEEDS_REVIEW"

        resolved = trade_repo.resolve_needs_review(
            t.id, exit_price=400.0, exit_time=datetime.now()
        )

        assert resolved.status == "CLOSED"
        assert resolved.pnl == pytest.approx((400.0 - 429.71) * 3)
        assert resolved.pnl_pct == pytest.approx(
            (400.0 - 429.71) / 429.71 * 100.0
        )
        assert trade_repo.get_needs_review() == []

    async def test_resolve_rejects_wrong_status(self, db_session: Session):
        trade_repo, _, _ = _setup_repos(db_session)
        t = _make_open_trade(trade_repo, symbol="AAPL", perm_id=911, qty=1)
        with pytest.raises(ValueError, match="is OPEN, not NEEDS_REVIEW"):
            trade_repo.resolve_needs_review(
                t.id, exit_price=1.0, exit_time=datetime.now()
            )

    async def test_write_off_path_marks_cancelled(self, db_session: Session):
        trade_repo, _, _ = _setup_repos(db_session)
        t = _make_open_trade(trade_repo, symbol="LUMN", perm_id=912, qty=167)
        trade_repo.mark_needs_review(t.id, "phantom")
        trade_repo.mark_cancelled(t.id, "written off")
        row = db_session.get(Trade, t.id)
        assert row.status == "CANCELLED"
        assert trade_repo.get_needs_review() == []

    async def test_get_needs_review_lists_all(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        for i, s in enumerate(["CAT", "INTC"]):
            _make_open_trade(trade_repo, symbol=s, perm_id=920 + i, qty=1)
        broker = MockBroker(positions=[_pos("AAPL", 5)])
        await run_reconcile(broker=broker, trade_repo=trade_repo,
                            snap_repo=snap_repo, daily_repo=daily_repo)
        rows = trade_repo.get_needs_review()
        assert [r.symbol for r in rows] == ["CAT", "INTC"]  # sorted by symbol
