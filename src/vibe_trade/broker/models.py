"""Broker-level data models."""

from __future__ import annotations

from dataclasses import dataclass
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
    # Tags the order with a free-form string IB stores on the execution. Used
    # by V2 to distinguish strategy exits ("donchian") from force-trim sells
    # ("trim"). Read back via `fill.execution.orderRef` in record / reconcile.
    order_ref: str = ""


@dataclass
class OpenOrder:
    """A working (unfilled) order currently live at the broker.

    Surfaced by the Session J manual override commands -- `cancel-pending`
    lists these and cancels by symbol. `perm_id` is IB's cross-process-stable
    order identifier (see PROJECT_MASTER_STATE §6).
    """

    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: int
    perm_id: int
    status: str  # IB orderStatus, e.g. "PreSubmitted", "Submitted"


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
