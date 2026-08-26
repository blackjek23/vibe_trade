"""Point-in-time S&P 500 index membership.

C-2, PROJECT_EVALUATION.md: the backtest's previous universe
(`data/sp100_top.py:SP100_TOP_BY_MCAP`) was a single snapshot of *today's*
top-100-by-market-cap names, applied unchanged across the whole 2018-2026
backtest window -- ranking by current market cap selects precisely the
companies that happened to grow into today's leaders. This module answers
"who was actually in the S&P 500 on date X" instead, scraped from Wikipedia's
"List of S&P 500 companies" page (free; no vendor purchase) and reconstructed
by walking the page's own dated additions/removals table backward from the
current member list.

Two known, deliberate limitations, not oversights -- see PROJECT_EVALUATION.md
and the CLAUDE.md/PROJECT_MASTER_STATE.md write-up once this ships:
- Wikipedia's changes table is community-maintained, not an authoritative
  vendor feed. Reasonably reliable, not guaranteed complete for the earliest
  years of the backtest window -- checked during implementation, not assumed.
- yfinance may not serve historical bars for some names that have been fully
  delisted from its own archive since; this module only reconstructs *index
  membership*, it says nothing about *data availability* for a given name.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime

from vibe_trade.data.universe import normalize_symbol

SOURCE_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Wikipedia's "Selected changes" table (Effective Date / Added / Removed /
# Reason -- the only source of *removal* dates) was dropped from the live
# page between 2026-08-08 and 2026-08-13, confirmed by walking the article's
# revision history during implementation. This is the last revision known to
# still have it. Additions since this date are recovered separately from
# each current member's own "Date added" column on the live page (see the
# CLI refresh command); removals since this date are a small, known gap --
# re-pinning to a newer revision (if the table reappears) or re-deriving
# this constant closes it.
CHANGES_TABLE_FALLBACK_REVID = 1368287955  # 2026-08-08T04:50:43Z


@dataclass(frozen=True)
class MembershipChange:
    """One dated addition/removal event from Wikipedia's constituent-changes
    table. A single table row can carry either or both sides (a straight
    swap on the same date), which is why `added`/`removed` are independent
    optionals rather than a single symbol + direction.
    """

    effective_date: date
    added: str | None
    removed: str | None


def parse_current_members(html: str) -> list[str]:
    """Parse the live page's "constituents" table (`id="constituents"`) into
    a normalized ticker list. Selects the table by id, not position, since
    that's a more stable contract than "the first table on the page."
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="constituents")
    if table is None:
        raise ValueError(
            "no table with id='constituents' found -- Wikipedia page structure "
            "changed since this parser was written"
        )
    members = []
    for row in table.find_all("tr")[1:]:  # skip the single header row
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        symbol = cells[0].get_text(strip=True)
        if symbol:
            members.append(normalize_symbol(symbol))
    return members


def parse_changes(html: str) -> list[MembershipChange]:
    """Parse the "changes" table (`id="changes"`: Effective Date / Added
    Ticker+Security / Removed Ticker+Security / Reason -- a 2-row header,
    6 cells per data row) into `MembershipChange` events.

    This table no longer exists on the *live* page (see
    `CHANGES_TABLE_FALLBACK_REVID`'s comment) -- `html` here is expected to
    come from that pinned older revision, not the live page `parse_current_members`
    reads. An empty or blank ticker cell means that side of the row doesn't
    apply (an add-only or remove-only event).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="changes")
    if table is None:
        raise ValueError(
            "no table with id='changes' found -- re-check CHANGES_TABLE_FALLBACK_REVID "
            "still points at a revision that has it"
        )
    changes: list[MembershipChange] = []
    for row in table.find_all("tr")[2:]:  # skip the two header rows
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        date_text, added_ticker, _added_security, removed_ticker, _removed_security = cells[:5]
        effective_date = datetime.strptime(date_text, "%B %d, %Y").date()
        added = normalize_symbol(added_ticker) if added_ticker else None
        removed = normalize_symbol(removed_ticker) if removed_ticker else None
        if added is None and removed is None:
            continue
        changes.append(MembershipChange(effective_date, added, removed))
    return changes


def parse_added_dates(html: str) -> dict[str, date]:
    """Parse the live page's "constituents" table "Date added" column
    (`YYYY-MM-DD` per symbol) into `{symbol: date_added}`.

    Feeds `recent_additions_since` to backfill additions the changes table's
    cutoff missed (see `CHANGES_TABLE_FALLBACK_REVID`). Symbols with a blank
    or unparseable date are silently skipped -- this column is only used to
    recover *recent* additions, so a handful of untraceable decades-old
    dates (there are none as of the version checked during implementation,
    but the format isn't a guaranteed contract) costs nothing.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="constituents")
    if table is None:
        raise ValueError(
            "no table with id='constituents' found -- Wikipedia page structure "
            "changed since this parser was written"
        )
    added_dates: dict[str, date] = {}
    for row in table.find_all("tr")[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 6:
            continue
        symbol = cells[0].get_text(strip=True)
        date_text = cells[5].get_text(strip=True)
        if not symbol or not date_text:
            continue
        try:
            added_dates[normalize_symbol(symbol)] = date.fromisoformat(date_text)
        except ValueError:
            continue
    return added_dates


def recent_additions_since(added_dates: dict[str, date], since: date) -> list[MembershipChange]:
    """Synthesize `MembershipChange` add-events for every symbol whose
    "Date added" is strictly after `since` -- recovers additions the changes
    table's cutoff (`CHANGES_TABLE_FALLBACK_REVID`) missed. Pure, no I/O.

    Deliberately one-sided: there is no equivalent source for *removals*
    after `since` (a company no longer in `added_dates` at all, having left
    the live page's constituents table entirely, carries no date -- that's
    the small, known gap documented on `CHANGES_TABLE_FALLBACK_REVID`).
    """
    return [
        MembershipChange(effective_date=added_on, added=symbol, removed=None)
        for symbol, added_on in added_dates.items()
        if added_on > since
    ]


_USER_AGENT = "vibe_trade-backtest-membership/1.0 (research script; see PROJECT_EVALUATION.md C-2)"


def _fetch_wikipedia_html(url: str = SOURCE_URL) -> str:
    """Fetch the *live* page's rendered HTML -- feeds `parse_current_members`
    and `parse_added_dates`. The changes table isn't on the live page
    anymore (see `CHANGES_TABLE_FALLBACK_REVID`); use
    `_fetch_wikipedia_revision_html` for that one instead.
    """
    import requests

    resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def _fetch_wikipedia_revision_html(revid: int = CHANGES_TABLE_FALLBACK_REVID) -> str:
    """Fetch one specific past revision's rendered HTML via the MediaWiki
    API -- feeds `parse_changes`, since the live page no longer has that
    table (see `CHANGES_TABLE_FALLBACK_REVID`).
    """
    import requests

    resp = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "parse", "oldid": revid, "format": "json", "prop": "text"},
        headers={"User-Agent": _USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["parse"]["text"]["*"]


def point_in_time_members(
    as_of: date,
    current_members: list[str],
    changes: list[MembershipChange],
) -> frozenset[str]:
    """Reconstruct S&P 500 membership as of `as_of`.

    Starts from `current_members` (today's list) and walks `changes`
    backward in descending date order, undoing every change strictly more
    recent than `as_of`: an addition after `as_of` means that symbol wasn't
    a member yet, so it's removed from the working set; a removal after
    `as_of` means the symbol was still a member, so it's added back. A
    change dated exactly `as_of` is treated as already having taken effect
    (i.e. `as_of` is "the day after" that change).

    Pure, no I/O -- `current_members`/`changes` are required here rather
    than defaulting to the generated `data.sp500_membership` module, so this
    stays trivially testable with a small hand-built fixture. The CLI/engine
    entry points wire in the real data explicitly.
    """
    members = set(current_members)
    for change in sorted(changes, key=lambda c: c.effective_date, reverse=True):
        if change.effective_date <= as_of:
            break
        if change.added is not None:
            members.discard(change.added)
        if change.removed is not None:
            members.add(change.removed)
    return frozenset(members)


@dataclass(frozen=True)
class MembershipTimeline:
    """Precomputed point-in-time membership for fast repeated lookups.

    `point_in_time_members` re-walks the full change list on every call --
    fine for a one-off query or a test, too slow for the ~2,000 lookups (one
    per trading day) a single backtest run needs. Built once via
    `build_membership_timeline`, `.at()` is an O(log n) bisect instead.
    """

    breakpoints: list[date]        # ascending, one per distinct change date
    values: list[frozenset[str]]   # values[bisect_right(breakpoints, as_of)] is the answer

    def at(self, as_of: date) -> frozenset[str]:
        """Membership as of `as_of`. Same semantics as `point_in_time_members`."""
        return self.values[bisect_right(self.breakpoints, as_of)]


def build_membership_timeline(
    current_members: list[str],
    changes: list[MembershipChange],
) -> MembershipTimeline:
    """Precompute a `MembershipTimeline` from the same inputs
    `point_in_time_members` takes.

    Every precomputed value is produced by calling `point_in_time_members`
    itself (once at each distinct change date, plus once for "before any
    recorded change" at `date.min`) -- the fast path is built from the slow,
    obviously-correct path, so the two can never silently diverge.
    """
    breakpoints = sorted({c.effective_date for c in changes})
    values = [point_in_time_members(date.min, current_members, changes)] + [
        point_in_time_members(bp, current_members, changes) for bp in breakpoints
    ]
    return MembershipTimeline(breakpoints=breakpoints, values=values)


def generate_artifact_source(
    current_members: list[str],
    changes: list[MembershipChange],
    *,
    last_updated: str,
) -> str:
    """Render the checked-in `data/sp500_membership.py` module source --
    pure string-building, no I/O, so this is independently testable from the
    fetch/parse steps that produce its inputs.

    Plain `(date, added, removed)` tuples for CHANGES rather than
    `MembershipChange` instances keep the generated file dependency-free and
    diff-friendly to review in git, matching `sp100_top.py`'s convention of
    a plain literal list.
    """
    changes_sorted = sorted(changes, key=lambda c: c.effective_date, reverse=True)
    members_lines = "".join(f'    "{s}",\n' for s in sorted(current_members))
    changes_lines = "".join(
        f'    ("{c.effective_date.isoformat()}", {c.added!r}, {c.removed!r}),\n'
        for c in changes_sorted
    )
    return (
        '"""Point-in-time S&P 500 index membership -- generated data, do not hand-edit.\n'
        "\n"
        "Generated by `vibe-trade refresh-sp500-membership`. Reconstructs which\n"
        "companies were actually in the S&P 500 on any given historical date, fixing\n"
        "the survivorship bias in the backtest's previous universe\n"
        '(`sp100_top.py`\'s "today\'s top 100 by market cap, applied unchanged across\n'
        "2018-2026\" -- see PROJECT_EVALUATION.md, finding C-2).\n"
        "\n"
        f"Source: {SOURCE_URL}\n"
        "CURRENT_MEMBERS comes from the live page. CHANGES comes from a pinned older\n"
        "revision (see backtest/membership.py:CHANGES_TABLE_FALLBACK_REVID) because\n"
        "the live page no longer carries the changes table, plus a small gap-fill for\n"
        "additions since that revision (recovered from each current member's own\n"
        '"Date added" column -- see backtest/membership.recent_additions_since).\n'
        "Removals since that revision are NOT recoverable from Wikipedia alone; this\n"
        "is a small, known, and documented gap, not an oversight.\n"
        "\n"
        "Refresh periodically (`vibe-trade refresh-sp500-membership`) to shrink that\n"
        "gap and pick up the index's ongoing turnover. Use\n"
        "`backtest.membership.load_default_timeline()` to query this data -- never\n"
        "index into CURRENT_MEMBERS/CHANGES directly.\n"
        '"""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        f'LAST_UPDATED: str = "{last_updated}"\n'
        f'SOURCE_URL: str = "{SOURCE_URL}"\n'
        "\n"
        "CURRENT_MEMBERS: list[str] = [\n"
        f"{members_lines}"
        "]\n"
        "\n"
        "# (effective_date, added, removed) -- descending by date.\n"
        "CHANGES: list[tuple[str, str | None, str | None]] = [\n"
        f"{changes_lines}"
        "]\n"
    )


def load_default_timeline() -> MembershipTimeline:
    """Build a `MembershipTimeline` from the generated `data.sp500_membership`
    module -- the entry point everything outside this module (the engine,
    the backtest CLI) should actually use day to day.

    Deferred import: `data.sp500_membership` doesn't exist until
    `vibe-trade refresh-sp500-membership` has been run at least once.
    """
    from vibe_trade.data.sp500_membership import CHANGES, CURRENT_MEMBERS

    changes = [
        MembershipChange(date.fromisoformat(d), added, removed)
        for d, added, removed in CHANGES
    ]
    return build_membership_timeline(CURRENT_MEMBERS, changes)
