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

Frictions: modeled, not assumed -- see the `Frictions` dataclass below.
Defaults to `NO_FRICTION` (all zero), the original behavior, so every
existing caller and test is unaffected; pass `frictions=` to cost a run.
Per-position cash check on BUY: if not enough cash, skip the order. No
margin, no shorting.

Universe: `universe` is whatever the caller fetched bars for -- by itself it
says nothing about which of those symbols were actually S&P 500 members on
any given day. Pass `membership` (a `backtest.membership.MembershipTimeline`)
to restrict entries to real point-in-time members and close that
survivorship-bias gap (C-2, PROJECT_EVALUATION.md); omit it to keep the old
unfiltered behavior.

Open positions at end-of-range stay open in the result (their unrealized P&L
is reflected in the final equity curve mark).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import SupportsFloat, cast

import pandas as pd

from vibe_trade.backtest.data import DEFAULT_CACHE_DIR, load_bars
from vibe_trade.backtest.membership import MembershipTimeline
from vibe_trade.risk.position_sizer import size_position
from vibe_trade.strategy.base import BaseStrategy, SignalType

logger = logging.getLogger(__name__)


# ----------------------------------------------------------- frictions
@dataclass(frozen=True)
class Frictions:
    """Trading costs applied to every simulated fill.

    All fields default to zero, so `Frictions()` (also available as
    `NO_FRICTION`) reproduces the engine's original frictionless behavior
    exactly.

    - `slippage_bps`: symmetric adverse-price cost applied to every fill's
      raw OPEN price -- a BUY fills *above* the open, a SELL fills *below*
      it, both moving against the trader. This is the same sign convention
      `scripts/measure_slippage.py` uses, so a number that script prints can
      be pasted straight in. See `MEASURED_SLIPPAGE_BPS_MEDIAN`/`_MEAN` below
      for the values measured from the 2026-05..07 paper run.
    - `commission_per_share` / `commission_min`: IB tiered/fixed US equities
      pricing (per-share rate with a per-order minimum) -- see
      `IB_COMMISSION_PER_SHARE`/`IB_COMMISSION_MIN`, the same constants
      `measure_slippage.py` uses, so a backtest run and a live-cost estimate
      agree on the commission model.

    Slippage is folded into the fill price itself (so it flows through cost
    basis and proceeds naturally); commission is charged as a separate
    per-order dollar amount on every fill, entry and exit alike. Neither
    applies to mark-to-market valuation -- friction is a cost of *executing*,
    not of holding, so the equity curve prices open positions at the raw
    close.
    """

    slippage_bps: float = 0.0
    commission_per_share: float = 0.0
    commission_min: float = 0.0

    def fill_price(self, raw_price: float, side: str) -> float:
        """Adjust a raw OPEN price for slippage. `side` is `"BUY"` or `"SELL"`."""
        adj = raw_price * self.slippage_bps / 10_000
        return raw_price + adj if side == "BUY" else raw_price - adj

    def commission(self, qty: int) -> float:
        """Dollar commission for one order of `qty` shares. Zero if both
        commission fields are zero, so an all-zero `Frictions` never charges
        the `commission_min` floor for doing nothing."""
        if self.commission_per_share <= 0 and self.commission_min <= 0:
            return 0.0
        return max(self.commission_min, self.commission_per_share * qty)


NO_FRICTION = Frictions()

# IB tiered/fixed US equities pricing -- mirrors scripts/measure_slippage.py's
# IB_PER_SHARE/IB_MIN_ORDER so a backtest run and that script's live-cost
# estimate never silently diverge on the commission model.
IB_COMMISSION_PER_SHARE = 0.005
IB_COMMISSION_MIN = 1.00

# Measured from 111 legs of the 2026-05-14 -> 07-29 paper run (early-May manual
# scratch placements excluded -- see scripts/measure_slippage.py and
# docs/playbooks/go-live-criteria.md). Per-leg bps; round trip is ~2x this.
# The median is the base case ("prefer this" per measure_slippage.py's own
# output); the mean is right-skewed by a few bad fills, so go-live-criteria.md
# uses it as the stress case, not the expected one.
MEASURED_SLIPPAGE_BPS_MEDIAN = 26.6
MEASURED_SLIPPAGE_BPS_MEAN = 40.9


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
    avg_cost: float             # slippage-adjusted fill price (commission tracked separately)
    entry_date: date
    entry_commission: float = 0.0


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
    force_trim_sells: int = 0        # Enhancement #1: over-cap relief sells queued
    total_commission: float = 0.0    # sum of Frictions.commission() paid across all fills


# ----------------------------------------------------------- helpers
def _backtest_trim_candidates(
    positions: dict[str, "_OpenPosition"],
    today_ts: pd.Timestamp,
    bars: dict[str, pd.DataFrame],
    max_positions: int,
    already_exiting: set[str],
) -> list[str]:
    """Backtest analogue of `RiskManager.select_force_trim_candidates`.

    Production uses IB's live ``unrealizedPNL``. Backtest uses
    ``(current_close - avg_cost) * shares`` as the proxy. Same ranking
    semantics (ascending dollar P&L, most-negative first). Pure function.
    """
    held_after_exits = len(positions) - len(already_exiting)
    over = held_after_exits - max_positions
    if over <= 0:
        return []
    eligible: list[tuple[float, str]] = []
    for sym, pos in positions.items():
        if sym in already_exiting:
            continue
        df = bars.get(sym)
        if df is not None and today_ts in df.index:
            cur_close = float(cast(SupportsFloat, df.loc[today_ts, "close"]))
            pnl_dollars = (cur_close - pos.avg_cost) * pos.shares
        else:
            pnl_dollars = 0.0  # missing bar -> treat as flat
        eligible.append((pnl_dollars, sym))
    eligible.sort(key=lambda t: t[0])
    return [s for _, s in eligible[:over]]


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
    membership: MembershipTimeline | None = None,
    frictions: Frictions | None = None,
) -> BacktestResult:
    """Execute one backtest run.

    `bars` is an optional pre-loaded {symbol: DataFrame} for tests. In
    production it's left None and we load from `cache_dir`.

    `frictions`, if given, applies `Frictions.slippage_bps` to every fill
    price and `Frictions.commission()` to every order (entry and exit
    alike), deducted from realized `BacktestTrade.pnl`. `None` (the default)
    is equivalent to `NO_FRICTION` -- the original zero-cost behavior, so
    every existing caller and test is unaffected.

    `membership`, if given, restricts *entries* to symbols that were
    actually S&P 500 members on the signal day (C-2, PROJECT_EVALUATION.md:
    `universe` alone is survivorship-biased -- it's whatever the caller
    fetched bars for, typically today's constituent list applied unchanged
    across the whole window). Held positions are never affected: a symbol
    that drops out of the index after entry can still be exited normally,
    since the exits loop iterates `positions`, not `universe`. Passing
    `None` (the default) preserves the old unfiltered-universe behavior,
    so every existing caller and test is unaffected.
    """
    frictions = frictions or NO_FRICTION

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
    force_trim_sells = 0
    total_commission = 0.0

    # ------------------------------------------------------------------ loop
    for today_ts in all_dates:
        today: date = today_ts.date()

        # --- 1. Fill pending orders at today's open
        new_pending: list[_PendingOrder] = []
        for order in pending:
            fill_df = bars.get(order.symbol)
            if fill_df is None or today_ts not in fill_df.index:
                # Symbol had no bar today (halt/delisting). Drop the order.
                if order.side == "BUY":
                    skipped_no_data += 1
                continue
            raw_open = float(cast(SupportsFloat, fill_df.loc[today_ts, "open"]))
            fill_price = frictions.fill_price(raw_open, order.side)

            if order.side == "BUY":
                entry_commission = frictions.commission(order.qty)
                cost = fill_price * order.qty + entry_commission
                if cost > cash:
                    skipped_no_cash += 1
                    continue
                cash -= cost
                total_commission += entry_commission
                positions[order.symbol] = _OpenPosition(
                    symbol=order.symbol,
                    shares=order.qty,
                    avg_cost=fill_price,
                    entry_date=today,
                    entry_commission=entry_commission,
                )
            elif order.side == "SELL":
                pos = positions.get(order.symbol)
                if pos is None:
                    continue  # already closed somehow; skip
                exit_commission = frictions.commission(pos.shares)
                proceeds = fill_price * pos.shares - exit_commission
                pnl = (fill_price - pos.avg_cost) * pos.shares - pos.entry_commission - exit_commission
                cash += proceeds
                total_commission += exit_commission
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
            mtm_df = bars.get(sym)
            if mtm_df is not None and today_ts in mtm_df.index:
                market_value += pos.shares * float(cast(SupportsFloat, mtm_df.loc[today_ts, "close"]))
            else:
                # Stale price: use avg_cost as conservative fallback.
                market_value += pos.shares * pos.avg_cost
        net_liq = cash + market_value
        equity_points.append((today_ts, net_liq))

        # --- 3. Generate signals from today's closed bars
        #
        # PARITY NOTE: this exits -> force-trim -> entries sequencing mirrors
        # jobs/submit.py:run_submit by hand (submit needs a broker; we need
        # next-open fills). If you change submit's phase order, cap handling,
        # or conflict resolution, update this loop to match or the backtest
        # silently diverges from production.
        # Exits phase: only held positions
        donchian_exit_symbols: set[str] = set()
        for sym, pos in list(positions.items()):
            exit_df = bars.get(sym)
            if exit_df is None:
                continue
            df_to_today = exit_df.loc[:today_ts]
            if len(df_to_today) < strategy.required_candles:
                continue
            sig = strategy.evaluate(sym, df_to_today)
            if sig.signal == SignalType.SELL:
                pending.append(_PendingOrder(side="SELL", symbol=sym, qty=pos.shares))
                donchian_exit_symbols.add(sym)

        # Force-trim phase (Enhancement #1): if held - donchian_exits > max_positions,
        # rank eligible positions by *current* unrealized $ P&L and queue SELL for
        # the worst (held - donchian - max) of them. See _backtest_trim_candidates.
        # In normal backtests this rarely fires because the sizer-side cap prevents
        # over-cap in the first place; it's the parity safety-net for production.
        trim_picks = _backtest_trim_candidates(
            positions, today_ts, bars, max_positions, donchian_exit_symbols,
        )
        for sym in trim_picks:
            pos = positions[sym]
            pending.append(_PendingOrder(side="SELL", symbol=sym, qty=pos.shares))
            donchian_exit_symbols.add(sym)  # treat as "exiting" for entries phase
            force_trim_sells += 1

        # Entries phase: skip if at cap; iterate universe minus held, minus
        # anything that wasn't actually an index member on this day.
        if len(positions) < max_positions:
            eligible = universe if membership is None else (
                [sym for sym in universe if sym in membership.at(today)]
            )
            buys_queued_today = 0
            for sym in eligible:
                if sym in positions:
                    continue
                entry_df = bars.get(sym)
                if entry_df is None or today_ts not in entry_df.index:
                    continue
                df_to_today = entry_df.loc[:today_ts]
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
        force_trim_sells=force_trim_sells,
        total_commission=total_commission,
    )
