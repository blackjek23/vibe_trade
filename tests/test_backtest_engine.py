"""Tests for vibe_trade.backtest.engine.run_backtest.

Synthetic OHLC built to fire Donchian breakouts at known dates so we can
assert exact entry/exit prices, dates, and quantities.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from vibe_trade.backtest.engine import BacktestResult, Frictions, run_backtest
from vibe_trade.backtest.membership import MembershipChange, build_membership_timeline
from vibe_trade.strategy.examples.donchian import DonchianStrategy


def _bars(closes: list[float], highs: list[float] | None = None,
          lows: list[float] | None = None, opens: list[float] | None = None,
          start: str = "2024-01-01") -> pd.DataFrame:
    """Build a daily-bar DataFrame with custom close (and optional H/L/open)."""
    n = len(closes)
    return pd.DataFrame(
        {
            "open": opens if opens is not None else closes,
            "high": highs if highs is not None else [c + 0.5 for c in closes],
            "low": lows if lows is not None else [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1000] * n,
        },
        index=pd.date_range(start, periods=n, freq="D"),
    ).rename_axis("date")


def _flat_then_breakout_bars(start: str = "2024-01-01") -> pd.DataFrame:
    """20 flat bars (high=100, low=99, close=99.5), bar 21 closes at 101 (BUY),
    bar 22 opens at 101.50 (the fill day)."""
    closes = [99.5] * 20 + [101.0, 101.5, 101.5, 101.5, 101.5]
    highs  = [100.0] * 20 + [101.5, 102.0, 102.0, 102.0, 102.0]
    lows   = [99.0] * 20 + [100.5, 101.0, 101.0, 101.0, 101.0]
    opens  = [99.5] * 20 + [101.0, 101.5, 101.5, 101.5, 101.5]
    return _bars(closes, highs, lows, opens, start=start)


class TestEmpty:
    def test_no_universe_returns_starting_equity(self):
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["AAPL"],
            start=date(2024, 1, 1),
            end=date(2024, 2, 1),
            bars={},  # no data
        )
        assert isinstance(result, BacktestResult)
        assert result.ending_equity == result.starting_equity
        assert result.trades == []

    def test_flat_data_no_signals_no_trades(self):
        flat = _bars([100.0] * 30)
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": flat},
        )
        # 30 days of perfectly flat bars: no breakouts -> no trades.
        assert result.trades == []
        assert result.open_positions_at_end == 0
        # Equity unchanged
        assert abs(result.ending_equity - result.starting_equity) < 1e-6


class TestBuyFlow:
    def test_buy_fills_at_next_day_open(self):
        # Breakout on bar 21 (index 20); fill on bar 22 (index 21) at its open.
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": _flat_then_breakout_bars()},
            starting_equity=10_000.0,
            pct_per_position=0.04,
            max_positions=25,
        )
        # No trade closed -> trades list still empty, but a position is open.
        assert result.open_positions_at_end == 1
        # 4% of $10k = $400. Fill at $101.50 = floor(400/101.50) = 3 shares
        # cash spent = 3 * 101.50 = 304.50
        # remaining cash = 9695.50 + position market value
        # market value at close of bar 22 = 3 * 101.50 = 304.50 (close == open here)
        assert abs(result.ending_equity - 10_000.0) < 1.0  # roughly flat after open

    def test_buy_skipped_when_no_cash(self):
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": _flat_then_breakout_bars()},
            starting_equity=50.0,  # too poor for even 1 share at $101.50
            pct_per_position=0.04,
            max_positions=25,
        )
        # Sizer skips: 4% of $50 = $2 < $101.50 (1 share). entries_skipped path.
        assert result.open_positions_at_end == 0
        assert result.trades == []


class TestSellFlow:
    def test_sell_signal_closes_position_at_next_day_open(self):
        # Build: 20 flat at 99.5, breakout to 101 (signal day 21),
        # fill open 101.50 (day 22), then breakdown to 95 on day 41
        # (need 20 fresh "low" bars between to reset the band, but for test
        # purposes the band post-breakout is built from days 22-41 highs/lows).
        # Simpler: keep all the flat low at 99 in bars[1..20], breakout at 21,
        # then bars 22..41 trade at ~101 (held), then bar 42 closes at 90
        # which is below min(low) of bars 22..41 (~100.5) -> SELL signal,
        # fill at bar 43's open.
        closes = (
            [99.5] * 20         # 0..19  flat baseline
            + [101.0]            # 20     BUY signal day
            + [101.5] * 20       # 21..40 holding (band rebuilds at ~101 lows)
            + [90.0]             # 41     SELL signal day
            + [89.0]             # 42     SELL fills at this open=89
        )
        highs = (
            [100.0] * 20
            + [101.5]
            + [102.0] * 20
            + [91.0]
            + [89.5]
        )
        lows = (
            [99.0] * 20
            + [100.5]
            + [101.0] * 20
            + [89.0]
            + [88.0]
        )
        opens = (
            [99.5] * 20
            + [101.0]
            + [101.5] * 20
            + [90.0]
            + [89.0]    # SELL fills at this open
        )
        df = _bars(closes, highs, lows, opens)

        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2030, 1, 1),
            bars={"X": df},
            starting_equity=10_000.0,
            pct_per_position=0.04,
        )
        # One round-trip trade should be in the log.
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.symbol == "X"
        # Entry at bar 22's open = 101.50
        assert abs(trade.entry_price - 101.5) < 1e-6
        # Exit at bar 43's open = 89.0
        assert abs(trade.exit_price - 89.0) < 1e-6
        # Loss: (89.0 - 101.50) * qty
        assert trade.pnl < 0
        # Position closed
        assert result.open_positions_at_end == 0


class TestFrictionsDataclass:
    """Pure unit tests for Frictions.fill_price/commission -- no backtest run
    needed to pin these down."""

    def test_zero_frictions_leaves_price_unchanged(self):
        f = Frictions()
        assert f.fill_price(100.0, "BUY") == 100.0
        assert f.fill_price(100.0, "SELL") == 100.0

    def test_slippage_moves_buy_up_and_sell_down(self):
        # 100 bps = 1%. Both directions cost money -- that's the point.
        f = Frictions(slippage_bps=100.0)
        assert abs(f.fill_price(100.0, "BUY") - 101.0) < 1e-9
        assert abs(f.fill_price(100.0, "SELL") - 99.0) < 1e-9

    def test_zero_frictions_charges_no_commission(self):
        # Both commission fields at their 0.0 default -> never charge the
        # commission_min floor for doing nothing.
        assert Frictions().commission(1) == 0.0
        assert Frictions().commission(1000) == 0.0

    def test_commission_floor_applies_below_minimum(self):
        f = Frictions(commission_per_share=0.005, commission_min=1.00)
        # 3 shares * $0.005 = $0.015, well under the $1.00 minimum.
        assert f.commission(3) == 1.00

    def test_commission_per_share_applies_above_minimum(self):
        f = Frictions(commission_per_share=0.005, commission_min=1.00)
        # 1000 shares * $0.005 = $5.00, above the minimum.
        assert f.commission(1000) == 5.00


class TestFrictionsInBacktest:
    """Frictions wired into run_backtest: slippage adjusts the recorded fill
    prices, commission nets out of realized pnl, and the round trip's total
    matches Frictions.commission() called directly -- no run needs frictions
    at all by default (every test above this class proves that already)."""

    # Same fixture as TestSellFlow.test_sell_signal_closes_position_at_next_day_open:
    # breakout signal day 20 -> fill (BUY) day 21 open=101.50; breakdown signal
    # day 41 -> fill (SELL) day 42 open=89.00.
    _closes = [99.5] * 20 + [101.0] + [101.5] * 20 + [90.0] + [89.0]
    _highs = [100.0] * 20 + [101.5] + [102.0] * 20 + [91.0] + [89.5]
    _lows = [99.0] * 20 + [100.5] + [101.0] * 20 + [89.0] + [88.0]
    _opens = [99.5] * 20 + [101.0] + [101.5] * 20 + [90.0] + [89.0]

    def _round_trip(self, frictions: Frictions | None) -> BacktestResult:
        df = _bars(self._closes, self._highs, self._lows, self._opens)
        return run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2030, 1, 1),
            bars={"X": df},
            starting_equity=10_000.0,
            pct_per_position=0.04,
            frictions=frictions,
        )

    def test_slippage_adjusts_recorded_entry_and_exit_prices(self):
        result = self._round_trip(Frictions(slippage_bps=100.0))  # 1%
        trade = result.trades[0]
        # BUY fills 1% above the raw open (101.50); SELL fills 1% below (89.00).
        assert abs(trade.entry_price - 101.5 * 1.01) < 1e-6
        assert abs(trade.exit_price - 89.0 * 0.99) < 1e-6

    def test_commission_reduces_pnl_by_exactly_the_round_trip_cost(self):
        frictions = Frictions(commission_per_share=0.005, commission_min=1.00)
        free = self._round_trip(None)
        costed = self._round_trip(frictions)
        # Same fills either way (commission doesn't move the fill price or
        # change the qty here), so the only difference is the two $1.00
        # per-order minimums (qty=3 keeps per-share well under the floor).
        entry_commission = frictions.commission(free.trades[0].qty)
        exit_commission = frictions.commission(free.trades[0].qty)
        assert entry_commission == 1.00
        assert exit_commission == 1.00
        assert abs(
            (free.trades[0].pnl - costed.trades[0].pnl) - (entry_commission + exit_commission)
        ) < 1e-6
        assert abs(costed.total_commission - (entry_commission + exit_commission)) < 1e-6

    def test_no_frictions_arg_matches_explicit_no_friction(self):
        # frictions=None (the default) must be indistinguishable from an
        # explicit all-zero Frictions().
        omitted = self._round_trip(None)
        explicit_zero = self._round_trip(Frictions())
        assert omitted.trades[0].pnl == explicit_zero.trades[0].pnl
        assert omitted.total_commission == explicit_zero.total_commission == 0.0


class TestMembershipFiltering:
    """C-2, PROJECT_EVALUATION.md: an optional `membership` timeline restricts
    *entries* to real point-in-time S&P 500 members. Omitting it (every test
    above this class) must keep the old unfiltered behavior exactly."""

    def test_entry_blocked_when_never_a_member(self):
        # Same breakout fixture as TestBuyFlow.test_buy_fills_at_next_day_open,
        # but X is never in the index -- membership.at() is always empty.
        never_member = build_membership_timeline(current_members=[], changes=[])
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": _flat_then_breakout_bars()},
            starting_equity=10_000.0,
            pct_per_position=0.04,
            max_positions=25,
            membership=never_member,
        )
        assert result.open_positions_at_end == 0
        assert result.trades == []

    def test_entry_allowed_when_always_a_member(self):
        # Sanity check: an always-member timeline reproduces the unfiltered
        # result exactly, so the filter isn't accidentally suppressing everyone.
        always_member = build_membership_timeline(current_members=["X"], changes=[])
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": _flat_then_breakout_bars()},
            starting_equity=10_000.0,
            pct_per_position=0.04,
            max_positions=25,
            membership=always_member,
        )
        assert result.open_positions_at_end == 1

    def test_held_position_still_exits_after_dropping_out_of_index(self):
        # X is a member when the BUY fires (2024-01-21), then leaves the
        # index on 2024-02-01 -- well before the SELL signal on 2024-02-11.
        # The exit must fire anyway: exits iterate held positions, not the
        # membership-filtered universe, so a position already open doesn't
        # get trapped by a later index removal.
        closes = (
            [99.5] * 20 + [101.0] + [101.5] * 20 + [90.0] + [89.0]
        )
        highs = (
            [100.0] * 20 + [101.5] + [102.0] * 20 + [91.0] + [89.5]
        )
        lows = (
            [99.0] * 20 + [100.5] + [101.0] * 20 + [89.0] + [88.0]
        )
        opens = (
            [99.5] * 20 + [101.0] + [101.5] * 20 + [90.0] + [89.0]
        )
        df = _bars(closes, highs, lows, opens)
        drops_out = build_membership_timeline(
            current_members=[],
            changes=[MembershipChange(date(2024, 2, 1), added=None, removed="X")],
        )

        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2030, 1, 1),
            bars={"X": df},
            starting_equity=10_000.0,
            pct_per_position=0.04,
            membership=drops_out,
        )
        assert len(result.trades) == 1
        assert result.trades[0].symbol == "X"
        assert result.open_positions_at_end == 0


class TestCapAndDedup:
    def test_position_cap_respected(self):
        # 30 distinct symbols all breakout on the same day; cap=5 -> only 5 fill.
        bar_template = _flat_then_breakout_bars()
        bars = {f"S{i}": bar_template.copy() for i in range(30)}
        universe = list(bars.keys())

        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=universe,
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars=bars,
            starting_equity=1_000_000.0,
            pct_per_position=0.04,
            max_positions=5,
        )
        # Exactly 5 positions opened (cap).
        assert result.open_positions_at_end == 5

    def test_held_ticker_not_re_bought(self):
        # Same symbol with two breakouts: first opens a position, second is
        # ignored because we already hold it.
        # Build: breakout day 20, hold, then "another breakout" day 41 still
        # holding -> should NOT re-buy.
        closes = [99.5] * 20 + [101.0] + [101.0] * 25  # held throughout
        df = _bars(closes)

        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": df},
            starting_equity=100_000.0,
            pct_per_position=0.04,
        )
        # Exactly one position; no second BUY despite continued breakout.
        assert result.open_positions_at_end == 1


class TestForceTrimParity:
    """Enhancement #1 -- backtest's force-trim helper matches production
    semantics (lowest unrealized $ P&L first, exclude already-exiting symbols).

    The tests below `test_normal_backtest_run_has_zero_force_trims` exercise
    only `_backtest_trim_candidates` in isolation -- they pin its own
    behavior but never checked it against `RiskManager.select_force_trim_
    candidates`, despite the class name and the module's own PARITY NOTE
    (`backtest/engine.py`) claiming the two are kept in sync (PROJECT_EVALUATION.md).
    `TestRealParityComparison` below closes that gap: one canonical scenario,
    fed to both implementations, asserting identical output.
    """

    def test_under_cap_returns_empty(self):
        from vibe_trade.backtest.engine import _OpenPosition, _backtest_trim_candidates

        ts = pd.Timestamp("2024-06-01")
        # 3 positions, max=5 -> no trim.
        positions = {
            f"S{i}": _OpenPosition(symbol=f"S{i}", shares=10, avg_cost=100.0,
                                   entry_date=date(2024, 1, 1))
            for i in range(3)
        }
        bars = {
            f"S{i}": _bars([100.0] * 200, start="2024-01-01")
            for i in range(3)
        }
        result = _backtest_trim_candidates(
            positions, ts, bars, max_positions=5, already_exiting=set(),
        )
        assert result == []

    def test_over_cap_picks_lowest_dollar_pnl(self):
        from vibe_trade.backtest.engine import _OpenPosition, _backtest_trim_candidates

        ts = pd.Timestamp("2024-06-01")
        # 6 positions, max=4 -> trim 2 worst by current close - avg_cost.
        positions = {}
        bars = {}
        for i, (sym, avg_cost, cur_close) in enumerate([
            ("A", 100.0, 120.0),   # +$200 (winner)
            ("B", 100.0, 110.0),   # +$100
            ("C", 100.0, 100.0),   # $0
            ("D", 100.0, 90.0),    # -$100
            ("E", 100.0, 80.0),    # -$200  <- 2nd worst
            ("F", 100.0, 70.0),    # -$300  <- worst
        ]):
            positions[sym] = _OpenPosition(
                symbol=sym, shares=10, avg_cost=avg_cost,
                entry_date=date(2024, 1, 1),
            )
            # Synthesize bars where the price at `ts` is `cur_close`.
            n_days = 200
            close_series = [cur_close] * n_days
            bars[sym] = _bars(close_series, start="2024-01-01")

        result = _backtest_trim_candidates(
            positions, ts, bars, max_positions=4, already_exiting=set(),
        )
        # Two worst by unrealized $ P&L are F and E.
        assert set(result) == {"E", "F"}
        # And the ordering must be most-negative-first.
        assert result[0] == "F"
        assert result[1] == "E"

    def test_already_exiting_excluded_and_reduces_count(self):
        from vibe_trade.backtest.engine import _OpenPosition, _backtest_trim_candidates

        ts = pd.Timestamp("2024-06-01")
        positions = {}
        bars = {}
        # 6 losers ranked -100, -200, ..., -600 ; max=4
        for i in range(6):
            sym = f"S{i}"
            close = 100.0 - (i + 1) * 10  # i=0 -> 90, i=5 -> 40
            positions[sym] = _OpenPosition(
                symbol=sym, shares=10, avg_cost=100.0,
                entry_date=date(2024, 1, 1),
            )
            bars[sym] = _bars([close] * 200, start="2024-01-01")
        # Donchian "decided" to exit the 2 worst (S5, S4).
        result = _backtest_trim_candidates(
            positions, ts, bars,
            max_positions=4, already_exiting={"S5", "S4"},
        )
        # held_after_exits = 6 - 2 = 4 = max -> no force-trim needed.
        assert result == []

    def test_normal_backtest_run_has_zero_force_trims(self):
        """Smoke test: a normal backtest (single symbol, single breakout)
        should never trigger force-trim because the sizer cap prevents
        over-cap in the first place."""
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": _flat_then_breakout_bars()},
            starting_equity=100_000.0,
            pct_per_position=0.04,
            max_positions=5,
        )
        assert result.force_trim_sells == 0


def _scenario_to_both(
    scenario: dict[str, tuple[float, float, int]],
    ts: pd.Timestamp = pd.Timestamp("2024-06-01"),
):
    """Build equivalent inputs for both trim implementations from ONE
    canonical scenario, so a parity test can't accidentally compare two
    subtly-different setups. `scenario` maps symbol -> (avg_cost, current
    price, shares); insertion order is preserved into both implementations'
    inputs, since tie-breaking depends on it.

    Returns (backtest_positions, backtest_bars, production_positions).
    """
    from vibe_trade.backtest.engine import _OpenPosition
    from vibe_trade.broker.models import Position

    bt_positions: dict[str, _OpenPosition] = {}
    bars: dict[str, pd.DataFrame] = {}
    prod_positions: list[Position] = []
    for sym, (avg_cost, price, shares) in scenario.items():
        bt_positions[sym] = _OpenPosition(
            symbol=sym, shares=shares, avg_cost=avg_cost, entry_date=date(2024, 1, 1),
        )
        bars[sym] = _bars([price] * 200, start="2024-01-01")
        unrealized = (price - avg_cost) * shares
        prod_positions.append(
            Position(
                symbol=sym, quantity=shares, avg_cost=avg_cost, market_price=price,
                market_value=price * shares, unrealized_pnl=unrealized,
            )
        )
    return bt_positions, bars, prod_positions


class TestRealParityComparison:
    """The audit's exact complaint about `TestForceTrimParity`: not one test
    invoked `RiskManager.select_force_trim_candidates` for comparison. Each
    test here builds one scenario via `_scenario_to_both` and runs it through
    BOTH implementations, asserting identical output -- content and order.
    """

    def test_mixed_winners_and_losers_pick_same_symbols_same_order(self):
        from vibe_trade.backtest.engine import _backtest_trim_candidates
        from vibe_trade.risk.manager import RiskManager

        scenario = {
            "A": (100.0, 120.0, 10),   # +$200 (winner)
            "B": (100.0, 110.0, 10),   # +$100
            "C": (100.0, 100.0, 10),   # $0
            "D": (100.0, 90.0, 10),    # -$100
            "E": (100.0, 80.0, 10),    # -$200
            "F": (100.0, 70.0, 10),    # -$300  (worst)
        }
        bt_positions, bars, prod_positions = _scenario_to_both(scenario)
        ts = pd.Timestamp("2024-06-01")

        bt_result = _backtest_trim_candidates(
            bt_positions, ts, bars, max_positions=4, already_exiting=set(),
        )
        prod_result = RiskManager.select_force_trim_candidates(
            prod_positions, max_positions=4, already_exiting=set(),
        )

        assert bt_result == prod_result == ["F", "E"]

    def test_already_exiting_exclusion_matches(self):
        """Mirrors the PARITY NOTE's actual warning: force-trim must see the
        post-exits held count, not the raw count, identically in both
        implementations.
        """
        from vibe_trade.backtest.engine import _backtest_trim_candidates
        from vibe_trade.risk.manager import RiskManager

        # 6 positions ranked -10..-60; strategy already decided to exit the
        # 2 worst (S5, S4) -- held_after_exits = 4 = max -> no trim in either.
        scenario = {
            f"S{i}": (100.0, 100.0 - (i + 1) * 10, 10) for i in range(6)
        }
        bt_positions, bars, prod_positions = _scenario_to_both(scenario)
        ts = pd.Timestamp("2024-06-01")
        already_exiting = {"S5", "S4"}

        bt_result = _backtest_trim_candidates(
            bt_positions, ts, bars, max_positions=4, already_exiting=already_exiting,
        )
        prod_result = RiskManager.select_force_trim_candidates(
            prod_positions, max_positions=4, already_exiting=already_exiting,
        )

        assert bt_result == prod_result == []

    def test_partial_trim_after_exits_matches(self):
        """Exits reduce the count, but not enough -- both implementations
        must queue the same remaining worst performer(s)."""
        from vibe_trade.backtest.engine import _backtest_trim_candidates
        from vibe_trade.risk.manager import RiskManager

        # 6 positions ranked -10..-60; strategy exits only S5 (worst) ->
        # held_after_exits = 5, max = 4 -> trim 1 more: the next-worst, S4.
        scenario = {
            f"S{i}": (100.0, 100.0 - (i + 1) * 10, 10) for i in range(6)
        }
        bt_positions, bars, prod_positions = _scenario_to_both(scenario)
        ts = pd.Timestamp("2024-06-01")
        already_exiting = {"S5"}

        bt_result = _backtest_trim_candidates(
            bt_positions, ts, bars, max_positions=4, already_exiting=already_exiting,
        )
        prod_result = RiskManager.select_force_trim_candidates(
            prod_positions, max_positions=4, already_exiting=already_exiting,
        )

        assert bt_result == prod_result == ["S4"]

    def test_tie_break_order_matches(self):
        """Equal unrealized $ P&L across positions -- both implementations
        sort with Python's stable sort, so ties must resolve in the same
        (insertion) order in both, or an operator reconciling backtest
        against a live tie-day would see different symbols picked.
        """
        from vibe_trade.backtest.engine import _backtest_trim_candidates
        from vibe_trade.risk.manager import RiskManager

        # All five positions tied at -$100; max=2 -> trim 3, insertion order
        # A, B, C, D, E must decide the tie identically in both.
        scenario = {sym: (100.0, 90.0, 10) for sym in ["A", "B", "C", "D", "E"]}
        bt_positions, bars, prod_positions = _scenario_to_both(scenario)
        ts = pd.Timestamp("2024-06-01")

        bt_result = _backtest_trim_candidates(
            bt_positions, ts, bars, max_positions=2, already_exiting=set(),
        )
        prod_result = RiskManager.select_force_trim_candidates(
            prod_positions, max_positions=2, already_exiting=set(),
        )

        assert bt_result == prod_result == ["A", "B", "C"]

    def test_under_cap_both_return_empty(self):
        from vibe_trade.backtest.engine import _backtest_trim_candidates
        from vibe_trade.risk.manager import RiskManager

        scenario = {f"S{i}": (100.0, 100.0, 10) for i in range(3)}
        bt_positions, bars, prod_positions = _scenario_to_both(scenario)
        ts = pd.Timestamp("2024-06-01")

        bt_result = _backtest_trim_candidates(
            bt_positions, ts, bars, max_positions=5, already_exiting=set(),
        )
        prod_result = RiskManager.select_force_trim_candidates(
            prod_positions, max_positions=5, already_exiting=set(),
        )

        assert bt_result == prod_result == []


class TestEquityCurve:
    def test_equity_curve_has_one_point_per_trading_day(self):
        df = _bars([100.0] * 50)
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": df},
        )
        assert len(result.equity_curve) == 50

    def test_equity_curve_index_is_datetime(self):
        df = _bars([100.0] * 30)
        result = run_backtest(
            strategy=DonchianStrategy(),
            universe=["X"],
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            bars={"X": df},
        )
        assert isinstance(result.equity_curve.index, pd.DatetimeIndex)
