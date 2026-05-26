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

    # ---- Sharpe on daily account_value returns (std-guarded, same shape
    # as backtest/metrics.py:72)
    values = [r.account_value for r in rows]
    returns: list[float] = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev and prev > 0:
            returns.append(values[i] / prev - 1.0)
    sharpe = 0.0
    if len(returns) >= 2:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var)
        if std > 0:
            sharpe = mean_r / std * math.sqrt(TRADING_DAYS_PER_YEAR)

    # ---- Max drawdown: walk forward, track running peak, find worst
    # (current/peak - 1). Peak date is the *running* peak at the trough,
    # not the eventual max.
    max_dd = 0.0
    dd_peak_date: date | None = None
    dd_trough_date: date | None = None
    cur_peak_value = values[0]
    cur_peak_date = rows[0].date
    for r in rows:
        v = r.account_value
        if v > cur_peak_value:
            cur_peak_value = v
            cur_peak_date = r.date
        dd = v / cur_peak_value - 1.0
        if dd < max_dd:
            max_dd = dd
            dd_peak_date = cur_peak_date
            dd_trough_date = r.date
    max_drawdown_pct = max_dd * 100.0

    # ---- Best/worst day P&L (realized + unrealized)
    day_pnls = [r.realized_pnl + r.unrealized_pnl for r in rows]
    best_day_pnl = max(day_pnls)
    worst_day_pnl = min(day_pnls)

    return ReportMetrics(
        start_value=start,
        end_value=end,
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        sharpe=float(sharpe),
        max_drawdown_pct=max_drawdown_pct,
        max_dd_peak_date=dd_peak_date,
        max_dd_trough_date=dd_trough_date,
        best_day_pnl=best_day_pnl,
        worst_day_pnl=worst_day_pnl,
        sample_size=len(rows),
    )


def compute_closed_trade_stats(closed: list[ClosedTrade]) -> ClosedTradeStats:
    raise NotImplementedError
