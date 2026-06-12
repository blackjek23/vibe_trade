"""Tests for the V2 database layer: engine, models, and repositories.

V2 flow touchpoints covered:
- TradeRepository: full lifecycle writes (SUBMITTED → OPEN → PENDING_CLOSE → CLOSED / etc.)
- PortfolioSnapshotRepository: end-of-day snapshot save/read
- DailyPnLRepository: extended upsert with total_cash + open_positions_count
- SignalRepository, ScanLogRepository: unchanged
- Engine: table creation, lazy-factory guard
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from vibe_trade.db.engine import get_session_factory, init_db
from vibe_trade.db.models import (
    DailyPnL,
    PortfolioSnapshot,
    ScanLog,
    Signal,
    Trade,
)
from vibe_trade.db.repository import (
    DailyPnLRepository,
    PortfolioSnapshotRepository,
    ScanLogRepository,
    SignalRepository,
    TradeRepository,
)


# ---------------------------------------------------------------------------
# TradeRepository — V2 lifecycle
# ---------------------------------------------------------------------------


class TestCreateSubmittedBuy:
    def test_creates_row_with_submitted_status(self, db_session: Session):
        repo = TradeRepository(db_session)
        now = datetime(2026, 4, 20, 16, 0, 5)
        trade = repo.create_submitted_buy(
            symbol="AAPL",
            strategy_name="ma_crossover",
            requested_quantity=15,
            ib_order_id=42,
            submitted_at=now,
        )
        assert trade.id is not None
        assert trade.symbol == "AAPL"
        assert trade.side == "BUY"
        assert trade.strategy_name == "ma_crossover"
        assert trade.requested_quantity == 15
        assert trade.ib_order_id == 42
        assert trade.perm_id is None  # default when not provided
        assert trade.submitted_at == now
        assert trade.status == "SUBMITTED"
        assert trade.entry_price is None
        assert trade.filled_quantity is None

    def test_stores_perm_id_when_provided(self, db_session: Session):
        repo = TradeRepository(db_session)
        trade = repo.create_submitted_buy(
            symbol="AAPL",
            strategy_name="ma_crossover",
            requested_quantity=15,
            ib_order_id=42,
            submitted_at=datetime(2026, 4, 20, 16, 0, 5),
            perm_id=507476881,  # IB persistent ID, survives reconnects
        )
        assert trade.perm_id == 507476881


class TestMarkPendingClose:
    def _open_trade(self, db_session: Session) -> Trade:
        """Helper: create an OPEN BUY trade the way reconcile would."""
        repo = TradeRepository(db_session)
        submitted = repo.create_submitted_buy(
            symbol="AAPL", strategy_name="ma_crossover",
            requested_quantity=10, ib_order_id=1,
            submitted_at=datetime(2026, 4, 20, 16, 0),
        )
        repo.confirm_buy_fill(
            trade_id=submitted.id,
            entry_price=185.0,
            filled_quantity=10,
            entry_time=datetime(2026, 4, 20, 16, 35),
            status="OPEN",
        )
        return submitted

    def test_happy_path(self, db_session: Session):
        open_trade = self._open_trade(db_session)
        repo = TradeRepository(db_session)
        now = datetime(2026, 4, 21, 16, 0, 5)
        updated = repo.mark_pending_close(
            trade_id=open_trade.id,
            exit_ib_order_id=99,
            exit_submitted_at=now,
        )
        assert updated.status == "PENDING_CLOSE"
        assert updated.exit_ib_order_id == 99
        assert updated.exit_submitted_at == now
        assert updated.exit_perm_id is None  # default when not provided

    def test_stores_exit_perm_id_when_provided(self, db_session: Session):
        open_trade = self._open_trade(db_session)
        repo = TradeRepository(db_session)
        updated = repo.mark_pending_close(
            trade_id=open_trade.id,
            exit_ib_order_id=99,
            exit_submitted_at=datetime(2026, 4, 21, 16, 0, 5),
            exit_perm_id=507476882,
        )
        assert updated.exit_perm_id == 507476882

    def test_rejects_non_open_status(self, db_session: Session):
        repo = TradeRepository(db_session)
        submitted = repo.create_submitted_buy(
            symbol="AAPL", strategy_name="s", requested_quantity=1,
            ib_order_id=1, submitted_at=datetime.now(),
        )
        with pytest.raises(ValueError, match="cannot go PENDING_CLOSE from status=SUBMITTED"):
            repo.mark_pending_close(submitted.id, 99, datetime.now())


class TestConfirmBuyFill:
    def _submitted(self, db_session: Session) -> Trade:
        return TradeRepository(db_session).create_submitted_buy(
            symbol="AAPL", strategy_name="ma_crossover",
            requested_quantity=10, ib_order_id=1,
            submitted_at=datetime(2026, 4, 20, 16, 0),
        )

    def test_full_fill(self, db_session: Session):
        trade = self._submitted(db_session)
        repo = TradeRepository(db_session)
        filled = repo.confirm_buy_fill(
            trade_id=trade.id,
            entry_price=185.25,
            filled_quantity=10,
            entry_time=datetime(2026, 4, 20, 16, 35),
            status="OPEN",
        )
        assert filled.status == "OPEN"
        assert filled.entry_price == 185.25
        assert filled.filled_quantity == 10

    def test_partial_fill(self, db_session: Session):
        trade = self._submitted(db_session)
        repo = TradeRepository(db_session)
        filled = repo.confirm_buy_fill(
            trade_id=trade.id, entry_price=185.0, filled_quantity=6,
            entry_time=datetime(2026, 4, 20, 16, 35), status="PARTIALLY_FILLED",
        )
        assert filled.status == "PARTIALLY_FILLED"
        assert filled.filled_quantity == 6

    def test_cancelled(self, db_session: Session):
        trade = self._submitted(db_session)
        repo = TradeRepository(db_session)
        filled = repo.confirm_buy_fill(
            trade_id=trade.id, entry_price=0.0, filled_quantity=0,
            entry_time=datetime(2026, 4, 20, 16, 35), status="CANCELLED",
        )
        assert filled.status == "CANCELLED"

    def test_rejects_invalid_status(self, db_session: Session):
        trade = self._submitted(db_session)
        repo = TradeRepository(db_session)
        with pytest.raises(ValueError, match="Invalid buy-fill status: CLOSED"):
            repo.confirm_buy_fill(trade.id, 185.0, 10, datetime.now(), "CLOSED")

    def test_rejects_non_submitted_trade(self, db_session: Session):
        trade = self._submitted(db_session)
        repo = TradeRepository(db_session)
        repo.confirm_buy_fill(trade.id, 185.0, 10, datetime.now(), "OPEN")
        with pytest.raises(ValueError, match="cannot confirm BUY fill from status=OPEN"):
            repo.confirm_buy_fill(trade.id, 185.0, 10, datetime.now(), "OPEN")


class TestConfirmCloseFill:
    def _pending_close(self, db_session: Session) -> Trade:
        repo = TradeRepository(db_session)
        t = repo.create_submitted_buy(
            symbol="AAPL", strategy_name="s", requested_quantity=10,
            ib_order_id=1, submitted_at=datetime(2026, 4, 20, 16, 0),
        )
        repo.confirm_buy_fill(t.id, 185.0, 10, datetime(2026, 4, 20, 16, 35), "OPEN")
        repo.mark_pending_close(t.id, 99, datetime(2026, 4, 21, 16, 0))
        return t

    def test_closed_stores_pnl_from_caller(self, db_session: Session):
        trade = self._pending_close(db_session)
        repo = TradeRepository(db_session)
        closed = repo.confirm_close_fill(
            trade_id=trade.id,
            exit_price=190.0,
            filled_quantity=10,
            exit_time=datetime(2026, 4, 21, 16, 40),
            pnl=50.0,
            pnl_pct=2.7,
            status="CLOSED",
        )
        assert closed.status == "CLOSED"
        assert closed.exit_price == 190.0
        assert closed.pnl == 50.0
        assert closed.pnl_pct == 2.7

    def test_cancelled_reverts_to_open(self, db_session: Session):
        """SELL never filled → position stays, trade goes back to OPEN."""
        trade = self._pending_close(db_session)
        repo = TradeRepository(db_session)
        reverted = repo.confirm_close_fill(
            trade_id=trade.id,
            exit_price=0.0, filled_quantity=0,
            exit_time=datetime(2026, 4, 21, 16, 40),
            pnl=0.0, pnl_pct=0.0, status="CANCELLED",
        )
        assert reverted.status == "OPEN"
        assert reverted.exit_ib_order_id is None
        assert reverted.exit_submitted_at is None
        assert reverted.exit_price is None

    def test_rejects_non_pending_close(self, db_session: Session):
        repo = TradeRepository(db_session)
        t = repo.create_submitted_buy(
            symbol="AAPL", strategy_name="s", requested_quantity=10,
            ib_order_id=1, submitted_at=datetime.now(),
        )
        with pytest.raises(ValueError, match="cannot confirm close from status=SUBMITTED"):
            repo.confirm_close_fill(t.id, 190.0, 10, datetime.now(), 0.0, 0.0, "CLOSED")


class TestMarkCancelled:
    def test_marks_and_records_reason(self, db_session: Session):
        repo = TradeRepository(db_session)
        t = repo.create_submitted_buy(
            symbol="AAPL", strategy_name="s", requested_quantity=10,
            ib_order_id=1, submitted_at=datetime.now(),
        )
        cancelled = repo.mark_cancelled(t.id, reason="IB rejected: no market data")
        assert cancelled.status == "CANCELLED"
        assert cancelled.notes == "IB rejected: no market data"


class TestGetPendingOrders:
    def test_returns_all_submitted_and_pending_close_any_date(self, db_session: Session):
        repo = TradeRepository(db_session)
        now = datetime(2026, 4, 20, 16, 0)

        # Today: one SUBMITTED, one PENDING_CLOSE
        t1 = repo.create_submitted_buy(
            symbol="AAPL", strategy_name="s", requested_quantity=10,
            ib_order_id=1, submitted_at=now,
        )
        t2 = repo.create_submitted_buy(
            symbol="MSFT", strategy_name="s", requested_quantity=5,
            ib_order_id=2, submitted_at=now,
        )
        repo.confirm_buy_fill(t2.id, 400.0, 5, now, "OPEN")
        repo.mark_pending_close(t2.id, 3, now)

        # Yesterday: SUBMITTED — MUST be returned too (stale rows stay
        # visible so a missed reconcile can resolve them; SELL-side Bug #5)
        repo.create_submitted_buy(
            symbol="GOOG", strategy_name="s", requested_quantity=3,
            ib_order_id=4, submitted_at=datetime(2026, 4, 19, 16, 0),
        )

        # An OPEN trade from today — not pending, should NOT be returned
        t4 = repo.create_submitted_buy(
            symbol="TSLA", strategy_name="s", requested_quantity=2,
            ib_order_id=5, submitted_at=now,
        )
        repo.confirm_buy_fill(t4.id, 300.0, 2, now, "OPEN")

        pending = repo.get_pending_orders()
        symbols = {t.symbol for t in pending}
        assert symbols == {"AAPL", "MSFT", "GOOG"}

    def test_empty_when_nothing_pending(self, db_session: Session):
        repo = TradeRepository(db_session)
        assert repo.get_pending_orders() == []


class TestTradeReads:
    def test_get_open_trades(self, db_session: Session):
        repo = TradeRepository(db_session)
        now = datetime.now()
        for i, sym in enumerate(["AAPL", "MSFT", "GOOG"]):
            t = repo.create_submitted_buy(
                symbol=sym, strategy_name="s", requested_quantity=10,
                ib_order_id=i, submitted_at=now,
            )
            if sym != "GOOG":
                repo.confirm_buy_fill(t.id, 100.0, 10, now, "OPEN")
        open_trades = repo.get_open_trades()
        assert {t.symbol for t in open_trades} == {"AAPL", "MSFT"}

    def test_get_recent_trades(self, db_session: Session):
        repo = TradeRepository(db_session)
        now = datetime.now()
        for i in range(5):
            repo.create_submitted_buy(
                symbol=f"SYM{i}", strategy_name="s", requested_quantity=1,
                ib_order_id=i, submitted_at=now,
            )
        assert len(repo.get_recent_trades(limit=3)) == 3

    def test_find_by_perm_id_returns_match(self, db_session: Session):
        repo = TradeRepository(db_session)
        repo.create_submitted_buy(
            symbol="AAPL", strategy_name="donchian", requested_quantity=10,
            ib_order_id=42, submitted_at=datetime.now(), perm_id=507476881,
        )
        found = repo.find_by_perm_id(507476881)
        assert found is not None
        assert found.symbol == "AAPL"

    def test_find_by_perm_id_returns_none_for_missing(self, db_session: Session):
        repo = TradeRepository(db_session)
        assert repo.find_by_perm_id(999999) is None

    def test_find_by_exit_perm_id_returns_match(self, db_session: Session):
        repo = TradeRepository(db_session)
        # Create OPEN trade then mark pending_close with exit_perm_id
        submitted = repo.create_submitted_buy(
            symbol="AAPL", strategy_name="donchian", requested_quantity=10,
            ib_order_id=42, submitted_at=datetime.now(),
        )
        repo.confirm_buy_fill(submitted.id, 100.0, 10, datetime.now(), "OPEN")
        repo.mark_pending_close(
            submitted.id, exit_ib_order_id=99,
            exit_submitted_at=datetime.now(), exit_perm_id=507476882,
        )
        found = repo.find_by_exit_perm_id(507476882)
        assert found is not None
        assert found.id == submitted.id

    def test_find_by_exit_perm_id_returns_none_for_missing(self, db_session: Session):
        repo = TradeRepository(db_session)
        assert repo.find_by_exit_perm_id(999999) is None


# ---------------------------------------------------------------------------
# PortfolioSnapshotRepository
# ---------------------------------------------------------------------------


def test_get_trades_opened_today(db_session: Session):
    from datetime import date, datetime
    from vibe_trade.db.repository import TradeRepository
    from vibe_trade.db.models import Trade

    repo = TradeRepository(db_session)
    today = date(2026, 5, 3)

    # Opened today (entry_time on today, status OPEN)
    t_open_today = Trade(
        symbol="AAPL", side="BUY", strategy_name="donchian",
        requested_quantity=10, filled_quantity=10,
        entry_price=180.0, entry_time=datetime(2026, 5, 3, 9, 35),
        status="OPEN",
    )
    # Opened yesterday (excluded)
    t_open_yest = Trade(
        symbol="MSFT", side="BUY", strategy_name="donchian",
        requested_quantity=5, filled_quantity=5,
        entry_price=400.0, entry_time=datetime(2026, 5, 2, 9, 35),
        status="OPEN",
    )
    # Today but still SUBMITTED (excluded — not yet opened)
    t_submitted_today = Trade(
        symbol="GOOGL", side="BUY", strategy_name="donchian",
        requested_quantity=3,
        submitted_at=datetime(2026, 5, 3, 16, 0),
        status="SUBMITTED",
    )
    # Partially filled today (included)
    t_partial_today = Trade(
        symbol="NVDA", side="BUY", strategy_name="donchian",
        requested_quantity=10, filled_quantity=7,
        entry_price=900.0, entry_time=datetime(2026, 5, 3, 9, 36),
        status="PARTIALLY_FILLED",
    )
    db_session.add_all([t_open_today, t_open_yest, t_submitted_today, t_partial_today])
    db_session.commit()

    result = repo.get_trades_opened_today(today)
    symbols = sorted(t.symbol for t in result)
    assert symbols == ["AAPL", "NVDA"]


def test_get_trades_closed_today(db_session: Session):
    from datetime import date, datetime
    from vibe_trade.db.repository import TradeRepository
    from vibe_trade.db.models import Trade

    repo = TradeRepository(db_session)
    today = date(2026, 5, 3)

    # Closed today (exit_time on today, status CLOSED)
    t_closed_today = Trade(
        symbol="GOOGL", side="BUY", strategy_name="donchian",
        requested_quantity=3, filled_quantity=3,
        entry_price=2800.0, entry_time=datetime(2026, 4, 28, 9, 35),
        exit_price=2850.0, exit_time=datetime(2026, 5, 3, 9, 40),
        pnl=150.0, pnl_pct=0.0179,
        status="CLOSED",
    )
    # Closed yesterday (excluded)
    t_closed_yest = Trade(
        symbol="META", side="BUY", strategy_name="donchian",
        requested_quantity=4, filled_quantity=4,
        entry_price=500.0, entry_time=datetime(2026, 4, 25, 9, 35),
        exit_price=510.0, exit_time=datetime(2026, 5, 2, 9, 40),
        pnl=40.0, pnl_pct=0.02,
        status="CLOSED",
    )
    # Open with exit_time NULL (excluded)
    t_open = Trade(
        symbol="AAPL", side="BUY", strategy_name="donchian",
        requested_quantity=10, filled_quantity=10,
        entry_price=180.0, entry_time=datetime(2026, 5, 3, 9, 35),
        status="OPEN",
    )
    db_session.add_all([t_closed_today, t_closed_yest, t_open])
    db_session.commit()

    result = repo.get_trades_closed_today(today)
    symbols = [t.symbol for t in result]
    assert symbols == ["GOOGL"]


def test_dailypnl_get_by_date(db_session: Session):
    from datetime import date
    from vibe_trade.db.repository import DailyPnLRepository

    repo = DailyPnLRepository(db_session)
    today = date(2026, 5, 3)

    # No row yet
    assert repo.get_by_date(today) is None

    # Insert via existing upsert_daily
    repo.upsert_daily(
        today=today,
        realized_pnl=124.30,
        unrealized_pnl=50.0,
        trades_opened=2,
        trades_closed=1,
        account_value=102_450.0,
    )
    record = repo.get_by_date(today)
    assert record is not None
    assert record.realized_pnl == 124.30
    assert record.account_value == 102_450.0


class TestPortfolioSnapshotRepository:
    def test_save_and_get(self, db_session: Session):
        repo = PortfolioSnapshotRepository(db_session)
        today = date(2026, 4, 20)
        rows = [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 180.0,
             "market_price": 185.0, "market_value": 1850.0, "unrealized_pnl": 50.0},
            {"symbol": "MSFT", "quantity": 5, "avg_cost": 400.0,
             "market_price": 410.0, "market_value": 2050.0, "unrealized_pnl": 50.0},
        ]
        saved = repo.save_snapshot(today, rows)
        assert len(saved) == 2

        fetched = repo.get_snapshot(today)
        assert len(fetched) == 2
        by_symbol = {r.symbol: r for r in fetched}
        assert by_symbol["AAPL"].quantity == 10
        assert by_symbol["AAPL"].unrealized_pnl == 50.0
        assert by_symbol["MSFT"].market_value == 2050.0

    def test_save_is_idempotent_for_same_date(self, db_session: Session):
        """Running reconcile twice on the same day shouldn't double-insert."""
        repo = PortfolioSnapshotRepository(db_session)
        today = date(2026, 4, 20)
        repo.save_snapshot(today, [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 180.0,
             "market_price": 185.0, "market_value": 1850.0, "unrealized_pnl": 50.0},
        ])
        repo.save_snapshot(today, [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 180.0,
             "market_price": 186.0, "market_value": 1860.0, "unrealized_pnl": 60.0},
            {"symbol": "MSFT", "quantity": 5, "avg_cost": 400.0,
             "market_price": 410.0, "market_value": 2050.0, "unrealized_pnl": 50.0},
        ])
        fetched = repo.get_snapshot(today)
        assert len(fetched) == 2
        by_symbol = {r.symbol: r for r in fetched}
        assert by_symbol["AAPL"].unrealized_pnl == 60.0

    def test_different_dates_coexist(self, db_session: Session):
        repo = PortfolioSnapshotRepository(db_session)
        repo.save_snapshot(date(2026, 4, 20), [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 180.0,
             "market_price": 185.0, "market_value": 1850.0, "unrealized_pnl": 50.0},
        ])
        repo.save_snapshot(date(2026, 4, 21), [
            {"symbol": "AAPL", "quantity": 10, "avg_cost": 180.0,
             "market_price": 186.0, "market_value": 1860.0, "unrealized_pnl": 60.0},
        ])
        assert len(repo.get_snapshot(date(2026, 4, 20))) == 1
        assert len(repo.get_snapshot(date(2026, 4, 21))) == 1


# ---------------------------------------------------------------------------
# SignalRepository (unchanged from V1)
# ---------------------------------------------------------------------------


class TestSignalRepository:
    def test_record_signal(self, db_session: Session):
        repo = SignalRepository(db_session)
        signal = repo.record_signal(
            symbol="AAPL", strategy_name="ma_crossover",
            signal_type="BUY", scan_id="abc-123", confidence=0.75,
        )
        assert signal.id is not None
        assert signal.signal_type == "BUY"
        assert signal.confidence == 0.75
        assert signal.executed is False

    def test_record_signal_with_metadata(self, db_session: Session):
        repo = SignalRepository(db_session)
        meta = {"sma_fast": 20, "sma_slow": 50}
        signal = repo.record_signal(
            symbol="MSFT", strategy_name="ma_crossover",
            signal_type="BUY", scan_id="abc-456", metadata=meta,
        )
        assert json.loads(signal.metadata_json) == meta

    def test_mark_executed(self, db_session: Session):
        repo = SignalRepository(db_session)
        signal = repo.record_signal(
            symbol="AAPL", strategy_name="ma_crossover",
            signal_type="BUY", scan_id="abc-789",
        )
        repo.mark_executed(signal.id, approved=True, reason="ok")
        updated = db_session.get(Signal, signal.id)
        assert updated.risk_approved is True
        assert updated.executed is True
        assert updated.risk_reason == "ok"


# ---------------------------------------------------------------------------
# DailyPnLRepository — extended with V2 fields
# ---------------------------------------------------------------------------


class TestDailyPnLRepository:
    def test_upsert_with_v2_fields(self, db_session: Session):
        repo = DailyPnLRepository(db_session)
        today = date(2026, 4, 20)
        record = repo.upsert_daily(
            today=today,
            realized_pnl=150.0, unrealized_pnl=75.0,
            trades_opened=3, trades_closed=1,
            account_value=100_000.0,
            total_cash=85_000.0,
            open_positions_count=5,
        )
        assert record.realized_pnl == 150.0
        assert record.unrealized_pnl == 75.0
        assert record.total_cash == 85_000.0
        assert record.open_positions_count == 5

    def test_upsert_updates_existing_row(self, db_session: Session):
        repo = DailyPnLRepository(db_session)
        today = date(2026, 4, 20)
        repo.upsert_daily(today=today, realized_pnl=100.0, unrealized_pnl=50.0,
                          trades_opened=2, trades_closed=0, account_value=100_000.0)
        repo.upsert_daily(today=today, realized_pnl=200.0, unrealized_pnl=80.0,
                          trades_opened=4, trades_closed=1, account_value=100_200.0,
                          total_cash=90_000.0, open_positions_count=4)
        rows = db_session.query(DailyPnL).filter(DailyPnL.date == today).all()
        assert len(rows) == 1
        assert rows[0].realized_pnl == 200.0
        assert rows[0].unrealized_pnl == 80.0
        assert rows[0].total_cash == 90_000.0

    def test_different_dates_coexist(self, db_session: Session):
        repo = DailyPnLRepository(db_session)
        repo.upsert_daily(today=date(2026, 4, 20), realized_pnl=100.0,
                          unrealized_pnl=50.0, trades_opened=1,
                          trades_closed=0, account_value=100_000.0)
        repo.upsert_daily(today=date(2026, 4, 21), realized_pnl=200.0,
                          unrealized_pnl=75.0, trades_opened=2,
                          trades_closed=1, account_value=100_200.0)
        assert db_session.query(DailyPnL).count() == 2


# ---------------------------------------------------------------------------
# ScanLogRepository (unchanged)
# ---------------------------------------------------------------------------


class TestScanLogRepository:
    def test_start_scan(self, db_session: Session):
        repo = ScanLogRepository(db_session)
        log = repo.start_scan("scan-001")
        assert log.status == "STARTED"
        assert log.started_at is not None

    def test_complete_scan_success(self, db_session: Session):
        repo = ScanLogRepository(db_session)
        repo.start_scan("scan-002")
        repo.complete_scan("scan-002", symbols_scanned=500,
                           signals_generated=12, orders_placed=2)
        log = db_session.query(ScanLog).filter(ScanLog.scan_id == "scan-002").first()
        assert log.status == "SUCCESS"
        assert log.errors is None

    def test_complete_scan_with_errors(self, db_session: Session):
        repo = ScanLogRepository(db_session)
        repo.start_scan("scan-003")
        errors = ["AAPL: timeout"]
        repo.complete_scan("scan-003", symbols_scanned=1, signals_generated=0,
                           orders_placed=0, errors=errors)
        log = db_session.query(ScanLog).filter(ScanLog.scan_id == "scan-003").first()
        assert log.status == "FAILED"
        assert json.loads(log.errors) == errors


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TestEngine:
    def test_init_db_creates_tables(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        factory = init_db(db_path)
        session = factory()
        table_names = inspect(session.bind).get_table_names()
        assert "trades" in table_names
        assert "signals" in table_names
        assert "daily_pnl" in table_names
        assert "portfolio_snapshot" in table_names
        assert "scan_log" in table_names
        session.close()

    def test_get_session_factory_without_init(self):
        import vibe_trade.db.engine as eng
        original = eng._session_factory
        eng._session_factory = None
        try:
            with pytest.raises(RuntimeError, match="Database not initialized"):
                get_session_factory()
        finally:
            eng._session_factory = original


# ---------------------------------------------------------------------------
# Models — V2 columns exist on Trade
# ---------------------------------------------------------------------------


class TestTradeModel:
    def test_has_v2_columns(self):
        cols = {c.name for c in Trade.__table__.columns}
        for expected in {
            "submitted_at", "exit_submitted_at", "exit_ib_order_id",
            "requested_quantity", "filled_quantity",
            "perm_id", "exit_perm_id",  # cross-process dedup targets
        }:
            assert expected in cols, f"Missing V2 column: {expected}"
        assert "quantity" not in cols, "Legacy 'quantity' column should be removed in V2"
        assert "trailing_stop" not in cols, "V1 'trailing_stop' column should be removed in V2"

    def test_perm_id_is_indexed(self):
        # perm_id is the cross-process dedup target -- must be indexed for fast lookup.
        idx_columns = set()
        for idx in Trade.__table__.indexes:
            for col in idx.columns:
                idx_columns.add(col.name)
        # SQLAlchemy also surfaces single-column indexes via column.index=True
        for col in Trade.__table__.columns:
            if col.index:
                idx_columns.add(col.name)
        assert "perm_id" in idx_columns
        assert "exit_perm_id" in idx_columns
