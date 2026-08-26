"""Tests for vibe_trade.backtest.membership — point-in-time S&P 500 membership.

All tests use small hand-built Wikipedia-shaped HTML fixtures. No live network
calls, ever (project convention) — `_fetch_wikipedia_html`/
`_fetch_wikipedia_revision_html` are intentionally never exercised here.
"""

from __future__ import annotations

from datetime import date

from vibe_trade.backtest.membership import (
    MembershipChange,
    build_membership_timeline,
    generate_artifact_source,
    parse_added_dates,
    parse_changes,
    parse_current_members,
    point_in_time_members,
    recent_additions_since,
)

_CONSTITUENTS_HTML = """
<table id="constituents">
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>
    <th>Headquarters</th><th>Date added</th><th>CIK</th><th>Founded</th></tr>
<tr><td>AAPL</td><td>Apple Inc.</td><td>Tech</td><td>Hardware</td>
    <td>Cupertino, CA</td><td>1982-11-30</td><td>320193</td><td>1976</td></tr>
<tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td><td>Insurance</td>
    <td>Omaha, NE</td><td>2010-02-16</td><td>1067983</td><td>1839</td></tr>
<tr><td>RDDT</td><td>Reddit Inc.</td><td>Comm Services</td><td>Internet</td>
    <td>San Francisco, CA</td><td>2026-08-18</td><td>1713445</td><td>2005</td></tr>
</table>
"""


class TestParseCurrentMembers:
    def test_parses_symbols_in_order(self):
        members = parse_current_members(_CONSTITUENTS_HTML)
        assert members == ["AAPL", "BRK-B", "RDDT"]

    def test_normalizes_dotted_tickers(self):
        # BRK.B -> BRK-B via normalize_symbol, matching yfinance-compatible convention.
        members = parse_current_members(_CONSTITUENTS_HTML)
        assert "BRK-B" in members
        assert "BRK.B" not in members

    def test_missing_table_raises(self):
        try:
            parse_current_members("<html><body>no table here</body></html>")
        except ValueError as exc:
            assert "constituents" in str(exc)
        else:
            raise AssertionError("expected ValueError for missing constituents table")


# 2-row header (date / added colspan2 / removed colspan2 / reason, then the
# ticker/security sub-header) -- parse_changes skips the first 2 rows.
_CHANGES_HTML = """
<table id="changes">
<tr><th>Date</th><th colspan="2">Added</th><th colspan="2">Removed</th><th>Reason</th></tr>
<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th><th></th></tr>
<tr><td>March 1, 2024</td><td>SMCI</td><td>Super Micro Computer</td>
    <td>WBA</td><td>Walgreens Boots Alliance</td><td>Market cap change</td></tr>
<tr><td>June 22, 2020</td><td>TSLA</td><td>Tesla Inc.</td>
    <td></td><td></td><td>Market cap change</td></tr>
<tr><td>September 23, 2020</td><td></td><td></td>
    <td>ETFC</td><td>E*Trade Financial</td><td>Acquired</td></tr>
</table>
"""


class TestParseChanges:
    def test_same_row_swap(self):
        changes = parse_changes(_CHANGES_HTML)
        swap = next(c for c in changes if c.effective_date == date(2024, 3, 1))
        assert swap.added == "SMCI"
        assert swap.removed == "WBA"

    def test_add_only_row(self):
        changes = parse_changes(_CHANGES_HTML)
        add_only = next(c for c in changes if c.effective_date == date(2020, 6, 22))
        assert add_only.added == "TSLA"
        assert add_only.removed is None

    def test_remove_only_row(self):
        changes = parse_changes(_CHANGES_HTML)
        remove_only = next(c for c in changes if c.effective_date == date(2020, 9, 23))
        assert remove_only.added is None
        assert remove_only.removed == "ETFC"

    def test_parses_all_rows(self):
        assert len(parse_changes(_CHANGES_HTML)) == 3

    def test_missing_table_raises(self):
        try:
            parse_changes("<html><body>no table here</body></html>")
        except ValueError as exc:
            assert "changes" in str(exc)
        else:
            raise AssertionError("expected ValueError for missing changes table")


class TestParseAddedDates:
    def test_parses_date_added_column(self):
        added = parse_added_dates(_CONSTITUENTS_HTML)
        assert added == {
            "AAPL": date(1982, 11, 30),
            "BRK-B": date(2010, 2, 16),
            "RDDT": date(2026, 8, 18),
        }

    def test_missing_table_raises(self):
        try:
            parse_added_dates("<html><body>no table here</body></html>")
        except ValueError as exc:
            assert "constituents" in str(exc)
        else:
            raise AssertionError("expected ValueError for missing constituents table")


class TestRecentAdditionsSince:
    def test_only_symbols_added_after_cutoff(self):
        added_dates = {
            "AAPL": date(1982, 11, 30),
            "BRK-B": date(2010, 2, 16),
            "RDDT": date(2026, 8, 18),
        }
        changes = recent_additions_since(added_dates, since=date(2026, 8, 8))
        assert changes == [MembershipChange(date(2026, 8, 18), "RDDT", None)]

    def test_empty_when_nothing_after_cutoff(self):
        added_dates = {"AAPL": date(1982, 11, 30)}
        assert recent_additions_since(added_dates, since=date(2026, 8, 8)) == []


# Shared scenario for TestPointInTimeMembers: current members are AAPL + SMCI,
# reached via a same-row swap (SMCI in / WBA out), an add-only event (TSLA,
# never removed since -- so it's NOT in current_members, meaning it must have
# been removed later by an event not modeled here; kept simple: TSLA stays
# out of current_members entirely to isolate the add-only undo path), and a
# remove-only event (ETFC out).
_CURRENT = ["AAPL", "SMCI"]
_CHANGES = [
    MembershipChange(date(2024, 3, 1), "SMCI", "WBA"),
    MembershipChange(date(2020, 6, 22), "TSLA", None),
    MembershipChange(date(2020, 9, 23), None, "ETFC"),
]


class TestPointInTimeMembers:
    def test_as_of_today_returns_current_members_unchanged(self):
        assert point_in_time_members(date(2025, 1, 1), _CURRENT, _CHANGES) == frozenset(_CURRENT)

    def test_as_of_exactly_a_change_date_is_already_effective(self):
        # effective_date <= as_of is treated as already applied -- exactly on
        # the swap's date, membership is the post-swap (i.e. current) set.
        assert point_in_time_members(date(2024, 3, 1), _CURRENT, _CHANGES) == frozenset(_CURRENT)

    def test_undoes_same_row_swap(self):
        # One day before the swap: SMCI wasn't in yet, WBA hadn't left yet.
        members = point_in_time_members(date(2024, 2, 29), _CURRENT, _CHANGES)
        assert members == frozenset({"AAPL", "WBA"})

    def test_undoes_remove_only_event(self):
        # Before ETFC's removal: it should still be a member.
        members = point_in_time_members(date(2020, 9, 22), _CURRENT, _CHANGES)
        assert "ETFC" in members

    def test_undoes_add_only_event(self):
        # Before TSLA's addition: TSLA isn't a member yet (was never in
        # current_members anyway, so this exercises the no-op discard path).
        members = point_in_time_members(date(2020, 6, 21), _CURRENT, _CHANGES)
        assert "TSLA" not in members

    def test_walks_multiple_changes_backward(self):
        # Before all three events: every undo applies (SMCI/WBA swap reverted,
        # ETFC still in, TSLA still out).
        members = point_in_time_members(date(2018, 1, 1), _CURRENT, _CHANGES)
        assert members == frozenset({"AAPL", "WBA", "ETFC"})

    def test_before_earliest_recorded_change(self):
        # Documents the limitation rather than crashing: with no earlier
        # changes to consult, this is simply the same as the earliest known
        # reconstructed state.
        members = point_in_time_members(date.min, _CURRENT, _CHANGES)
        assert members == frozenset({"AAPL", "WBA", "ETFC"})


class TestMembershipTimeline:
    """The fast `.at()` path must never diverge from the slow, obviously-
    correct `point_in_time_members` -- checked at every breakpoint and at a
    mid-interval date, not just spot-checked once."""

    def test_matches_point_in_time_members_at_every_breakpoint(self):
        timeline = build_membership_timeline(_CURRENT, _CHANGES)
        for change in _CHANGES:
            expected = point_in_time_members(change.effective_date, _CURRENT, _CHANGES)
            assert timeline.at(change.effective_date) == expected

    def test_matches_point_in_time_members_mid_interval(self):
        timeline = build_membership_timeline(_CURRENT, _CHANGES)
        for as_of in (date(2018, 1, 1), date(2020, 7, 1), date(2021, 1, 1), date(2025, 1, 1)):
            assert timeline.at(as_of) == point_in_time_members(as_of, _CURRENT, _CHANGES)

    def test_before_earliest_breakpoint(self):
        timeline = build_membership_timeline(_CURRENT, _CHANGES)
        assert timeline.at(date.min) == point_in_time_members(date.min, _CURRENT, _CHANGES)


class TestGenerateArtifactSource:
    def test_output_is_valid_python(self):
        src = generate_artifact_source(_CURRENT, _CHANGES, last_updated="2026-08-26")
        compile(src, "<generated>", "exec")  # raises SyntaxError if malformed

    def test_round_trips_members_and_changes(self):
        src = generate_artifact_source(_CURRENT, _CHANGES, last_updated="2026-08-26")
        namespace: dict[str, object] = {}
        exec(compile(src, "<generated>", "exec"), namespace)  # noqa: S102 -- trusted, just-generated source
        assert namespace["LAST_UPDATED"] == "2026-08-26"
        assert sorted(namespace["CURRENT_MEMBERS"]) == sorted(_CURRENT)  # type: ignore[arg-type]
        round_tripped = {
            MembershipChange(date.fromisoformat(d), added, removed)
            for d, added, removed in namespace["CHANGES"]  # type: ignore[union-attr]
        }
        assert round_tripped == set(_CHANGES)

    def test_changes_sorted_descending_by_date(self):
        src = generate_artifact_source(_CURRENT, _CHANGES, last_updated="2026-08-26")
        namespace: dict[str, object] = {}
        exec(compile(src, "<generated>", "exec"), namespace)  # noqa: S102 -- trusted, just-generated source
        dates = [d for d, _added, _removed in namespace["CHANGES"]]  # type: ignore[union-attr]
        assert dates == sorted(dates, reverse=True)
