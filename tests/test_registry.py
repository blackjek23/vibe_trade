"""Tests for the Session L strategy registry."""

from __future__ import annotations

import pytest

from vibe_trade.config import StrategyConfig
from vibe_trade.strategy.examples.donchian import DonchianStrategy
from vibe_trade.strategy.examples.ema_crossover import EMACrossoverStrategy
from vibe_trade.strategy.examples.macd_crossover import MACDCrossoverStrategy
from vibe_trade.strategy.examples.sma_crossover import SMACrossoverStrategy
from vibe_trade.strategy.registry import (
    STRATEGY_FACTORIES,
    build_strategies,
    build_strategy,
)


class TestBuildStrategy:
    def test_all_known_ids_build(self):
        assert isinstance(build_strategy("donchian"), DonchianStrategy)
        assert isinstance(build_strategy("sma"), SMACrossoverStrategy)
        assert isinstance(build_strategy("ema"), EMACrossoverStrategy)
        assert isinstance(build_strategy("macd"), MACDCrossoverStrategy)

    def test_params_applied(self):
        strat = build_strategy("sma", {"fast": 5, "slow": 10})
        assert isinstance(strat, SMACrossoverStrategy)
        assert strat.fast == 5 and strat.slow == 10

    def test_donchian_period_param(self):
        strat = build_strategy("donchian", {"period": 30})
        assert strat.period == 30

    def test_unknown_id_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy id"):
            build_strategy("nope")

    def test_registry_covers_all_session_l_strategies(self):
        assert set(STRATEGY_FACTORIES) >= {"donchian", "sma", "ema", "macd"}


class TestBuildStrategies:
    def test_default_donchian_only(self):
        built = build_strategies([StrategyConfig(id="donchian")], default_pct=0.018)
        assert len(built) == 1
        assert isinstance(built[0].strategy, DonchianStrategy)
        assert built[0].pct_per_position == 0.018

    def test_priority_order_preserved(self):
        configs = [
            StrategyConfig(id="macd"),
            StrategyConfig(id="donchian"),
            StrategyConfig(id="sma"),
        ]
        built = build_strategies(configs, default_pct=0.018)
        assert [b.strategy.name for b in built] == ["macd", "donchian", "sma"]

    def test_disabled_filtered_out(self):
        configs = [
            StrategyConfig(id="donchian"),
            StrategyConfig(id="sma", enabled=False),
            StrategyConfig(id="ema"),
        ]
        built = build_strategies(configs, default_pct=0.018)
        assert [b.strategy.name for b in built] == ["donchian", "ema"]

    def test_pct_override_vs_fallback(self):
        configs = [
            StrategyConfig(id="donchian"),  # no override -> global
            StrategyConfig(id="sma", pct_per_position=0.01),  # override
        ]
        built = build_strategies(configs, default_pct=0.018)
        assert built[0].pct_per_position == 0.018
        assert built[1].pct_per_position == 0.01

    def test_unknown_id_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy id"):
            build_strategies([StrategyConfig(id="bogus")], default_pct=0.018)
