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
from vibe_trade.notify.console import ConsoleNotifier
from vibe_trade.strategy.registry import load_strategies

app = typer.Typer(name="vibe-trade", help="Vibe Trade — Stock Trading Bot")
console = Console()


def _setup_logging(level: str, log_file: str | None = None) -> None:
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=fmt, handlers=handlers)


def _get_notifier(config):
    """Get the appropriate notifier based on config."""
    if config.telegram.enabled:
        from vibe_trade.notify.telegram import TelegramNotifier
        return TelegramNotifier(config.telegram)
    return ConsoleNotifier()


@app.command()
def scan(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """Run a single scan cycle."""
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    init_db(config.general.db_path)

    strategies = load_strategies(config.strategy)
    notifier = _get_notifier(config)

    console.print(f"[bold]Running scan cycle[/bold] (mode={config.general.mode})")
    from vibe_trade.scanner import run_scan_cycle
    asyncio.run(run_scan_cycle(config, strategies, notifier))
    console.print("[green]Scan complete[/green]")


@app.command()
def start(
    config_path: Optional[str] = typer.Option(None, "--config", "-c", help="Config file path"),
) -> None:
    """Start the scheduler for periodic scans."""
    config = load_config(config_path)
    _setup_logging(config.general.log_level, config.general.log_file)
    init_db(config.general.db_path)

    strategies = load_strategies(config.strategy)
    notifier = _get_notifier(config)

    console.print(
        f"[bold]Starting scheduler[/bold] — "
        f"every {config.scheduler.interval_minutes}min during market hours"
    )
    from vibe_trade.scheduler import start_scheduler
    start_scheduler(config, strategies, notifier)


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
        table.add_column("Trail Stop", justify="right")
        table.add_column("Strategy")

        for t in open_trades:
            table.add_row(
                t.symbol,
                t.side,
                str(t.quantity),
                f"${t.entry_price:.2f}" if t.entry_price else "-",
                f"${t.trailing_stop:.2f}" if t.trailing_stop else "-",
                t.strategy_name,
            )
        console.print(table)
    else:
        console.print("[dim]No open positions[/dim]")

    # Today's P&L
    from vibe_trade.db.models import DailyPnL as DailyPnLModel
    today_pnl = session.query(DailyPnLModel).filter_by(date=date.today()).first()
    if today_pnl:
        color = "green" if today_pnl.total_pnl >= 0 else "red"
        console.print(
            f"\nToday's P&L: [{color}]${today_pnl.total_pnl:,.2f}[/{color}] | "
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

        table.add_row(
            t.symbol,
            t.side,
            str(t.quantity),
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
