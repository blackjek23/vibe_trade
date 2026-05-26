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


def _zero_metrics() -> ReportMetrics:
    return ReportMetrics(
        start_value=None,
        end_value=None,
        total_return_pct=0.0,
        cagr_pct=None,
        sharpe=0.0,
        max_drawdown_pct=0.0,
        max_dd_peak_date=None,
        max_dd_trough_date=None,
        best_day_pnl=0.0,
        worst_day_pnl=0.0,
        sample_size=0,
    )


def compute_metrics(daily_rows: list[DailyRow]) -> ReportMetrics:
    rows = sorted(
        [r for r in daily_rows if r.account_value is not None],
        key=lambda r: r.date,
    )
    if not rows:
        return _zero_metrics()

    start = rows[0].account_value
    end = rows[-1].account_value
    total_return_pct = (end / start - 1.0) * 100.0 if start else 0.0

    span_days = (rows[-1].date - rows[0].date).days
    if span_days > 0 and start and end and start > 0 and end > 0:
        years = span_days / 365.25
        cagr_pct = ((end / start) ** (1 / years) - 1) * 100.0
    else:
        cagr_pct = None

    return ReportMetrics(
        start_value=start,
        end_value=end,
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        sharpe=0.0,                 # filled in Task 3
        max_drawdown_pct=0.0,       # filled in Task 3
        max_dd_peak_date=None,      # filled in Task 3
        max_dd_trough_date=None,    # filled in Task 3
        best_day_pnl=0.0,           # filled in Task 3
        worst_day_pnl=0.0,          # filled in Task 3
        sample_size=len(rows),
    )


def compute_closed_trade_stats(closed: list[ClosedTrade]) -> ClosedTradeStats:
    raise NotImplementedError
