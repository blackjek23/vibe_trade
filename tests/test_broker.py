"""Unit tests for IBBroker (logic only; integration tests hit real IB paper)."""

from __future__ import annotations

import pytest

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.config import BrokerConfig


class _FakeIB:
    """Minimal stand-in for ib_async.IB used to drive IBBroker.connect()."""

    def __init__(self, fail_times: int = 0, exc: Exception | None = None):
        self.fail_times = fail_times
        self.exc = exc or ConnectionRefusedError("TWS not available")
        self.calls = 0
        self._connected = False

    async def connectAsync(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        self._connected = True

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False


class TestIBBrokerConnectRetry:
    async def test_connect_succeeds_first_try(self, monkeypatch):
        broker = IBBroker(BrokerConfig(retry_backoff_seconds=0.01), mode="paper")
        fake = _FakeIB(fail_times=0)
        broker.ib = fake
        await broker.connect()
        assert fake.calls == 1
        assert fake.isConnected()

    async def test_connect_retries_then_succeeds(self, monkeypatch):
        broker = IBBroker(
            BrokerConfig(connect_retries=3, retry_backoff_seconds=0.01),
            mode="paper",
        )
        fake = _FakeIB(fail_times=2)
        broker.ib = fake
        await broker.connect()
        # 2 failures + 1 success
        assert fake.calls == 3

    async def test_connect_exhausts_retries_and_raises(self):
        broker = IBBroker(
            BrokerConfig(connect_retries=2, retry_backoff_seconds=0.01),
            mode="paper",
        )
        fake = _FakeIB(fail_times=99)
        broker.ib = fake
        with pytest.raises(ConnectionRefusedError):
            await broker.connect()
        # connect_retries=2 means 1 initial + 2 retries = 3 attempts total
        assert fake.calls == 3

    async def test_connect_no_retries_when_disabled(self):
        broker = IBBroker(
            BrokerConfig(connect_retries=0, retry_backoff_seconds=0.01),
            mode="paper",
        )
        fake = _FakeIB(fail_times=99)
        broker.ib = fake
        with pytest.raises(ConnectionRefusedError):
            await broker.connect()
        assert fake.calls == 1
