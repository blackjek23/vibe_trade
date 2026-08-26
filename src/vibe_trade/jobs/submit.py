"""V2 submit job — runs at 16:00 Asia/Jerusalem, places market orders.

Flow (locked by docs/ARCHITECTURE_V2.md and project_v2_next_sessions.md memory):
1. Connect with client_id = SUBMIT_CLIENT_ID (1)
2. Pull account + positions from IB (source of truth, NOT the DB)
3. Exits phase: for each long position, evaluate strategy on yesterday's
   daily bar; if SELL, place a market sell at the held quantity.
4. Entries phase: if not at the position cap, iterate the S&P 500 universe
   minus held tickers; for each universe ticker, evaluate strategy; if BUY,
   size the position via `position_sizer.size_position(...)`; if shares > 0,
   place a market buy.
5. Disconnect.

Invariants:
- NO DB writes (V2 separation: record at 16:25 does the DB part). The CLI reads
  the DB to build the symbol->owner map and passes it in; this function stays
  DB-free.
- NO trailing stops, no risk-per-share math — sizing is purely % of net_liq.
- Multiple strategies, priority-ordered (Session L). Entries: first strategy to
  BUY a ticker wins. Exits: strategy-scoped (only the owner evaluates).
- Held tickers are skipped in entries phase (no averaging).

The function is broker-agnostic for testability — pass any object that
satisfies the BaseBroker interface. The CLI command (Step 3) handles the
real-IB connection lifecycle around `run_submit`.

PARITY NOTE: backtest/engine.py replicates the exits -> force-trim -> entries
sequencing of this job by hand. If you change the phase order, cap handling,
or entry conflict resolution here, update the backtest loop to match.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from vibe_trade.broker.base import BaseBroker
from vibe_trade.broker.models import PLACEMENT_FAILURE_STATUSES, OrderRequest
from vibe_trade.data.market_calendar import today_us_eastern
from vibe_trade.data.provider import DataProvider
from vibe_trade.risk.manager import RiskManager
from vibe_trade.risk.position_sizer import (
    DEFAULT_MAX_POSITIONS,
    size_position,
)
from vibe_trade.strategy.base import SignalType
from vibe_trade.strategy.registry import BuiltStrategy

logger = logging.getLogger(__name__)

# Hardcoded client IDs per V2 design (memory: project_v2_next_sessions.md).
# fill.execution.clientId reveals which phase placed each order.
SUBMIT_CLIENT_ID: int = 1
RECORD_CLIENT_ID: int = 2
RECONCILE_CLIENT_ID: int = 3

# Daily-bar lookback for yfinance: Donchian needs N+1 bars, but yfinance
# returns trading days within a calendar period. 60 calendar days yields
# ~42 trading days, comfortable margin over the 21 we need.
DEFAULT_LOOKBACK_DAYS: int = 60

# A genuinely flat account holds ~all its net liquidation as cash (no
# positions to tie it up). If `get_positions()` comes back empty AND cash is
# meaningfully below net_liq, that combination is far more likely a
# failed/racing IB position read (Gateway mid-login, a slow handshake, a
# multi-account setup that hasn't auto-subscribed) than a real flat account.
# See C-1, PROJECT_EVALUATION.md.
EMPTY_POSITION_CASH_RATIO: float = 0.95

# IB paper accounts carry a DU/DF id prefix; live accounts a U prefix. See
# SEC-2, PROJECT_EVALUATION.md.
PAPER_ACCOUNT_PREFIXES: tuple[str, ...] = ("DU", "DF")


def _last_bar_is_closed(candles: pd.DataFrame, *, now: datetime | None = None) -> bool:
    """True unless the candle DataFrame's last row is today's US trading date.

    Submit is scheduled at 16:00 Asia/Jerusalem, meant to land comfortably
    before the 09:30 ET US open so yfinance only ever has fully-closed prior
    days to hand back. Israel and the US flip DST on different dates (roughly
    three weeks a year -- see H-1, PROJECT_EVALUATION.md); during that
    mismatch 16:00 IDT lands *after* the US open, and yfinance appends a live,
    still-moving intraday quote as the final row. Every strategy here
    evaluates `candles.iloc[-1]` as though it were a closed daily bar
    (invariant #1) -- silently trading a live quote turns a tested daily
    strategy into an untested intraday one on exactly those days.

    An empty DataFrame is not this function's concern (callers already skip
    on `len(candles) < required_candles` / `candles.empty`), so it returns
    False there rather than asserting anything about freshness. `now`, if
    given, must be timezone-aware -- see `today_us_eastern`.
    """
    if candles.empty:
        return False
    # Explicit annotation: pandas ships no type stubs, so `.date()` on an
    # index entry infers as `Any` -- without this mypy reports "Returning
    # Any from function declared to return bool" on the line below.
    last_bar_date: date = candles.index[-1].date()
    return last_bar_date < today_us_eastern(now)


@dataclass
class SubmitResult:
    universe_size: int = 0
    held_count: int = 0

    # Exits (positions we held and evaluated)
    exits_evaluated: int = 0   # positions we ran the strategy on
    exits_signaled: int = 0    # SELL signals returned
    exits_placed: int = 0      # SELL orders accepted by IB
    exits_failed: int = 0      # exceptions during exit handling

    # Force-trim (over-cap relief, Enhancement #1)
    trim_signaled: int = 0     # candidates returned by RiskManager.select_force_trim_candidates
    trim_placed: int = 0       # SELL orders accepted by IB (orderRef="trim")
    trim_failed: int = 0       # exceptions during trim placement

    # Entries (universe tickers we evaluated)
    entries_evaluated: int = 0       # universe tickers we ran the strategy on
    entries_signaled: int = 0        # BUY signals returned
    entries_placed: int = 0          # BUY orders accepted by IB
    entries_skipped_sizing: int = 0  # BUY signaled but sizer returned 0
    entries_failed: int = 0          # exceptions during entry handling

    entries_phase_skipped: bool = False  # True if at position cap
    cap_reason: str = ""

    # True when submit aborted because today is not a NYSE trading day (a
    # weekend or US market holiday the fixed `* * 1-5` cron schedule doesn't
    # know about) -- H-4, PROJECT_EVALUATION.md.
    aborted_non_trading_day: bool = False

    # Double-run guard: True when submit aborted because strategy orderRefs
    # were already present at IB today (cron retry / manual re-run).
    aborted_duplicate_run: bool = False

    # True when submit aborted because IB reported 0 positions but the cash
    # balance says otherwise -- a suspected failed/racing position read (C-1).
    aborted_empty_position_read: bool = False

    # True when submit aborted because the account IB Gateway is actually
    # serving doesn't match the configured `mode` (SEC-2).
    aborted_mode_mismatch: bool = False

    data_unavailable: int = 0  # universe tickers yfinance returned no bars for
    stale_bars_skipped: int = 0  # last bar was today's US date, not a closed prior day (H-1)

    # Per-strategy attribution (Session L): {strategy_id: count} for placed orders.
    entries_placed_by_strategy: dict[str, int] = field(default_factory=dict)
    exits_placed_by_strategy: dict[str, int] = field(default_factory=dict)

    errors: list[str] = field(default_factory=list)


def _required_lookback_days(strategies: list[BuiltStrategy]) -> int:
    """Calendar-day lookback covering the largest strategy `required_candles`.

    `required_candles` is in trading bars; ~5 trading days per 7 calendar days,
    so multiply by 1.6 and add a buffer. Never below DEFAULT_LOOKBACK_DAYS so
    short-window strategies keep their historical margin.
    """
    max_required = max(s.strategy.required_candles for s in strategies)
    return max(DEFAULT_LOOKBACK_DAYS, math.ceil(max_required * 1.6) + 15)


async def run_submit(
    *,
    broker: BaseBroker,
    strategies: list[BuiltStrategy],
    data_provider: DataProvider,
    risk_manager: RiskManager,
    universe: list[str],
    position_strategies: dict[str, str] | None = None,
    max_positions: int = DEFAULT_MAX_POSITIONS,
    lookback_days: int | None = None,
    force: bool = False,
    mode: str = "paper",
    now: datetime | None = None,
    is_trading_day: bool = True,
) -> SubmitResult:
    """Execute one submit cycle. Caller manages the broker connection.

    `strategies` is the priority-ordered list of active strategies (highest
    priority first). `position_strategies` maps held symbol -> owning strategy id
    (supplied by the CLI from the DB so this function stays DB-free); a position
    whose owner is missing or no longer active is exited by the highest-priority
    strategy (`strategies[0]`). A non-empty map is also the second signal for
    the C-1 empty-position-read guard below -- it means the DB has at least
    one OPEN row for a symbol IB isn't reporting at all.

    `mode` is the configured `general.mode` ("paper" or "live"); it is checked
    against the IB account id actually connected to (SEC-2) and has nothing to
    do with which port was dialed -- that decision already happened in
    `broker.connect()`. This is the last chance to catch a config/Gateway
    mismatch before an order is placed.

    `now`, if given, must be timezone-aware -- passed through to the H-1
    bar-freshness check (`_last_bar_is_closed`) for deterministic tests.
    Defaults to real time in US/Eastern.

    `is_trading_day` (H-4, PROJECT_EVALUATION.md) should be computed by the
    caller from `data.market_calendar.is_us_trading_day` -- deliberately not
    computed in here from real wall-clock time, which would make this
    function's behavior depend on which day the test suite happens to run.
    The CLI checks it before even connecting to IB, so the common path never
    reaches this guard; it exists as defense-in-depth for any other caller.

    Returns a SubmitResult counting what happened. Per-symbol exceptions are
    logged and recorded but never abort the run -- one bad ticker shouldn't
    take down the whole submit.
    """
    if not strategies:
        raise ValueError("run_submit requires at least one strategy")

    result = SubmitResult()

    if not is_trading_day:
        result.aborted_non_trading_day = True
        msg = (
            "submit aborted: today is not a NYSE trading day (weekend or US "
            "market holiday) -- nothing to evaluate."
        )
        result.errors.append(msg)
        logger.error(msg)
        return result

    # ------------------------------------------------- double-run guard
    # Submit has no DB state (V2 invariant), so IB itself is the dedup
    # source: if any of our strategy orderRefs (or "trim") already appear in
    # today's fills or working orders, submit already ran today and a re-run
    # (cron retry, manual re-invocation) would duplicate every order.
    if not force:
        known_refs = {b.strategy.name for b in strategies} | {"trim"}
        seen_today = await broker.get_today_order_refs()
        dup_refs = sorted(seen_today & known_refs)
        if dup_refs:
            result.aborted_duplicate_run = True
            msg = (
                f"submit already ran today: order ref(s) {dup_refs} found at "
                "IB. Re-running would duplicate orders -- aborting. "
                "Pass force=True (CLI: --force) to override."
            )
            result.errors.append(msg)
            logger.error(msg)
            return result
    owners = position_strategies or {}
    by_name = {b.strategy.name: b for b in strategies}
    default_built = strategies[0]
    if lookback_days is None:
        lookback_days = _required_lookback_days(strategies)

    account = await broker.get_account_summary()

    # --------------------------------------------------- mode-mismatch guard
    # config.toml's `mode` and the account IB Gateway actually serves (set by
    # a separate TRADING_MODE env var read by IBC) are two unlinked decisions.
    # An empty account_id means the read itself is untrustworthy (Gateway
    # mid-login) -- preflight's job to catch, not this check's.
    if account.account_id:
        is_paper_account = account.account_id.startswith(PAPER_ACCOUNT_PREFIXES)
        if is_paper_account != (mode == "paper"):
            result.aborted_mode_mismatch = True
            msg = (
                f"submit aborted: config mode={mode!r} but IB account "
                f"{account.account_id!r} looks like a "
                f"{'paper' if is_paper_account else 'live'} account -- Gateway "
                "may be serving the wrong account for this config. Refusing "
                "to place orders until this is resolved."
            )
            result.errors.append(msg)
            logger.error(msg)
            return result

    positions = await broker.get_positions()

    # Long positions only (quantity > 0). Shorts aren't part of V2 first iter.
    longs = [p for p in positions if p.quantity > 0]
    held_symbols: set[str] = {p.symbol for p in longs}
    result.universe_size = len(universe)
    result.held_count = len(longs)

    # ------------------------------------------- empty-position-read guard
    # Two independent signals, either one enough to distrust an empty read
    # (matches the OR in the original fix -- reconcile._sweep_open_rows uses
    # the DB-mismatch half of this same instinct, read-only; C-1,
    # PROJECT_EVALUATION.md called out that submit lacked either half):
    #   1. Cash doesn't back up a flat account (structural, no DB needed).
    #   2. The DB thinks positions are OPEN that IB doesn't report at all --
    #      `owners` is built by the CLI from `get_open_trades()` before
    #      calling run_submit, so a non-empty map here already means at
    #      least one OPEN row exists (this function stays DB-free itself;
    #      it only reads what the caller already fetched).
    cash_looks_suspicious = (
        account.total_cash < account.net_liquidation * EMPTY_POSITION_CASH_RATIO
    )
    db_reports_open_positions = bool(owners)
    if not longs and (cash_looks_suspicious or db_reports_open_positions):
        result.aborted_empty_position_read = True
        reasons = []
        if cash_looks_suspicious:
            reasons.append(
                f"total_cash (${account.total_cash:,.2f}) is well below "
                f"net_liquidation (${account.net_liquidation:,.2f})"
            )
        if db_reports_open_positions:
            reasons.append(
                f"the DB reports {len(owners)} OPEN position(s) IB doesn't show at all"
            )
        msg = (
            f"submit aborted: IB reported 0 long positions but "
            f"{' and '.join(reasons)} -- treating this as a failed/racing "
            "position read, not a flat account. Investigate before re-running."
        )
        result.errors.append(msg)
        logger.error(msg)
        return result

    logger.info(
        "submit start: %d held, $%.2f net_liq, universe=%d, strategies=%s",
        len(longs), account.net_liquidation, len(universe),
        [b.strategy.name for b in strategies],
    )

    # ----------------------------------------------------------------- exits
    # Strategy-scoped: each position is evaluated only by the strategy that
    # opened it (owner). Unknown/orphan owners fall back to the highest-priority
    # strategy.
    strategy_exit_symbols: set[str] = set()
    for pos in longs:
        result.exits_evaluated += 1
        try:
            # "" default (never a real strategy id) instead of None: dict.get
            # requires a str key, and an unowned/orphan position should fall
            # through to default_built exactly like an unrecognized one does.
            built = by_name.get(owners.get(pos.symbol, ""), default_built)
            strategy = built.strategy
            candles = await data_provider.get_candles(
                pos.symbol, timeframe="1d", lookback_days=lookback_days
            )
            if len(candles) < strategy.required_candles:
                logger.warning(
                    "exit %s: only %d candles (need %d) -- holding",
                    pos.symbol, len(candles), strategy.required_candles,
                )
                continue
            if not _last_bar_is_closed(candles, now=now):
                result.stale_bars_skipped += 1
                logger.warning(
                    "exit %s: last bar (%s) is today's US trading date, not a "
                    "closed prior day -- holding to avoid evaluating a live "
                    "intraday quote as a daily bar (H-1)",
                    pos.symbol, candles.index[-1].date(),
                )
                continue

            sig = strategy.evaluate(pos.symbol, candles)
            if sig.signal != SignalType.SELL:
                continue

            result.exits_signaled += 1
            logger.info(
                "SELL signal: %s (qty=%d, strategy=%s)",
                pos.symbol, pos.quantity, strategy.name,
            )

            order_result = await broker.place_market_order(
                OrderRequest(
                    symbol=pos.symbol, side="SELL", quantity=pos.quantity,
                    order_ref=strategy.name,
                )
            )
            if order_result.status not in PLACEMENT_FAILURE_STATUSES:
                result.exits_placed += 1
                result.exits_placed_by_strategy[strategy.name] = (
                    result.exits_placed_by_strategy.get(strategy.name, 0) + 1
                )
                strategy_exit_symbols.add(pos.symbol)
            else:
                result.exits_failed += 1
                result.errors.append(
                    f"exit {pos.symbol}: order status={order_result.status} "
                    f"err={order_result.error_message}"
                )
        except Exception as exc:  # noqa: BLE001 -- intentional broad catch
            result.exits_failed += 1
            err = f"exit {pos.symbol}: {exc!r}"
            logger.exception(err)
            result.errors.append(err)

    # ------------------------------------------------------------ force-trim
    # Enhancement #1: if we still hold more than `max_positions` after strategy
    # exits, sell the worst-performing (lowest unrealized $ P&L) until we're
    # back at the cap. Trim orders are tagged orderRef="trim" so record /
    # analytics can tell them apart from strategy exits.
    trim_symbols = risk_manager.select_force_trim_candidates(
        longs, max_positions, already_exiting=strategy_exit_symbols,
    )
    result.trim_signaled = len(trim_symbols)
    if trim_symbols:
        logger.info(
            "FORCE TRIM: held=%d max=%d -> trimming %d position(s): %s",
            len(longs) - len(strategy_exit_symbols), max_positions,
            len(trim_symbols), trim_symbols,
        )
        # Map symbol -> Position so we know the quantity to sell.
        by_symbol = {p.symbol: p for p in longs}
        for sym in trim_symbols:
            try:
                pos = by_symbol[sym]
                order_result = await broker.place_market_order(
                    OrderRequest(
                        symbol=sym, side="SELL", quantity=pos.quantity,
                        order_ref="trim",
                    )
                )
                if order_result.status not in PLACEMENT_FAILURE_STATUSES:
                    result.trim_placed += 1
                else:
                    result.trim_failed += 1
                    result.errors.append(
                        f"trim {sym}: order status={order_result.status} "
                        f"err={order_result.error_message}"
                    )
            except Exception as exc:  # noqa: BLE001
                result.trim_failed += 1
                err = f"trim {sym}: {exc!r}"
                logger.exception(err)
                result.errors.append(err)

    # --------------------------------------------------------------- entries
    cap_check = risk_manager.can_open_new_position(positions)
    if not cap_check.approved:
        result.entries_phase_skipped = True
        result.cap_reason = cap_check.reason
        logger.info("entries phase skipped: %s", cap_check.reason)
        return result

    # Prefetch every candidate's daily bars concurrently. The universe scan is
    # the slow part of submit (~5 min sequential); bounded concurrency in
    # get_candles_batch cuts it to ~30 s (Hygiene #4, SESSION_H_FINDINGS.md).
    entry_symbols = [s for s in universe if s not in held_symbols]
    candles_by_symbol = await data_provider.get_candles_batch(
        entry_symbols, timeframe="1d", lookback_days=lookback_days
    )

    # Track positions we've added during this run so the sizer's cap stays
    # honest as we place BUYs (orders placed but not yet filled still count).
    placed_this_run = 0

    for symbol in entry_symbols:
        result.entries_evaluated += 1
        try:
            candles = candles_by_symbol[symbol]
            if candles.empty:
                result.data_unavailable += 1
                continue
            if not _last_bar_is_closed(candles, now=now):
                result.stale_bars_skipped += 1
                logger.warning(
                    "entry %s: last bar (%s) is today's US trading date, not a "
                    "closed prior day -- skipping (H-1)",
                    symbol, candles.index[-1].date(),
                )
                continue

            # Priority conflict resolution: the first (highest-priority) strategy
            # that returns BUY claims the ticker. One order per ticker.
            chosen: BuiltStrategy | None = None
            for built in strategies:
                if len(candles) < built.strategy.required_candles:
                    continue
                if built.strategy.evaluate(symbol, candles).signal == SignalType.BUY:
                    chosen = built
                    break
            if chosen is None:
                continue

            result.entries_signaled += 1
            strat_id = chosen.strategy.name

            price = float(candles["close"].iloc[-1])
            current_count = len(longs) + placed_this_run
            shares = size_position(
                net_liquidation=account.net_liquidation,
                price=price,
                current_position_count=current_count,
                pct_per_position=chosen.pct_per_position,
                max_positions=max_positions,
            )
            if shares <= 0:
                result.entries_skipped_sizing += 1
                logger.info(
                    "BUY %s (%s) skipped by sizer: price=$%.2f count=%d",
                    symbol, strat_id, price, current_count,
                )
                continue

            logger.info(
                "BUY signal: %s shares=%d price=$%.2f strategy=%s",
                symbol, shares, price, strat_id,
            )
            order_result = await broker.place_market_order(
                OrderRequest(
                    symbol=symbol, side="BUY", quantity=shares, order_ref=strat_id,
                )
            )
            if order_result.status not in PLACEMENT_FAILURE_STATUSES:
                result.entries_placed += 1
                result.entries_placed_by_strategy[strat_id] = (
                    result.entries_placed_by_strategy.get(strat_id, 0) + 1
                )
                placed_this_run += 1
                held_symbols.add(symbol)
            else:
                result.entries_failed += 1
                result.errors.append(
                    f"entry {symbol}: order status={order_result.status} "
                    f"err={order_result.error_message}"
                )
        except Exception as exc:  # noqa: BLE001
            result.entries_failed += 1
            err = f"entry {symbol}: {exc!r}"
            logger.exception(err)
            result.errors.append(err)

    if result.data_unavailable:
        logger.info(
            "skipped %d ticker(s) — data unavailable", result.data_unavailable
        )
    if result.stale_bars_skipped:
        logger.warning(
            "skipped %d symbol(s) — last bar was today's US date, not closed "
            "(H-1: check for a DST mismatch between Asia/Jerusalem and US/Eastern)",
            result.stale_bars_skipped,
        )

    return result
