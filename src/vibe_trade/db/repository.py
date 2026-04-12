"""Data access layer for trades, signals, and P&L."""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from vibe_trade.db.models import DailyPnL, ScanLog, Signal, Trade


class TradeRepository:
    def __init__(self, session: Session):
        self.session = session

    def create_trade(
        self,
        symbol: str,
        side: str,
        strategy_name: str,
        entry_price: float,
        quantity: int,
        trailing_stop: float | None = None,
        ib_order_id: int | None = None,
    ) -> Trade:
        trade = Trade(
            symbol=symbol,
            side=side,
            strategy_name=strategy_name,
            entry_price=entry_price,
            entry_time=datetime.now(),
            quantity=quantity,
            trailing_stop=trailing_stop,
            status="OPEN",
            ib_order_id=ib_order_id,
        )
        self.session.add(trade)
        self.session.commit()
        return trade

    def close_trade(self, trade_id: int, exit_price: float) -> Trade:
        trade = self.session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"Trade {trade_id} not found")
        trade.exit_price = exit_price
        trade.exit_time = datetime.now()
        trade.status = "CLOSED"
        if trade.entry_price and trade.quantity:
            if trade.side == "BUY":
                trade.pnl = (exit_price - trade.entry_price) * trade.quantity
            else:
                trade.pnl = (trade.entry_price - exit_price) * trade.quantity
            trade.pnl_pct = trade.pnl / (trade.entry_price * trade.quantity) * 100
        self.session.commit()
        return trade

    def get_open_trades(self) -> list[Trade]:
        return self.session.query(Trade).filter(Trade.status == "OPEN").all()

    def get_open_trade_for_symbol(self, symbol: str) -> Trade | None:
        return (
            self.session.query(Trade)
            .filter(Trade.status == "OPEN", Trade.symbol == symbol)
            .first()
        )

    def update_trailing_stop(self, trade_id: int, new_stop: float) -> None:
        trade = self.session.get(Trade, trade_id)
        if trade:
            trade.trailing_stop = new_stop
            self.session.commit()

    def get_recent_trades(self, limit: int = 20) -> list[Trade]:
        return (
            self.session.query(Trade)
            .order_by(Trade.created_at.desc())
            .limit(limit)
            .all()
        )


class SignalRepository:
    def __init__(self, session: Session):
        self.session = session

    def record_signal(
        self,
        symbol: str,
        strategy_name: str,
        signal_type: str,
        scan_id: str,
        confidence: float | None = None,
        metadata: dict | None = None,
    ) -> Signal:
        signal = Signal(
            symbol=symbol,
            strategy_name=strategy_name,
            signal_type=signal_type,
            confidence=confidence,
            metadata_json=json.dumps(metadata) if metadata else None,
            scan_id=scan_id,
        )
        self.session.add(signal)
        self.session.commit()
        return signal

    def mark_executed(self, signal_id: int, approved: bool, reason: str = "") -> None:
        signal = self.session.get(Signal, signal_id)
        if signal:
            signal.risk_approved = approved
            signal.risk_reason = reason
            signal.executed = approved
            self.session.commit()


class DailyPnLRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert_daily(
        self,
        today: date,
        realized_pnl: float,
        unrealized_pnl: float,
        trades_opened: int,
        trades_closed: int,
        account_value: float,
    ) -> DailyPnL:
        record = self.session.query(DailyPnL).filter(DailyPnL.date == today).first()
        if record is None:
            record = DailyPnL(date=today)
            self.session.add(record)
        record.realized_pnl = realized_pnl
        record.unrealized_pnl = unrealized_pnl
        record.total_pnl = realized_pnl + unrealized_pnl
        record.trades_opened = trades_opened
        record.trades_closed = trades_closed
        record.account_value = account_value
        self.session.commit()
        return record


class ScanLogRepository:
    def __init__(self, session: Session):
        self.session = session

    def start_scan(self, scan_id: str) -> ScanLog:
        log = ScanLog(scan_id=scan_id, started_at=datetime.now(), status="STARTED")
        self.session.add(log)
        self.session.commit()
        return log

    def complete_scan(
        self,
        scan_id: str,
        symbols_scanned: int,
        signals_generated: int,
        orders_placed: int,
        errors: list[str] | None = None,
    ) -> None:
        log = self.session.query(ScanLog).filter(ScanLog.scan_id == scan_id).first()
        if log:
            log.completed_at = datetime.now()
            log.symbols_scanned = symbols_scanned
            log.signals_generated = signals_generated
            log.orders_placed = orders_placed
            log.errors = json.dumps(errors) if errors else None
            log.status = "FAILED" if errors else "SUCCESS"
            self.session.commit()
