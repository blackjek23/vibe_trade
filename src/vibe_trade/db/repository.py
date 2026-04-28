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
            trade.filled_quantity = filled_quantity
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

    # ------------------------------------------------------------------- reads

    def get_open_trades(self) -> list[Trade]:
        return self.session.query(Trade).filter(Trade.status == "OPEN").all()

    def get_pending_orders_for_today(self, today: date) -> list[Trade]:
        """All trades needing reconciliation today: SUBMITTED BUYs or PENDING_CLOSE SELLs
        whose order was submitted today.
        """
        start = datetime.combine(today, datetime.min.time())
        end = datetime.combine(today, datetime.max.time())
        return (
            self.session.query(Trade)
            .filter(
                (
                    (Trade.status == "SUBMITTED")
                    & (Trade.submitted_at >= start)
                    & (Trade.submitted_at <= end)
                )
                | (
                    (Trade.status == "PENDING_CLOSE")
                    & (Trade.exit_submitted_at >= start)
                    & (Trade.exit_submitted_at <= end)
                )
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
