"""SQLAlchemy engine and session management."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from vibe_trade.db.migrations import run_migrations
from vibe_trade.db.models import Base


_session_factory: sessionmaker[Session] | None = None


def init_db(db_path: str) -> sessionmaker[Session]:
    """Initialize the database engine, create any wholly-missing tables, and
    apply pending schema migrations.

    `create_all` alone only creates tables that don't exist yet -- it never
    alters one that does, which is C-3 in PROJECT_EVALUATION.md. See
    `db/migrations.py` for the idempotent-ALTER-TABLE mechanism that closes
    that gap.
    """
    global _session_factory

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite:///{path}", echo=False)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        run_migrations(conn)

    _session_factory = sessionmaker(bind=engine)
    return _session_factory


def get_session_factory(db_path: str | None = None) -> sessionmaker[Session]:
    """Get the session factory, initializing if needed."""
    global _session_factory
    if _session_factory is None:
        if db_path is None:
            raise RuntimeError("Database not initialized. Call init_db() first.")
        return init_db(db_path)
    return _session_factory
