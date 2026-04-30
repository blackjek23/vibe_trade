"""Day-by-day backtest engine.

Reuses production code where it matters:
- DonchianStrategy (or any BaseStrategy) for signal generation
- position_sizer.size_position for share-count math
- Same V2 invariants: exits-then-entries, no averaging, skip-held-tickers,
  next-day-open fills

Simulation model:
- For each trading day in the date range:
  1. Apply orders queued yesterday at today's OPEN (next-day-open fill)
  2. Mark-to-market with today's CLOSE for the equity curve
  3. Evaluate strategy on bars up through today (treating today as the
     just-closed bar -- matches V2 cron timing of running at 16:00 Jerusalem
     when the prior US session has just settled)
  4. Queue tomorrow's orders: SELLs for held positions that signaled SELL,
     BUYs for universe tickers that signaled BUY, sized via size_position

Frictions: zero (commissions, slippage, fees). Per-position cash check on BUY:
if not enough cash, skip the order. No margin, no shorting.

Open positions at end-of-range stay open in the result (their unrealized P&L
is reflected in the final equity curve mark).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from vibe_trade.backtest.data import DEFAULT_CACHE_DIR, load_bars
from vibe_trade.risk.position_sizer import size_position
from vibe_trade.strategy.base import BaseStrategy, SignalType

logger = logging.getLogger(__name__)


# ----------------------------------------------------------- result types
@dataclass
class BacktestTrade:
    symbol: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    qty: int
    pnl: float

    @property
    def pnl_pct(self) -> float:
        basis = self.entry_price * self.qty
        return (self.pnl / basis * 100.0) if basis > 0 else 0.0

    @property
    def holding_days(self) -> int:
        return (self.exit_date - self.entry_date).days


@dataclass
class _OpenPosition:
    symbol: str
    shares: int
    avg_cost: float
    entry_date: date


@dataclass
class _PendingOrder:
    side: str   # "BUY" or "SELL"
    symbol: str
    qty: int


@dataclass
class BacktestResult:
    starting_equity: float
    ending_equity: float
    equity_curve: pd.Series          # indexed by trading-day Timestamp
    trades: list[BacktestTrade] = field(default_factory=list)
    open_positions_at_end: int = 0
    skipped_buys_no_cash: int = 0    # BUY signals that couldn't fill (cash short)
    skipped_buys_no_data: int = 0    # filled-day had no bar (delisting/halt)


# ----------------------------------------------------------- engine
def run_backtest(
    *,
    strategy: BaseStrategy,
    universe: list[str],
    start: date,
    end: date,
    starting_equity: float = 100_000.0,
    pct_per_position: float = 0.04,
    max_positions: int = 25,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    bars: dict[str, pd.DataFrame] | None = None,
) -> BacktestResult:
    """Execute one backtest run.

    `bars` is an optional pre-loaded {symbol: DataFrame} for tests. In
    production it's left None and we load from `cache_dir`.
    """
    # ------------------------------------------------------------------ load
    if bars is None:
        bars = {}
        for sym in universe:
            df = load_bars(sym, cache_dir=cache_dir, start=start, end=end)
            if not df.empty:
                bars[sym] = df

    if not bars:
        # Defensive: nothing to simulate -> return flat curve.
        empty_idx = pd.DatetimeIndex([])
        return BacktestResult(
            starting_equity=starting_equity,
            ending_equity=starting_equity,
            equity_curve=pd.Series([], index=empty_idx, dtype=float),
        )

    # Trading-day timeline = union of all bar dates (one symbol may halt while
    # others trade; we still need to mark equity on those days).
    all_dates: list[pd.Timestamp] = sorted(
        set().union(*(b.index for b in bars.values()))
    )

    # ------------------------------------------------------------------ state
    cash: float = starting_equity
    positions: dict[str, _OpenPosition] = {}
    pending: list[_PendingOrder] = []
    trades: list[BacktestTrade] = []
    equity_points: list[tuple[pd.Timestamp, float]] = []
    skipped_no_cash = 0
    skipped_no_data = 0

    universe_set = set(universe)

    # ------------------------------------------------------------------ loop
    for today_ts in all_dates:
        today: date = today_ts.date()

        # --- 1. Fill pending orders at today's open
        new_pending: list[_PendingOrder] = []
        for order in pending:
            df = bars.get(order.symbol)
            if df is None or today_ts not in df.index:
                # Symbol had no bar today (halt/delisting). Drop the order.
                if order.side == "BUY":
                    skipped_no_data += 1
                continue
            fill_price = float(df.loc[today_ts, "open"])

            if order.side == "BUY":
                cost = fill_price * order.qty
                if cost > cash:
                    skipped_no_cash += 1
                    continue
                cash -= cost
                positions[order.symbol] = _OpenPosition(
                    symbol=order.symbol,
                    shares=order.qty,
                    avg_cost=fill_price,
                    entry_date=today,
                )
            elif order.side == "SELL":
                pos = positions.get(order.symbol)
                if pos is None:
                    continue  # already closed somehow; skip
                proceeds = fill_price * pos.shares
                pnl = (fill_price - pos.avg_cost) * pos.shares
                cash += proceeds
                trades.append(
                    BacktestTrade(
                        symbol=pos.symbol,
                        entry_date=pos.entry_date,
                        entry_price=pos.avg_cost,
                        exit_date=today,
                        exit_price=fill_price,
                        qty=pos.shares,
                        pnl=pnl,
                    )
                )
                del positions[order.symbol]
        pending = new_pending  # cleared

        # --- 2. Mark-to-market with today's close
        market_value = 0.0
        for sym, pos in positions.items():
            df = bars.get(sym)
            if df is not None and today_ts in df.index:
                market_value += pos.shares * float(df.loc[today_ts, "close"])
            else:
                # Stale price: use avg_cost as conservative fallback.
                market_value += pos.shares * pos.avg_cost
        net_liq = cash + market_value
        equity_points.append((today_ts, net_liq))

        # --- 3. Generate signals from today's closed bars
        # Exits phase: only held positions
        for sym, pos in list(positions.items()):
            df = bars.get(sym)
            if df is None:
                continue
            df_to_today = df.loc[:today_ts]
            if len(df_to_today) < strategy.required_candles:
                continue
            sig = strategy.evaluate(sym, df_to_today)
            if sig.signal == SignalType.SELL:
                pending.append(_PendingOrder(side="SELL", symbol=sym, qty=pos.shares))

        # Entries phase: skip if at cap; iterate universe minus held
        if len(positions) < max_positions:
            buys_queued_today = 0
            for sym in universe:
                if sym in positions:
                    continue
                df = bars.get(sym)
                if df is None or today_ts not in df.index:
                    continue
                df_to_today = df.loc[:today_ts]
                if len(df_to_today) < strategy.required_candles:
                    continue
                sig = strategy.evaluate(sym, df_to_today)
                if sig.signal != SignalType.BUY:
                    continue
                price = float(df_to_today["close"].iloc[-1])
                shares = size_position(
                    net_liquidation=net_liq,
                    price=price,
                    current_position_count=len(positions) + buys_queued_today,
                    pct_per_position=pct_per_position,
                    max_positions=max_positions,
                )
                if shares <= 0:
                    continue
                pending.append(_PendingOrder(side="BUY", symbol=sym, qty=shares))
                buys_queued_today += 1

    # ------------------------------------------------------------------ result
    equity_series = pd.Series(
        [v for _, v in equity_points],
        index=pd.DatetimeIndex([d for d, _ in equity_points]),
        dtype=float,
        name="equity",
    )

    return BacktestResult(
        starting_equity=starting_equity,
        ending_equity=float(equity_series.iloc[-1]) if len(equity_series) else starting_equity,
        equity_curve=equity_series,
        trades=trades,
        open_positions_at_end=len(positions),
        skipped_buys_no_cash=skipped_no_cash,
        skipped_buys_no_data=skipped_no_data,
    )
