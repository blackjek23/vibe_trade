"""Tests for the database layer: engine, models, and repositories."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from vibe_trade.db.engine import get_session_factory, init_db
from vibe_trade.db.models import Base, DailyPnL, ScanLog, Signal, Trade
from vibe_trade.db.repository import (
    DailyPnLRepository,
    ScanLogRepository,
    SignalRepository,
    TradeRepository,
)


# ---------------------------------------------------------------------------
# TradeRepository
# ---------------------------------------------------------------------------


class TestTradeRepository:
    def test_create_trade(self, db_session: Session):
        repo = TradeRepository(db_session)
        trade = repo.create_trade(
            symbol="AAPL",
            side="BUY",
            strategy_name="ma_crossover",
            entry_price=185.50,
            quantity=15,
            trailing_stop=180.00,
        )
        assert trade.id is not None
        assert trade.symbol == "AAPL"
        assert trade.side == "BUY"
        assert trade.strategy_name == "ma_crossover"
        assert trade.entry_price == 185.50
        assert trade.quantity == 15
        assert trade.trailing_stop == 180.00
        assert trade.status == "OPEN"
        assert trade.entry_time is not None
        assert trade.exit_price is None
        assert trade.pnl is None

    def test_close_trade_buy(self, db_session: Session):
        repo = TradeRepository(db_session)
        trade = repo.create_trade(
            symbol="AAPL", side="BUY", strategy_name="ma_crossover",
            entry_price=185.50, quantity=15,
        )
        closed = repo.close_trade(trade.id, exit_price=190.00)
        assert closed.status == "CLOSED"
        assert closed.exit_price == 190.00
        assert closed.exit_time is not None
        assert closed.pnl == pytest.approx((190.00 - 185.50) * 15)
        assert closed.pnl_pct == pytest.approx(
            ((190.00 - 185.50) * 15) / (185.50 * 15) * 100
        )

    def test_close_trade_sell(self, db_session: Session):
        repo = TradeRepository(db_session)
        trade = repo.create_trade(
            symbol="AAPL", side="SELL", strategy_name="rsi_mean_revert",
            entry_price=190.00, quantity=10,
        )
        closed = repo.close_trade(trade.id, exit_price=185.00)
        assert closed.pnl == pytest.approx((190.00 - 185.00) * 10)

    def test_close_trade_not_found(self, db_session: Session):
        repo = TradeRepository(db_session)
        with pytest.raises(ValueError, match="Trade 999 not found"):
            repo.close_trade(999, exit_price=100.0)

    def test_get_open_trades(self, db_session: Session):
        repo = TradeRepository(db_session)
        repo.create_trade(
            symbol="AAPL", side="BUY", strategy_name="ma_crossover",
            entry_price=185.0, quantity=10,
        )
        repo.create_trade(
            symbol="MSFT", side="BUY", strategy_name="ma_crossover",
            entry_price=400.0, quantity=5,
        )
        t3 = repo.create_trade(
            symbol="GOOG", side="BUY", strategy_name="ma_crossover",
            entry_price=140.0, quantity=20,
        )
        repo.close_trade(t3.id, exit_price=145.0)

        open_trades = repo.get_open_trades()
        assert len(open_trades) == 2
        symbols = {t.symbol for t in open_trades}
        assert symbols == {"AAPL", "MSFT"}

    def test_get_open_trade_for_symbol(self, db_session: Session):
        repo = TradeRepository(db_session)
        repo.create_trade(
            symbol="AAPL", side="BUY", strategy_name="ma_crossover",
            entry_price=185.0, quantity=10,
        )
        repo.create_trade(
            symbol="MSFT", side="BUY", strategy_name="ma_crossover",
            entry_price=400.0, quantity=5,
        )
        result = repo.get_open_trade_for_symbol("AAPL")
        assert result is not None
        assert result.symbol == "AAPL"

    def test_get_open_trade_for_symbol_none(self, db_session: Session):
        repo = TradeRepository(db_session)
        result = repo.get_open_trade_for_symbol("TSLA")
        assert result is None

    def test_update_trailing_stop(self, db_session: Session):
        repo = TradeRepository(db_session)
        trade = repo.create_trade(
            symbol="AAPL", side="BUY", strategy_name="ma_crossover",
            entry_price=185.0, quantity=10, trailing_stop=180.0,
        )
        repo.update_trailing_stop(trade.id, 185.0)
        updated = db_session.get(Trade, trade.id)
        assert updated.trailing_stop == 185.0

    def test_get_recent_trades(self, db_session: Session):
        repo = TradeRepository(db_session)
        for i in range(5):
            repo.create_trade(
                symbol=f"SYM{i}", side="BUY", strategy_name="ma_crossover",
                entry_price=100.0 + i, quantity=10,
            )
        recent = repo.get_recent_trades(limit=3)
        assert len(recent) == 3

    def test_create_trade_with_ib_order_id(self, db_session: Session):
        repo = TradeRepository(db_session)
        trade = repo.create_trade(
            symbol="AAPL", side="BUY", strategy_name="ma_crossover",
            entry_price=185.0, quantity=10, ib_order_id=12345,
        )
        assert trade.ib_order_id == 12345


# ---------------------------------------------------------------------------
# SignalRepository
# ---------------------------------------------------------------------------


class TestSignalRepository:
    def test_record_signal(self, db_session: Session):
        repo = SignalRepository(db_session)
        signal = repo.record_signal(
            symbol="AAPL",
            strategy_name="ma_crossover",
            signal_type="BUY",
            scan_id="abc-123",
            confidence=0.75,
        )
        assert signal.id is not None
        assert signal.symbol == "AAPL"
        assert signal.strategy_name == "ma_crossover"
        assert signal.signal_type == "BUY"
        assert signal.scan_id == "abc-123"
        assert signal.confidence == 0.75
        assert signal.executed is False

    def test_record_signal_with_metadata(self, db_session: Session):
        repo = SignalRepository(db_session)
        meta = {"sma_fast": 20, "sma_slow": 50, "rsi": 45.2}
        signal = repo.record_signal(
            symbol="MSFT",
            strategy_name="ma_crossover",
            signal_type="BUY",
            scan_id="abc-456",
            metadata=meta,
        )
        assert signal.metadata_json is not None
        parsed = json.loads(signal.metadata_json)
        assert parsed == meta

    def test_mark_executed_approved(self, db_session: Session):
        repo = SignalRepository(db_session)
        signal = repo.record_signal(
            symbol="AAPL", strategy_name="ma_crossover",
            signal_type="BUY", scan_id="abc-789",
        )
        repo.mark_executed(signal.id, approved=True, reason="All checks passed")
        updated = db_session.get(Signal, signal.id)
        assert updated.risk_approved is True
        assert updated.executed is True
        assert updated.risk_reason == "All checks passed"

    def test_mark_executed_rejected(self, db_session: Session):
        repo = SignalRepository(db_session)
        signal = repo.record_signal(
            symbol="AAPL", strategy_name="ma_crossover",
            signal_type="BUY", scan_id="abc-000",
        )
        repo.mark_executed(signal.id, approved=False, reason="Max positions reached")
        updated = db_session.get(Signal, signal.id)
        assert updated.risk_approved is False
        assert updated.executed is False
        assert updated.risk_reason == "Max positions reached"


# ---------------------------------------------------------------------------
# DailyPnLRepository
# ---------------------------------------------------------------------------


class TestDailyPnLRepository:
    def test_upsert_daily_insert(self, db_session: Session):
        repo = DailyPnLRepository(db_session)
        today = date(2026, 4, 13)
        record = repo.upsert_daily(
            today=today,
            realized_pnl=150.0,
            unrealized_pnl=75.0,
            trades_opened=3,
            trades_closed=1,
            account_value=100_000.0,
        )
        assert record.date == today
        assert record.realized_pnl == 150.0
        assert record.unrealized_pnl == 75.0
        assert record.total_pnl == 225.0
        assert record.trades_opened == 3
        assert record.trades_closed == 1
        assert record.account_value == 100_000.0

    def test_upsert_daily_update(self, db_session: Session):
        repo = DailyPnLRepository(db_session)
        today = date(2026, 4, 13)
        repo.upsert_daily(
            today=today, realized_pnl=100.0, unrealized_pnl=50.0,
            trades_opened=2, trades_closed=0, account_value=100_000.0,
        )
        repo.upsert_daily(
            today=today, realized_pnl=200.0, unrealized_pnl=80.0,
            trades_opened=4, trades_closed=1, account_value=100_200.0,
        )
        rows = db_session.query(DailyPnL).filter(DailyPnL.date == today).all()
        assert len(rows) == 1
        assert rows[0].realized_pnl == 200.0
        assert rows[0].total_pnl == 280.0

    def test_upsert_daily_different_dates(self, db_session: Session):
        repo = DailyPnLRepository(db_session)
        repo.upsert_daily(
            today=date(2026, 4, 13), realized_pnl=100.0, unrealized_pnl=50.0,
            trades_opened=1, trades_closed=0, account_value=100_000.0,
        )
        repo.upsert_daily(
            today=date(2026, 4, 14), realized_pnl=200.0, unrealized_pnl=75.0,
            trades_opened=2, trades_closed=1, account_value=100_200.0,
        )
        all_rows = db_session.query(DailyPnL).all()
        assert len(all_rows) == 2


# ---------------------------------------------------------------------------
# ScanLogRepository
# ---------------------------------------------------------------------------


class TestScanLogRepository:
    def test_start_scan(self, db_session: Session):
        repo = ScanLogRepository(db_session)
        log = repo.start_scan("scan-001")
        assert log.scan_id == "scan-001"
        assert log.status == "STARTED"
        assert log.started_at is not None

    def test_complete_scan_success(self, db_session: Session):
        repo = ScanLogRepository(db_session)
        repo.start_scan("scan-002")
        repo.complete_scan(
            scan_id="scan-002",
            symbols_scanned=500,
            signals_generated=12,
            orders_placed=2,
        )
        log = db_session.query(ScanLog).filter(ScanLog.scan_id == "scan-002").first()
        assert log.status == "SUCCESS"
        assert log.symbols_scanned == 500
        assert log.signals_generated == 12
        assert log.orders_placed == 2
        assert log.completed_at is not None
        assert log.errors is None

    def test_complete_scan_with_errors(self, db_session: Session):
        repo = ScanLogRepository(db_session)
        repo.start_scan("scan-003")
        errors = ["AAPL: timeout", "MSFT: no data"]
        repo.complete_scan(
            scan_id="scan-003",
            symbols_scanned=498,
            signals_generated=10,
            orders_placed=1,
            errors=errors,
        )
        log = db_session.query(ScanLog).filter(ScanLog.scan_id == "scan-003").first()
        assert log.status == "FAILED"
        parsed_errors = json.loads(log.errors)
        assert parsed_errors == errors

    def test_complete_scan_not_found(self, db_session: Session):
        repo = ScanLogRepository(db_session)
        repo.complete_scan(
            scan_id="nonexistent",
            symbols_scanned=0,
            signals_generated=0,
            orders_placed=0,
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TestEngine:
    def test_init_db_creates_tables(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        factory = init_db(db_path)
        session = factory()
        engine = session.bind
        table_names = inspect(engine).get_table_names()
        assert "trades" in table_names
        assert "signals" in table_names
        assert "daily_pnl" in table_names
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
