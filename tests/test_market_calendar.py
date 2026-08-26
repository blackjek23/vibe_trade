"""Tests for `data/market_calendar.py` (H-4, PROJECT_EVALUATION.md).

Cron fires `* * 1-5` with no holiday awareness. On a US market holiday that
falls on a weekday (~9/year), submit would place BUY day-orders that sit
working overnight, and the *next* session's double-run guard then finds
those orderRefs still at IB and aborts the whole day -- exits included.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from vibe_trade.data.market_calendar import is_us_trading_day, today_us_eastern


class TestIsUsTradingDay:
    def test_ordinary_weekday_is_a_trading_day(self):
        assert is_us_trading_day(date(2026, 3, 9)) is True  # Monday

    def test_saturday_is_not_a_trading_day(self):
        assert is_us_trading_day(date(2026, 3, 7)) is False

    def test_sunday_is_not_a_trading_day(self):
        assert is_us_trading_day(date(2026, 3, 8)) is False

    def test_thanksgiving_2026_is_not_a_trading_day(self):
        """The exact H-4 failure scenario from PROJECT_EVALUATION.md."""
        assert is_us_trading_day(date(2026, 11, 26)) is False

    def test_day_after_thanksgiving_2026_is_a_trading_day(self):
        """Half day at IB, but still a session -- must not be excluded."""
        assert is_us_trading_day(date(2026, 11, 27)) is True

    def test_new_years_day_is_not_a_trading_day(self):
        assert is_us_trading_day(date(2026, 1, 1)) is False


class TestTodayUsEastern:
    def test_normal_offset_matches_local_date(self):
        # 16:00 IDT (UTC+2, both sides on standard time) -> 09:00 EST same day.
        now = datetime(2026, 3, 6, 16, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        assert today_us_eastern(now) == date(2026, 3, 6)

    def test_dst_mismatch_still_resolves_correctly(self):
        # US already in DST (from Mar 8), Israel not yet (until Mar 27) --
        # the 6-hour gap that causes H-1/H-4 -- but the *date* math itself
        # must still land on the correct US calendar day.
        now = datetime(2026, 3, 9, 16, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        assert today_us_eastern(now) == date(2026, 3, 9)

    def test_naive_now_is_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            today_us_eastern(datetime(2026, 3, 6, 16, 0))

    def test_defaults_to_real_time_when_omitted(self):
        # Just confirm it doesn't raise and returns a date in the right
        # ballpark -- not asserting an exact value against wall-clock time.
        result = today_us_eastern()
        assert isinstance(result, date)
