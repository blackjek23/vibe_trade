"""Shared test fixtures for vibe_trade tests."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from vibe_trade.config import AppConfig
from vibe_trade.db.models import Base


@pytest.fixture
def config() -> AppConfig:
    """Default config with all defaults (no file needed)."""
    return AppConfig()


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite session for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def sample_candles():
    """60 days of synthetic OHLCV data for testing strategies."""
    import numpy as np
    import pandas as pd

    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=60, freq="D")
    close = 100 + np.cumsum(np.random.randn(60) * 0.5)
    return pd.DataFrame(
        {
            "open": close - 0.3,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.random.uniform(1000, 5000, 60),
        },
        index=dates,
    )
