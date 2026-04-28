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
        submitted = _make_submitted_buy(trade_repo, perm_id=111)
        closed = _make_pending_close(trade_repo, sell_perm=901)
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


class TestRobustness:
    async def test_per_trade_exception_doesnt_abort(self, db_session: Session):
        trade_repo, snap_repo, daily_repo = _setup_repos(db_session)
        # Two pending; first will raise during transition because the orderStatus
        # mock is malformed, second processes cleanly.
        bad_buy = _make_submitted_buy(trade_repo, symbol="BAD", perm_id=111)
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
