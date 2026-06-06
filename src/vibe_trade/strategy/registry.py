"""Strategy registry (Session L) — maps config ids to strategy instances.

`build_strategies` turns the ordered `AppConfig.strategies` list into an ordered
list of `BuiltStrategy` (strategy instance + resolved position size). Order is the
priority order used by submit for entry conflict resolution. Each strategy's
`pct_per_position` is resolved here: the per-strategy override if set, else the
global default passed in by the caller.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from vibe_trade.strategy.base import BaseStrategy
from vibe_trade.strategy.examples.donchian import DonchianStrategy
from vibe_trade.strategy.examples.ema_crossover import EMACrossoverStrategy
from vibe_trade.strategy.examples.macd_crossover import MACDCrossoverStrategy
from vibe_trade.strategy.examples.sma_crossover import SMACrossoverStrategy

StrategyFactory = Callable[[dict], BaseStrategy]

# id -> factory(params) -> strategy. Factories read numeric `params` with the
# strategy's own defaults so an empty params dict yields the standard strategy.
STRATEGY_FACTORIES: dict[str, StrategyFactory] = {
    "donchian": lambda p: DonchianStrategy(period=int(p.get("period", 20))),
    "sma": lambda p: SMACrossoverStrategy(
        fast=int(p.get("fast", 20)), slow=int(p.get("slow", 50))
    ),
    "ema": lambda p: EMACrossoverStrategy(
        fast=int(p.get("fast", 12)), slow=int(p.get("slow", 26))
    ),
    "macd": lambda p: MACDCrossoverStrategy(
        fast=int(p.get("fast", 12)),
        slow=int(p.get("slow", 26)),
        signal=int(p.get("signal", 9)),
    ),
}


@dataclass
class BuiltStrategy:
    """A constructed strategy with its resolved per-position size."""

    strategy: BaseStrategy
    pct_per_position: float  # resolved: per-strategy override or global default


def build_strategy(strategy_id: str, params: dict | None = None) -> BaseStrategy:
    """Construct a single strategy by id (used by the backtest selector)."""
    factory = STRATEGY_FACTORIES.get(strategy_id)
    if factory is None:
        known = ", ".join(sorted(STRATEGY_FACTORIES))
        raise ValueError(f"Unknown strategy id {strategy_id!r}. Known: {known}")
    return factory(dict(params or {}))


def build_strategies(
    strategy_configs: Iterable, default_pct: float
) -> list[BuiltStrategy]:
    """Build enabled strategies, in config order, with resolved sizing.

    `strategy_configs` is any iterable of objects exposing `id`, `enabled`,
    `pct_per_position` and `params` (i.e. `config.StrategyConfig`). Disabled
    entries are skipped. An unknown id raises ValueError.
    """
    built: list[BuiltStrategy] = []
    for cfg in strategy_configs:
        if not cfg.enabled:
            continue
        strategy = build_strategy(cfg.id, cfg.params)
        pct = cfg.pct_per_position if cfg.pct_per_position is not None else default_pct
        built.append(BuiltStrategy(strategy=strategy, pct_per_position=pct))
    return built
