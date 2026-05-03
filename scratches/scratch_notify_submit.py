"""Live notification check for the submit phase.

Reads today's orders from IB paper (placed earlier via `scratch_place_order.py`
or by the real `submit` job), builds a SubmitResult-shaped summary, and sends
the formatted Telegram message via the configured notifier.

Run with:
    .venv/Scripts/python scratch_notify_submit.py

Prerequisites:
    - TWS or IB Gateway running on paper port 7497 with API enabled
    - config/config.toml with `mode = "paper"`
    - Some orders placed today (otherwise you'll get a "0 placed" message)
    - Telegram configured in config.toml (or you'll see the message printed
      via ConsoleNotifier instead)

Safety:
    - Refuses to run if config says mode = "live"
    - Uses client_id = 8 (notifier-dedicated, won't collide with cron jobs)
    - No DB writes, no order placement -- pure read + notify
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timezone

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.cli import _format_submit_msg, _get_notifier
from vibe_trade.config import load_config
from vibe_trade.jobs.submit import SubmitResult

NOTIFIER_CLIENT_ID = 8


async def main() -> None:
    config = load_config()

    if config.general.mode != "paper":
        print(f"ERROR: config says mode={config.general.mode!r}; refusing to run against live.")
        sys.exit(1)

    broker_config = config.broker.model_copy()
    broker_config.client_id = NOTIFIER_CLIENT_ID

    broker = IBBroker(broker_config, mode="paper")
    notifier = _get_notifier(config)

    print(
        f"Connecting to IB paper at {broker_config.host}:{broker_config.get_port('paper')} "
        f"(client_id={NOTIFIER_CLIENT_ID})..."
    )
    await broker.connect()
    try:
        # Give ib_async a beat to hydrate the trades cache after connect.
        await asyncio.sleep(1.0)

        # Filter today's orders only -- ib.trades() returns all orders the
        # client has seen, but only "today" matters for the submit summary.
        today = date.today()
        trades = list(broker.ib.trades())
        todays = [
            t for t in trades
            if t.log and t.log[0].time.astimezone(timezone.utc).date() == today
        ]

        buys = [t for t in todays if t.order.action == "BUY"]
        sells = [t for t in todays if t.order.action == "SELL"]

        # Synthesize a SubmitResult from what we observe. Submit's full result
        # tracks evaluated/signaled separately, but those aren't recoverable
        # from IB state -- we only see what was placed.
        result = SubmitResult(
            exits_evaluated=len(sells),
            exits_signaled=len(sells),
            exits_placed=len(sells),
            entries_evaluated=len(buys),
            entries_signaled=len(buys),
            entries_placed=len(buys),
        )

        msg = _format_submit_msg(result, today)
        print()
        print("--- formatted message ---")
        print(msg)
        print("--- sending ---")
        await notifier.notify_summary(msg)
        print("done.")
    finally:
        await broker.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
