"""Performance metrics computed from a BacktestResult.

Pure functional -- no I/O, no IB, no DB. Given the equity curve + trade log,
return a BacktestMetrics dataclass with the standard sharpe/drawdown/win-rate
suite. All metrics are robust against degenerate inputs (no trades, single-day
curve, all wins, all losses).

Sharpe assumes 0 risk-free rate and 252 trading days per year. Returns daily
percentage changes from the equity curve.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

import numpy as np
import pandas as pd

from vibe_trade.backtest.engine import BacktestResult

TRADING_DAYS_PER_YEAR: int = 252


@dataclass
class BacktestMetrics:
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float
    n_trades: int
    win_rate: float        # 0.0 to 1.0
    profit_factor: float   # gross_wins / gross_losses; inf if no losses
    avg_win: float         # mean P&L of winning trades; 0 if no winners
    avg_loss: float        # mean P&L of losing trades (negative); 0 if no losers
    avg_holding_days: float
    exposure_pct: float    # % of trading days with at least one open position


@dataclass
class BenchmarkMetrics:
    symbol: str
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float


def compute_metrics(result: BacktestResult) -> BacktestMetrics:
    eq = result.equity_curve
    trades = result.trades

    # ------------------------------------------------------------ returns
    if len(eq) == 0:
        return _zero_metrics()

    total_return_pct = (result.ending_equity / result.starting_equity - 1.0) * 100.0

    # CAGR over the actual span of the equity curve
    span_days = (eq.index[-1] - eq.index[0]).days
    if span_days > 0:
        years = span_days / 365.25
        if result.starting_equity > 0 and result.ending_equity > 0:
            cagr_pct = ((result.ending_equity / result.starting_equity) ** (1 / years) - 1) * 100.0
        else:
            cagr_pct = 0.0
    else:
        cagr_pct = 0.0

    # ------------------------------------------------------------ sharpe
    daily_returns = eq.pct_change().dropna()
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = (
            daily_returns.mean() / daily_returns.std()
            * math.sqrt(TRADING_DAYS_PER_YEAR)
        )
    else:
        sharpe = 0.0

    # ------------------------------------------------------------ drawdown
    running_peak = eq.cummax()
    drawdown = (eq / running_peak - 1.0)
    max_drawdown_pct = float(drawdown.min() * 100.0) if len(drawdown) else 0.0

    # ------------------------------------------------------------ trades
    n_trades = len(trades)
    if n_trades == 0:
        win_rate = 0.0
        profit_factor = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        avg_holding_days = 0.0
    else:
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [t.pnl for t in trades if t.pnl < 0]
        win_rate = len(wins) / n_trades
        gross_wins = sum(wins)
        gross_losses = abs(sum(losses))
        if gross_losses > 0:
            profit_factor = gross_wins / gross_losses
        else:
            profit_factor = float("inf") if gross_wins > 0 else 0.0
        avg_win = (sum(wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(losses) / len(losses)) if losses else 0.0
        avg_holding_days = sum(t.holding_days for t in trades) / n_trades

    # ------------------------------------------------------------ exposure
    # Approximation: a day "has exposure" if any trade was open on it.
    # Span: from first BUY to last SELL (or end).
    if n_trades > 0:
        # eq is always built from a DatetimeIndex (engine.py); pandas-stubs
        # types .index generically as Index[Any] regardless.
        exposed_days = _count_exposed_days(trades, cast(pd.DatetimeIndex, eq.index))
        exposure_pct = exposed_days / len(eq) * 100.0 if len(eq) else 0.0
    else:
        # Open-at-end positions count as exposure for their holding span,
        # but the simpler approximation is "no closed trades = no exposure".
        # Use open_positions_at_end as a hint that something is open all the
        # way to end-of-curve.
        exposure_pct = 100.0 if result.open_positions_at_end > 0 else 0.0

    return BacktestMetrics(
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        sharpe=float(sharpe),
        max_drawdown_pct=max_drawdown_pct,
        n_trades=n_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win=avg_win,
        avg_loss=avg_loss,
        avg_holding_days=avg_holding_days,
        exposure_pct=exposure_pct,
    )


def _zero_metrics() -> BacktestMetrics:
    return BacktestMetrics(
        total_return_pct=0.0, cagr_pct=0.0, sharpe=0.0, max_drawdown_pct=0.0,
        n_trades=0, win_rate=0.0, profit_factor=0.0,
        avg_win=0.0, avg_loss=0.0, avg_holding_days=0.0, exposure_pct=0.0,
    )


def _count_exposed_days(trades, all_dates: pd.DatetimeIndex) -> int:
    """Count distinct trading days where at least one trade was open.

    A trade is "open" on day D if entry_date <= D <= exit_date.
    """
    exposed: set = set()
    date_set = set(d.date() for d in all_dates)
    for t in trades:
        d = t.entry_date
        while d <= t.exit_date:
            if d in date_set:
                exposed.add(d)
            d = _next_day(d)
    return len(exposed)


def _next_day(d):
    from datetime import timedelta
    return d + timedelta(days=1)


def compute_benchmark(symbol: str, close_prices: pd.Series) -> BenchmarkMetrics:
    """Buy-and-hold metrics for a benchmark ETF (e.g. SPY, QQQ).

    `close_prices` is a DatetimeIndex-ed Series of daily closes, already
    sliced to the backtest date range.
    """
    if len(close_prices) < 2:
        return BenchmarkMetrics(
            symbol=symbol, total_return_pct=0.0, cagr_pct=0.0,
            sharpe=0.0, max_drawdown_pct=0.0,
        )

    total_return_pct = (close_prices.iloc[-1] / close_prices.iloc[0] - 1.0) * 100.0

    span_days = (close_prices.index[-1] - close_prices.index[0]).days
    if span_days > 0:
        years = span_days / 365.25
        cagr_pct = ((close_prices.iloc[-1] / close_prices.iloc[0]) ** (1 / years) - 1) * 100.0
    else:
        cagr_pct = 0.0

    daily_returns = close_prices.pct_change().dropna()
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe = float(
            daily_returns.mean() / daily_returns.std()
            * math.sqrt(TRADING_DAYS_PER_YEAR)
        )
    else:
        sharpe = 0.0

    running_peak = close_prices.cummax()
    drawdown = close_prices / running_peak - 1.0
    max_drawdown_pct = float(drawdown.min() * 100.0)

    return BenchmarkMetrics(
        symbol=symbol,
        total_return_pct=float(total_return_pct),
        cagr_pct=float(cagr_pct),
        sharpe=sharpe,
        max_drawdown_pct=max_drawdown_pct,
    )


# Silence the numpy import lint -- kept intentionally for stability across
# pandas backends, though current code paths use pandas methods directly.
_ = np
