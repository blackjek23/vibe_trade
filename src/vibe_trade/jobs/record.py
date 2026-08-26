"""V2 record job — runs at 16:25 Asia/Jerusalem, persists today's submissions to DB.

Flow:
1. Connect with `client_id = RECORD_CLIENT_ID` (2)
2. Read `ib.fills()` (NOT `ib.trades()` -- order fields reset to 0 across
   processes; fills are intact). Verified live 2026-04-27.
3. Group fills by `execution.permId` (the cross-process dedup target)
4. For each permId-group:
   - BUY side ("BOT"): dedup on `Trade.perm_id`; if not present, insert as
     SUBMITTED via `create_submitted_buy(perm_id=...)`
   - SELL side ("SLD"): dedup on `Trade.exit_perm_id`; if not present, find
     a matching OPEN trade by symbol and call `mark_pending_close(exit_perm_id=...)`

Invariants:
- `strategy_name` is read from the fill's `execution.orderRef` (Session L
  multi-strategy attribution). submit tags each BUY with its strategy id; record
  stamps it onto the trade row. Falls back to the `strategy_name` arg (default
  "donchian") when the orderRef is empty (legacy/pre-L fills). The "trim" tag is
  a SELL ref and never reaches the BUY-insert path.
- `requested_quantity` prefers IB's live `totalQuantity` for a BUY still
  working at record time (`get_open_orders`, cross-client/cross-process via
  `reqAllOpenOrdersAsync`); it falls back to total filled shares only when the
  order has already fully resolved (filled or cancelled) by record time, where
  fills-so-far and the original ask agree anyway. Before this (H-2,
  PROJECT_EVALUATION.md) a still-partially-filled order recorded whatever had
  filled *as* the request, so reconcile's later `filled == requested` check
  came out backwards in both directions.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.db.repository import TradeRepository

logger = logging.getLogger(__name__)


@dataclass
class RecordResult:
    fills_seen: int = 0           # individual ib.fills() entries
    perm_ids_seen: int = 0        # unique permIds (= unique orders)
    buys_inserted: int = 0
    buys_skipped_dup: int = 0
    sells_flipped: int = 0
    sells_skipped_dup: int = 0
    sells_skipped_no_open: int = 0
    errors: list[str] = field(default_factory=list)


async def run_record(
    *,
    broker: IBBroker,
    repo: TradeRepository,
    strategy_name: str = "donchian",
    now: datetime | None = None,
) -> RecordResult:
    """Execute one record cycle. Caller manages broker connection.

    `strategy_name` is the fallback strategy id used only when a BUY fill has an
    empty `execution.orderRef` (legacy/pre-Session-L fills). Normally the strategy
    is read per-fill from the orderRef submit stamped on the order.

    `now` is the timestamp written to `submitted_at` / `exit_submitted_at`
    rows. Defaults to `datetime.now()` -- pass an explicit value in tests
    for deterministic comparisons.
    """
    result = RecordResult()
    timestamp = now or datetime.now()

    # A still-working order's IB-reported totalQuantity (via `get_open_orders`,
    # which calls reqAllOpenOrdersAsync -- cross-client, cross-process) is the
    # true original ask. Fills-so-far is not: at 16:25/16:35 a BUY that hasn't
    # fully filled yet is indistinguishable from one whose original size was
    # smaller, which inverted partial-fill detection in both directions (H-2,
    # PROJECT_EVALUATION.md). Only orders still open at record time land here;
    # an order that has already fully resolved (filled or cancelled) by then
    # has no "requested" value IB will hand back except the fills themselves,
    # which is correct in that case anyway.
    requested_qty_by_perm: dict[int, int] = {
        oo.perm_id: oo.quantity for oo in await broker.get_open_orders()
    }

    fills = list(broker.ib.fills())
    result.fills_seen = len(fills)

    # Group by permId. Each group represents one logical order; multiple
    # fills appear for partial-fill scenarios (sum the shares).
    by_perm: dict[int, list] = defaultdict(list)
    for f in fills:
        # permId=0 is an IB quirk (legacy/manual orders). Grouping them would
        # collapse distinct orders into one bucket -- skip with a warning.
        if not f.execution.permId:
            logger.warning(
                "fill with permId=0 ignored: %s %s shares=%s",
                f.contract.symbol, f.execution.side, f.execution.shares,
            )
            continue
        by_perm[f.execution.permId].append(f)
    result.perm_ids_seen = len(by_perm)

    logger.info(
        "record start: %d fills across %d permIds, strategy=%r",
        result.fills_seen, result.perm_ids_seen, strategy_name,
    )

    for perm_id, group in by_perm.items():
        try:
            # All fills in a group share contract + side -- pick from group[0].
            symbol = group[0].contract.symbol
            ib_side = group[0].execution.side  # "BOT" or "SLD"
            order_id = group[0].execution.orderId
            total_shares = int(sum(f.execution.shares for f in group))

            if ib_side == "BOT":
                if repo.find_by_perm_id(perm_id) is not None:
                    result.buys_skipped_dup += 1
                    logger.debug(
                        "skip BUY %s perm_id=%d already in DB", symbol, perm_id,
                    )
                    continue
                # Attribute the trade to the strategy that placed it (orderRef),
                # falling back to the default for empty/legacy refs.
                order_ref = (getattr(group[0].execution, "orderRef", "") or "").strip()
                trade_strategy = order_ref or strategy_name
                # Prefer IB's live totalQuantity for an order still working at
                # record time (H-2) -- fall back to fills-so-far only for an
                # order that has already fully resolved, where they agree anyway.
                requested_qty = requested_qty_by_perm.get(perm_id, total_shares)
                trade = repo.create_submitted_buy(
                    symbol=symbol,
                    strategy_name=trade_strategy,
                    requested_quantity=requested_qty,
                    ib_order_id=order_id,
                    submitted_at=timestamp,
                    perm_id=perm_id,
                )
                result.buys_inserted += 1
                logger.info(
                    "BUY %s perm_id=%d shares=%d/%d strategy=%s -> trade_id=%d SUBMITTED",
                    symbol, perm_id, total_shares, requested_qty, trade_strategy, trade.id,
                )

            elif ib_side == "SLD":
                if repo.find_by_exit_perm_id(perm_id) is not None:
                    result.sells_skipped_dup += 1
                    logger.debug(
                        "skip SELL %s perm_id=%d already tracked", symbol, perm_id,
                    )
                    continue

                # FIFO-ordered (entry_time asc, id asc) -- matches how IB
                # reports realized P&L on a partial unwind, and gives a
                # deterministic target when a symbol wrongly has more than one
                # OPEN row (C-4: a plain get_open_trades() filter has no
                # ORDER BY and cannot make this guarantee).
                open_trade = repo.find_open_by_symbol(symbol)
                if open_trade is None:
                    result.sells_skipped_no_open += 1
                    logger.warning(
                        "SELL %s perm_id=%d -- no OPEN trade in DB to flip",
                        symbol, perm_id,
                    )
                    continue

                repo.mark_pending_close(
                    trade_id=open_trade.id,
                    exit_ib_order_id=order_id,
                    exit_submitted_at=timestamp,
                    exit_perm_id=perm_id,
                )
                result.sells_flipped += 1
                logger.info(
                    "SELL %s perm_id=%d -> trade_id=%d OPEN -> PENDING_CLOSE",
                    symbol, perm_id, open_trade.id,
                )

            else:
                msg = f"unknown fill side={ib_side!r} for {symbol} perm_id={perm_id}"
                result.errors.append(msg)
                logger.warning(msg)

        except Exception as exc:  # noqa: BLE001 -- never abort on one bad order
            err = f"perm_id={perm_id}: {exc!r}"
            logger.exception(err)
            result.errors.append(err)

    return result
