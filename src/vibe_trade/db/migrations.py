"""Idempotent schema migrations, run automatically by ``init_db``.

``Base.metadata.create_all`` (SQLAlchemy) only creates tables that don't exist
yet -- it never alters one that does. A column added to a model here after a
database already exists in production is therefore silently never applied,
and every read of it raises ``OperationalError: no such column`` at the worst
possible time (C-3, ``PROJECT_EVALUATION.md``). This module is the fix: a
``schema_version`` singleton row plus a small ordered list of idempotent
``ALTER TABLE`` steps, run by ``init_db`` right after ``create_all``.

Deliberately not Alembic: this is a single-file SQLite database with one
writer process at a time, not a multi-engine deployment needing branching or
downgrades. A version int plus "add the column if it isn't already there"
covers every migration this project has needed so far, with zero new
dependencies.

To add a migration:
1. Add the new column(s) to the model in ``models.py`` as usual.
2. Write a ``_migrate_to_vN(conn)`` function below, using
   ``_add_column_if_missing`` / ``_create_index_if_missing`` -- both
   idempotent, which is what makes it safe to run against a database that
   already has the change (exactly what happens right after a fresh
   ``create_all``).
3. Append ``(N, _migrate_to_vN)`` to ``MIGRATIONS``.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _column_exists(conn: Connection, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(row[1] == column for row in rows)


def _add_column_if_missing(conn: Connection, table: str, column: str, coltype: str) -> None:
    if not _column_exists(conn, table, column):
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))


def _index_exists(conn: Connection, index_name: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"),
        {"n": index_name},
    ).fetchone()
    return row is not None


def _create_index_if_missing(conn: Connection, index_name: str, table: str, column: str) -> None:
    if not _index_exists(conn, index_name):
        conn.execute(text(f"CREATE INDEX {index_name} ON {table} ({column})"))


def _migrate_to_v1(conn: Connection) -> None:
    """Baseline V2 columns that, in early prod databases, were added to
    tables which already existed before these columns were: ``trades.perm_id``
    / ``exit_perm_id`` (Session B, cross-process fill dedup) and
    ``daily_pnl.total_cash`` / ``open_positions_count`` (reconcile's V2
    extras -- see the "V2: extra fields" comment in ``models.py``). Column
    types and index names match what ``Base.metadata.create_all`` produces on
    a fresh database, so a migrated database and a brand-new one end up with
    an identical schema.
    """
    _add_column_if_missing(conn, "trades", "perm_id", "BIGINT")
    _create_index_if_missing(conn, "ix_trades_perm_id", "trades", "perm_id")
    _add_column_if_missing(conn, "trades", "exit_perm_id", "BIGINT")
    _create_index_if_missing(conn, "ix_trades_exit_perm_id", "trades", "exit_perm_id")
    _add_column_if_missing(conn, "daily_pnl", "total_cash", "FLOAT")
    _add_column_if_missing(conn, "daily_pnl", "open_positions_count", "INTEGER")


def _migrate_to_v2(conn: Connection) -> None:
    """H-3: give the exit leg its own quantity column. Before this,
    `confirm_close_fill`/`close_from_open` overwrote `trades.filled_quantity`
    (the BUY-leg fill count) with the SELL-leg fill count -- destroying the
    entry cost basis on a partial exit and leaving the un-sold remainder
    invisible to every OPEN/PARTIALLY_FILLED query. See PROJECT_EVALUATION.md.
    """
    _add_column_if_missing(conn, "trades", "exit_filled_quantity", "INTEGER")


# Ordered (target_version, migration_fn) pairs, applied in order starting just
# above whatever version is currently stamped in `schema_version`.
MIGRATIONS: list[tuple[int, Callable[[Connection], None]]] = [
    (1, _migrate_to_v1),
    (2, _migrate_to_v2),
]
CURRENT_SCHEMA_VERSION: int = MIGRATIONS[-1][0] if MIGRATIONS else 0


def run_migrations(conn: Connection) -> int:
    """Apply every migration above the stamped version. Returns the version
    landed on. ``conn`` must be inside a transaction the caller commits.

    A database with no ``schema_version`` row starts from 0, whether it's
    brand new (just created by ``create_all``, and therefore already at
    ``CURRENT_SCHEMA_VERSION`` because it was built from today's models) or a
    legacy pre-migration database that may be missing columns. Either way is
    safe because each migration's own idempotency -- not this function's --
    is what makes re-running it a no-op.
    """
    row = conn.execute(text("SELECT version FROM schema_version WHERE id = 1")).fetchone()
    installed = row[0] if row is not None else 0

    for target_version, migration_fn in MIGRATIONS:
        if installed < target_version:
            migration_fn(conn)
            installed = target_version

    if row is None:
        conn.execute(
            text("INSERT INTO schema_version (id, version) VALUES (1, :v)"),
            {"v": installed},
        )
    elif installed != row[0]:
        conn.execute(
            text("UPDATE schema_version SET version = :v WHERE id = 1"),
            {"v": installed},
        )
    return installed
