"""V2 reconcile job — runs at 23:30 Asia/Jerusalem, finalizes today's trades.

Flow:
1. Connect with `client_id = RECONCILE_CLIENT_ID` (3)
2. Pull `ib.trades()` (for orderStatus terminal-state detection) and
   `ib.fills()` (for execution details: shares/price/permId/realized PnL)
3. Pull account summary + positions for portfolio snapshot
4. For each pending trade in DB (every SUBMITTED + PENDING_CLOSE row, any
   date -- stale rows from missed runs are resolved via day-order expiry):
   - SUBMITTED BUY: aggregate fills by `trade.perm_id`. Transition to
     OPEN / PARTIALLY_FILLED / CANCELLED via `confirm_buy_fill` or
     `mark_cancelled`. CANCELLED is set when no fills exist AND the IB
     orderStatus.status is in {"Cancelled","Inactive","ApiCancelled"}.
   - PENDING_CLOSE: aggregate fills by `trade.exit_perm_id`. Transition to
     CLOSED / PARTIALLY_FILLED via `confirm_close_fill`, with realized PnL
     read from `commissionReport.realizedPNL` (we don't compute it). A no-fill
     cancelled SELL reverts the trade to OPEN (position still held).
5. Write `portfolio_snapshot` rows (one per held ticker)
6. Upsert `daily_pnl` with real `trades_opened` / `trades_closed` counts

Idempotent: rerunning is safe. Already-reconciled trades aren't in
`get_pending_orders` so they're skipped. Snapshot is delete-then-insert.

Cross-process: identifies orders by `permId` (preserved across processes), not
`orderId` (resets to 0). Verified live 2026-04-27.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.db.repository import (
    DailyPnLRepository,
    PortfolioSnapshotRepository,
    TradeRepository,
)

logger = logging.getLogger(__name__)

TERMINAL_CANCELLED: set[str] = {"Cancelled", "Inactive", "ApiCancelled"}


@dataclass
class ReconcileResult:
    pending_count: int = 0
    opened: int = 0                # SUBMITTED -> OPEN or PARTIALLY_FILLED
    closed: int = 0                # PENDING_CLOSE -> CLOSED or PARTIALLY_FILLED
    cancelled: int = 0             # buy never filled, or sell cancelled (reverted to OPEN)
    skipped_still_working: int = 0 # no fills yet, status still working
    orphan_fills_inserted: int = 0 # fills with no DB row -- back-filled straight to OPEN (Bug #5)
    orphan_sells_unmatched: int = 0 # SELL fills with no matching OPEN trade in DB (warn-only)
    snapshot_rows: int = 0
    errors: list[str] = field(default_factory=list)


def _strip_tz(dt: datetime | None) -> datetime | None:
    """ib_async fill times are tz-aware (+00:00). DB columns are naive."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _aggregate_fills(fills: list) -> tuple[float, float, datetime | None, float]:
    """Sum shares, weighted-avg price, latest fill time, total realized PnL."""
    total_shares = sum(f.execution.shares for f in fills)
    if total_shares <= 0:
        return 0.0, 0.0, None, 0.0
    weighted_price = (
        sum(f.execution.price * f.execution.shares for f in fills) / total_shares
    )
    latest_time = max(f.time for f in fills)
    realized_pnl = sum(
        (f.commissionReport.realizedPNL or 0.0) if f.commissionReport else 0.0
        for f in fills
    )
    return total_shares, weighted_price, _strip_tz(latest_time), realized_pnl


async def run_reconcile(
    *,
    broker: IBBroker,
    trade_repo: TradeRepository,
    snap_repo: PortfolioSnapshotRepository,
    daily_repo: DailyPnLRepository,
    today: date | None = None,
) -> ReconcileResult:
    """Execute one reconcile cycle. Caller manages broker connection."""
    result = ReconcileResult()
    today = today or date.today()

    account = await broker.get_account_summary()
    positions = await broker.get_positions()
    ib_trades = list(broker.ib.trades())
    ib_fills = list(broker.ib.fills())

    # Index by permId (cross-process stable). orderId is unreliable (resets).
    trades_by_perm: dict[int, object] = {
        t.order.permId: t for t in ib_trades if t.order.permId
    }
    fills_by_perm: dict[int, list] = defaultdict(list)
    for f in ib_fills:
        # permId=0 is an IB quirk (legacy/manual orders). Grouping them would
        # collapse distinct orders into one bucket -- skip with a warning.
        if not f.execution.permId:
            logger.warning(
                "fill with permId=0 ignored: %s %s shares=%s",
                f.contract.symbol, f.execution.side, f.execution.shares,
            )
            continue
        fills_by_perm[f.execution.permId].append(f)

    logger.info(
        "reconcile start: account net_liq=$%.2f positions=%d "
        "ib_trades=%d ib_fills=%d",
        account.net_liquidation, len(positions), len(ib_trades), len(ib_fills),
    )

    pending = trade_repo.get_pending_orders()
    result.pending_count = len(pending)
    logger.info("found %d pending trade(s) in DB", len(pending))

    held_symbols = {p.symbol for p in positions if p.quantity > 0}

    # Track permIds processed via the DB-pending path so we don't double-handle
    # them as orphans below.
    processed_perm_ids: set[int] = set()
    for trade in pending:
        try:
            if trade.status == "SUBMITTED":
                if trade.perm_id:
                    processed_perm_ids.add(trade.perm_id)
                _reconcile_buy(
                    trade, trades_by_perm, fills_by_perm, trade_repo, result,
                    today=today,
                )
            elif trade.status == "PENDING_CLOSE":
                if trade.exit_perm_id:
                    processed_perm_ids.add(trade.exit_perm_id)
                _reconcile_sell(
                    trade, trades_by_perm, fills_by_perm, trade_repo, result,
                    today=today, held_symbols=held_symbols,
                )
            else:
                msg = f"trade_id={trade.id} unexpected status={trade.status}"
                result.errors.append(msg)
                logger.warning(msg)
        except Exception as exc:  # noqa: BLE001
            err = f"trade_id={trade.id}: {exc!r}"
            logger.exception(err)
            result.errors.append(err)

    # ---------------------------------------------------- orphan fills (Bug #5)
    # Late-fill recovery: any permId present in `ib.fills()` but with no matching
    # DB row gets back-filled straight to OPEN. Happens when a market order placed
    # at 16:00 fills *after* the 16:25 record run, so record never saw it.
    #
    # Idempotent: if the orphan was inserted on a previous reconcile run, the
    # `find_by_perm_id` check below short-circuits and the row is left alone.
    for perm_id, fills in fills_by_perm.items():
        if perm_id in processed_perm_ids:
            continue  # already handled via DB-pending path above
        if trade_repo.find_by_perm_id(perm_id) is not None:
            continue  # previously back-filled (idempotent re-run)
        if trade_repo.find_by_exit_perm_id(perm_id) is not None:
            continue  # already tracked as a SELL exit_perm_id
        try:
            side = fills[0].execution.side
            if side != "BOT":
                # SELL orphans (no matching OPEN in DB) would need a buy row
                # to attach to. For now we count + warn, no insert.
                result.orphan_sells_unmatched += 1
                logger.warning(
                    "ORPHAN SELL perm_id=%d %s -- no matching OPEN trade in DB",
                    perm_id, fills[0].contract.symbol,
                )
                continue
            total_shares, avg_px, latest_time, _realized = _aggregate_fills(fills)
            if total_shares <= 0:
                continue
            symbol = fills[0].contract.symbol
            ib_order_id = fills[0].execution.orderId
            strategy_name = (
                getattr(fills[0].execution, "orderRef", "") or "donchian"
            )
            trade_repo.create_filled_buy_from_fill(
                symbol=symbol,
                strategy_name=strategy_name,
                filled_quantity=int(total_shares),
                entry_price=avg_px,
                ib_order_id=ib_order_id,
                submitted_at=latest_time or datetime.now(),
                entry_time=latest_time or datetime.now(),
                perm_id=perm_id,
            )
            result.orphan_fills_inserted += 1
            logger.info(
                "ORPHAN BUY %s perm_id=%d shares=%d avg=$%.2f -> OPEN (back-filled)",
                symbol, perm_id, int(total_shares), avg_px,
            )
        except Exception as exc:  # noqa: BLE001
            err = f"orphan perm_id={perm_id}: {exc!r}"
            logger.exception(err)
            result.errors.append(err)

    # ---------------------------------------------------- portfolio snapshot
    snap_rows = snap_repo.save_snapshot(
        today,
        [
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "avg_cost": p.avg_cost,
                "market_price": p.market_price,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in positions
        ],
    )
    result.snapshot_rows = len(snap_rows)

    # ---------------------------------------------------- daily P&L
    # Orphan back-fills count as openings for the day -- they're real positions
    # that we just learned about late.
    daily_repo.upsert_daily(
        today=today,
        realized_pnl=account.realized_pnl,
        unrealized_pnl=account.unrealized_pnl,
        trades_opened=result.opened + result.orphan_fills_inserted,
        trades_closed=result.closed,
        account_value=account.net_liquidation,
        total_cash=account.total_cash,
        # Longs only -- must match submit's cap definition (quantity > 0).
        # len(positions) also counted shorts/zero rows and read 61 on a
        # 50-cap day (PROJECT_MASTER_STATE.md §7 anomaly).
        open_positions_count=len([p for p in positions if p.quantity > 0]),
    )

    logger.info(
        "reconcile done: opened=%d closed=%d cancelled=%d skipped=%d "
        "orphan_fills=%d orphan_sells=%d snapshot_rows=%d",
        result.opened, result.closed, result.cancelled,
        result.skipped_still_working,
        result.orphan_fills_inserted, result.orphan_sells_unmatched,
        result.snapshot_rows,
    )
    return result


def _is_stale(submitted: datetime | None, today: date) -> bool:
    """A pending row from a previous day. IB market orders are DAY orders --
    they cannot fill on a later day, so a stale row with no visible fills is
    resolvable: the order expired at that day's close."""
    return submitted is not None and submitted.date() < today


def _reconcile_buy(
    trade,
    trades_by_perm: dict,
    fills_by_perm: dict,
    repo: TradeRepository,
    result: ReconcileResult,
    *,
    today: date,
) -> None:
    """SUBMITTED -> OPEN / PARTIALLY_FILLED / CANCELLED."""
    perm_id = trade.perm_id
    fills = fills_by_perm.get(perm_id, []) if perm_id else []
    ib_trade = trades_by_perm.get(perm_id) if perm_id else None
    order_status = (
        ib_trade.orderStatus.status if ib_trade and ib_trade.orderStatus else "?"
    )

    if not fills:
        if order_status in TERMINAL_CANCELLED:
            repo.mark_cancelled(
                trade.id,
                f"BUY perm_id={perm_id} {order_status} with no fills",
            )
            result.cancelled += 1
            logger.info(
                "CANCEL BUY trade_id=%d %s perm_id=%s status=%s",
                trade.id, trade.symbol, perm_id, order_status,
            )
        elif _is_stale(trade.submitted_at, today):
            # Day order from a previous day, no fills visible, IB doesn't
            # know it anymore -- it expired unfilled at that day's close.
            repo.mark_cancelled(
                trade.id,
                f"BUY perm_id={perm_id} stale day order (submitted "
                f"{trade.submitted_at:%Y-%m-%d}), expired unfilled",
            )
            result.cancelled += 1
            logger.warning(
                "STALE BUY trade_id=%d %s perm_id=%s submitted=%s -> CANCELLED",
                trade.id, trade.symbol, perm_id, trade.submitted_at,
            )
        else:
            result.skipped_still_working += 1
            logger.info(
                "skip BUY trade_id=%d %s perm_id=%s status=%s (still working)",
                trade.id, trade.symbol, perm_id, order_status,
            )
        return

    total_shares, avg_px, latest_time, _realized = _aggregate_fills(fills)
    filled_qty = int(total_shares)
    new_status = (
        "OPEN" if filled_qty == trade.requested_quantity else "PARTIALLY_FILLED"
    )
    repo.confirm_buy_fill(
        trade_id=trade.id,
        entry_price=avg_px,
        filled_quantity=filled_qty,
        entry_time=latest_time or datetime.now(),
        status=new_status,
    )
    result.opened += 1
    logger.info(
        "FILL BUY trade_id=%d %s req=%d filled=%d avg=$%.2f -> %s",
        trade.id, trade.symbol, trade.requested_quantity, filled_qty, avg_px, new_status,
    )


def _reconcile_sell(
    trade,
    trades_by_perm: dict,
    fills_by_perm: dict,
    repo: TradeRepository,
    result: ReconcileResult,
    *,
    today: date,
    held_symbols: set[str],
) -> None:
    """PENDING_CLOSE -> CLOSED / PARTIALLY_FILLED / (CANCELLED reverts to OPEN)."""
    perm_id = trade.exit_perm_id
    fills = fills_by_perm.get(perm_id, []) if perm_id else []
    ib_trade = trades_by_perm.get(perm_id) if perm_id else None
    order_status = (
        ib_trade.orderStatus.status if ib_trade and ib_trade.orderStatus else "?"
    )

    if not fills:
        stale = _is_stale(trade.exit_submitted_at, today)
        if order_status in TERMINAL_CANCELLED or (
            stale and trade.symbol in held_symbols
        ):
            # Cancelled by IB, or a stale day order whose position we still
            # hold (= it expired unfilled). Either way: revert the row to
            # OPEN, position still held.
            repo.confirm_close_fill(
                trade_id=trade.id,
                exit_price=0.0,
                filled_quantity=0,
                exit_time=datetime.now(),
                pnl=0.0,
                pnl_pct=0.0,
                status="CANCELLED",
            )
            result.cancelled += 1
            logger.info(
                "CANCEL SELL trade_id=%d %s perm_id=%s status=%s%s -> reverted OPEN",
                trade.id, trade.symbol, perm_id, order_status,
                " (stale, position still held)" if stale else "",
            )
        elif stale:
            # Stale SELL, no fills visible, and we no longer hold the symbol:
            # it DID sell on a previous day but the fill is gone from
            # ib.fills() (e.g. reconcile missed that day). We can't invent an
            # exit price -- flag for manual resolution via the override CLI.
            msg = (
                f"trade_id={trade.id} {trade.symbol}: stale PENDING_CLOSE "
                f"(submitted {trade.exit_submitted_at:%Y-%m-%d}), position no "
                f"longer held -- exit fill unrecoverable from ib.fills(); "
                f"resolve manually"
            )
            result.errors.append(msg)
            logger.error(msg)
        else:
            result.skipped_still_working += 1
            logger.info(
                "skip SELL trade_id=%d %s perm_id=%s status=%s (still working)",
                trade.id, trade.symbol, perm_id, order_status,
            )
        return

    total_shares, avg_px, latest_time, realized_pnl = _aggregate_fills(fills)
    filled_qty = int(total_shares)
    basis = (trade.entry_price or 0.0) * (trade.filled_quantity or 0)
    pnl_pct = (realized_pnl / basis * 100.0) if basis > 0 else 0.0
    expected = trade.filled_quantity or trade.requested_quantity
    new_status = "CLOSED" if filled_qty == expected else "PARTIALLY_FILLED"
    repo.confirm_close_fill(
        trade_id=trade.id,
        exit_price=avg_px,
        filled_quantity=filled_qty,
        exit_time=latest_time or datetime.now(),
        pnl=realized_pnl,
        pnl_pct=pnl_pct,
        status=new_status,
    )
    result.closed += 1
    logger.info(
        "FILL SELL trade_id=%d %s filled=%d avg=$%.2f pnl=$%+.2f (%+.2f%%) -> %s",
        trade.id, trade.symbol, filled_qty, avg_px, realized_pnl, pnl_pct, new_status,
    )
