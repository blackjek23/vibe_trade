"""Scheduler — runs scan cycles at configured intervals."""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from vibe_trade.config import AppConfig, SchedulerConfig
from vibe_trade.notify.base import BaseNotifier
from vibe_trade.scanner import run_scan_cycle
from vibe_trade.strategy.base import BaseStrategy

logger = logging.getLogger(__name__)

DAY_MAP = {
    "mon": "0", "tue": "1", "wed": "2", "thu": "3", "fri": "4", "sat": "5", "sun": "6",
}


def _run_scan(config: AppConfig, strategies: list[BaseStrategy], notifier: BaseNotifier) -> None:
    """Wrapper to run async scan cycle from sync scheduler."""
    asyncio.run(run_scan_cycle(config, strategies, notifier))


def start_scheduler(
    config: AppConfig,
    strategies: list[BaseStrategy],
    notifier: BaseNotifier,
) -> None:
    """Start the blocking scheduler."""
    sched_config = config.scheduler

    # Build cron day_of_week string
    days = ",".join(DAY_MAP.get(d.lower(), d) for d in sched_config.trading_days)

    # Parse market hours
    open_h, open_m = sched_config.market_open.split(":")
    close_h, close_m = sched_config.market_close.split(":")

    scheduler = BlockingScheduler(timezone=sched_config.timezone)

    trigger = CronTrigger(
        day_of_week=days,
        hour=f"{open_h}-{close_h}",
        minute=f"*/{sched_config.interval_minutes}" if sched_config.interval_minutes < 60 else open_m,
        timezone=sched_config.timezone,
    )

    scheduler.add_job(
        _run_scan,
        trigger=trigger,
        args=[config, strategies, notifier],
        id="scan_cycle",
        name="Vibe Trade Scan Cycle",
        misfire_grace_time=300,
    )

    logger.info(
        f"Scheduler started: every {sched_config.interval_minutes}min, "
        f"{sched_config.market_open}-{sched_config.market_close} ET, "
        f"days={sched_config.trading_days}"
    )
    scheduler.start()
