"""OPS-1 dead-man's-switch ping tests (tests/TEST_REGISTRY.csv row per test)."""

from __future__ import annotations

from urllib.error import URLError

from vibe_trade.notify.healthcheck import ping_healthcheck


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestPingHealthcheck:
    def test_blank_url_returns_false_without_a_request(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "vibe_trade.notify.healthcheck.urllib.request.urlopen",
            lambda *a, **k: called.append(1),
        )
        assert ping_healthcheck("") is False
        assert called == []

    def test_2xx_response_returns_true(self, monkeypatch):
        monkeypatch.setattr(
            "vibe_trade.notify.healthcheck.urllib.request.urlopen",
            lambda url, timeout: _FakeResponse(200),
        )
        assert ping_healthcheck("https://hc-ping.com/fake-uuid") is True

    def test_non_2xx_response_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            "vibe_trade.notify.healthcheck.urllib.request.urlopen",
            lambda url, timeout: _FakeResponse(500),
        )
        assert ping_healthcheck("https://hc-ping.com/fake-uuid") is False

    def test_network_error_returns_false_not_raises(self, monkeypatch):
        def _raise(url, timeout):
            raise URLError("no route to host")

        monkeypatch.setattr(
            "vibe_trade.notify.healthcheck.urllib.request.urlopen", _raise
        )
        # Must never raise -- a monitoring-service hiccup can't fail the job.
        assert ping_healthcheck("https://hc-ping.com/fake-uuid") is False

    def test_timeout_is_forwarded(self, monkeypatch):
        seen = {}

        def _capture(url, timeout):
            seen["timeout"] = timeout
            return _FakeResponse(200)

        monkeypatch.setattr(
            "vibe_trade.notify.healthcheck.urllib.request.urlopen", _capture
        )
        ping_healthcheck("https://hc-ping.com/fake-uuid", timeout=3.5)
        assert seen["timeout"] == 3.5
