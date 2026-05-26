"""Pure metric computation for `vibe-trade report`.

No DB, no rich, no I/O. Reuses the sharpe/drawdown PATTERN from
`backtest/metrics.py` but does not import it -- different input shape
(daily_pnl rows vs an equity Series). Cheap duplication beats coupling
two reports that will evolve separately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from vibe_trade.reports.data import ClosedTrade, DailyRow

TRADING_DAYS_PER_YEAR: int = 252


@dataclass
class ReportMetrics:
    start_value: float | None
    end_value: float | None
    total_return_pct: float
    cagr_pct: float | None
    sharpe: float
    max_drawdown_pct: float
    max_dd_peak_date: date | None
    max_dd_trough_date: date | None
    best_day_pnl: float
    worst_day_pnl: float
    sample_size: int


@dataclass
class ClosedTradeStats:
    n: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    avg_holding_days: float


def compute_metrics(daily_rows: list[DailyRow]) -> ReportMetrics:
    raise NotImplementedError


def compute_closed_trade_stats(closed: list[ClosedTrade]) -> ClosedTradeStats:
    raise NotImplementedError
