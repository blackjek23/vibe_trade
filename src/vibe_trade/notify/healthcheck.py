"""OPS-1 (PROJECT_EVALUATION.md): dead-man's-switch ping to an external
uptime-monitoring service (e.g. https://healthchecks.io), distinct from the
Telegram failure alerts in notify/telegram.py and notify/console.py.

A Telegram alert requires this process to actually run and successfully
reach Telegram to fire -- it can't cover the process never running at all
(host down, cron/systemd itself broken, network gone entirely). A hosted
dead-man's switch flips that: the monitoring service raises its own alarm
when an *expected* ping goes missing, so total silence becomes visible
without this process having to do anything itself.

Uses urllib (stdlib) rather than `requests` deliberately -- a plain GET ping
doesn't need a new dependency, and `uv.lock` can't be regenerated in every
environment this runs in (see PROJECT_MASTER_STATE.md).
"""

from __future__ import annotations

import logging
import urllib.request
from urllib.error import URLError

logger = logging.getLogger(__name__)


def ping_healthcheck(ping_url: str, *, timeout: float = 10.0) -> bool:
    """Best-effort GET to `ping_url`. Never raises -- a hiccup reaching the
    monitoring service must not itself fail the job it's meant to be
    watching. Returns True on a 2xx response; False (logged as a warning)
    on any failure, including a blank `ping_url` (nothing to ping).
    """
    if not ping_url:
        return False
    try:
        with urllib.request.urlopen(ping_url, timeout=timeout) as resp:  # noqa: S310 -- fixed operator-configured URL, not user input
            return 200 <= resp.status < 300
    except (URLError, OSError, ValueError) as exc:
        logger.warning("OPS-1 healthcheck ping to %s failed: %r", ping_url, exc)
        return False
