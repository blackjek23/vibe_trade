"""Read-only DB loaders for `vibe-trade report`.

Returns simple dataclasses, never ORM objects, so metrics and render
have no SQLAlchemy dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class DailyRow:
    date: date
    realized_pnl: float
    unrealized_pnl: float
    account_value: float | None
    open_positions_count: int | None


@dataclass
class HoldingRow:
    symbol: str
    quantity: int
    avg_cost: float | None
    market_price: float | None
    market_value: float | None
    unrealized_pnl: float | None


@dataclass
class ClosedTrade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    pnl: float
    pnl_pct: float | None


# ---------------------------------------------------------------- loaders


from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from vibe_trade.db.models import DailyPnL, PortfolioSnapshot, Trade


def load_daily_pnl(session: Session, days: int, today: date) -> list[DailyRow]:
    """Return daily_pnl rows where date >= today - `days`, oldest first."""
    cutoff = today - timedelta(days=days)
    rows = (
        session.query(DailyPnL)
        .filter(DailyPnL.date >= cutoff)
        .order_by(DailyPnL.date)
        .all()
    )
    return [
        DailyRow(
            date=r.date,
            realized_pnl=r.realized_pnl or 0.0,
            unrealized_pnl=r.unrealized_pnl or 0.0,
            account_value=r.account_value,
            open_positions_count=r.open_positions_count,
        )
        for r in rows
    ]


def load_latest_holdings(session: Session) -> tuple[date | None, list[HoldingRow]]:
    """Return (snapshot_date, rows) from the MAX(date) row of
    portfolio_snapshot. (None, []) when the table is empty."""
    latest = session.query(func.max(PortfolioSnapshot.date)).scalar()
    if latest is None:
        return (None, [])
    rows = (
        session.query(PortfolioSnapshot)
        .filter(PortfolioSnapshot.date == latest)
        .all()
    )
    holdings = [
        HoldingRow(
            symbol=r.symbol,
            quantity=r.quantity,
            avg_cost=r.avg_cost,
            market_price=r.market_price,
            market_value=r.market_value,
            unrealized_pnl=r.unrealized_pnl,
        )
        for r in rows
    ]
    return (latest, holdings)
