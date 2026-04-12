"""Tests for config loading and validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vibe_trade.config import (
    AppConfig,
    BrokerConfig,
    GeneralConfig,
    MACrossoverConfig,
    RSIMeanRevertConfig,
    RiskConfig,
    SchedulerConfig,
    StrategyConfig,
    TrailingStopConfig,
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


class TestTrailingStopConfig:
    def test_defaults(self):
        c = TrailingStopConfig()
        assert c.method == "atr"
        assert c.atr_multiplier == 1.5

    def test_multiplier_must_be_positive(self):
        with pytest.raises(ValidationError):
            TrailingStopConfig(atr_multiplier=0)
        with pytest.raises(ValidationError):
            TrailingStopConfig(atr_multiplier=-1)

    def test_percentage_bounds(self):
        with pytest.raises(ValidationError):
            TrailingStopConfig(percentage=0)
        with pytest.raises(ValidationError):
            TrailingStopConfig(percentage=51)


class TestRiskConfig:
    def test_defaults(self):
        c = RiskConfig()
        assert c.max_risk_per_trade_pct == 1.0
        assert c.max_open_positions == 5

    def test_rejects_negative_risk(self):
        with pytest.raises(ValidationError):
            RiskConfig(max_risk_per_trade_pct=-1)

    def test_rejects_zero_positions(self):
        with pytest.raises(ValidationError):
            RiskConfig(max_open_positions=0)

    def test_exposure_bounds(self):
        with pytest.raises(ValidationError):
            RiskConfig(max_portfolio_exposure_pct=101)


class TestMACrossoverConfig:
    def test_defaults(self):
        c = MACrossoverConfig()
        assert c.fast_period == 20
        assert c.slow_period == 50

    def test_slow_must_be_greater_than_fast(self):
        with pytest.raises(ValidationError, match="slow_period.*must be greater"):
            MACrossoverConfig(fast_period=50, slow_period=20)

    def test_equal_periods_rejected(self):
        with pytest.raises(ValidationError):
            MACrossoverConfig(fast_period=20, slow_period=20)

    def test_period_bounds(self):
        with pytest.raises(ValidationError):
            MACrossoverConfig(fast_period=1)


class TestRSIMeanRevertConfig:
    def test_defaults(self):
        c = RSIMeanRevertConfig()
        assert c.oversold == 30
        assert c.overbought == 70

    def test_swapped_values_rejected(self):
        # Bounds enforce oversold <= 49 and overbought >= 51, so swapped values fail
        with pytest.raises(ValidationError):
            RSIMeanRevertConfig(oversold=70, overbought=30)

    def test_oversold_bounds(self):
        with pytest.raises(ValidationError):
            RSIMeanRevertConfig(oversold=0)
        with pytest.raises(ValidationError):
            RSIMeanRevertConfig(oversold=50)

    def test_overbought_bounds(self):
        with pytest.raises(ValidationError):
            RSIMeanRevertConfig(overbought=50)
        with pytest.raises(ValidationError):
            RSIMeanRevertConfig(overbought=100)


class TestStrategyConfig:
    def test_defaults(self):
        c = StrategyConfig()
        assert c.active == ["ma_crossover"]
        assert c.timeframe == "1h"

    def test_rejects_invalid_timeframe(self):
        with pytest.raises(ValidationError, match="Invalid timeframe"):
            StrategyConfig(timeframe="5m")

    def test_rejects_empty_active(self):
        with pytest.raises(ValidationError, match="At least one strategy"):
            StrategyConfig(active=[])

    def test_valid_timeframes(self):
        for tf in ("1h", "4h", "1d"):
            c = StrategyConfig(timeframe=tf)
            assert c.timeframe == tf


class TestAppConfig:
    def test_defaults(self):
        c = AppConfig()
        assert c.general.mode == "paper"
        assert c.broker.paper_port == 7497
        assert c.risk.max_open_positions == 5

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
        assert c.strategy.active == ["ma_crossover"]
