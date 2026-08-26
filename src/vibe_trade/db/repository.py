"""Data access layer for trades, signals, portfolio snapshots, and P&L.

V2 flow (see docs/ARCHITECTURE_V2.md):
- Submit (16:00): no DB writes.
- Record (16:25): `create_submitted_buy` / `mark_pending_close`.
- Reconcile (23:30): `confirm_buy_fill` / `confirm_close_fill` / `mark_cancelled`,
  then `DailyPnLRepository.upsert_daily` + `PortfolioSnapshotRepository.save_snapshot`.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy.orm import Session

from vibe_trade.db.models import (
    DailyPnL,
    PortfolioSnapshot,
    ScanLog,
    Signal,
    Trade,
)


class TradeRepository:
    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------ writes

    def create_submitted_buy(
        self,
        symbol: str,
        strategy_name: str,
        requested_quantity: int,
        ib_order_id: int,
        submitted_at: datetime,
        perm_id: int | None = None,
    ) -> Trade:
        """Called by `record` when a BUY order submitted at 16:00 is seen in IB's today list.

        `perm_id` is IB's persistent order ID. It survives client reconnects and
        is the cross-process dedup target. `ib_order_id` is the session-scoped
        orderId — informational only, may be 0 when read from a separate process.
        """
        trade = Trade(
            symbol=symbol,
            side="BUY",
            strategy_name=strategy_name,
            requested_quantity=requested_quantity,
            ib_order_id=ib_order_id,
            perm_id=perm_id,
            submitted_at=submitted_at,
            status="SUBMITTED",
        )
        self.session.add(trade)
        self.session.commit()
        return trade

    def create_filled_buy_from_fill(
        self,
        symbol: str,
        strategy_name: str,
        filled_quantity: int,
        entry_price: float,
        ib_order_id: int,
        submitted_at: datetime,
        entry_time: datetime,
        perm_id: int,
    ) -> Trade:
        """Insert a BUY that filled before `record` could observe it.

        Used by `reconcile` when an `ib.fills()` permId has no matching DB row,
        which happens when a market order placed at 16:00 fills *after* the
        16:25 record run (the documented "late-fill" edge case — Bug #5).

        Status goes straight to ``OPEN`` (no intermediate SUBMITTED row).
        `requested_quantity` is set equal to `filled_quantity` because we never
        observed the original order size — the fill is our source of truth.

        Idempotency: the `perm_id` unique index guarantees one row per IB order;
        callers should `find_by_perm_id` first to avoid a constraint violation.
        """
        trade = Trade(
            symbol=symbol,
            side="BUY",
            strategy_name=strategy_name,
            requested_quantity=filled_quantity,
            filled_quantity=filled_quantity,
            ib_order_id=ib_order_id,
            perm_id=perm_id,
            submitted_at=submitted_at,
            entry_time=entry_time,
            entry_price=entry_price,
            status="OPEN",
        )
        self.session.add(trade)
        self.session.commit()
        return trade

    def mark_pending_close(
        self,
        trade_id: int,
        exit_ib_order_id: int,
        exit_submitted_at: datetime,
        exit_perm_id: int | None = None,
    ) -> Trade:
        """Called by `record` when a SELL order closes an existing OPEN position.

        Flips OPEN → PENDING_CLOSE and records the SELL order id + time.
        """
        trade = self.session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"Trade {trade_id} not found")
        if trade.status != "OPEN":
            raise ValueError(
                f"Trade {trade_id} cannot go PENDING_CLOSE from status={trade.status}"
            )
        trade.exit_ib_order_id = exit_ib_order_id
        trade.exit_perm_id = exit_perm_id
        trade.exit_submitted_at = exit_submitted_at
        trade.status = "PENDING_CLOSE"
        self.session.commit()
        return trade

    def confirm_buy_fill(
        self,
        trade_id: int,
        entry_price: float,
        filled_quantity: int,
        entry_time: datetime,
        status: str,
    ) -> Trade:
        """Called by `reconcile` to finalize a SUBMITTED BUY.

        `status` must be one of: OPEN (fully filled), PARTIALLY_FILLED, CANCELLED.
        """
        if status not in {"OPEN", "PARTIALLY_FILLED", "CANCELLED"}:
            raise ValueError(f"Invalid buy-fill status: {status}")
        trade = self.session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"Trade {trade_id} not found")
        if trade.status != "SUBMITTED":
            raise ValueError(
                f"Trade {trade_id} cannot confirm BUY fill from status={trade.status}"
            )
        trade.entry_price = entry_price
        trade.filled_quantity = filled_quantity
        trade.entry_time = entry_time
        trade.status = status
        self.session.commit()
        return trade

    def confirm_close_fill(
        self,
        trade_id: int,
        exit_price: float,
        filled_quantity: int,
        exit_time: datetime,
        pnl: float,
        pnl_pct: float,
        status: str,
    ) -> Trade:
        """Called by `reconcile` to finalize a PENDING_CLOSE SELL.

        `pnl` and `pnl_pct` are read from IB's fill report — we do not compute them here.
        `status` must be one of: CLOSED (fully filled), PARTIALLY_FILLED, CANCELLED.
        A CANCELLED close reverts the trade to OPEN (position still held).

        `filled_quantity` is the exit leg's shares and is stored in
        `exit_filled_quantity`, not `filled_quantity` -- the entry-leg column
        set by `confirm_buy_fill` is never touched here (H-3, was clobbered by
        a partial exit before PROJECT_EVALUATION.md's fix).
        """
        if status not in {"CLOSED", "PARTIALLY_FILLED", "CANCELLED"}:
            raise ValueError(f"Invalid close-fill status: {status}")
        trade = self.session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"Trade {trade_id} not found")
        if trade.status != "PENDING_CLOSE":
            raise ValueError(
                f"Trade {trade_id} cannot confirm close from status={trade.status}"
            )
        if status == "CANCELLED":
            # SELL never filled — position is still ours.
            trade.status = "OPEN"
            trade.exit_ib_order_id = None
            trade.exit_perm_id = None
            trade.exit_submitted_at = None
        else:
            trade.exit_price = exit_price
            # Exit leg's own column (H-3) -- entry filled_quantity is never
            # touched again after confirm_buy_fill, partial exit or not.
            trade.exit_filled_quantity = filled_quantity
            trade.exit_time = exit_time
            trade.pnl = pnl
            trade.pnl_pct = pnl_pct
            trade.status = status
        self.session.commit()
        return trade

    def mark_cancelled(self, trade_id: int, reason: str) -> Trade:
        """Force a trade to CANCELLED (e.g. BUY never filled). Records reason in notes."""
        trade = self.session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"Trade {trade_id} not found")
        trade.status = "CANCELLED"
        trade.notes = reason
        self.session.commit()
        return trade

    def close_from_open(
        self,
        trade_id: int,
        *,
        exit_price: float,
        filled_quantity: int,
        exit_time: datetime,
        exit_perm_id: int,
        exit_ib_order_id: int | None = None,
        ib_realized_pnl: float | None = None,
        reason: str = "",
    ) -> Trade:
        """Close an ``OPEN`` trade directly from fill data, skipping PENDING_CLOSE.

        Needed when a SELL filled at IB but no ``record`` run ever flipped the row
        to PENDING_CLOSE (a missed 16:25 job). ``confirm_close_fill`` refuses that
        transition by design, so this is the recovery door — used only by
        reconcile's orphan-SELL matching.

        ``filled_quantity`` here is the exit leg's shares and is stored in
        ``exit_filled_quantity``, not ``filled_quantity`` -- the entry-leg
        column is never touched (H-3).

        **P&L basis:** computed from this row's own ``entry_price`` and
        ``exit_price``, so the stored ``pnl`` always reconciles against the prices
        in the same row. IB's ``realizedPNL`` (account-level average-cost basis) is
        recorded in ``notes`` for traceability rather than in ``pnl`` — mixing the
        two bases is what made `pnl` contradict its own prices on re-bought
        symbols. See PROJECT_MASTER_STATE for the accounting-policy note.
        """
        trade = self.session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"Trade {trade_id} not found")
        if trade.status != "OPEN":
            raise ValueError(
                f"Trade {trade_id} cannot close from status={trade.status}"
            )

        expected = trade.filled_quantity or trade.requested_quantity or 0
        entry_price = trade.entry_price or 0.0
        basis = entry_price * filled_quantity
        pnl = (exit_price - entry_price) * filled_quantity
        pnl_pct = (pnl / basis * 100.0) if basis > 0 else 0.0

        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.exit_perm_id = exit_perm_id
        trade.exit_ib_order_id = exit_ib_order_id
        # Exit leg's own column (H-3) -- entry filled_quantity above (`expected`)
        # is never touched again after confirm_buy_fill, partial exit or not.
        trade.exit_filled_quantity = filled_quantity
        trade.pnl = pnl
        trade.pnl_pct = pnl_pct
        trade.status = "CLOSED" if filled_quantity == expected else "PARTIALLY_FILLED"

        note = reason or "recovered orphan SELL fill"
        if ib_realized_pnl is not None:
            note += f" (IB realizedPNL={ib_realized_pnl:+.2f})"
        trade.notes = note

        self.session.commit()
        return trade

    def mark_needs_review(self, trade_id: int, reason: str) -> Trade:
        """Park a row that reconcile cannot resolve without human input.

        Used for an ``OPEN`` row whose symbol is no longer held at IB and whose
        exit fill is gone from ``ib.fills()`` (only the current session is
        returned, so a missed reconcile day loses it permanently). We refuse to
        invent an exit price — the row is flagged and excluded from the pending
        scan so it stops silently masquerading as a live position.
        """
        trade = self.session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"Trade {trade_id} not found")
        trade.status = "NEEDS_REVIEW"
        trade.notes = reason
        self.session.commit()
        return trade

    def get_needs_review(self) -> list[Trade]:
        """Rows parked by reconcile's drift sweep, awaiting a human decision."""
        return (
            self.session.query(Trade)
            .filter(Trade.status == "NEEDS_REVIEW")
            .order_by(Trade.symbol.asc(), Trade.id.asc())
            .all()
        )

    def resolve_needs_review(
        self,
        trade_id: int,
        *,
        exit_price: float,
        exit_time: datetime,
        note: str = "",
    ) -> Trade:
        """Close a ``NEEDS_REVIEW`` row using a human-supplied exit price.

        P&L is computed from the row's own entry/exit prices — the same basis rule
        as `close_from_open`, so a resolved row reconciles against its own numbers.
        """
        trade = self.session.get(Trade, trade_id)
        if trade is None:
            raise ValueError(f"Trade {trade_id} not found")
        if trade.status != "NEEDS_REVIEW":
            raise ValueError(
                f"Trade {trade_id} is {trade.status}, not NEEDS_REVIEW"
            )
        qty = trade.filled_quantity or 0
        entry_price = trade.entry_price or 0.0
        basis = entry_price * qty
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.pnl = (exit_price - entry_price) * qty
        trade.pnl_pct = (trade.pnl / basis * 100.0) if basis > 0 else 0.0
        trade.status = "CLOSED"
        trade.notes = note or f"manually resolved at {exit_price}"
        self.session.commit()
        return trade

    def find_open_by_symbol(self, symbol: str) -> Trade | None:
        """Oldest ``OPEN`` row for a symbol (FIFO), or None.

        FIFO matches how IB reports realized P&L on a partial unwind, and gives a
        deterministic target when a symbol wrongly has more than one OPEN row.
        """
        return (
            self.session.query(Trade)
            .filter(Trade.status == "OPEN", Trade.symbol == symbol)
            .order_by(Trade.entry_time.asc(), Trade.id.asc())
            .first()
        )

    # ------------------------------------------------------------------- reads

    def get_open_trades(self) -> list[Trade]:
        return self.session.query(Trade).filter(Trade.status == "OPEN").all()

    def get_pending_orders(self) -> list[Trade]:
        """All trades needing reconciliation: every SUBMITTED BUY and
        PENDING_CLOSE SELL, regardless of submit date.

        Deliberately NOT date-filtered: a row left pending by a missed or
        crashed reconcile run must stay visible to later runs, otherwise it
        is stuck forever (SELL-side twin of Bug #5 — see SESSION_H_FINDINGS).
        Stale rows are resolved by reconcile's day-order expiry logic.
        """
        return (
            self.session.query(Trade)
            .filter(Trade.status.in_(("SUBMITTED", "PENDING_CLOSE")))
            .all()
        )

    def get_trades_opened_today(self, today: date) -> list[Trade]:
        """Trades whose BUY filled today (status OPEN or PARTIALLY_FILLED, entry_time on `today`).

        Used by reconcile's Telegram summary. Read-only.
        """
        start = datetime.combine(today, datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
        return (
            self.session.query(Trade)
            .filter(
                Trade.status.in_(("OPEN", "PARTIALLY_FILLED")),
                Trade.entry_time >= start,
                Trade.entry_time <= end,
            )
            .all()
        )

    def get_trades_closed_today(self, today: date) -> list[Trade]:
        """Trades whose SELL filled today (status CLOSED, exit_time on `today`).

        Used by reconcile's Telegram summary. Read-only.
        """
        start = datetime.combine(today, datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
        return (
            self.session.query(Trade)
            .filter(
                Trade.status == "CLOSED",
                Trade.exit_time >= start,
                Trade.exit_time <= end,
            )
            .all()
        )

    def find_by_perm_id(self, perm_id: int) -> Trade | None:
        """Cross-process dedup target: BUY-side IB persistent ID."""
        return (
            self.session.query(Trade)
            .filter(Trade.perm_id == perm_id)
            .first()
        )

    def find_by_exit_perm_id(self, exit_perm_id: int) -> Trade | None:
        """Cross-process dedup target: SELL-side IB persistent ID."""
        return (
            self.session.query(Trade)
            .filter(Trade.exit_perm_id == exit_perm_id)
            .first()
        )

    def get_recent_trades(self, limit: int = 20) -> list[Trade]:
        return (
            self.session.query(Trade)
            .order_by(Trade.created_at.desc())
            .limit(limit)
            .all()
        )


class SignalRepository:
    def __init__(self, session: Session):
        self.session = session

    def record_signal(
        self,
        symbol: str,
        strategy_name: str,
        signal_type: str,
        scan_id: str,
        confidence: float | None = None,
        metadata: dict | None = None,
    ) -> Signal:
        signal = Signal(
            symbol=symbol,
            strategy_name=strategy_name,
            signal_type=signal_type,
            confidence=confidence,
            metadata_json=json.dumps(metadata) if metadata else None,
            scan_id=scan_id,
        )
        self.session.add(signal)
        self.session.commit()
        return signal

    def mark_executed(self, signal_id: int, approved: bool, reason: str = "") -> None:
        signal = self.session.get(Signal, signal_id)
        if signal:
            signal.risk_approved = approved
            signal.risk_reason = reason
            signal.executed = approved
            self.session.commit()


class DailyPnLRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert_daily(
        self,
        today: date,
        realized_pnl: float,
        unrealized_pnl: float,
        trades_opened: int,
        trades_closed: int,
        account_value: float,
        total_cash: float | None = None,
        open_positions_count: int | None = None,
    ) -> DailyPnL:
        record = self.session.query(DailyPnL).filter(DailyPnL.date == today).first()
        if record is None:
            record = DailyPnL(date=today)
            self.session.add(record)
        record.realized_pnl = realized_pnl
        record.unrealized_pnl = unrealized_pnl
        record.trades_opened = trades_opened
        record.trades_closed = trades_closed
        record.account_value = account_value
        record.total_cash = total_cash
        record.open_positions_count = open_positions_count
        self.session.commit()
        return record

    def get_by_date(self, target_date: date) -> DailyPnL | None:
        """Read-only lookup of the DailyPnL row for `target_date`. Used by reconcile's
        Telegram summary."""
        return (
            self.session.query(DailyPnL)
            .filter(DailyPnL.date == target_date)
            .first()
        )


class PortfolioSnapshotRepository:
    """One row per held position per day. Written by `reconcile` at 23:30."""

    def __init__(self, session: Session):
        self.session = session

    def save_snapshot(self, snapshot_date: date, rows: list[dict]) -> list[PortfolioSnapshot]:
        """Idempotent: deletes any existing rows for `snapshot_date`, then inserts `rows`.

        Each row dict has keys: symbol, quantity, avg_cost, market_price,
        market_value, unrealized_pnl. Missing optional keys default to None.
        """
        self.session.query(PortfolioSnapshot).filter(
            PortfolioSnapshot.date == snapshot_date
        ).delete()
        saved: list[PortfolioSnapshot] = []
        for row in rows:
            snap = PortfolioSnapshot(
                date=snapshot_date,
                symbol=row["symbol"],
                quantity=row["quantity"],
                avg_cost=row.get("avg_cost"),
                market_price=row.get("market_price"),
                market_value=row.get("market_value"),
                unrealized_pnl=row.get("unrealized_pnl"),
            )
            self.session.add(snap)
            saved.append(snap)
        self.session.commit()
        return saved

    def get_snapshot(self, snapshot_date: date) -> list[PortfolioSnapshot]:
        return (
            self.session.query(PortfolioSnapshot)
            .filter(PortfolioSnapshot.date == snapshot_date)
            .order_by(PortfolioSnapshot.symbol)
            .all()
        )


class ScanLogRepository:
    def __init__(self, session: Session):
        self.session = session

    def start_scan(self, scan_id: str) -> ScanLog:
        log = ScanLog(scan_id=scan_id, started_at=datetime.now(), status="STARTED")
        self.session.add(log)
        self.session.commit()
        return log

    def complete_scan(
        self,
        scan_id: str,
        symbols_scanned: int,
        signals_generated: int,
        orders_placed: int,
        errors: list[str] | None = None,
    ) -> None:
        log = self.session.query(ScanLog).filter(ScanLog.scan_id == scan_id).first()
        if log:
            log.completed_at = datetime.now()
            log.symbols_scanned = symbols_scanned
            log.signals_generated = signals_generated
            log.orders_placed = orders_placed
            log.errors = json.dumps(errors) if errors else None
            log.status = "FAILED" if errors else "SUCCESS"
            self.session.commit()
