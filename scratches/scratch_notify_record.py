"""Live notification check for the record phase.

Reads today's record-related rows from `data/test_paper.db` (populated by
`scratch_orders_save.py`), builds a RecordResult-shaped summary, and sends
the formatted Telegram message via the configured notifier.

Run with:
    .venv/Scripts/python scratch_notify_record.py

Prerequisites:
    - `scratch_orders_save.py` has run earlier today (otherwise the DB has
      no SUBMITTED rows and you'll get a "0 BUYs / 0 SELLs" message)
    - Telegram configured in config.toml (or ConsoleNotifier fallback prints
      the message)

Safety:
    - Refuses to run if config says mode = "live"
    - No IB connection (DB-only), no client_id concerns
    - Read-only: no DB writes, no order placement
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime

from vibe_trade.cli import _format_record_msg, _get_notifier
from vibe_trade.config import load_config
from vibe_trade.db.engine import init_db
from vibe_trade.db.models import Trade
from vibe_trade.jobs.record import RecordResult

# scratch convention: separate test DB so prod data stays clean
SCRATCH_DB_PATH = "data/test_paper.db"


async def main() -> None:
    config = load_config()

    if config.general.mode != "paper":
        print(f"ERROR: config says mode={config.general.mode!r}; refusing to run against live.")
        sys.exit(1)

    notifier = _get_notifier(config)
    session_factory = init_db(SCRATCH_DB_PATH)
    session = session_factory()
    try:
        today = date.today()
        start = datetime.combine(today, datetime.min.time())
        end = datetime.combine(today, datetime.max.time())

        # BUYs the record job inserted today: SUBMITTED rows whose submitted_at is today.
        buys_today = (
            session.query(Trade)
            .filter(
                Trade.side == "BUY",
                Trade.submitted_at >= start,
                Trade.submitted_at <= end,
            )
            .count()
        )

        # SELL flips today: trades whose exit_submitted_at is today (PENDING_CLOSE
        # or already moved beyond if reconcile ran since).
        sells_today = (
            session.query(Trade)
            .filter(
                Trade.exit_submitted_at >= start,
                Trade.exit_submitted_at <= end,
            )
            .count()
        )

        result = RecordResult(
            buys_inserted=buys_today,
            sells_flipped=sells_today,
        )

        msg = _format_record_msg(result, today)
        print()
        print("--- formatted message ---")
        print(msg)
        print("--- sending ---")
        await notifier.notify_summary(msg)
        print("done.")
    finally:
        session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
