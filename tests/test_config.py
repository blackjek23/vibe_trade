"""Tests for config loading and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibe_trade.config import (
    AppConfig,
    BrokerConfig,
    GeneralConfig,
    HealthcheckConfig,
    RiskConfig,
    SchedulerConfig,
    StrategyConfig,
    UniverseConfig,
    load_config,
)


class TestGeneralConfig:
    def test_defaults(self):
        c = GeneralConfig()
        assert c.mode == "paper"
        assert c.log_level == "INFO"
        assert c.db_path == "data/vibe_trade.db"

    def test_mode_must_be_paper_or_live(self):
        with pytest.raises(ValidationError):
            GeneralConfig(mode="test")

    def test_log_level_validated(self):
        c = GeneralConfig(log_level="debug")
        assert c.log_level == "DEBUG"  # uppercased

    def test_log_level_rejects_invalid(self):
        with pytest.raises(ValidationError, match="Invalid log_level"):
            GeneralConfig(log_level="BANANA")


class TestBrokerConfig:
    def test_defaults(self):
        c = BrokerConfig()
        assert c.paper_port == 7497
        assert c.live_port == 7496

    def test_get_port_paper(self):
        c = BrokerConfig()
        assert c.get_port("paper") == 7497

    def test_get_port_live(self):
        c = BrokerConfig()
        assert c.get_port("live") == 7496

    def test_timeout_bounds(self):
        with pytest.raises(ValidationError):
            BrokerConfig(timeout=0)
        with pytest.raises(ValidationError):
            BrokerConfig(timeout=999)

    def test_client_id_non_negative(self):
        with pytest.raises(ValidationError):
            BrokerConfig(client_id=-1)

    def test_connect_retries_defaults(self):
        c = BrokerConfig()
        assert c.connect_retries == 3
        assert c.retry_backoff_seconds == 2.0

    def test_connect_retries_bounds(self):
        with pytest.raises(ValidationError):
            BrokerConfig(connect_retries=-1)
        with pytest.raises(ValidationError):
            BrokerConfig(connect_retries=11)

    def test_retry_backoff_must_be_positive(self):
        with pytest.raises(ValidationError):
            BrokerConfig(retry_backoff_seconds=0)
        with pytest.raises(ValidationError):
            BrokerConfig(retry_backoff_seconds=61)

    def test_order_pacing_defaults(self):
        c = BrokerConfig()
        assert c.order_pacing_seconds == 0.05

    def test_order_pacing_allows_zero(self):
        c = BrokerConfig(order_pacing_seconds=0)
        assert c.order_pacing_seconds == 0

    def test_order_pacing_rejects_negative_and_huge(self):
        with pytest.raises(ValidationError):
            BrokerConfig(order_pacing_seconds=-0.01)
        with pytest.raises(ValidationError):
            BrokerConfig(order_pacing_seconds=5.1)


class TestUniverseConfig:
    def test_default_sp500(self):
        c = UniverseConfig()
        assert c.source == "sp500"

    def test_custom_symbols_uppercased(self):
        c = UniverseConfig(source="custom", custom_symbols=["aapl", " msft ", "googl"])
        assert c.custom_symbols == ["AAPL", "MSFT", "GOOGL"]

    def test_empty_strings_stripped(self):
        c = UniverseConfig(source="custom", custom_symbols=["AAPL", "", "  "])
        assert c.custom_symbols == ["AAPL"]


class TestSchedulerConfig:
    def test_defaults(self):
        c = SchedulerConfig()
        assert c.interval_minutes == 60
        assert c.market_open == "09:30"

    def test_interval_bounds(self):
        with pytest.raises(ValidationError):
            SchedulerConfig(interval_minutes=0)
        with pytest.raises(ValidationError):
            SchedulerConfig(interval_minutes=1441)

    def test_valid_time_format(self):
        c = SchedulerConfig(market_open="08:00", market_close="15:30")
        assert c.market_open == "08:00"

    def test_invalid_time_format(self):
        with pytest.raises(ValidationError, match="HH:MM"):
            SchedulerConfig(market_open="9:30:00")

    def test_invalid_time_values(self):
        with pytest.raises(ValidationError, match="Invalid time"):
            SchedulerConfig(market_open="25:00")

    def test_trading_days_validated(self):
        c = SchedulerConfig(trading_days=["MON", "Tue"])
        assert c.trading_days == ["mon", "tue"]

    def test_invalid_trading_day(self):
        with pytest.raises(ValidationError, match="Invalid trading day"):
            SchedulerConfig(trading_days=["monday"])


class TestRiskConfig:
    def test_defaults_match_locked_v2_spec(self):
        c = RiskConfig()
        assert c.pct_per_position == 0.018
        assert c.max_open_positions == 50

    def test_rejects_zero_pct(self):
        with pytest.raises(ValidationError):
            RiskConfig(pct_per_position=0)

    def test_rejects_pct_over_one(self):
        with pytest.raises(ValidationError):
            RiskConfig(pct_per_position=1.5)

    def test_rejects_zero_positions(self):
        with pytest.raises(ValidationError):
            RiskConfig(max_open_positions=0)

    def test_rejects_excessive_positions(self):
        with pytest.raises(ValidationError):
            RiskConfig(max_open_positions=200)

    def test_no_v1_trailing_stop_field(self):
        # Regression guard: V1 had RiskConfig.trailing_stop, V2 must not.
        c = RiskConfig()
        assert not hasattr(c, "trailing_stop")


class TestHealthcheckConfig:
    """OPS-1 dead-man's switch config -- opt-in, disabled by default."""

    def test_defaults_disabled(self):
        c = HealthcheckConfig()
        assert c.enabled is False
        assert c.ping_url == ""
        assert c.timeout_seconds == 10.0

    def test_timeout_must_be_positive(self):
        with pytest.raises(ValidationError):
            HealthcheckConfig(timeout_seconds=0)

    def test_timeout_capped_at_60(self):
        with pytest.raises(ValidationError):
            HealthcheckConfig(timeout_seconds=61)


class TestAppConfig:
    def test_defaults(self):
        c = AppConfig()
        assert c.general.mode == "paper"
        assert c.broker.paper_port == 7497
        assert c.risk.max_open_positions == 50

    def test_healthcheck_defaults_disabled(self):
        c = AppConfig()
        assert c.healthcheck.enabled is False
        assert c.healthcheck.ping_url == ""

    def test_load_config_no_file(self):
        """Should return defaults when no config file exists."""
        c = load_config("nonexistent/path.toml")
        assert c.general.mode == "paper"

    def test_load_config_from_example(self, tmp_path):
        """Should load from the example config file."""
        import shutil
        src = "config/config.example.toml"
        dst = tmp_path / "config.toml"
        shutil.copy(src, dst)
        c = load_config(str(dst))
        assert c.general.mode == "paper"
        assert c.risk.pct_per_position == 0.018
        assert c.risk.max_open_positions == 50


class TestStrategiesConfig:
    """Session L multi-strategy registry config section."""

    def test_default_is_single_donchian(self):
        c = AppConfig()
        assert len(c.strategies) == 1
        assert c.strategies[0].id == "donchian"
        assert c.strategies[0].enabled is True
        assert c.strategies[0].pct_per_position is None  # -> global fallback

    def test_strategy_config_defaults(self):
        s = StrategyConfig(id="sma")
        assert s.enabled is True
        assert s.pct_per_position is None
        assert s.params == {}

    def test_pct_override_validation(self):
        with pytest.raises(ValidationError):
            StrategyConfig(id="sma", pct_per_position=0)
        with pytest.raises(ValidationError):
            StrategyConfig(id="sma", pct_per_position=1.5)

    def test_parses_toml_strategies_block(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            "[[strategies]]\n"
            'id = "donchian"\n\n'
            "[[strategies]]\n"
            'id = "sma"\n'
            "pct_per_position = 0.01\n"
            "[strategies.params]\n"
            "fast = 10\n"
            "slow = 30\n"
        )
        c = load_config(str(cfg))
        assert [s.id for s in c.strategies] == ["donchian", "sma"]
        assert c.strategies[1].pct_per_position == 0.01
        assert c.strategies[1].params == {"fast": 10, "slow": 30}

    def test_absent_section_defaults_to_donchian(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text('[general]\nmode = "paper"\n')
        c = load_config(str(cfg))
        assert [s.id for s in c.strategies] == ["donchian"]


class TestConfigEnvVar:
    """Hygiene #3: when no explicit path is given, load_config honours the
    VIBE_TRADE_CONFIG env var. This lets `docker compose run` invocations that
    override the service command (e.g. config-check, dropping --config) still
    find the mounted /config/config.toml.
    """

    def test_env_var_used_when_no_explicit_path(self, tmp_path, monkeypatch):
        cfg = tmp_path / "from_env.toml"
        cfg.write_text('[general]\nmode = "live"\n')
        monkeypatch.setenv("VIBE_TRADE_CONFIG", str(cfg))
        c = load_config()  # no explicit path
        assert c.general.mode == "live"

    def test_explicit_path_overrides_env_var(self, tmp_path, monkeypatch):
        env_cfg = tmp_path / "from_env.toml"
        env_cfg.write_text('[general]\nmode = "live"\n')
        explicit = tmp_path / "explicit.toml"
        explicit.write_text('[general]\nmode = "paper"\n')
        monkeypatch.setenv("VIBE_TRADE_CONFIG", str(env_cfg))
        c = load_config(str(explicit))
        assert c.general.mode == "paper"  # explicit arg wins over the env var
