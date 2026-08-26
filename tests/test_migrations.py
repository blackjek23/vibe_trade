"""Tests for the schema migration mechanism (`db/migrations.py`).

C-3, PROJECT_EVALUATION.md: `Base.metadata.create_all` only creates tables
that don't exist yet -- it never alters one that does, so a production
database created before a column was added to a model silently never gets
it, and every read of that column raises `OperationalError` at the worst
time. These tests open a copy of an "older" database file (built by hand
with raw sqlite3, mimicking a pre-perm_id / pre-total_cash prod DB) and
confirm `init_db` brings it up to the current schema without touching
existing data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import inspect, text

from vibe_trade.db.engine import init_db
from vibe_trade.db.migrations import CURRENT_SCHEMA_VERSION, run_migrations


def _build_legacy_db(db_path: Path) -> None:
    """A `trades`/`daily_pnl` schema as it looked before perm_id, exit_perm_id,
    total_cash and open_positions_count existed -- no `schema_version` table
    at all, matching a real pre-migration prod database.
    """
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol VARCHAR(10) NOT NULL,
                side VARCHAR(4) NOT NULL,
                strategy_name VARCHAR(50) NOT NULL,
                requested_quantity INTEGER NOT NULL,
                filled_quantity INTEGER,
                status VARCHAR(20)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE daily_pnl (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE UNIQUE NOT NULL,
                realized_pnl FLOAT,
                unrealized_pnl FLOAT
            )
            """
        )
        con.execute(
            "INSERT INTO trades (symbol, side, strategy_name, requested_quantity, status) "
            "VALUES ('AAPL', 'BUY', 'donchian', 10, 'OPEN')"
        )
        con.execute(
            "INSERT INTO daily_pnl (date, realized_pnl, unrealized_pnl) "
            "VALUES ('2026-05-01', 100.0, 50.0)"
        )
        con.commit()
    finally:
        con.close()


class TestLegacyDatabaseMigration:
    def test_missing_columns_are_added(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _build_legacy_db(db_path)

        init_db(str(db_path))

        con = sqlite3.connect(str(db_path))
        trades_cols = {r[1] for r in con.execute("PRAGMA table_info(trades)")}
        daily_pnl_cols = {r[1] for r in con.execute("PRAGMA table_info(daily_pnl)")}
        con.close()

        assert {"perm_id", "exit_perm_id"} <= trades_cols
        assert {"total_cash", "open_positions_count"} <= daily_pnl_cols

    def test_indexes_are_created(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _build_legacy_db(db_path)

        init_db(str(db_path))

        con = sqlite3.connect(str(db_path))
        index_names = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        con.close()

        assert "ix_trades_perm_id" in index_names
        assert "ix_trades_exit_perm_id" in index_names

    def test_existing_data_is_preserved(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _build_legacy_db(db_path)

        init_db(str(db_path))

        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT symbol, side, requested_quantity, perm_id FROM trades"
        ).fetchone()
        con.close()

        assert row == ("AAPL", "BUY", 10, None)

    def test_schema_version_is_stamped(self, tmp_path):
        db_path = tmp_path / "legacy.db"
        _build_legacy_db(db_path)

        init_db(str(db_path))

        con = sqlite3.connect(str(db_path))
        version = con.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
        con.close()

        assert version == CURRENT_SCHEMA_VERSION

    def test_second_init_db_is_a_no_op(self, tmp_path):
        """Re-running init_db (every job start) must not error or re-add
        columns/indexes that already exist."""
        db_path = tmp_path / "legacy.db"
        _build_legacy_db(db_path)

        init_db(str(db_path))
        init_db(str(db_path))  # must not raise

        con = sqlite3.connect(str(db_path))
        count = con.execute("SELECT count(*) FROM trades").fetchone()[0]
        version_rows = con.execute("SELECT count(*) FROM schema_version").fetchone()[0]
        con.close()

        assert count == 1
        assert version_rows == 1  # still a singleton, not duplicated


class TestFreshDatabaseMigration:
    def test_fresh_db_lands_on_current_version(self, tmp_path):
        """A brand-new DB is built from today's models, which already have
        every column any migration would add -- it should be stamped current
        immediately, not treated as needing catch-up.
        """
        db_path = tmp_path / "fresh.db"
        init_db(str(db_path))

        con = sqlite3.connect(str(db_path))
        version = con.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
        con.close()

        assert version == CURRENT_SCHEMA_VERSION

    def test_fresh_db_has_all_current_tables(self, tmp_path):
        db_path = tmp_path / "fresh.db"
        factory = init_db(str(db_path))
        session = factory()
        table_names = inspect(session.bind).get_table_names()
        session.close()

        assert "schema_version" in table_names


class TestRunMigrationsDirectly:
    def test_no_op_when_already_current(self, tmp_path):
        """run_migrations on an already-current DB should not touch anything
        beyond confirming the stamped version."""
        db_path = tmp_path / "current.db"
        init_db(str(db_path))

        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            landed = run_migrations(conn)

        assert landed == CURRENT_SCHEMA_VERSION
        with engine.begin() as conn:
            version = conn.execute(text("SELECT version FROM schema_version WHERE id = 1")).fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION
