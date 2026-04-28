"""Tests for the V2 record job (`vibe_trade.jobs.record.run_record`).

Mocks `broker.ib.fills()` with SimpleNamespace shapes matching ib_async's Fill.
Uses real DB session via the shared `db_session` fixture.

Cross-process invariant under test: record dedups by `permId`, NOT `orderId`,
because orderIds reset to 0 in a fresh process.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from vibe_trade.db.models import Trade
from vibe_trade.db.repository import TradeRepository
from vibe_trade.jobs.record import run_record


def _fill(perm_id: int, order_id: int, symbol: str, side: str, shares: int, price: float = 100.0):
    """Build a Fill-shaped SimpleNamespace."""
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        execution=SimpleNamespace(
            permId=perm_id,
            orderId=order_id,
            shares=float(shares),
            price=price,
            side=side,  # "BOT" or "SLD"
        ),
        commissionReport=SimpleNamespace(realizedPNL=0.0, commission=1.0),
        time=datetime.now(),
    )


class MockBroker:
    """Just enough of the broker surface for run_record: `ib.fills()`."""

    def __init__(self, fills):
        self.ib = SimpleNamespace(fills=lambda: list(fills))


# ============================================================== tests


class TestEmpty:
    async def test_no_fills_no_db_writes(self, db_session: Session):
        broker = MockBroker(fills=[])
        repo = TradeRepository(db_session)
        result = await run_record(broker=broker, repo=repo)
        assert result.fills_seen == 0
        assert result.perm_ids_seen == 0
        assert result.buys_inserted == 0
        assert db_session.query(Trade).count() == 0


class TestBuyFills:
    async def test_buy_inserts_submitted_row(self, db_session: Session):
        broker = MockBroker(
            fills=[_fill(perm_id=507476881, order_id=21, symbol="T", side="BOT", shares=10)]
        )
        repo = TradeRepository(db_session)
        ts = datetime(2026, 4, 28, 16, 25)
        result = await run_record(broker=broker, repo=repo, now=ts)

        assert result.buys_inserted == 1
        assert result.buys_skipped_dup == 0

        rows = db_session.query(Trade).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.symbol == "T"
        assert row.side == "BUY"
        assert row.status == "SUBMITTED"
        assert row.requested_quantity == 10
        assert row.perm_id == 507476881
        assert row.ib_order_id == 21
        assert row.submitted_at == ts
        assert row.strategy_name == "donchian"  # default constant

    async def test_buy_dedup_by_perm_id(self, db_session: Session):
        # Re-run on identical fills: nothing new inserted.
        broker = MockBroker(
            fills=[_fill(perm_id=111, order_id=21, symbol="T", side="BOT", shares=10)]
        )
        repo = TradeRepository(db_session)

        r1 = await run_record(broker=broker, repo=repo)
        assert r1.buys_inserted == 1

        r2 = await run_record(broker=broker, repo=repo)
        assert r2.buys_inserted == 0
        assert r2.buys_skipped_dup == 1
        assert db_session.query(Trade).count() == 1

    async def test_partial_fills_aggregated_by_perm_id(self, db_session: Session):
        # Same permId across two fill events (partial fill scenario):
        # total shares should sum.
        broker = MockBroker(
            fills=[
                _fill(perm_id=111, order_id=21, symbol="T", side="BOT", shares=4),
                _fill(perm_id=111, order_id=21, symbol="T", side="BOT", shares=6),
            ]
        )
        repo = TradeRepository(db_session)
        result = await run_record(broker=broker, repo=repo)

        assert result.fills_seen == 2
        assert result.perm_ids_seen == 1
        assert result.buys_inserted == 1

        row = db_session.query(Trade).first()
        assert row.requested_quantity == 10  # 4 + 6

    async def test_strategy_name_override(self, db_session: Session):
        broker = MockBroker(
            fills=[_fill(perm_id=111, order_id=21, symbol="T", side="BOT", shares=1)]
        )
        repo = TradeRepository(db_session)
        await run_record(broker=broker, repo=repo, strategy_name="ma_crossover")

        row = db_session.query(Trade).first()
        assert row.strategy_name == "ma_crossover"


class TestSellFills:
    def _open_trade(self, db_session: Session, symbol: str = "F", qty: int = 134) -> int:
        """Helper: create an OPEN BUY trade for the given symbol."""
        repo = TradeRepository(db_session)
        submitted = repo.create_submitted_buy(
            symbol=symbol, strategy_name="donchian", requested_quantity=qty,
            ib_order_id=4, submitted_at=datetime(2026, 4, 27, 16, 0), perm_id=900001,
        )
        repo.confirm_buy_fill(
            submitted.id, entry_price=12.43, filled_quantity=qty,
            entry_time=datetime(2026, 4, 27, 16, 30), status="OPEN",
        )
        return submitted.id

    async def test_sell_flips_open_to_pending_close(self, db_session: Session):
        trade_id = self._open_trade(db_session)
        broker = MockBroker(
            fills=[_fill(perm_id=507476882, order_id=23, symbol="F", side="SLD", shares=1)]
        )
        repo = TradeRepository(db_session)
        ts = datetime(2026, 4, 28, 16, 25)
        result = await run_record(broker=broker, repo=repo, now=ts)

        assert result.sells_flipped == 1
        row = db_session.get(Trade, trade_id)
        assert row.status == "PENDING_CLOSE"
        assert row.exit_perm_id == 507476882
        assert row.exit_ib_order_id == 23
        assert row.exit_submitted_at == ts

    async def test_sell_dedup_by_exit_perm_id(self, db_session: Session):
        self._open_trade(db_session)
        broker = MockBroker(
            fills=[_fill(perm_id=222, order_id=23, symbol="F", side="SLD", shares=1)]
        )
        repo = TradeRepository(db_session)

        r1 = await run_record(broker=broker, repo=repo)
        assert r1.sells_flipped == 1

        r2 = await run_record(broker=broker, repo=repo)
        assert r2.sells_flipped == 0
        assert r2.sells_skipped_dup == 1

    async def test_sell_with_no_open_trade_skipped(self, db_session: Session):
        # No OPEN trade for "F" exists; SELL fill can't be matched.
        broker = MockBroker(
            fills=[_fill(perm_id=222, order_id=23, symbol="F", side="SLD", shares=1)]
        )
        repo = TradeRepository(db_session)
        result = await run_record(broker=broker, repo=repo)

        assert result.sells_flipped == 0
        assert result.sells_skipped_no_open == 1
        # Nothing in the DB
        assert db_session.query(Trade).count() == 0


class TestMixedAndErrors:
    async def test_buy_and_sell_in_same_run(self, db_session: Session):
        # Pre-existing OPEN F trade for the SELL fill to flip.
        repo = TradeRepository(db_session)
        f_open = repo.create_submitted_buy(
            symbol="F", strategy_name="donchian", requested_quantity=10,
            ib_order_id=4, submitted_at=datetime(2026, 4, 27, 16, 0), perm_id=900001,
        )
        repo.confirm_buy_fill(f_open.id, 12.43, 10, datetime(2026, 4, 27, 16, 30), "OPEN")

        broker = MockBroker(
            fills=[
                _fill(perm_id=111, order_id=21, symbol="T", side="BOT", shares=65),
                _fill(perm_id=222, order_id=23, symbol="F", side="SLD", shares=1),
            ]
        )
        result = await run_record(broker=broker, repo=repo)
        assert result.buys_inserted == 1
        assert result.sells_flipped == 1

    async def test_unknown_side_recorded_as_error(self, db_session: Session):
        broker = MockBroker(
            fills=[_fill(perm_id=111, order_id=21, symbol="X", side="BOGUS", shares=1)]
        )
        repo = TradeRepository(db_session)
        result = await run_record(broker=broker, repo=repo)
        assert result.buys_inserted == 0
        assert result.sells_flipped == 0
        assert any("BOGUS" in e for e in result.errors)

    async def test_per_perm_id_exception_doesnt_abort(self, db_session: Session):
        # First fill has a malformed shape (missing contract); second is fine.
        bad = SimpleNamespace(
            contract=None,  # will AttributeError on .symbol
            execution=SimpleNamespace(permId=111, orderId=1, shares=1.0, price=10.0, side="BOT"),
            commissionReport=SimpleNamespace(realizedPNL=0.0, commission=0.0),
            time=datetime.now(),
        )
        good = _fill(perm_id=222, order_id=2, symbol="T", side="BOT", shares=5)
        broker = MockBroker(fills=[bad, good])
        repo = TradeRepository(db_session)

        result = await run_record(broker=broker, repo=repo)
        assert result.errors  # bad one recorded
        assert result.buys_inserted == 1  # good one still processed
        assert db_session.query(Trade).filter(Trade.symbol == "T").count() == 1
