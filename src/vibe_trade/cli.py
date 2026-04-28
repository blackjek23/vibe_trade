"""CLI commands for Vibe Trade."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from vibe_trade.config import load_config
from vibe_trade.db.engine import init_db

app = typer.Typer(name="vibe-trade", help="Vibe Trade -- Stock Trading Bot")
console = Console()


def _setup_logging(level: str, log_file: str | None = None) -> None:
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=fmt, handlers=handlers)


@app.command()
def submit(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """V2 submit job (16:00 Asia/Jerusalem).

    Exits phase: SELL signals on held positions.
    Entries phase: BUY signals on universe minus held tickers.
    Places market orders, NO DB writes (record at 16:25 handles persistence).
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    asyncio.run(_run_submit_cli(config))


async def _run_submit_cli(config) -> None:
    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.data.provider import DataProvider
    from vibe_trade.data.universe import load_universe
    from vibe_trade.jobs.submit import SUBMIT_CLIENT_ID, run_submit
    from vibe_trade.risk.manager import RiskManager
    from vibe_trade.strategy.examples.donchian import DonchianStrategy

    broker_config = config.broker.model_copy()
    broker_config.client_id = SUBMIT_CLIENT_ID

    broker = IBBroker(broker_config, mode=config.general.mode)
    universe = load_universe(config.universe)

    console.print(
        f"[bold]Submit[/bold] mode={config.general.mode} "
        f"client_id={SUBMIT_CLIENT_ID} universe_size={len(universe)}"
    )
    console.print(
        f"Connecting to {broker_config.host}:"
        f"{broker_config.get_port(config.general.mode)}..."
    )

    await broker.connect()
    try:
        result = await run_submit(
            broker=broker,
            strategy=DonchianStrategy(),
            data_provider=DataProvider(),
            risk_manager=RiskManager(config.risk),
            universe=universe,
            pct_per_position=config.risk.pct_per_position,
            max_positions=config.risk.max_open_positions,
        )
    finally:
        await broker.disconnect()

    _print_submit_summary(result)


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
    asyncio.run(_run_record_cli(config))


async def _run_record_cli(config) -> None:
    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.db.engine import init_db as _init
    from vibe_trade.db.repository import TradeRepository
    from vibe_trade.jobs.record import run_record
    from vibe_trade.jobs.submit import RECORD_CLIENT_ID

    broker_config = config.broker.model_copy()
    broker_config.client_id = RECORD_CLIENT_ID
    broker = IBBroker(broker_config, mode=config.general.mode)
    session_factory = _init(config.general.db_path)

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
    asyncio.run(_run_reconcile_cli(config))


async def _run_reconcile_cli(config) -> None:
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

    console.print(
        f"[bold]Reconcile[/bold] mode={config.general.mode} "
        f"client_id={RECONCILE_CLIENT_ID}"
    )

    await broker.connect()
    session = session_factory()
    try:
        await asyncio.sleep(1.0)
        result = await run_reconcile(
            broker=broker,
            trade_repo=TradeRepository(session),
            snap_repo=PortfolioSnapshotRepository(session),
            daily_repo=DailyPnLRepository(session),
        )
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
    table.add_row("portfolio_snapshot rows", str(result.snapshot_rows))
    console.print(table)
    if result.errors:
        console.print(f"\n[red]{len(result.errors)} error(s):[/red]")
        for e in result.errors[:10]:
            console.print(f"  - {e}")


@app.command()
def status(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """Show current open positions and daily P&L."""
    config = load_config(config_path)
    session_factory = init_db(config.general.db_path)
    session = session_factory()

    from vibe_trade.db.repository import DailyPnLRepository, TradeRepository
    from datetime import date

    trade_repo = TradeRepository(session)
    daily_repo = DailyPnLRepository(session)

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


@app.command(name="config-check")
def config_check(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """Validate the config file."""
    try:
        config = load_config(config_path)
        console.print("[green]Config is valid[/green]")
        console.print(f"  Mode: {config.general.mode}")
        console.print(f"  Broker: IB @ {config.broker.host}:{config.broker.get_port(config.general.mode)}")
        console.print(f"  Universe: {config.universe.source}")
        console.print(f"  Strategies: {config.strategy.active}")
        console.print(f"  Max positions: {config.risk.max_open_positions}")
        console.print(f"  Telegram: {'enabled' if config.telegram.enabled else 'disabled'}")
    except Exception as e:
        console.print(f"[red]Config error: {e}[/red]")
        raise typer.Exit(1)


@app.command()
def panic(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """PANIC: Close all positions immediately."""
    if not confirm:
        confirmed = typer.confirm(
            "This will close ALL positions immediately. Are you sure?",
            abort=True,
        )

    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)

    notifier = _get_notifier(config)

    async def _run_panic():
        from vibe_trade.broker.ib_broker import IBBroker
        from vibe_trade.risk.panic import panic_close_all

        broker = IBBroker(config.broker, mode=config.general.mode)
        await broker.connect()
        try:
            results = await panic_close_all(broker)
            for r in results:
                status_color = "green" if r["status"] == "FILLED" else "red"
                console.print(
                    f"  [{status_color}]{r['status']}[/{status_color}] "
                    f"{r['side']} {r['quantity']}x {r['symbol']}"
                )
            await notifier.notify_panic(
                f"PANIC executed: {len(results)} positions closed"
            )
        finally:
            await broker.disconnect()

    console.print("[bold red]PANIC MODE — Closing all positions[/bold red]")
    asyncio.run(_run_panic())
    console.print("[green]All positions closed[/green]")
