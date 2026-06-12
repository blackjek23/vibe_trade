"""Tests for the V2 reconcile job (`vibe_trade.jobs.reconcile.run_reconcile`).

Mocks `broker.ib.trades()` + `broker.ib.fills()` + `broker.get_account_summary()` +
`broker.get_positions()`. Uses real DB session via the shared `db_session` fixture.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from vibe_trade.broker.models import AccountSummary, Position
from vibe_trade.db.models import DailyPnL, PortfolioSnapshot, Trade
from vibe_trade.db.repository import (
    DailyPnLRepository,
    PortfolioSnapshotRepository,
    TradeRepository,
)
from vibe_trade.jobs.reconcile import run_reconcile


def _fill(perm_id: int, order_id: int, symbol: str, side: str, shares: int,
          price: float = 100.0, realized_pnl: float = 0.0):
    """Fill-shaped SimpleNamespace. Note tz-aware time (matches ib_async)."""
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        execution=SimpleNamespace(
            permId=perm_id,
            orderId=order_id,
            shares=float(shares),
            price=price,
            side=side,
        ),
        commissionReport=SimpleNamespace(realizedPNL=realized_pnl, commission=1.0),
        time=datetime(2026, 4, 28, 19, 30, tzinfo=timezone.utc),
    )


def _ib_trade(perm_id: int, symbol: str, action: str, status: str = "Filled"):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        order=SimpleNamespace(permId=perm_id, action=action, totalQuantity=0.0, orderId=0),
        orderStatus=SimpleNamespace(status=status, filled=0.0),
    )


class MockBroker:
    def __init__(self, *, trades=None, fills=None, account=None, positions=None):
        self.ib = SimpleNamespace(
            trades=lambda: list(trades or []),
            fills=lambda: list(fills or []),
        )
        self._account = account or AccountSummary(
            account_id="DU000001", net_liquidation=100_000.0, total_cash=40_000.0,
            unrealized_pnl=500.0, realized_pnl=200.0,
        )
        self._positions = positions or []

    async def get_account_summary(self):
        return self._account

    async def get_positions(self):
        return list(self._positions)


def _setup_repos(db_session: Session):
    return (
        TradeRepository(db_session),
        PortfolioSnapshotRepository(db_session),
        DailyPnLRepository(db_session),
    )


def _make_submitted_buy(repo: TradeRepository, *, symbol="T", perm_id=111, qty=10) -> Trade:
    """Submitted-today, regardless of system date (uses datetime.now())."""
    return repo.create_submitted_buy(
        symbol=symbol, strategy_name="donchian", requested_quantity=qty,
        ib_order_id=21, submitted_at=datetime.now(), perm_id=perm_id,
    )


def _make_pending_close(repo: TradeRepository, *, symbol="F", buy_perm=900, sell_perm=901,
                        qty=10, entry_price=12.0) -> Trade:
    """Submitted-yesterday BUY (filled), then SELL submitted-today."""
    submitted = repo.create_submitted_buy(
        symbol=symbol, strategy_name="donchian", requested_quantity=qty,
        ib_order_id=4, submitted_at=datetime.now(), perm_id=buy_perm,
    )
    repo.confirm_buy_fill(
        submitted.id, entry_price=entry_price, filled_quantity=qty,
        entry_time=datetime.now(), status="OPEN",
    )
    repo.mark_pending_close(
        submitted.id, exit_ib_order_id=23,
        exit_submitted_at=datetime.now(), exit_perm_id=sell_perm,
    )
    return submitted


# ============================================================== tests


class TestNoPending:
    async def test_no_pending_no_transitions(self, db_session: Session):
        broker = MockBroker()
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.pending_count == 0
        assert result.opened == 0
        assert result.closed == 0


class TestBuyReconciliation:
    async def test_full_fill_transitions_to_open(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        submitted = _make_submitted_buy(trade_repo, perm_id=111, qty=10)

        broker = MockBroker(
            trades=[_ib_trade(perm_id=111, symbol="T", action="BUY", status="Filled")],
            fills=[_fill(perm_id=111, order_id=21, symbol="T", side="BOT",
                         shares=10, price=26.09)],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo, today=date.today(),
        )
        assert result.opened == 1
        row = db_session.get(Trade, submitted.id)
        assert row.status == "OPEN"
        assert row.entry_price == pytest.approx(26.09)
        assert row.filled_quantity == 10
        assert row.entry_time is not None
        assert row.entry_time.tzinfo is None  # naive after _strip_tz

    async def test_partial_fill_transitions_to_partially_filled(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        submitted = _make_submitted_buy(trade_repo, perm_id=111, qty=10)

        # Only 4 of 10 shares filled
        broker = MockBroker(
            trades=[_ib_trade(perm_id=111, symbol="T", action="BUY", status="Filled")],
            fills=[_fill(perm_id=111, order_id=21, symbol="T", side="BOT",
                         shares=4, price=26.0)],
        )
        await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        row = db_session.get(Trade, submitted.id)
        assert row.status == "PARTIALLY_FILLED"
        assert row.filled_quantity == 4

    async def test_no_fills_cancelled_marks_cancelled(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        submitted = _make_submitted_buy(trade_repo, perm_id=111)

        broker = MockBroker(
            trades=[_ib_trade(perm_id=111, symbol="T", action="BUY", status="Cancelled")],
            fills=[],  # no fills
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.cancelled == 1
        row = db_session.get(Trade, submitted.id)
        assert row.status == "CANCELLED"

    async def test_no_fills_still_working_skipped(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        submitted = _make_submitted_buy(trade_repo, perm_id=111)

        broker = MockBroker(
            trades=[_ib_trade(perm_id=111, symbol="T", action="BUY", status="Submitted")],
            fills=[],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.skipped_still_working == 1
        row = db_session.get(Trade, submitted.id)
        assert row.status == "SUBMITTED"  # untouched


class TestSellReconciliation:
    async def test_full_fill_closes_with_realized_pnl(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        # OPEN F at $12 entry, then PENDING_CLOSE at exit_perm=901
        trade = _make_pending_close(trade_repo, buy_perm=900, sell_perm=901,
                                    qty=10, entry_price=12.0)

        broker = MockBroker(
            trades=[_ib_trade(perm_id=901, symbol="F", action="SELL", status="Filled")],
            fills=[_fill(perm_id=901, order_id=23, symbol="F", side="SLD",
                         shares=10, price=14.0, realized_pnl=20.0)],  # +$2/share x 10
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.closed == 1
        row = db_session.get(Trade, trade.id)
        assert row.status == "CLOSED"
        assert row.exit_price == 14.0
        assert row.pnl == 20.0
        # pnl_pct = 20 / (12 * 10) * 100 = 16.67%
        assert abs(row.pnl_pct - 16.666666) < 1e-3

    async def test_cancelled_sell_reverts_to_open(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        trade = _make_pending_close(trade_repo, sell_perm=901)

        broker = MockBroker(
            trades=[_ib_trade(perm_id=901, symbol="F", action="SELL", status="Cancelled")],
            fills=[],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.cancelled == 1
        row = db_session.get(Trade, trade.id)
        assert row.status == "OPEN"  # reverted; position still held
        assert row.exit_perm_id is None
        assert row.exit_ib_order_id is None

    async def test_partial_sell_fills_partially_filled(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        trade = _make_pending_close(trade_repo, sell_perm=901, qty=10)

        # Only 6 of 10 shares sold
        broker = MockBroker(
            trades=[_ib_trade(perm_id=901, symbol="F", action="SELL", status="Filled")],
            fills=[_fill(perm_id=901, order_id=23, symbol="F", side="SLD",
                         shares=6, price=14.0, realized_pnl=12.0)],
        )
        await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        row = db_session.get(Trade, trade.id)
        assert row.status == "PARTIALLY_FILLED"
        assert row.filled_quantity == 6


class TestPortfolioSnapshot:
    async def test_writes_snapshot_for_each_position(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        positions = [
            Position(symbol="AAPL", quantity=10, avg_cost=150.0,
                     market_price=160.0, market_value=1600.0, unrealized_pnl=100.0),
            Position(symbol="GOOG", quantity=5, avg_cost=2800.0,
                     market_price=2850.0, market_value=14250.0, unrealized_pnl=250.0),
        ]
        broker = MockBroker(positions=positions)
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.snapshot_rows == 2
        snaps = db_session.query(PortfolioSnapshot).order_by(PortfolioSnapshot.symbol).all()
        assert len(snaps) == 2
        assert snaps[0].symbol == "AAPL"
        assert snaps[0].market_value == 1600.0

    async def test_snapshot_idempotent_on_rerun(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        positions = [Position(symbol="AAPL", quantity=10, avg_cost=150.0,
                              market_price=160.0, market_value=1600.0, unrealized_pnl=100.0)]
        broker = MockBroker(positions=positions)

        await run_reconcile(broker=broker, trade_repo=trade_repo,
                            snap_repo=snap_repo, daily_repo=daily_repo)
        await run_reconcile(broker=broker, trade_repo=trade_repo,
                            snap_repo=snap_repo, daily_repo=daily_repo)
        # Still exactly 1 row for today (delete-then-insert).
        assert db_session.query(PortfolioSnapshot).count() == 1


class TestDailyPnL:
    async def test_upserts_with_real_counts(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        # Set up 1 BUY filling and 1 SELL filling in the same run.
        _make_submitted_buy(trade_repo, perm_id=111)
        _make_pending_close(trade_repo, sell_perm=901)
        broker = MockBroker(
            trades=[
                _ib_trade(perm_id=111, symbol="T", action="BUY", status="Filled"),
                _ib_trade(perm_id=901, symbol="F", action="SELL", status="Filled"),
            ],
            fills=[
                _fill(perm_id=111, order_id=21, symbol="T", side="BOT",
                      shares=10, price=26.0),
                _fill(perm_id=901, order_id=23, symbol="F", side="SLD",
                      shares=10, price=14.0, realized_pnl=20.0),
            ],
            account=AccountSummary(
                account_id="DU01", net_liquidation=95_000.0, total_cash=20_000.0,
                unrealized_pnl=300.0, realized_pnl=20.0,
            ),
            positions=[Position(symbol="X", quantity=1, avg_cost=10.0,
                                market_price=10.0, market_value=10.0, unrealized_pnl=0.0)],
        )
        await run_reconcile(broker=broker, trade_repo=trade_repo,
                            snap_repo=snap_repo, daily_repo=daily_repo)

        row = db_session.query(DailyPnL).filter_by(date=date.today()).first()
        assert row is not None
        assert row.trades_opened == 1
        assert row.trades_closed == 1
        assert row.account_value == 95_000.0
        assert row.total_cash == 20_000.0
        assert row.realized_pnl == 20.0
        assert row.unrealized_pnl == 300.0
        assert row.open_positions_count == 1


class TestOrphanFills:
    """Bug #5 — late-fill recovery. Fills present in ib.fills() with no matching
    DB row get back-filled directly to OPEN (no intermediate SUBMITTED step).
    Happens when a market order placed at 16:00 fills after the 16:25 record run.
    """

    async def test_orphan_buy_back_filled_to_open(self, db_session: Session):
        """Single orphan BUY fill, no DB pending rows -> new OPEN row inserted."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        # No pending row in DB; reconcile must back-fill.
        broker = MockBroker(
            trades=[_ib_trade(perm_id=777, symbol="LATE", action="BUY", status="Filled")],
            fills=[_fill(perm_id=777, order_id=99, symbol="LATE", side="BOT",
                         shares=15, price=42.50)],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.orphan_fills_inserted == 1
        assert result.opened == 0  # nothing in pending; orphans are tracked separately

        row = trade_repo.find_by_perm_id(777)
        assert row is not None
        assert row.symbol == "LATE"
        assert row.side == "BUY"
        assert row.status == "OPEN"
        assert row.filled_quantity == 15
        assert row.requested_quantity == 15  # we never saw the original ask
        assert row.entry_price == pytest.approx(42.50)
        assert row.entry_time is not None
        assert row.entry_time.tzinfo is None  # naive after _strip_tz

    async def test_orphan_fills_idempotent_on_rerun(self, db_session: Session):
        """Re-running reconcile must not duplicate orphan rows."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        broker = MockBroker(
            trades=[_ib_trade(perm_id=888, symbol="DUP", action="BUY", status="Filled")],
            fills=[_fill(perm_id=888, order_id=1, symbol="DUP", side="BOT",
                         shares=10, price=50.0)],
        )
        # First run inserts.
        result1 = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result1.orphan_fills_inserted == 1
        # Second run sees the existing row and skips.
        result2 = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result2.orphan_fills_inserted == 0
        assert db_session.query(Trade).filter(Trade.perm_id == 888).count() == 1

    async def test_orphan_mixed_with_pending_dont_double_handle(self, db_session: Session):
        """Pending SUBMITTED row + orphan fill in the same run: pending processed
        via the normal path; orphan inserted as new OPEN. Same permId is never
        seen by both paths.
        """
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        # 1 pending SUBMITTED (perm 111) + 1 orphan (perm 222), both BUYs.
        submitted = _make_submitted_buy(trade_repo, symbol="PEND", perm_id=111, qty=10)
        broker = MockBroker(
            trades=[
                _ib_trade(perm_id=111, symbol="PEND", action="BUY", status="Filled"),
                _ib_trade(perm_id=222, symbol="ORPH", action="BUY", status="Filled"),
            ],
            fills=[
                _fill(perm_id=111, order_id=1, symbol="PEND", side="BOT",
                      shares=10, price=20.0),
                _fill(perm_id=222, order_id=2, symbol="ORPH", side="BOT",
                      shares=7, price=33.0),
            ],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.opened == 1                  # PEND went SUBMITTED -> OPEN
        assert result.orphan_fills_inserted == 1   # ORPH inserted fresh

        pend_row = db_session.get(Trade, submitted.id)
        assert pend_row.status == "OPEN"
        assert pend_row.entry_price == pytest.approx(20.0)

        orph_row = trade_repo.find_by_perm_id(222)
        assert orph_row is not None
        assert orph_row.status == "OPEN"
        assert orph_row.symbol == "ORPH"
        assert orph_row.filled_quantity == 7

    async def test_orphan_sell_warns_but_doesnt_insert(self, db_session: Session):
        """SELL fill with no matching OPEN trade in DB -- count as unmatched,
        do NOT insert (we'd need a buy row to attach P&L to)."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        broker = MockBroker(
            trades=[_ib_trade(perm_id=555, symbol="GHOST", action="SELL", status="Filled")],
            fills=[_fill(perm_id=555, order_id=1, symbol="GHOST", side="SLD",
                         shares=5, price=10.0)],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.orphan_sells_unmatched == 1
        assert result.orphan_fills_inserted == 0
        assert trade_repo.find_by_perm_id(555) is None  # nothing inserted
        assert trade_repo.find_by_exit_perm_id(555) is None


class TestStalePendingResolution:
    """Pending rows from previous days (missed/crashed reconcile runs) must
    stay visible and get resolved -- the SELL-side twin of Bug #5.
    """

    @staticmethod
    def _yesterday() -> datetime:
        from datetime import timedelta
        return datetime.now() - timedelta(days=1)

    def _make_stale_pending_close(self, repo: TradeRepository, *, symbol="F",
                                  buy_perm=900, sell_perm=901, qty=10,
                                  entry_price=12.0) -> Trade:
        """OPEN trade whose SELL was submitted YESTERDAY (never reconciled)."""
        yesterday = self._yesterday()
        submitted = repo.create_submitted_buy(
            symbol=symbol, strategy_name="donchian", requested_quantity=qty,
            ib_order_id=4, submitted_at=yesterday, perm_id=buy_perm,
        )
        repo.confirm_buy_fill(
            submitted.id, entry_price=entry_price, filled_quantity=qty,
            entry_time=yesterday, status="OPEN",
        )
        repo.mark_pending_close(
            submitted.id, exit_ib_order_id=23,
            exit_submitted_at=yesterday, exit_perm_id=sell_perm,
        )
        return submitted

    async def test_stale_pending_close_with_todays_fill_closes(self, db_session: Session):
        """The headline late-SELL-fill case: SELL flipped PENDING_CLOSE yesterday,
        fill shows up in today's ib.fills() -> CLOSED with real P&L (previously
        the date filter hid the row forever)."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        trade = self._make_stale_pending_close(trade_repo, sell_perm=901,
                                               qty=10, entry_price=12.0)
        broker = MockBroker(
            trades=[_ib_trade(perm_id=901, symbol="F", action="SELL", status="Filled")],
            fills=[_fill(perm_id=901, order_id=23, symbol="F", side="SLD",
                         shares=10, price=14.0, realized_pnl=20.0)],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.closed == 1
        row = db_session.get(Trade, trade.id)
        assert row.status == "CLOSED"
        assert row.pnl == 20.0

    async def test_stale_buy_no_fills_marked_cancelled(self, db_session: Session):
        """SUBMITTED yesterday, no fills, IB doesn't know the order -- day
        orders can't fill on a later day, so it expired: CANCELLED."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        stale = trade_repo.create_submitted_buy(
            symbol="OLD", strategy_name="donchian", requested_quantity=10,
            ib_order_id=9, submitted_at=self._yesterday(), perm_id=333,
        )
        broker = MockBroker()  # IB has no trace of the order
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.cancelled == 1
        row = db_session.get(Trade, stale.id)
        assert row.status == "CANCELLED"
        assert "stale day order" in row.notes

    async def test_stale_sell_position_still_held_reverts_open(self, db_session: Session):
        """PENDING_CLOSE from yesterday, no fills, but we still hold the
        symbol at IB -- the SELL expired unfilled: revert to OPEN."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        trade = self._make_stale_pending_close(trade_repo, symbol="HELD",
                                               sell_perm=902)
        broker = MockBroker(positions=[
            Position(symbol="HELD", quantity=10, avg_cost=12.0,
                     market_price=13.0, market_value=130.0, unrealized_pnl=10.0),
        ])
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.cancelled == 1
        row = db_session.get(Trade, trade.id)
        assert row.status == "OPEN"
        assert row.exit_perm_id is None

    async def test_stale_sell_position_gone_flags_manual_resolution(self, db_session: Session):
        """PENDING_CLOSE from yesterday, no fills, position no longer held --
        it sold but the fill is unrecoverable from ib.fills(). Don't invent a
        price: flag an error and leave the row for manual resolution."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        trade = self._make_stale_pending_close(trade_repo, symbol="GONE",
                                               sell_perm=903)
        broker = MockBroker()  # symbol not in positions, no fills
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert any("resolve manually" in e for e in result.errors)
        row = db_session.get(Trade, trade.id)
        assert row.status == "PENDING_CLOSE"  # untouched, awaiting operator

    async def test_same_day_still_working_not_treated_as_stale(self, db_session: Session):
        """A row submitted TODAY with a working order must keep being skipped,
        not cancelled by the staleness logic."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        submitted = _make_submitted_buy(trade_repo, perm_id=111)
        broker = MockBroker(
            trades=[_ib_trade(perm_id=111, symbol="T", action="BUY", status="Submitted")],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.skipped_still_working == 1
        assert db_session.get(Trade, submitted.id).status == "SUBMITTED"


class TestPermIdZeroGuard:
    async def test_permid_zero_fill_ignored_not_orphaned(self, db_session: Session):
        """permId=0 fills (IB quirk for legacy/manual orders) must not be
        grouped or back-filled -- two distinct orders would collapse into one."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        broker = MockBroker(
            fills=[
                _fill(perm_id=0, order_id=1, symbol="MANUAL1", side="BOT", shares=5),
                _fill(perm_id=0, order_id=2, symbol="MANUAL2", side="BOT", shares=7),
            ],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.orphan_fills_inserted == 0
        assert trade_repo.find_by_perm_id(0) is None


class TestLongsOnlyPositionCount:
    async def test_open_positions_count_excludes_non_longs(self, db_session: Session):
        """daily_pnl.open_positions_count must match submit's cap definition
        (quantity > 0). Shorts/zero rows previously inflated it past the cap
        (61 on a 50-cap day, PROJECT_MASTER_STATE.md §7)."""
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        positions = [
            Position(symbol="LONG1", quantity=10, avg_cost=10.0,
                     market_price=10.0, market_value=100.0, unrealized_pnl=0.0),
            Position(symbol="LONG2", quantity=5, avg_cost=20.0,
                     market_price=20.0, market_value=100.0, unrealized_pnl=0.0),
            Position(symbol="SHORT", quantity=-3, avg_cost=30.0,
                     market_price=30.0, market_value=-90.0, unrealized_pnl=0.0),
            Position(symbol="FLAT", quantity=0, avg_cost=0.0,
                     market_price=0.0, market_value=0.0, unrealized_pnl=0.0),
        ]
        broker = MockBroker(positions=positions)
        await run_reconcile(broker=broker, trade_repo=trade_repo,
                            snap_repo=snap_repo, daily_repo=daily_repo)
        row = db_session.query(DailyPnL).filter_by(date=date.today()).first()
        assert row.open_positions_count == 2


class TestRobustness:
    async def test_per_trade_exception_doesnt_abort(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        # Two pending; first will raise during transition because the orderStatus
        # mock is malformed, second processes cleanly.
        _make_submitted_buy(trade_repo, symbol="BAD", perm_id=111)
        good_buy = _make_submitted_buy(trade_repo, symbol="GOOD", perm_id=222)

        # bad_buy's fills will trigger a TypeError because we set shares to a string.
        bad_fill = SimpleNamespace(
            contract=SimpleNamespace(symbol="BAD"),
            execution=SimpleNamespace(permId=111, orderId=1, shares="oops",
                                       price=10.0, side="BOT"),
            commissionReport=SimpleNamespace(realizedPNL=0.0, commission=0.0),
            time=datetime(2026, 4, 28, 19, 30, tzinfo=timezone.utc),
        )
        good_fill = _fill(perm_id=222, order_id=2, symbol="GOOD", side="BOT",
                          shares=10, price=20.0)

        broker = MockBroker(
            trades=[
                _ib_trade(perm_id=111, symbol="BAD", action="BUY", status="Filled"),
                _ib_trade(perm_id=222, symbol="GOOD", action="BUY", status="Filled"),
            ],
            fills=[bad_fill, good_fill],
        )
        result = await run_reconcile(
            broker=broker, trade_repo=trade_repo,
            snap_repo=snap_repo, daily_repo=daily_repo,
        )
        assert result.errors  # bad_buy recorded
        assert result.opened == 1  # good_buy still processed
        assert db_session.get(Trade, good_buy.id).status == "OPEN"
