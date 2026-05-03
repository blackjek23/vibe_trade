"""Live notification check for the reconcile phase (the daily summary).

Reads today's opened trades, closed trades, and DailyPnL row from
`data/test_paper.db` (populated by `scratch_reconcile.py`), then sends the
full daily summary table via the configured notifier.

This is the most useful of the three notify scratches: it exercises the
monospace table formatting end-to-end against real trade data and shows
how the message renders on Telegram mobile.

Run with:
    .venv/Scripts/python scratch_notify_reconcile.py

Prerequisites:
    - `scratch_reconcile.py` has run earlier today, ideally after some
      open/close activity. With no trades today you'll get the
      "No trades today." fallback message -- which is also a valid test.
    - Telegram configured in config.toml (or ConsoleNotifier fallback)

Safety:
    - Refuses to run if config says mode = "live"
    - No IB connection (DB-only)
    - Read-only: no DB writes, no order placement
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

from vibe_trade.cli import _format_reconcile_msg, _get_notifier
from vibe_trade.config import load_config
from vibe_trade.db.engine import init_db
from vibe_trade.db.repository import DailyPnLRepository, TradeRepository
from vibe_trade.jobs.reconcile import ReconcileResult

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
        trade_repo = TradeRepository(session)
        daily_repo = DailyPnLRepository(session)

        opened_today = trade_repo.get_trades_opened_today(today)
        closed_today = trade_repo.get_trades_closed_today(today)
        daily_row = daily_repo.get_by_date(today)

        # Synthesize a ReconcileResult shaped from observed counts. The
        # production result tracks more nuanced state (cancelled, snapshot
        # rows, etc.) but those aren't recoverable from DB state alone --
        # the summary message only needs opened/closed counts + errors.
        result = ReconcileResult(
            opened=len(opened_today),
            closed=len(closed_today),
        )

        print(
            f"loaded: {len(opened_today)} opened, {len(closed_today)} closed, "
            f"DailyPnL row={'yes' if daily_row else 'no'}"
        )

        msg = _format_reconcile_msg(
            result, opened_today, closed_today, daily_row, today,
        )
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
