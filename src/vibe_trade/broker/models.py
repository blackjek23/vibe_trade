"""Broker-level data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class AccountSummary:
    account_id: str
    net_liquidation: float
    total_cash: float
    unrealized_pnl: float
    realized_pnl: float


@dataclass
class Position:
    symbol: str
    quantity: int
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float


@dataclass
class OrderRequest:
    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: int
    order_type: str = "MKT"


@dataclass
class OrderResult:
    order_id: int
    symbol: str
    side: str
    quantity: int
    status: str  # "SUBMITTED", "FILLED", "CANCELLED", "ERROR"
    fill_price: float | None = None
    fill_time: datetime | None = None
    error_message: str | None = None
