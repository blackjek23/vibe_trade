"""Preflight job — runs at 15:50 Asia/Jerusalem, 10 minutes before submit.

Answers one question: **is everything in place for today's submit to work?**

Motivation: on 10 trading days between 2026-05 and 2026-07 no job ran at all,
because IB Gateway wasn't up. The crash-alert wrapper *did* fire a `[CRITICAL]`
Telegram alert each time — but at 16:00, when the trading window had already
opened and there was nothing to do about it. Preflight moves that discovery
10 minutes earlier, and reports success as well as failure so that *silence*
becomes the anomaly instead of just one more failure message.

Broker-agnostic like `run_submit` — pass anything satisfying `BaseBroker`, so the
whole thing is testable without IB. The CLI owns the connection lifecycle.

Deliberately read-only: no orders, no DB writes. Safe to run at any time, as
often as you like.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from vibe_trade.broker.base import BaseBroker
from vibe_trade.data.market_calendar import is_us_trading_day, today_us_eastern
from vibe_trade.jobs.submit import PAPER_ACCOUNT_PREFIXES
from vibe_trade.strategy.registry import BuiltStrategy

logger = logging.getLogger(__name__)

# A live IB connection that reports zero equity means we're talking to the API
# but the account isn't loaded yet (Gateway mid-login) — a real "not ready".
MIN_NET_LIQUIDATION: float = 1.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class PreflightResult:
    checks: list[Check] = field(default_factory=list)
    account_id: str = ""
    net_liquidation: float = 0.0
    held_count: int = 0
    universe_size: int = 0
    strategy_names: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail))


async def run_preflight(
    *,
    broker: BaseBroker,
    universe: list[str],
    strategies: list[BuiltStrategy],
    max_positions: int,
    mode: str = "paper",
    min_net_liquidation: float = MIN_NET_LIQUIDATION,
    now: datetime | None = None,
) -> PreflightResult:
    """Verify submit's preconditions. Never raises for a *failed check* — the
    failure is recorded so every check runs and the report is complete.

    The caller has already connected the broker; reaching this function at all
    means the API handshake succeeded, which is itself the most important check.

    `now`, if given, must be timezone-aware -- passed to the market-calendar
    check (H-4) for deterministic tests. Defaults to real time.
    """
    result = PreflightResult()

    # --- US market calendar (H-4). Informational only -- never fails
    # preflight, because a holiday is a valid day to do nothing on, not a
    # problem. Exists so an operator sees *why* submit will skip today
    # instead of wondering why the 16:00 run was silent.
    today_et = today_us_eastern(now)
    if is_us_trading_day(today_et):
        result.add("market_session", True, f"{today_et.isoformat()}: NYSE open today")
    else:
        result.add(
            "market_session", True,
            f"{today_et.isoformat()}: NYSE CLOSED today (holiday/weekend) -- "
            "submit will skip cleanly",
        )

    # --- IB account readable, and actually populated
    try:
        account = await broker.get_account_summary()
        result.account_id = account.account_id
        result.net_liquidation = account.net_liquidation
        if account.net_liquidation >= min_net_liquidation:
            result.add(
                "ib_account", True,
                f"{account.account_id or '?'} net_liq=${account.net_liquidation:,.2f}",
            )
        else:
            result.add(
                "ib_account", False,
                f"net_liq=${account.net_liquidation:,.2f} below "
                f"${min_net_liquidation:,.2f} -- Gateway may still be logging in",
            )

        # --- account IB Gateway is serving matches configured mode (SEC-2)
        # config.toml's `mode` and the account Gateway actually connects to
        # (set by a separate TRADING_MODE env var read by IBC) are two
        # unlinked decisions. An empty account_id means the read itself is
        # untrustworthy (Gateway mid-login) -- already caught by ib_account
        # above, so skip rather than pile on a second failure for the
        # same root cause.
        if account.account_id:
            is_paper_account = account.account_id.startswith(PAPER_ACCOUNT_PREFIXES)
            if is_paper_account == (mode == "paper"):
                result.add(
                    "account_mode_match", True,
                    f"account={account.account_id} mode={mode}",
                )
            else:
                result.add(
                    "account_mode_match", False,
                    f"config mode={mode!r} but account {account.account_id!r} "
                    f"looks like a {'paper' if is_paper_account else 'live'} "
                    "account -- Gateway may be serving the wrong account",
                )
    except Exception as exc:  # noqa: BLE001
        result.add("ib_account", False, f"{type(exc).__name__}: {exc}")

    # --- positions readable (submit's source of truth for what we hold)
    try:
        positions = await broker.get_positions()
        longs = [p for p in positions if p.quantity > 0]
        result.held_count = len(longs)
        result.add("ib_positions", True, f"{len(longs)} long position(s)")
        if len(longs) > max_positions:
            result.add(
                "position_cap", False,
                f"holding {len(longs)} > cap {max_positions} -- submit will "
                f"force-trim {len(longs) - max_positions} today",
            )
        else:
            result.add("position_cap", True, f"{len(longs)}/{max_positions}")
    except Exception as exc:  # noqa: BLE001
        result.add("ib_positions", False, f"{type(exc).__name__}: {exc}")

    # --- universe non-empty (an empty universe means submit scans nothing)
    result.universe_size = len(universe)
    result.add(
        "universe", bool(universe),
        f"{len(universe)} symbol(s)" if universe else "EMPTY -- nothing to scan",
    )

    # --- at least one strategy built
    result.strategy_names = [b.strategy.name for b in strategies]
    result.add(
        "strategies", bool(strategies),
        ", ".join(result.strategy_names) if strategies else "none enabled",
    )

    logger.info(
        "preflight: %s (%d check(s), %d failure(s))",
        "READY" if result.ok else "NOT READY",
        len(result.checks), len(result.failures),
    )
    return result
