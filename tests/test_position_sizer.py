"""Tests for V2 position sizing.

Locked spec: 1.8% of net_liq, max 50 positions, floor to whole share, skip if
1 share > target. See project_v2_next_sessions.md memory.
"""

from __future__ import annotations

import math

from vibe_trade.risk.position_sizer import (
    DEFAULT_MAX_POSITIONS,
    DEFAULT_PCT_PER_POSITION,
    size_position,
)


class TestHappyPath:
    def test_typical_sizing_at_default_pct(self):
        # $100K net_liq, $50 stock, default 1.8% target = $1,800 -> 36 shares
        n = size_position(net_liquidation=100_000.0, price=50.0, current_position_count=10)
        assert n == 36

    def test_floors_to_whole_share(self):
        # $100K * 1.8% = $1,800; price $77 → 1800/77 = 23.376... → floor 23
        n = size_position(net_liquidation=100_000.0, price=77.0, current_position_count=0)
        assert n == 23

    def test_uses_net_liq_not_cash(self):
        # The function takes net_liq directly — caller passes the right value.
        # Sanity: same call returns same result regardless of unstated cash.
        n1 = size_position(net_liquidation=95_381.36, price=26.09, current_position_count=37)
        # 1.8% of 95,381.36 = 1716.86; 1716.86 / 26.09 = 65.81 → 65
        assert n1 == 65


class TestPositionCap:
    def test_at_cap_returns_zero(self):
        n = size_position(net_liquidation=100_000.0, price=50.0, current_position_count=50)
        assert n == 0

    def test_one_below_cap_returns_normally(self):
        n = size_position(net_liquidation=100_000.0, price=50.0, current_position_count=49)
        assert n == 36  # 1.8% of 100K = 1800; 1800 / 50 = 36

    def test_above_cap_returns_zero(self):
        n = size_position(net_liquidation=100_000.0, price=50.0, current_position_count=99)
        assert n == 0

    def test_custom_cap_respected(self):
        # If we tightened to 30 max, 31 positions held → skip
        n = size_position(
            net_liquidation=100_000.0, price=50.0,
            current_position_count=31, max_positions=30,
        )
        assert n == 0


class TestOneShareTooBig:
    def test_skips_when_share_exceeds_target(self):
        # $100K * 1.8% = $1,800. BRK.A at $700,000 → 1 share dwarfs target → skip.
        n = size_position(net_liquidation=100_000.0, price=700_000.0, current_position_count=0)
        assert n == 0

    def test_share_exactly_at_target_returns_one(self):
        # price == target_dollars exactly: 1800 / 1800 = 1.0 → floor 1
        n = size_position(net_liquidation=100_000.0, price=1_800.0, current_position_count=0)
        assert n == 1

    def test_share_one_dollar_over_target_skips(self):
        # price 1801 > target 1800 → skip per "1 share > target"
        n = size_position(net_liquidation=100_000.0, price=1_801.0, current_position_count=0)
        assert n == 0


class TestDefensiveInputs:
    def test_zero_net_liq_returns_zero(self):
        n = size_position(net_liquidation=0.0, price=50.0, current_position_count=0)
        assert n == 0

    def test_negative_net_liq_returns_zero(self):
        n = size_position(net_liquidation=-1_000.0, price=50.0, current_position_count=0)
        assert n == 0

    def test_zero_price_returns_zero(self):
        n = size_position(net_liquidation=100_000.0, price=0.0, current_position_count=0)
        assert n == 0

    def test_negative_price_returns_zero(self):
        n = size_position(net_liquidation=100_000.0, price=-50.0, current_position_count=0)
        assert n == 0


class TestCustomPct:
    def test_explicit_pct_overrides_default(self):
        # 2.5% of $100K = $2,500; price $50 → 50 shares
        n = size_position(
            net_liquidation=100_000.0, price=50.0,
            current_position_count=0, pct_per_position=0.025,
        )
        assert n == 50

    def test_pct_default_matches_locked_spec(self):
        assert math.isclose(DEFAULT_PCT_PER_POSITION, 0.018)
        assert DEFAULT_MAX_POSITIONS == 50
