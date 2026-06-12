"""Read-only DB loaders for `vibe-trade report`.

Returns simple dataclasses, never ORM objects, so metrics and render
have no SQLAlchemy dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from vibe_trade.db.models import DailyPnL, PortfolioSnapshot, Trade


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


def load_trade_activity(
    session: Session, days: int, today: date,
) -> dict[date, int]:
    """Count of `trades` rows grouped by entry_time::date, within window.

    Source of truth for activity -- the `daily_pnl.trades_opened` column
    is unreliable (often reads 0 even on days with confirmed entries).
    """
    cutoff = today - timedelta(days=days)
    cutoff_dt = datetime.combine(cutoff, datetime.min.time())
    rows = (
        session.query(Trade)
        .filter(Trade.entry_time.is_not(None))
        .filter(Trade.entry_time >= cutoff_dt)
        .all()
    )
    counts: dict[date, int] = {}
    for t in rows:
        d = t.entry_time.date()
        counts[d] = counts.get(d, 0) + 1
    return counts


def load_closed_trades(
    session: Session, days: int, today: date,
) -> list[ClosedTrade]:
    """`trades` rows with status='CLOSED' AND exit_time within the window."""
    cutoff = today - timedelta(days=days)
    cutoff_dt = datetime.combine(cutoff, datetime.min.time())
    rows = (
        session.query(Trade)
        .filter(Trade.status == "CLOSED")
        .filter(Trade.exit_time.is_not(None))
        .filter(Trade.exit_time >= cutoff_dt)
        .all()
    )
    return [
        ClosedTrade(
            symbol=t.symbol,
            entry_time=t.entry_time,
            exit_time=t.exit_time,
            pnl=t.pnl or 0.0,
            pnl_pct=t.pnl_pct,
        )
        for t in rows
    ]


def detect_outlier_days(daily_rows: list[DailyRow]) -> set[date]:
    """Days that look like a Gateway/reconcile artifact:
    open_positions_count == 0 AND realized_pnl != 0.

    Returned dates are included in metrics; callers mark them in the
    output so the operator sees the anomaly.
    """
    return {
        r.date
        for r in daily_rows
        if r.open_positions_count == 0 and r.realized_pnl != 0
    }
