"""Strategy registry — maps config names to strategy classes."""

from __future__ import annotations

from vibe_trade.config import StrategyConfig
from vibe_trade.strategy.base import BaseStrategy
from vibe_trade.strategy.examples.ma_crossover import MACrossoverStrategy
from vibe_trade.strategy.examples.rsi_mean_revert import RSIMeanRevertStrategy


STRATEGY_MAP: dict[str, type[BaseStrategy]] = {
    "ma_crossover": MACrossoverStrategy,
    "rsi_mean_revert": RSIMeanRevertStrategy,
}


def load_strategies(config: StrategyConfig) -> list[BaseStrategy]:
    """Instantiate active strategies from config."""
    strategies = []
    for name in config.active:
        cls = STRATEGY_MAP.get(name)
        if cls is None:
            raise ValueError(
                f"Unknown strategy '{name}'. Available: {list(STRATEGY_MAP.keys())}"
            )

        if name == "ma_crossover":
            strategies.append(
                MACrossoverStrategy(
                    fast_period=config.ma_crossover.fast_period,
                    slow_period=config.ma_crossover.slow_period,
                )
            )
        elif name == "rsi_mean_revert":
            strategies.append(
                RSIMeanRevertStrategy(
                    rsi_period=config.rsi_mean_revert.rsi_period,
                    oversold=config.rsi_mean_revert.oversold,
                    overbought=config.rsi_mean_revert.overbought,
                )
            )
        else:
            strategies.append(cls())

    return strategies
