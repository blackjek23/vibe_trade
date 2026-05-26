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
