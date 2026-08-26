"""CLI commands for Vibe Trade."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

import typer
from rich.console import Console
from rich.table import Table

from vibe_trade.config import load_config
from vibe_trade.db.engine import init_db

logger = logging.getLogger(__name__)
app = typer.Typer(name="vibe-trade", help="Vibe Trade -- Stock Trading Bot")
console = Console()


class _JsonFormatter(logging.Formatter):
    """One JSON object per log record. Used only on the file handler."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime

        payload = {
            "time": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _setup_logging(level: str, log_file: str | None = None) -> None:
    """Configure root logger with plain stdout + JSON-rotating file handler.

    File handler rotates at midnight, keeps 7 backups (one week of history).
    Database is the source of truth for historical analytics; logs are
    only for short-term operational debugging.
    """
    from logging.handlers import TimedRotatingFileHandler

    plain_fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(getattr(logging, level, logging.INFO))

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(plain_fmt))
    root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setFormatter(_JsonFormatter())
        root.addHandler(file_handler)


def _get_notifier(config):
    """Return TelegramNotifier when enabled, ConsoleNotifier otherwise.

    The Console fallback is what `panic` and the V2 jobs degrade to in dev
    when Telegram credentials aren't set.
    """
    from vibe_trade.notify.console import ConsoleNotifier
    from vibe_trade.notify.telegram import TelegramNotifier

    if config.telegram.enabled:
        return TelegramNotifier(config.telegram)
    return ConsoleNotifier()


def _run_with_crash_alert(
    job_name: str,
    config,
    coro_factory: Callable[..., Coroutine[Any, Any, object]],
    **factory_kwargs,
) -> None:
    """Run an async job with a top-level safety net.

    Wraps ``asyncio.run(coro_factory(config, **factory_kwargs))`` in a
    try/except so that ANY uncaught exception (Gateway disconnect, DB error,
    whatever) results in:

    1. Full traceback logged to the rotating JSON file.
    2. A ``[CRITICAL]`` Telegram alert via a *fresh* notifier (the original
       notifier inside the failed coroutine may be bound to a closed event loop).
    3. Re-raise so cron sees a non-zero exit code.

    This is Bug #6 in docs/SESSION_H_FINDINGS.md -- before this wrapper, a
    Gateway disconnect at 16:00 silently crashed without notification.
    """
    _warn_if_live_mode(job_name, config)
    try:
        asyncio.run(coro_factory(config, **factory_kwargs))
    except Exception as exc:
        logger.exception("%s crashed: %s", job_name, exc)
        _send_crash_alert(job_name, config, exc)
        raise


def _warn_if_live_mode(job_name: str, config) -> None:
    """Loud, unmissable banner when running against a real-money account.

    Guards against the silent paper->live config slip: every job announces
    live mode in both the console and the log file. ASCII only (Windows
    cp1252 console gotcha, see CLAUDE.md).
    """
    mode = getattr(getattr(config, "general", None), "mode", "paper")
    if mode != "live":
        return
    port = config.broker.get_port("live")
    logger.warning(
        "LIVE TRADING MODE: %s will place real-money orders (port %d)",
        job_name, port,
    )
    console.print(
        f"[bold red]*** LIVE TRADING MODE -- {job_name} places real-money "
        f"orders (port {port}) ***[/bold red]"
    )


def _send_crash_alert(job_name: str, config, exc: Exception) -> None:
    """Best-effort [CRITICAL] alert. Failures here are logged but suppressed
    so they don't shadow the original exception.
    """
    try:
        notifier = _get_notifier(config)
        short = (str(exc) or repr(exc))[:200]
        msg = (
            f"[CRITICAL] {job_name} failed on {date.today().isoformat()}: "
            f"{type(exc).__name__} -- {short}\nSee logs."
        )
        asyncio.run(notifier.notify_error(msg))
    except Exception as alert_exc:  # noqa: BLE001
        logger.exception("Failed to send crash alert for %s: %s", job_name, alert_exc)


def _format_submit_msg(result, today) -> str:
    """Build the Telegram message for a submit run. Pure function."""
    lines = [f"[SUBMIT] {today.isoformat()}"]
    lines.append(
        f"Exits:   {result.exits_placed} placed, {result.exits_failed} failed"
    )
    if result.trim_signaled:
        lines.append(
            f"Trim:    {result.trim_placed} placed, {result.trim_failed} failed "
            f"(over-cap relief)"
        )
    if result.entries_phase_skipped:
        lines.append(f"Entries phase skipped: {result.cap_reason}")
    else:
        lines.append(
            f"Entries: {result.entries_placed} placed, {result.entries_failed} failed"
        )
    if result.stale_bars_skipped:
        lines.append(
            f"[WARN] {result.stale_bars_skipped} symbol(s) skipped: last bar "
            f"was today's US date, not closed (H-1 -- possible Israel/US DST "
            f"mismatch, check the clock)"
        )
    if result.errors:
        lines.append(f"{len(result.errors)} error(s):")
        for err in result.errors[:10]:
            lines.append(f"  - {err}")
    return "\n".join(lines)


def _format_record_msg(result, today) -> str:
    """Build the Telegram message for a record run. Pure function."""
    lines = [
        f"[RECORD] {today.isoformat()}",
        f"{result.buys_inserted} BUYs recorded, "
        f"{result.sells_flipped} SELLs flipped",
    ]
    if result.errors:
        lines.append(f"{len(result.errors)} error(s):")
        for err in result.errors[:10]:
            lines.append(f"  - {err}")
    return "\n".join(lines)


def _format_reconcile_msg(result, opened, closed, pnl, today) -> str:
    """Build the daily summary message. Pure function.

    `opened`: list of Trade rows whose entry_time is today (BUYs only — V2 has no shorts).
    `closed`: list of Trade rows whose exit_time is today (each with `pnl` set).
    `pnl`: DailyPnL row for `today`, or None if reconcile didn't write one.
    """
    lines = [
        f"[DAILY SUMMARY] {today.isoformat()}",
        f"Opened: {result.opened}  Closed: {result.closed}",
        "",
    ]

    if not opened and not closed:
        lines.append("No trades today.")
    else:
        lines.append("```")
        lines.append("Symbol  Side  Qty  P&L")
        lines.append("-" * 26)
        for t in opened:
            lines.append(f"{t.symbol:<7} BUY  {t.filled_quantity:>4}")
        for t in closed:
            sign = "+" if (t.pnl or 0) >= 0 else "-"
            amount = abs(t.pnl or 0)
            pnl_str = f"{sign}${amount:,.2f}"
            # exit_filled_quantity is the SELL leg's own column (H-3);
            # filled_quantity is the entry (BUY) leg and no longer holds the
            # exit amount. Fall back to it only for rows closed before this
            # column existed.
            exit_qty = getattr(t, "exit_filled_quantity", None)
            sold_qty = exit_qty if exit_qty is not None else t.filled_quantity
            lines.append(
                f"{t.symbol:<7} SELL {sold_qty:>4}  {pnl_str}"
            )
        lines.append("```")

    if pnl is not None:
        sign = "+" if (pnl.realized_pnl or 0) >= 0 else "-"
        rp = abs(pnl.realized_pnl or 0)
        lines.append("")
        lines.append(f"Realized P&L: {sign}${rp:,.2f}")
        if pnl.account_value is not None:
            lines.append(f"Account:    ${pnl.account_value:,.2f}")

    if result.errors:
        lines.append("")
        lines.append(f"{len(result.errors)} error(s):")
        for err in result.errors[:10]:
            lines.append(f"  - {err}")

    return "\n".join(lines)


@app.command()
def submit(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
    force: bool = typer.Option(
        False, "--force",
        help="Bypass the double-run guard (orders already at IB today)",
    ),
) -> None:
    """V2 submit job (16:00 Asia/Jerusalem).

    Exits phase: SELL signals on held positions.
    Entries phase: BUY signals on universe minus held tickers.
    Places market orders, NO DB writes (record at 16:25 handles persistence).
    Aborts if strategy orders are already at IB today (re-run protection);
    --force overrides.
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    _run_with_crash_alert("submit", config, _run_submit_cli, force=force)


async def _run_submit_cli(config, *, force: bool = False) -> None:
    from datetime import date as _date

    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.data.market_calendar import is_us_trading_day, today_us_eastern
    from vibe_trade.data.provider import DataProvider
    from vibe_trade.data.universe import load_universe
    from vibe_trade.db.engine import init_db as _init
    from vibe_trade.db.repository import TradeRepository
    from vibe_trade.jobs.submit import SUBMIT_CLIENT_ID, run_submit
    from vibe_trade.risk.manager import RiskManager
    from vibe_trade.strategy.registry import build_strategies

    # H-4: the `* * 1-5` cron schedule fires on US market holidays (~9/year).
    # Checked here, before even connecting to IB, so a holiday costs nothing
    # more than this one calendar lookup -- no stray day-orders get placed
    # for the double-run guard to trip over on the next real session.
    today_et = today_us_eastern()
    if not is_us_trading_day(today_et):
        msg = f"{today_et.isoformat()} is not a US market trading day -- nothing to do."
        console.print(f"[dim]{msg}[/dim]")
        notifier = _get_notifier(config)
        await notifier.notify_summary(f"[SUBMIT] {today_et.isoformat()}\n{msg}")
        return

    broker_config = config.broker.model_copy()
    broker_config.client_id = SUBMIT_CLIENT_ID

    broker = IBBroker(broker_config, mode=config.general.mode)
    universe = load_universe(config.universe)
    notifier = _get_notifier(config)

    # Priority-ordered active strategies (Session L).
    strategies = build_strategies(config.strategies, config.risk.pct_per_position)

    # Read-only DB lookup: map each held symbol to the strategy that opened it,
    # so exits stay strategy-scoped. Submit itself writes nothing (V2 invariant).
    session_factory = _init(config.general.db_path)
    session = session_factory()
    try:
        open_trades = TradeRepository(session).get_open_trades()
        position_strategies = {t.symbol: t.strategy_name for t in open_trades}
    finally:
        session.close()

    console.print(
        f"[bold]Submit[/bold] mode={config.general.mode} "
        f"client_id={SUBMIT_CLIENT_ID} universe_size={len(universe)} "
        f"strategies={[b.strategy.name for b in strategies]}"
    )
    console.print(
        f"Connecting to {broker_config.host}:"
        f"{broker_config.get_port(config.general.mode)}..."
    )

    await broker.connect()
    try:
        result = await run_submit(
            broker=broker,
            strategies=strategies,
            data_provider=DataProvider(),
            risk_manager=RiskManager(config.risk),
            universe=universe,
            position_strategies=position_strategies,
            max_positions=config.risk.max_open_positions,
            force=force,
            mode=config.general.mode,
        )
    finally:
        await broker.disconnect()

    _print_submit_summary(result)

    msg = _format_submit_msg(result, _date.today())
    if result.errors or result.stale_bars_skipped:
        await notifier.notify_error(msg)
    else:
        await notifier.notify_summary(msg)


def _print_submit_summary(result) -> None:
    """Render a SubmitResult to the console as a tidy table."""
    console.print(
        f"\n[dim]universe={result.universe_size}  held={result.held_count}[/dim]"
    )

    table = Table(title="Submit Result")
    table.add_column("Phase", style="cyan")
    table.add_column("Evaluated", justify="right")
    table.add_column("Signaled", justify="right")
    table.add_column("Placed", justify="right", style="green")
    table.add_column("Failed", justify="right", style="red")
    table.add_row(
        "Exits",
        str(result.exits_evaluated),
        str(result.exits_signaled),
        str(result.exits_placed),
        str(result.exits_failed),
    )
    if result.trim_signaled or result.trim_placed:
        table.add_row(
            "Trim",
            "-",
            str(result.trim_signaled),
            str(result.trim_placed),
            str(result.trim_failed),
        )
    if not result.entries_phase_skipped:
        table.add_row(
            "Entries",
            str(result.entries_evaluated),
            str(result.entries_signaled),
            str(result.entries_placed),
            str(result.entries_failed),
        )
    console.print(table)

    if result.entries_phase_skipped:
        console.print(
            f"[yellow]Entries phase skipped: {result.cap_reason}[/yellow]"
        )
    if result.entries_skipped_sizing > 0:
        console.print(
            f"[dim]{result.entries_skipped_sizing} BUY signal(s) skipped by sizer "
            f"(at cap or 1 share > target)[/dim]"
        )
    if result.stale_bars_skipped > 0:
        console.print(
            f"[yellow]{result.stale_bars_skipped} symbol(s) skipped: last bar "
            f"was today's US date, not closed (H-1 -- check for a DST "
            f"mismatch)[/yellow]"
        )
    if result.errors:
        console.print(f"\n[red]{len(result.errors)} error(s):[/red]")
        for e in result.errors[:10]:
            console.print(f"  - {e}")
        if len(result.errors) > 10:
            console.print(f"  ... and {len(result.errors) - 10} more")


@app.command()
def record(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """V2 record job (16:25 Asia/Jerusalem).

    Reads today's fills from IB, persists each as SUBMITTED in the DB
    (BUYs) or flips matching OPEN -> PENDING_CLOSE (SELLs). Cross-process
    dedup by `permId`. No order placement.
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    init_db(config.general.db_path)
    _run_with_crash_alert("record", config, _run_record_cli)


async def _run_record_cli(config) -> None:
    from datetime import date as _date

    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.db.engine import init_db as _init
    from vibe_trade.db.repository import TradeRepository
    from vibe_trade.jobs.record import run_record
    from vibe_trade.jobs.submit import RECORD_CLIENT_ID

    broker_config = config.broker.model_copy()
    broker_config.client_id = RECORD_CLIENT_ID
    broker = IBBroker(broker_config, mode=config.general.mode)
    session_factory = _init(config.general.db_path)
    notifier = _get_notifier(config)

    console.print(
        f"[bold]Record[/bold] mode={config.general.mode} "
        f"client_id={RECORD_CLIENT_ID}"
    )

    await broker.connect()
    session = session_factory()
    try:
        # Give ib_async a beat to hydrate the fill cache after connect.
        await asyncio.sleep(1.0)
        repo = TradeRepository(session)
        result = await run_record(broker=broker, repo=repo)
    finally:
        session.close()
        await broker.disconnect()

    table = Table(title="Record Result")
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right")
    table.add_row("fills seen", str(result.fills_seen))
    table.add_row("unique permIds", str(result.perm_ids_seen))
    table.add_row("BUYs inserted", str(result.buys_inserted))
    table.add_row("BUYs skipped (dup)", str(result.buys_skipped_dup))
    table.add_row("SELLs flipped to PENDING_CLOSE", str(result.sells_flipped))
    table.add_row("SELLs skipped (dup)", str(result.sells_skipped_dup))
    table.add_row("SELLs skipped (no OPEN match)", str(result.sells_skipped_no_open))
    console.print(table)
    if result.errors:
        console.print(f"\n[red]{len(result.errors)} error(s):[/red]")
        for e in result.errors[:10]:
            console.print(f"  - {e}")

    msg = _format_record_msg(result, _date.today())
    if result.errors:
        await notifier.notify_error(msg)
    else:
        await notifier.notify_summary(msg)


@app.command()
def reconcile(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """V2 reconcile job (23:30 Asia/Jerusalem).

    Finalizes today's pending trades by reading IB fills + orderStatus,
    transitioning SUBMITTED -> OPEN/PARTIAL/CANCELLED and PENDING_CLOSE
    -> CLOSED/PARTIAL with realized P&L. Writes portfolio_snapshot and
    upserts daily_pnl with real counts.
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    init_db(config.general.db_path)
    _run_with_crash_alert("reconcile", config, _run_reconcile_cli)


async def _run_reconcile_cli(config) -> None:
    from datetime import date as _date

    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.db.engine import init_db as _init
    from vibe_trade.db.repository import (
        DailyPnLRepository,
        PortfolioSnapshotRepository,
        TradeRepository,
    )
    from vibe_trade.jobs.reconcile import run_reconcile
    from vibe_trade.jobs.submit import RECONCILE_CLIENT_ID

    broker_config = config.broker.model_copy()
    broker_config.client_id = RECONCILE_CLIENT_ID
    broker = IBBroker(broker_config, mode=config.general.mode)
    session_factory = _init(config.general.db_path)
    notifier = _get_notifier(config)

    console.print(
        f"[bold]Reconcile[/bold] mode={config.general.mode} "
        f"client_id={RECONCILE_CLIENT_ID}"
    )

    await broker.connect()
    session = session_factory()
    try:
        await asyncio.sleep(1.0)
        trade_repo = TradeRepository(session)
        daily_repo = DailyPnLRepository(session)
        result = await run_reconcile(
            broker=broker,
            trade_repo=trade_repo,
            snap_repo=PortfolioSnapshotRepository(session),
            daily_repo=daily_repo,
        )
        # Read for the summary message before closing the session
        today = _date.today()
        opened_today = trade_repo.get_trades_opened_today(today)
        closed_today = trade_repo.get_trades_closed_today(today)
        daily_row = daily_repo.get_by_date(today)
    finally:
        session.close()
        await broker.disconnect()

    table = Table(title="Reconcile Result")
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right")
    table.add_row("pending DB rows", str(result.pending_count))
    table.add_row("opened (SUBMITTED -> OPEN/PARTIAL)", str(result.opened))
    table.add_row("closed (PENDING_CLOSE -> CLOSED/PARTIAL)", str(result.closed))
    table.add_row("cancelled", str(result.cancelled))
    table.add_row("skipped (still working)", str(result.skipped_still_working))
    table.add_row("orphan fills back-filled to OPEN", str(result.orphan_fills_inserted))
    if result.orphan_sells_unmatched:
        table.add_row("orphan SELLs (unmatched)", str(result.orphan_sells_unmatched))
    table.add_row("portfolio_snapshot rows", str(result.snapshot_rows))
    console.print(table)
    if result.errors:
        console.print(f"\n[red]{len(result.errors)} error(s):[/red]")
        for e in result.errors[:10]:
            console.print(f"  - {e}")

    msg = _format_reconcile_msg(result, opened_today, closed_today, daily_row, today)
    if result.errors:
        await notifier.notify_error(msg)
    else:
        await notifier.notify_summary(msg)


@app.command(name="refresh-sp100")
def refresh_sp100() -> None:
    """Refresh the static top-100 S&P 500 list (by market cap).

    Hits yfinance for every SP500 symbol's market cap (~5 min), sorts
    descending, and rewrites src/vibe_trade/data/sp100_top.py with the
    top 100. Commit the resulting file change.
    """
    from datetime import date as _date

    from vibe_trade.backtest.data import get_top_n_by_mcap
    from vibe_trade.data.universe import SP500_SYMBOLS

    console.print(
        f"[bold]Refreshing top-100 list[/bold] from {len(SP500_SYMBOLS)} S&P 500 symbols..."
    )
    console.print("[dim]This will take ~5 minutes (yfinance Ticker.info is slow).[/dim]")

    top100 = get_top_n_by_mcap(SP500_SYMBOLS, n=100, force_refresh=True)
    console.print(f"  ranked {len(top100)} symbols")

    out_path = Path("src/vibe_trade/data/sp100_top.py")
    today = _date.today().isoformat()
    body_lines = [f'    "{s}",' for s in top100]
    out_path.write_text(
        '"""Top-100 S&P 500 by market cap -- static snapshot for backtest reproducibility.\n'
        '\n'
        'Generated by `vibe-trade refresh-sp100`. Hardcoded so the backtest universe\n'
        'is reproducible across runs (yfinance market caps drift over time, and\n'
        'runs against a moving target are not comparable).\n'
        '\n'
        'Refresh quarterly or when significantly out of date. Each refresh updates\n'
        'both the symbol list and `LAST_UPDATED` below.\n'
        '\n'
        "Survivorship-bias caveat: this is *today's* top 100, used across all\n"
        'historical dates. Names that were top-100 in 2018 but are smaller now (or\n'
        'delisted) are missing. Acceptable for a yes/no "is the strategy profitable"\n'
        'question; not for production-grade research. Documented in docs/ROADMAP.md\n'
        'under Phase 4 hardening.\n'
        '"""\n'
        '\n'
        'from __future__ import annotations\n'
        '\n'
        f'LAST_UPDATED: str = "{today}"\n'
        '\n'
        'SP100_TOP_BY_MCAP: list[str] = [\n'
        + '\n'.join(body_lines) + '\n'
        ']\n'
    )
    console.print(f"  [green]wrote {out_path}[/green]")
    console.print(f"  LAST_UPDATED = {today}")
    console.print("\n[dim]Commit src/vibe_trade/data/sp100_top.py to lock in the new list.[/dim]")


@app.command(name="refresh-sp500-membership")
def refresh_sp500_membership() -> None:
    """Refresh point-in-time S&P 500 index membership (C-2, PROJECT_EVALUATION.md).

    Scrapes Wikipedia's "List of S&P 500 companies" page for the current
    constituent list and (from a pinned older revision -- the live page no
    longer carries it) the dated additions/removals history, then rewrites
    src/vibe_trade/data/sp500_membership.py. Fixes the backtest's previous
    survivorship bias: `sp100_top.py` applied *today's* top-100 unchanged
    across the whole 2018-2026 window; this lets the backtest ask "who was
    actually in the index on date X" instead. Commit the resulting file.
    """
    from datetime import date as _date

    from vibe_trade.backtest.membership import (
        CHANGES_TABLE_FALLBACK_REVID,
        _fetch_wikipedia_html,
        _fetch_wikipedia_revision_html,
        generate_artifact_source,
        parse_added_dates,
        parse_changes,
        parse_current_members,
    )

    console.print("[bold]Refreshing point-in-time S&P 500 membership[/bold]")
    console.print("Fetching live page...")
    live_html = _fetch_wikipedia_html()
    current_members = parse_current_members(live_html)
    added_dates = parse_added_dates(live_html)
    console.print(f"  {len(current_members)} current members")

    console.print(f"Fetching pinned revision {CHANGES_TABLE_FALLBACK_REVID} for change history...")
    old_html = _fetch_wikipedia_revision_html(CHANGES_TABLE_FALLBACK_REVID)
    changes = parse_changes(old_html)
    console.print(f"  {len(changes)} historical changes")

    from vibe_trade.backtest.membership import recent_additions_since

    cutoff = _date(2026, 8, 8)  # keep in sync with CHANGES_TABLE_FALLBACK_REVID's date
    gap_fill = recent_additions_since(added_dates, since=cutoff)
    if gap_fill:
        console.print(
            f"  [yellow]{len(gap_fill)} addition(s) since the pinned revision, "
            f"recovered from \"Date added\": {[c.added for c in gap_fill]}[/yellow]"
        )
    all_changes = changes + gap_fill

    today = _date.today().isoformat()
    source = generate_artifact_source(current_members, all_changes, last_updated=today)
    out_path = Path("src/vibe_trade/data/sp500_membership.py")
    out_path.write_text(source)

    console.print(f"  [green]wrote {out_path}[/green]")
    console.print(f"  LAST_UPDATED = {today}, {len(current_members)} members, {len(all_changes)} changes")
    console.print(
        "\n[dim]Commit src/vibe_trade/data/sp500_membership.py to lock in the refresh. "
        "Removals since the pinned revision are NOT recoverable from Wikipedia alone -- "
        "a small, known, documented gap.[/dim]"
    )


@app.command()
def backtest(
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD (exclusive)"),
    top_n: int = typer.Option(100, "--top-n", help="Top N S&P 500 by market cap (ignored with --universe sp500)"),
    universe_mode: str = typer.Option(
        "top100", "--universe",
        help="Universe source: 'top100' (today's top-N by market cap, "
             "survivorship-biased -- see PROJECT_EVALUATION.md C-2) or "
             "'sp500' (full point-in-time membership; requires "
             "`vibe-trade refresh-sp500-membership` to have been run at least once)",
    ),
    friction_mode: str = typer.Option(
        "none", "--friction",
        help="Cost model: 'none' (zero frictions, the historical default), "
             "'median' (measured 2026-05..07 paper-run slippage, base case), "
             "or 'stress' (same run's mean slippage -- right-skewed by a few "
             "bad fills). Both costed modes add IB per-share commission. See "
             "backtest.engine.Frictions and docs/playbooks/go-live-criteria.md",
    ),
    pct: float = typer.Option(0.04, "--pct", help="Per-position size as fraction of net_liq"),
    max_positions: int = typer.Option(25, "--max-positions", help="Position cap"),
    equity: float = typer.Option(100_000.0, "--equity", help="Starting equity"),
    strategy: str = typer.Option(
        "donchian", "--strategy",
        help="Strategy id to backtest (donchian, sma, ema, macd)",
    ),
    output_dir: Optional[str] = typer.Option(
        None, "--output", help="Output directory (default: backtests/<timestamp>/)"
    ),
    force_refresh: bool = typer.Option(
        False, "--force-refresh", help="Re-fetch bars + market caps even if cached"
    ),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Backtest a single strategy against S&P 500 history.

    Select the strategy with --strategy (default donchian). Default settings
    differ from production: top-100 / 4% / 25-cap (vs production's full universe
    / 1.8% / 50-cap) for cleaner read-out per docs/ROADMAP.md Session I. Frictions
    default to none, matching every backtest run before this flag existed --
    pass --friction median (or stress) to cost a run.
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level)
    _run_backtest_cli(
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        top_n=top_n,
        universe_mode=universe_mode,
        friction_mode=friction_mode,
        pct=pct,
        max_positions=max_positions,
        equity=equity,
        strategy_id=strategy,
        output_dir=output_dir,
        force_refresh=force_refresh,
    )


def _run_backtest_cli(
    *, start, end, top_n: int, universe_mode: str = "top100", friction_mode: str = "none",
    pct: float, max_positions: int,
    equity: float, strategy_id: str, output_dir: str | None, force_refresh: bool,
) -> None:
    import json
    from dataclasses import asdict
    from datetime import datetime as _dt

    from vibe_trade.backtest.data import fetch_and_cache_bars
    from vibe_trade.backtest.engine import (
        IB_COMMISSION_MIN,
        IB_COMMISSION_PER_SHARE,
        MEASURED_SLIPPAGE_BPS_MEAN,
        MEASURED_SLIPPAGE_BPS_MEDIAN,
        Frictions,
        run_backtest,
    )
    from vibe_trade.backtest.metrics import compute_metrics
    from vibe_trade.strategy.registry import build_strategy

    strategy = build_strategy(strategy_id)

    # ---------------------------------------------------------- frictions
    if friction_mode == "none":
        frictions = None
    elif friction_mode in ("median", "stress"):
        slippage_bps = MEASURED_SLIPPAGE_BPS_MEDIAN if friction_mode == "median" else MEASURED_SLIPPAGE_BPS_MEAN
        frictions = Frictions(
            slippage_bps=slippage_bps,
            commission_per_share=IB_COMMISSION_PER_SHARE,
            commission_min=IB_COMMISSION_MIN,
        )
    else:
        console.print(f"[red]ERROR:[/red] unknown --friction '{friction_mode}' (want none, median, or stress)")
        raise typer.Exit(code=1)
    if frictions is None:
        console.print("  frictions: none (zero-cost fills)")
    else:
        console.print(
            f"  frictions: {friction_mode}  slippage={frictions.slippage_bps:.1f}bps/leg  "
            f"commission=${frictions.commission_per_share}/sh (${frictions.commission_min:.2f} min)"
        )

    # ---------------------------------------------------------- universe
    console.print(
        f"[bold]Backtest[/bold] {start} -> {end}  strategy={strategy.name}  "
        f"universe={universe_mode}  pct={pct:.3f}  max_pos={max_positions}  "
        f"equity=${equity:,.0f}"
    )
    membership = None
    if universe_mode == "sp500":
        from vibe_trade.backtest.membership import load_default_timeline, members_ever_in_range

        try:
            membership = load_default_timeline()
        except ImportError:
            console.print(
                "[red]ERROR:[/red] no point-in-time membership data -- run "
                "`vibe-trade refresh-sp500-membership` first."
            )
            raise typer.Exit(code=1) from None
        universe = sorted(members_ever_in_range(membership, start, end))
        console.print(
            f"  using {len(universe)} symbols ever an S&P 500 member {start} -> {end} "
            f"(point-in-time, survivorship-bias-free; --top-n ignored)"
        )
    elif universe_mode == "top100":
        from vibe_trade.data.sp100_top import LAST_UPDATED, SP100_TOP_BY_MCAP

        if not SP100_TOP_BY_MCAP:
            console.print(
                "[red]ERROR:[/red] sp100_top.py is empty -- run `vibe-trade refresh-sp100` first."
            )
            raise typer.Exit(code=1)
        universe = SP100_TOP_BY_MCAP[:top_n]
        console.print(
            f"  using {len(universe)} symbols from sp100_top.py (LAST_UPDATED={LAST_UPDATED})"
        )
    else:
        console.print(f"[red]ERROR:[/red] unknown --universe '{universe_mode}' (want top100 or sp500)")
        raise typer.Exit(code=1)

    # ---------------------------------------------------------- bars
    console.print("Ensuring historical bars are cached...")
    paths = fetch_and_cache_bars(
        universe, start=start, end=end, force_refresh=force_refresh,
    )
    console.print(f"  bars available for {len(paths)} symbols")

    # ---------------------------------------------------------- run
    console.print("Running backtest...")
    result = run_backtest(
        strategy=strategy,
        universe=list(paths.keys()),
        start=start, end=end,
        starting_equity=equity,
        pct_per_position=pct,
        max_positions=max_positions,
        membership=membership,
        frictions=frictions,
    )
    metrics = compute_metrics(result)

    # ---------------------------------------------------------- benchmarks
    from vibe_trade.backtest.data import load_bars as _load_bars
    from vibe_trade.backtest.metrics import BenchmarkMetrics, compute_benchmark

    BENCH_SYMBOLS = ["SPY", "QQQ"]
    console.print("Computing benchmarks (SPY, QQQ buy-and-hold)...")
    bench_paths = fetch_and_cache_bars(
        BENCH_SYMBOLS, start=start, end=end, force_refresh=force_refresh,
    )
    benchmarks: list[BenchmarkMetrics] = []
    bench_closes = {}  # {symbol: close-price Series} for the plot overlay
    for sym in BENCH_SYMBOLS:
        if sym not in bench_paths:
            continue
        bench_df = _load_bars(sym, start=start, end=end)
        if not bench_df.empty:
            bench_closes[sym] = bench_df["close"]
            benchmarks.append(compute_benchmark(sym, bench_df["close"]))

    # ---------------------------------------------------------- output dir
    out = Path(output_dir) if output_dir else (
        Path("backtests") / _dt.now().strftime("%Y%m%d_%H%M%S")
    )
    out.mkdir(parents=True, exist_ok=True)

    # equity.csv
    result.equity_curve.to_csv(out / "equity.csv", header=["equity"])

    # trades.csv
    if result.trades:
        trades_df = pd_DataFrame_from_trades(result.trades)
        trades_df.to_csv(out / "trades.csv", index=False)

    # metrics.json (params + metrics together)
    summary = {
        "params": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "strategy": strategy.name,
            "universe_mode": universe_mode,
            "friction_mode": friction_mode,
            "top_n": top_n,
            "pct_per_position": pct,
            "max_positions": max_positions,
            "starting_equity": equity,
            "universe_size": len(paths),
        },
        "result": {
            "ending_equity": result.ending_equity,
            "open_positions_at_end": result.open_positions_at_end,
            "skipped_buys_no_cash": result.skipped_buys_no_cash,
            "skipped_buys_no_data": result.skipped_buys_no_data,
            "total_commission": result.total_commission,
        },
        "metrics": _metrics_to_jsonable(asdict(metrics)),
        "benchmarks": {
            bm.symbol: asdict(bm) for bm in benchmarks
        },
    }
    (out / "metrics.json").write_text(json.dumps(summary, indent=2))

    # plot
    from vibe_trade.backtest.plot import save_backtest_plot
    png_path = save_backtest_plot(
        equity_curve=result.equity_curve,
        benchmarks=bench_closes,
        output_path=out,
        starting_equity=equity,
    )
    console.print(f"  [green]saved plot -> {png_path.name}[/green]")

    # ---------------------------------------------------------- summary
    table = Table(title="Backtest Result")
    table.add_column("Metric", style="cyan")
    table.add_column("Strategy", justify="right")
    for bm in benchmarks:
        table.add_column(f"{bm.symbol} B&H", justify="right")
    table.add_row(
        "Total return",
        f"{metrics.total_return_pct:+.2f}%",
        *[f"{bm.total_return_pct:+.2f}%" for bm in benchmarks],
    )
    table.add_row(
        "CAGR",
        f"{metrics.cagr_pct:+.2f}%",
        *[f"{bm.cagr_pct:+.2f}%" for bm in benchmarks],
    )
    table.add_row(
        "Sharpe (annual)",
        f"{metrics.sharpe:.2f}",
        *[f"{bm.sharpe:.2f}" for bm in benchmarks],
    )
    table.add_row(
        "Max drawdown",
        f"{metrics.max_drawdown_pct:.2f}%",
        *[f"{bm.max_drawdown_pct:.2f}%" for bm in benchmarks],
    )
    table.add_row("# trades", str(metrics.n_trades), *["--" for _ in benchmarks])
    table.add_row("Win rate", f"{metrics.win_rate * 100:.1f}%", *["--" for _ in benchmarks])
    pf_str = "inf" if metrics.profit_factor == float("inf") else f"{metrics.profit_factor:.2f}"
    table.add_row("Profit factor", pf_str, *["--" for _ in benchmarks])
    table.add_row("Avg win / loss", f"${metrics.avg_win:,.2f} / ${metrics.avg_loss:,.2f}", *["--" for _ in benchmarks])
    table.add_row("Avg holding (days)", f"{metrics.avg_holding_days:.1f}", *["--" for _ in benchmarks])
    table.add_row("Exposure", f"{metrics.exposure_pct:.1f}%", *["--" for _ in benchmarks])
    table.add_row("Open at end", str(result.open_positions_at_end), *["--" for _ in benchmarks])
    console.print(table)

    # ---------------------------------------------------------- verdict
    for bm in benchmarks:
        diff = metrics.total_return_pct - bm.total_return_pct
        direction = "higher" if diff > 0 else "lower"
        color = "green" if diff > 0 else "red"
        console.print(
            f"  Strategy vs {bm.symbol}: [{color}]{diff:+.2f}pp {direction}[/{color}]"
        )

    console.print(f"\n[dim]Outputs: {out.resolve()}[/dim]")


def pd_DataFrame_from_trades(trades):
    """Trades -> DataFrame with computed pct/holding columns."""
    import pandas as pd
    rows = [
        {
            "symbol": t.symbol,
            "entry_date": t.entry_date.isoformat(),
            "entry_price": t.entry_price,
            "exit_date": t.exit_date.isoformat(),
            "exit_price": t.exit_price,
            "qty": t.qty,
            "pnl": t.pnl,
            "pnl_pct": t.pnl_pct,
            "holding_days": t.holding_days,
        }
        for t in trades
    ]
    return pd.DataFrame(rows)


def _metrics_to_jsonable(d: dict) -> dict:
    """Replace inf with the string 'inf' so json.dumps doesn't choke."""
    out = {}
    for k, v in d.items():
        if isinstance(v, float) and (v == float("inf") or v == float("-inf")):
            out[k] = str(v)
        else:
            out[k] = v
    return out


@app.command()
def status(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """Show current open positions and daily P&L."""
    config = load_config(config_path)
    session_factory = init_db(config.general.db_path)
    session = session_factory()

    from vibe_trade.db.repository import TradeRepository

    trade_repo = TradeRepository(session)

    # Open positions
    open_trades = trade_repo.get_open_trades()
    if open_trades:
        table = Table(title="Open Positions")
        table.add_column("Symbol", style="cyan")
        table.add_column("Side")
        table.add_column("Qty", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Strategy")

        for t in open_trades:
            qty = t.filled_quantity if t.filled_quantity is not None else t.requested_quantity
            table.add_row(
                t.symbol,
                t.side,
                str(qty),
                f"${t.entry_price:.2f}" if t.entry_price else "-",
                t.strategy_name,
            )
        console.print(table)
    else:
        console.print("[dim]No open positions[/dim]")

    # Today's P&L (V2: total = realized + unrealized; total_pnl column was removed)
    from vibe_trade.db.models import DailyPnL as DailyPnLModel
    today_pnl = session.query(DailyPnLModel).filter_by(date=date.today()).first()
    if today_pnl:
        total = (today_pnl.realized_pnl or 0.0) + (today_pnl.unrealized_pnl or 0.0)
        color = "green" if total >= 0 else "red"
        console.print(
            f"\nToday's P&L: [{color}]${total:,.2f}[/{color}] | "
            f"Account: ${today_pnl.account_value:,.2f}"
        )

    session.close()


@app.command()
def trades(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of recent trades"),
) -> None:
    """List recent trades."""
    config = load_config(config_path)
    session_factory = init_db(config.general.db_path)
    session = session_factory()

    from vibe_trade.db.repository import TradeRepository
    trade_repo = TradeRepository(session)
    recent = trade_repo.get_recent_trades(limit)

    if not recent:
        console.print("[dim]No trades yet[/dim]")
        session.close()
        return

    table = Table(title=f"Recent Trades (last {limit})")
    table.add_column("Symbol", style="cyan")
    table.add_column("Side")
    table.add_column("Qty", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Exit", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("Status")
    table.add_column("Strategy")

    for t in recent:
        pnl_str = ""
        if t.pnl is not None:
            color = "green" if t.pnl >= 0 else "red"
            pnl_str = f"[{color}]${t.pnl:,.2f}[/{color}]"

        qty = t.filled_quantity if t.filled_quantity is not None else t.requested_quantity
        table.add_row(
            t.symbol,
            t.side,
            str(qty),
            f"${t.entry_price:.2f}" if t.entry_price else "-",
            f"${t.exit_price:.2f}" if t.exit_price else "-",
            pnl_str or "-",
            t.status,
            t.strategy_name,
        )

    console.print(table)
    session.close()


@app.command()
def report(
    days: int = typer.Option(30, "--days", "-d",
                             help="Calendar days back from today"),
    config_path: Optional[str] = typer.Option(None, "--config", "-c",
                                              help="Config file path"),
) -> None:
    """Read-only performance dashboard: equity, holdings, activity, trade stats."""
    from datetime import date as date_cls

    from vibe_trade.reports.data import (
        detect_outlier_days,
        load_closed_trades,
        load_daily_pnl,
        load_latest_holdings,
        load_trade_activity,
    )
    from vibe_trade.reports.metrics import (
        compute_closed_trade_stats,
        compute_metrics,
    )
    from vibe_trade.reports.render import render_report

    config = load_config(config_path)
    session_factory = init_db(config.general.db_path)
    session = session_factory()
    try:
        today = date_cls.today()
        daily_rows = load_daily_pnl(session, days, today)
        holdings_as_of, holdings = load_latest_holdings(session)
        activity = load_trade_activity(session, days, today)
        closed = load_closed_trades(session, days, today)
        outliers = detect_outlier_days(daily_rows)

        metrics = compute_metrics(daily_rows)
        closed_stats = compute_closed_trade_stats(closed)

        render_report(
            metrics=metrics,
            daily_rows=daily_rows,
            holdings=holdings,
            holdings_as_of=holdings_as_of,
            activity=activity,
            closed_stats=closed_stats,
            outliers=outliers,
            window_days=days,
            today=today,
            console=console,
        )
    finally:
        session.close()


@app.command(name="report-weekly")
def report_weekly(
    config_path: Optional[str] = typer.Option(None, "--config", "-c",
                                              help="Config file path"),
) -> None:
    """Weekly report job (Saturday morning).

    Builds a dashboard PNG (equity curve + holdings + metrics) over the last
    7 days, writes it to ``general.reports_dir``, and sends it to Telegram.
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    _run_with_crash_alert("report-weekly", config, _run_report_weekly_cli)


async def _run_report_weekly_cli(config) -> None:
    from datetime import date as _date
    from pathlib import Path

    from vibe_trade.reports.data import (
        detect_outlier_days,
        load_closed_trades,
        load_daily_pnl,
        load_latest_holdings,
        load_trade_activity,
    )
    from vibe_trade.reports.metrics import (
        compute_closed_trade_stats,
        compute_metrics,
    )
    from vibe_trade.reports.plot import save_report_plot

    window_days = 7
    notifier = _get_notifier(config)
    session_factory = init_db(config.general.db_path)
    session = session_factory()
    try:
        today = _date.today()
        daily_rows = load_daily_pnl(session, window_days, today)
        holdings_as_of, holdings = load_latest_holdings(session)
        activity = load_trade_activity(session, window_days, today)
        closed = load_closed_trades(session, window_days, today)
        outliers = detect_outlier_days(daily_rows)

        metrics = compute_metrics(daily_rows)
        closed_stats = compute_closed_trade_stats(closed)

        png_path = save_report_plot(
            metrics=metrics,
            daily_rows=daily_rows,
            holdings=holdings,
            holdings_as_of=holdings_as_of,
            activity=activity,
            closed_stats=closed_stats,
            outliers=outliers,
            window_days=window_days,
            today=today,
            output_path=Path(config.general.reports_dir),
            period_label="Weekly",
        )
    finally:
        session.close()

    console.print(f"[green]Weekly report saved -> {png_path}[/green]")

    if metrics.sample_size == 0:
        caption = f"vibe_trade weekly report ({today}) -- no P&L data this week."
    else:
        caption = (
            f"vibe_trade weekly report ({today})\n"
            f"Return {metrics.total_return_pct:+.2f}%  "
            f"Sharpe {metrics.sharpe:.2f}  "
            f"MaxDD {metrics.max_drawdown_pct:.2f}%  "
            f"Opened {sum(activity.values())}  Closed {closed_stats.n}"
        )
    await notifier.notify_report_image(png_path, caption=caption)


@app.command(name="config-check")
def config_check(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """Validate the config file."""
    try:
        from vibe_trade.strategy.registry import build_strategies

        config = load_config(config_path)
        # Resolve the registry so an unknown/typo'd strategy id is caught here,
        # not mid-cron. Order shown is the entry-priority order.
        built = build_strategies(config.strategies, config.risk.pct_per_position)
        active = ", ".join(
            f"{b.strategy.name}({b.pct_per_position:.3%})" for b in built
        )
        console.print("[green]Config is valid[/green]")
        console.print(f"  Mode: {config.general.mode}")
        console.print(f"  Broker: IB @ {config.broker.host}:{config.broker.get_port(config.general.mode)}")
        console.print(f"  Universe: {config.universe.source}")
        console.print(f"  Max positions: {config.risk.max_open_positions}")
        console.print(f"  Strategies (priority): {active or '(none enabled)'}")
        console.print(f"  Telegram: {'enabled' if config.telegram.enabled else 'disabled'}")
    except Exception as e:
        console.print(f"[red]Config error: {e}[/red]")
        raise typer.Exit(1)


@app.command(name="close-position")
def close_position(
    symbol: str = typer.Argument(..., help="Ticker to close (e.g. AAPL)"),
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Manually market-close the full position for one ticker (off-cycle).

    No DB write -- the next record/reconcile run persists the resulting fill
    via ib.fills(), exactly as it does for a submit-phase exit.
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    asyncio.run(_run_close_position_cli(config, symbol.upper(), confirm))


async def _run_close_position_cli(config, symbol: str, skip_confirm: bool) -> None:
    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.jobs.override import OVERRIDE_CLIENT_ID, run_close_position

    broker_config = config.broker.model_copy()
    broker_config.client_id = OVERRIDE_CLIENT_ID
    broker = IBBroker(broker_config, mode=config.general.mode)
    notifier = _get_notifier(config)

    console.print(
        f"[bold]Close position[/bold] {symbol} mode={config.general.mode} "
        f"client_id={OVERRIDE_CLIENT_ID}"
    )

    def _confirm(sym: str, qty: int) -> bool:
        if skip_confirm:
            return True
        return typer.confirm(f"Close your full {sym} position ({qty} shares)?")

    await broker.connect()
    try:
        result = await run_close_position(
            broker=broker, symbol=symbol, confirm=_confirm
        )
    finally:
        await broker.disconnect()

    if not result.found:
        console.print(f"[yellow]{symbol} is not held -- nothing to close.[/yellow]")
        raise typer.Exit(1)
    if result.aborted:
        console.print("[dim]Aborted -- no order placed.[/dim]")
        return

    console.print(
        f"[green]Placed SELL {result.quantity}x {symbol}[/green] "
        f"-- status={result.status}"
    )
    console.print(
        "[dim]No DB write; the next record/reconcile run persists the fill.[/dim]"
    )
    await notifier.notify_summary(
        f"[CLOSE-POSITION] {symbol}: SELL {result.quantity} ({result.status})"
    )


@app.command(name="cancel-pending")
def cancel_pending(
    symbol: Optional[str] = typer.Argument(
        None, help="Ticker whose working orders to cancel; omit to just list"
    ),
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """List working orders, or cancel every working order for one ticker.

    With no argument: prints the working-order table and exits, cancelling
    nothing. With a ticker: cancels all of that ticker's working orders.
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    asyncio.run(
        _run_cancel_pending_cli(config, symbol.upper() if symbol else None)
    )


async def _run_cancel_pending_cli(config, symbol: str | None) -> None:
    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.jobs.override import run_cancel_pending
    from vibe_trade.jobs.submit import SUBMIT_CLIENT_ID

    # Connect as submit's client: IB only honours cancelOrder from the client
    # that placed the order, and openTrades() is client-scoped. See the module
    # docstring in jobs/override.py.
    broker_config = config.broker.model_copy()
    broker_config.client_id = SUBMIT_CLIENT_ID
    broker = IBBroker(broker_config, mode=config.general.mode)
    notifier = _get_notifier(config)

    console.print(
        f"[bold]Cancel pending[/bold] mode={config.general.mode} "
        f"client_id={SUBMIT_CLIENT_ID}"
    )

    await broker.connect()
    try:
        result = await run_cancel_pending(broker=broker, symbol=symbol)
    finally:
        await broker.disconnect()

    if result.listing:
        table = Table(title="Working Orders")
        table.add_column("Symbol", style="cyan")
        table.add_column("Side")
        table.add_column("Qty", justify="right")
        table.add_column("permId", justify="right")
        table.add_column("Status")
        for o in result.listing:
            table.add_row(
                o.symbol, o.side, str(o.quantity), str(o.perm_id), o.status
            )
        console.print(table)
    else:
        console.print("[dim]No working orders.[/dim]")

    if symbol is None:
        return

    if not result.matched:
        console.print(
            f"[yellow]No working order for {symbol} -- nothing cancelled.[/yellow]"
        )
        raise typer.Exit(1)

    console.print(
        f"[green]Cancelled {len(result.cancelled)} order(s) for {symbol}[/green]"
    )
    await notifier.notify_summary(
        f"[CANCEL-PENDING] {symbol}: cancelled {len(result.cancelled)} order(s)"
    )


@app.command()
def preflight(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
    quiet: bool = typer.Option(
        False, "--quiet", help="Only notify on failure (skip the daily heartbeat)"
    ),
) -> None:
    """Verify today's submit can work. Run at 15:50, 10 min before submit.

    Read-only: no orders, no DB writes. Exits non-zero if anything is not ready,
    so cron surfaces it. Sends Telegram on failure and (unless --quiet) on
    success too, so that silence becomes the anomaly rather than just one more
    failure message.
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    _run_with_crash_alert("preflight", config, _run_preflight_cli, quiet=quiet)


async def _run_preflight_cli(config, *, quiet: bool = False) -> None:
    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.data.universe import load_universe
    from vibe_trade.jobs.preflight import run_preflight
    from vibe_trade.jobs.submit import SUBMIT_CLIENT_ID
    from vibe_trade.strategy.registry import build_strategies

    # Connect as submit's client so we exercise the exact identity submit will
    # use: if client_id 1 is already taken by a stuck process, we find out now.
    broker_config = config.broker.model_copy()
    broker_config.client_id = SUBMIT_CLIENT_ID
    broker = IBBroker(broker_config, mode=config.general.mode)
    notifier = _get_notifier(config)

    universe = load_universe(config.universe)
    strategies = build_strategies(config.strategies, config.risk.pct_per_position)

    await broker.connect()
    try:
        result = await run_preflight(
            broker=broker,
            universe=universe,
            strategies=strategies,
            max_positions=config.risk.max_open_positions,
            mode=config.general.mode,
        )
    finally:
        await broker.disconnect()

    table = Table(title="Preflight")
    table.add_column("Check", style="cyan")
    table.add_column("")
    table.add_column("Detail", overflow="fold")
    for c in result.checks:
        table.add_row(
            c.name,
            "[green]OK[/green]" if c.ok else "[red]FAIL[/red]",
            c.detail,
        )
    console.print(table)

    if result.ok:
        console.print("[bold green]READY[/bold green] - submit can run at 16:00")
        if not quiet:
            await notifier.notify_summary(
                f"[PREFLIGHT] {date.today().isoformat()} READY - "
                f"net_liq=${result.net_liquidation:,.2f}, "
                f"{result.held_count} held, universe={result.universe_size}, "
                f"strategies={','.join(result.strategy_names)}"
            )
        return

    detail = "; ".join(f"{c.name}: {c.detail}" for c in result.failures)
    console.print(f"[bold red]NOT READY[/bold red] - {detail}")
    await notifier.notify_error(
        f"[PREFLIGHT] {date.today().isoformat()} NOT READY - {detail}\n"
        f"Submit runs at 16:00. Fix before then."
    )
    raise typer.Exit(1)


@app.command(name="review-trades")
def review_trades(
    resolve: Optional[int] = typer.Option(
        None, "--resolve", help="Trade id to resolve (see the listing for ids)"
    ),
    exit_price: Optional[float] = typer.Option(
        None, "--exit-price", help="Actual exit price; closes the row and computes P&L"
    ),
    write_off: bool = typer.Option(
        False, "--write-off",
        help="Mark the row CANCELLED instead — the position was never really ours",
    ),
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """List or resolve NEEDS_REVIEW trades. DB-only, never touches IB.

    Reconcile's drift sweep parks a row here when IB doesn't hold the symbol and
    the exit fill is gone from `ib.fills()` (only the current session is returned,
    so a missed reconcile day loses it permanently). It deliberately refuses to
    invent an exit price, which is what this command supplies.

    \b
    vibe-trade review-trades                                  # list them
    vibe-trade review-trades --resolve 27 --exit-price 415.50  # close with real price
    vibe-trade review-trades --resolve 27 --write-off          # never a real position
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    session_factory = init_db(config.general.db_path)

    from datetime import datetime

    from vibe_trade.db.repository import TradeRepository

    with session_factory() as session:
        repo = TradeRepository(session)

        if resolve is None:
            rows = repo.get_needs_review()
            if not rows:
                console.print("[green]No trades need review.[/green]")
                return
            table = Table(title="Trades Needing Review")
            table.add_column("id", justify="right", style="cyan")
            table.add_column("Symbol")
            table.add_column("Qty", justify="right")
            table.add_column("Entry", justify="right")
            table.add_column("Entered")
            table.add_column("Why", overflow="fold")
            for t in rows:
                table.add_row(
                    str(t.id), t.symbol, str(t.filled_quantity),
                    f"{t.entry_price:.2f}" if t.entry_price else "-",
                    t.entry_time.strftime("%Y-%m-%d") if t.entry_time else "-",
                    t.notes or "",
                )
            console.print(table)
            console.print(
                "\n[dim]Resolve with: vibe-trade review-trades --resolve <id> "
                "--exit-price <px>[/dim]"
            )
            return

        if write_off:
            trade = repo.mark_cancelled(
                resolve, "manually written off — position never held at IB"
            )
            console.print(
                f"[yellow]Trade {trade.id} {trade.symbol} -> CANCELLED "
                f"(written off)[/yellow]"
            )
            return

        if exit_price is None:
            console.print(
                "[red]--exit-price is required to resolve "
                "(or pass --write-off).[/red]"
            )
            raise typer.Exit(2)

        trade = repo.resolve_needs_review(
            resolve, exit_price=exit_price, exit_time=datetime.now()
        )
        console.print(
            f"[green]Trade {trade.id} {trade.symbol} -> CLOSED[/green]  "
            f"{trade.entry_price:.2f} -> {exit_price:.2f} x{trade.filled_quantity}  "
            f"P&L ${trade.pnl:+,.2f} ({trade.pnl_pct:+.2f}%)"
        )


@app.command()
def panic(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """PANIC: Close all positions immediately.

    Cancels every working order, then market-closes every held position.
    Uses its own client id (never submit's) so it can still connect if
    another job's client id is stuck -- precisely the scenario an operator
    reaches for this in.
    """
    if not confirm:
        typer.confirm(
            "This will close ALL positions immediately. Are you sure?",
            abort=True,
        )

    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    console.print("[bold red]PANIC MODE — Closing all positions[/bold red]")
    _run_with_crash_alert("panic", config, _run_panic_cli)


async def _run_panic_cli(config) -> None:
    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.risk.panic import PANIC_CLIENT_ID, panic_close_all

    broker_config = config.broker.model_copy()
    broker_config.client_id = PANIC_CLIENT_ID
    broker = IBBroker(broker_config, mode=config.general.mode)
    notifier = _get_notifier(config)

    await broker.connect()
    try:
        result = await panic_close_all(broker)
    finally:
        await broker.disconnect()

    console.print(f"[dim]Cancelled {result.cancelled_orders} open order(s)[/dim]")
    for d in result.details:
        status_color = "green" if d["ok"] else "red"
        console.print(
            f"  [{status_color}]{d['status']}[/{status_color}] "
            f"{d['side']} {d['quantity']}x {d['symbol']}"
        )

    attempted = result.closed + result.failed
    msg = f"PANIC executed: {result.closed}/{attempted} position(s) closed"
    if not result.all_succeeded:
        msg += f" -- {result.failed} FAILED, check the account immediately"
    await notifier.notify_panic(msg)

    if result.all_succeeded:
        console.print(f"[green]All {attempted} position(s) closed[/green]")
    else:
        console.print(
            f"[bold red]{result.failed} of {attempted} position(s) FAILED to "
            f"close -- check the account immediately[/bold red]"
        )
        # Raise (rather than a quiet non-zero typer.Exit) so this goes through
        # the same path as any other job failure: _run_with_crash_alert fires
        # a second, distinctly-worded [CRITICAL] alert and the process exits
        # non-zero. A partial panic failure is not a case to under-alert on.
        raise RuntimeError(msg)
