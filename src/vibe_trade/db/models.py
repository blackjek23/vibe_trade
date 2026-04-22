"""SQLAlchemy ORM models for trade tracking."""

from __future__ import annotations

from datetime import datetime, date

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Trade(Base):
    """A trade row covers the full lifecycle of a position:
    SUBMITTED → OPEN → (PENDING_CLOSE →) CLOSED / CANCELLED / PARTIALLY_FILLED.

    See docs/ARCHITECTURE_V2.md for the status lifecycle details.
    """

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY or SELL
    strategy_name: Mapped[str] = mapped_column(String(50), nullable=False)

    # Entry (BUY side)
    entry_price: Mapped[float | None] = mapped_column(Float)  # filled at submit-time=None
    entry_time: Mapped[datetime | None] = mapped_column(DateTime)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)  # when BUY was sent to IB
    ib_order_id: Mapped[int | None] = mapped_column(Integer)  # BUY order id

    # Exit (SELL side)
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime)
    exit_submitted_at: Mapped[datetime | None] = mapped_column(DateTime)  # when SELL was sent
    exit_ib_order_id: Mapped[int | None] = mapped_column(Integer)  # SELL order id

    # Quantities — requested is what we asked for; filled is what actually executed.
    requested_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int | None] = mapped_column(Integer)

    # Statuses: SUBMITTED, OPEN, PENDING_CLOSE, CLOSED, CANCELLED, PARTIALLY_FILLED
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    pnl: Mapped[float | None] = mapped_column(Float)
    pnl_pct: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(50), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(4))  # BUY, SELL, HOLD
    confidence: Mapped[float | None] = mapped_column(Float)
    metadata_json: Mapped[str | None] = mapped_column(Text)
    risk_approved: Mapped[bool | None] = mapped_column(Boolean)
    risk_reason: Mapped[str | None] = mapped_column(Text)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    scan_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DailyPnL(Base):
    __tablename__ = "daily_pnl"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, unique=True, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    trades_opened: Mapped[int] = mapped_column(Integer, default=0)
    trades_closed: Mapped[int] = mapped_column(Integer, default=0)
    account_value: Mapped[float | None] = mapped_column(Float)
    # V2: extra fields for reconcile job
    total_cash: Mapped[float | None] = mapped_column(Float)
    open_positions_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PortfolioSnapshot(Base):
    """One row per held position per day, written by `vibe-trade reconcile` at 23:30.

    Paired with `DailyPnL` (one row per day for aggregate numbers).
    See docs/ARCHITECTURE_V2.md.
    """

    __tablename__ = "portfolio_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_cost: Mapped[float | None] = mapped_column(Float)
    market_price: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ScanLog(Base):
    __tablename__ = "scan_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    symbols_scanned: Mapped[int] = mapped_column(Integer, default=0)
    signals_generated: Mapped[int] = mapped_column(Integer, default=0)
    orders_placed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="STARTED")  # STARTED, SUCCESS, PARTIAL, FAILED
