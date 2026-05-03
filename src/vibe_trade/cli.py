"""CLI commands for Vibe Trade."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from vibe_trade.config import load_config
from vibe_trade.db.engine import init_db

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


@app.command()
def backtest(
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD (exclusive)"),
    top_n: int = typer.Option(100, "--top-n", help="Top N S&P 500 by market cap"),
    pct: float = typer.Option(0.04, "--pct", help="Per-position size as fraction of net_liq"),
    max_positions: int = typer.Option(25, "--max-positions", help="Position cap"),
    equity: float = typer.Option(100_000.0, "--equity", help="Starting equity"),
    output_dir: Optional[str] = typer.Option(
        None, "--output", help="Output directory (default: backtests/<timestamp>/)"
    ),
    force_refresh: bool = typer.Option(
        False, "--force-refresh", help="Re-fetch bars + market caps even if cached"
    ),
    config_path: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Backtest the locked Donchian strategy against S&P 500 history.

    Default settings differ from production: top-100 / 4% / 25-cap (vs
    production's full universe / 1.8% / 50-cap) for cleaner read-out per
    docs/ROADMAP.md Session I.
    """
    config = load_config(config_path)
    _setup_logging(config.general.log_level)
    _run_backtest_cli(
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
        top_n=top_n,
        pct=pct,
        max_positions=max_positions,
        equity=equity,
        output_dir=output_dir,
        force_refresh=force_refresh,
    )


def _run_backtest_cli(
    *, start, end, top_n: int, pct: float, max_positions: int,
    equity: float, output_dir: str | None, force_refresh: bool,
) -> None:
    import json
    from dataclasses import asdict
    from datetime import datetime as _dt

    from vibe_trade.backtest.data import fetch_and_cache_bars
    from vibe_trade.backtest.engine import run_backtest
    from vibe_trade.backtest.metrics import compute_metrics
    from vibe_trade.data.sp100_top import LAST_UPDATED, SP100_TOP_BY_MCAP
    from vibe_trade.strategy.examples.donchian import DonchianStrategy

    # ---------------------------------------------------------- universe
    console.print(
        f"[bold]Backtest[/bold] {start} -> {end}  top_n={top_n}  "
        f"pct={pct:.3f}  max_pos={max_positions}  equity=${equity:,.0f}"
    )
    if not SP100_TOP_BY_MCAP:
        console.print(
            "[red]ERROR:[/red] sp100_top.py is empty -- run `vibe-trade refresh-sp100` first."
        )
        raise typer.Exit(code=1)
    universe = SP100_TOP_BY_MCAP[:top_n]
    console.print(
        f"  using {len(universe)} symbols from sp100_top.py (LAST_UPDATED={LAST_UPDATED})"
    )

    # ---------------------------------------------------------- bars
    console.print("Ensuring historical bars are cached...")
    paths = fetch_and_cache_bars(
        universe, start=start, end=end, force_refresh=force_refresh,
    )
    console.print(f"  bars available for {len(paths)} symbols")

    # ---------------------------------------------------------- run
    console.print("Running backtest...")
    result = run_backtest(
        strategy=DonchianStrategy(),
        universe=list(paths.keys()),
        start=start, end=end,
        starting_equity=equity,
        pct_per_position=pct,
        max_positions=max_positions,
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
    bench_closes: dict[str, "pd.Series"] = {}
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
