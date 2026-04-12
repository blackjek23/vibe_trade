"""SQLAlchemy engine and session management."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from vibe_trade.db.models import Base


_session_factory: sessionmaker[Session] | None = None


def init_db(db_path: str) -> sessionmaker[Session]:
    """Initialize the database engine and create tables if needed."""
    global _session_factory

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite:///{path}", echo=False)
    Base.metadata.create_all(engine)

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
