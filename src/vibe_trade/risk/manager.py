"""V2 risk manager — minimal pre-trade gates.

The heavy lifting (position sizing, share count cap) lives in `position_sizer.py`.
This module is now just two intent-revealing checks called by `submit`:

- `can_open_new_position(positions)` — once per entries phase, before scanning.
  Returns False if we're at the max-positions cap.
- `can_trade_symbol(signal, positions)` — once per signal.
  Returns False if we're already holding the symbol (V2 invariant: one position
  per ticker, no averaging).

Concentration / exposure checks from V1 are gone — they're now structurally
enforced by the sizer's per-position % cap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from vibe_trade.broker.models import Position
from vibe_trade.config import RiskConfig
from vibe_trade.strategy.base import SignalResult

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    approved: bool
    reason: str = ""


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config

    def can_open_new_position(self, positions: list[Position]) -> RiskDecision:
        """Called once at the start of the entries phase.

        Returns False if the open-position cap is reached -- entire BUY scan
        should be skipped that day. Exits still run regardless.
        """
        held = sum(1 for p in positions if p.quantity != 0)
        if held >= self.config.max_open_positions:
            return RiskDecision(
                approved=False,
                reason=f"Position cap reached ({held}/{self.config.max_open_positions})",
            )
        return RiskDecision(approved=True)

    def can_trade_symbol(
        self,
        signal: SignalResult,
        positions: list[Position],
    ) -> RiskDecision:
        """Called per BUY signal. Skip if we already hold this symbol."""
        for pos in positions:
            if pos.symbol == signal.symbol and pos.quantity != 0:
                return RiskDecision(
                    approved=False,
                    reason=f"Already holding position in {signal.symbol}",
                )
        return RiskDecision(approved=True)
