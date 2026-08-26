"""US equity market calendar check.

Cron fires `* * 1-5` (deploy/crontab.example) with no holiday awareness --
roughly nine US market holidays a year fall on a weekday. On one of those days
submit would evaluate the prior close and place BUY market orders; IB holds
them overnight as working day orders, and the *following* session's
double-run guard then finds those still-working strategy orderRefs and aborts
the entire next day, exits included (H-4, PROJECT_EVALUATION.md).

This module answers one question -- "is today (US/Eastern) a NYSE trading
day" -- so `run_submit` and `run_preflight` can skip cleanly on a holiday
instead of firing wrongly and discovering the damage a day later via the
double-run guard's side effect.

Backed by `pandas_market_calendars`, not a hand-maintained date list: the
project has already been bitten by hand-synced data going stale elsewhere
(see the file-inventory drift noted in PROJECT_EVALUATION.md's Documentation
Accuracy section), and a hardcoded holiday list is exactly that failure mode
waiting to happen again next January.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

US_EASTERN = ZoneInfo("America/New_York")

_NYSE = mcal.get_calendar("NYSE")


def today_us_eastern(now: datetime | None = None) -> date:
    """Today's calendar date in US/Eastern.

    `now`, if given, must be timezone-aware -- a naive datetime's
    `.astimezone()` silently assumes the host's local timezone, which is
    exactly the DST-arithmetic footgun this module exists to close (see also
    `jobs/submit._last_bar_is_closed`, H-1).
    """
    if now is not None and now.tzinfo is None:
        raise ValueError("today_us_eastern: now must be timezone-aware")
    return (now or datetime.now(US_EASTERN)).astimezone(US_EASTERN).date()


def is_us_trading_day(day: date) -> bool:
    """True if `day` is a NYSE trading session (not a weekend or holiday)."""
    return not _NYSE.schedule(start_date=day, end_date=day).empty
