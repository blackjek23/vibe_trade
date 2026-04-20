"""Unit tests for IBBroker (logic only; integration tests hit real IB paper)."""

from __future__ import annotations

import pytest

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.broker.models import OrderRequest
from vibe_trade.config import BrokerConfig


class _FakeIB:
    """Minimal stand-in for ib_async.IB used to drive IBBroker.connect()."""

    def __init__(self, fail_times: int = 0, exc: Exception | None = None):
        self.fail_times = fail_times
        self.exc = exc or ConnectionRefusedError("TWS not available")
        self.calls = 0
        self._connected = False
        self.qualify_calls: list[str] = []

    async def connectAsync(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        self._connected = True

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    async def qualifyContractsAsync(self, contract) -> None:
        # Record the symbol so tests can count how often we qualified.
        self.qualify_calls.append(contract.symbol)

    def placeOrder(self, contract, order):
        # Return a minimal trade-like object with no fills, status SUBMITTED.
        class _Status:
            status = "Submitted"

        class _Order:
            orderId = 123

        class _Trade:
            orderStatus = _Status()
            order = _Order()
            fills: list = []

            def isDone(self):
                return False

        return _Trade()


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


class TestContractCaching:
    async def test_same_symbol_qualified_only_once(self, monkeypatch):
        # Speed up order poll (avoid 10 * 0.5s sleep for a SUBMITTED trade)
        import vibe_trade.broker.ib_broker as mod
        monkeypatch.setattr(mod.asyncio, "sleep", _instant_sleep)

        broker = IBBroker(BrokerConfig(), mode="paper")
        fake = _FakeIB()
        broker.ib = fake

        await broker.place_market_order(OrderRequest(symbol="AAPL", side="BUY", quantity=1))
        await broker.place_market_order(OrderRequest(symbol="AAPL", side="BUY", quantity=2))
        await broker.place_market_order(OrderRequest(symbol="AAPL", side="SELL", quantity=1))

        assert fake.qualify_calls == ["AAPL"]
        assert "AAPL" in broker._contract_cache

    async def test_different_symbols_each_qualified_once(self, monkeypatch):
        import vibe_trade.broker.ib_broker as mod
        monkeypatch.setattr(mod.asyncio, "sleep", _instant_sleep)

        broker = IBBroker(BrokerConfig(), mode="paper")
        fake = _FakeIB()
        broker.ib = fake

        await broker.place_market_order(OrderRequest(symbol="AAPL", side="BUY", quantity=1))
        await broker.place_market_order(OrderRequest(symbol="MSFT", side="BUY", quantity=1))
        await broker.place_market_order(OrderRequest(symbol="AAPL", side="BUY", quantity=1))
        await broker.place_market_order(OrderRequest(symbol="MSFT", side="SELL", quantity=1))

        assert fake.qualify_calls == ["AAPL", "MSFT"]

    async def test_disconnect_clears_cache(self):
        broker = IBBroker(BrokerConfig(), mode="paper")
        fake = _FakeIB()
        broker.ib = fake
        # Prime cache directly without hitting placeOrder
        await broker._get_qualified_contract("AAPL")
        assert "AAPL" in broker._contract_cache

        fake._connected = True  # so disconnect() enters the branch
        await broker.disconnect()
        assert broker._contract_cache == {}


async def _instant_sleep(_seconds: float) -> None:
    return None


class TestOrderPacing:
    async def test_pacing_sleep_called_after_qualify(self, monkeypatch):
        sleep_durations: list[float] = []

        async def _track_sleep(seconds: float) -> None:
            sleep_durations.append(seconds)

        import vibe_trade.broker.ib_broker as mod
        monkeypatch.setattr(mod.asyncio, "sleep", _track_sleep)

        broker = IBBroker(BrokerConfig(order_pacing_seconds=0.1), mode="paper")
        broker.ib = _FakeIB()

        await broker._get_qualified_contract("AAPL")
        # After first qualify, one pace call of 0.1s
        assert 0.1 in sleep_durations

    async def test_pacing_zero_skips_sleep(self, monkeypatch):
        sleep_durations: list[float] = []

        async def _track_sleep(seconds: float) -> None:
            sleep_durations.append(seconds)

        import vibe_trade.broker.ib_broker as mod
        monkeypatch.setattr(mod.asyncio, "sleep", _track_sleep)

        broker = IBBroker(BrokerConfig(order_pacing_seconds=0.0), mode="paper")
        broker.ib = _FakeIB()

        await broker._get_qualified_contract("AAPL")
        # Pacing disabled — no sleep should have been issued by _pace()
        assert sleep_durations == []

    async def test_cached_qualify_does_not_pace(self, monkeypatch):
        sleep_durations: list[float] = []

        async def _track_sleep(seconds: float) -> None:
            sleep_durations.append(seconds)

        import vibe_trade.broker.ib_broker as mod
        monkeypatch.setattr(mod.asyncio, "sleep", _track_sleep)

        broker = IBBroker(BrokerConfig(order_pacing_seconds=0.1), mode="paper")
        broker.ib = _FakeIB()

        await broker._get_qualified_contract("AAPL")  # pace fires
        sleep_durations.clear()
        await broker._get_qualified_contract("AAPL")  # cache hit — no pace
        assert sleep_durations == []
