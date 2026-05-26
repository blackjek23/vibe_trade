"""Rich terminal renderer for `vibe-trade report`.

Pure rendering -- no computation, no DB. Receives everything it needs
as keyword args. A future web UI renders the same dataclasses to HTML
instead of calling this.
"""

from __future__ import annotations

from datetime import date, timedelta

from rich.console import Console
from rich.table import Table

from vibe_trade.reports.data import DailyRow, HoldingRow
from vibe_trade.reports.metrics import ClosedTradeStats, ReportMetrics


SMALL_SAMPLE_THRESHOLD = 60


def render_report(
    *,
    metrics: ReportMetrics,
    daily_rows: list[DailyRow],
    holdings: list[HoldingRow],
    holdings_as_of: date | None,
    activity: dict[date, int],
    closed_stats: ClosedTradeStats,
    outliers: set[date],
    window_days: int,
    today: date,
    console: Console,
) -> None:
    # Empty-window guard -- single sentinel, no further sections.
    if metrics.sample_size == 0:
        console.print(
            f"[yellow]No daily P&L data in last {window_days} days. "
            f"Has reconcile run?[/yellow]"
        )
        return

    span_start = today - timedelta(days=window_days)
    _section_header(console, span_start, today, window_days, metrics.sample_size)
    _section_equity(console, metrics)
    _section_holdings(console, holdings, holdings_as_of)
    _section_activity(console, activity, outliers, closed_stats)
    _section_trade_stats(console, closed_stats)


def _section_header(
    console: Console, span_start: date, today: date,
    window_days: int, sample_size: int,
) -> None:
    console.print(
        f"\n[bold]vibe_trade report[/bold] -- {span_start} -> {today} "
        f"(last {window_days} days, {sample_size} daily_pnl rows)"
    )
    if sample_size < SMALL_SAMPLE_THRESHOLD:
        console.print(
            "[yellow]Small sample -- risk metrics below are indicative only.[/yellow]"
        )


def _section_equity(console: Console, metrics: ReportMetrics) -> None:
    t = Table(title="Equity & Risk", show_header=False)
    t.add_column("Metric", style="cyan")
    t.add_column("Value")

    if metrics.start_value is not None and metrics.end_value is not None:
        color = "green" if metrics.total_return_pct >= 0 else "red"
        t.add_row(
            "Account value (start -> end)",
            f"${metrics.start_value:,.2f} -> ${metrics.end_value:,.2f}  "
            f"([{color}]{metrics.total_return_pct:+.2f}%[/{color}])",
        )

    thin = metrics.sample_size < SMALL_SAMPLE_THRESHOLD
    if metrics.cagr_pct is None:
        t.add_row("CAGR (annualized)", "n/a (span too short)")
    else:
        suffix = " [yellow](extrapolated from short span)[/yellow]" if thin else ""
        t.add_row("CAGR (annualized)", f"{metrics.cagr_pct:+.1f}%{suffix}")

    sharpe_suffix = " [yellow](thin sample)[/yellow]" if thin else ""
    t.add_row(
        "Sharpe (daily, 252-day annualized)",
        f"{metrics.sharpe:.2f}{sharpe_suffix}",
    )

    dd_text = f"{metrics.max_drawdown_pct:.2f}%"
    if metrics.max_dd_peak_date and metrics.max_dd_trough_date:
        dd_text += (
            f" (peak {metrics.max_dd_peak_date}, "
            f"trough {metrics.max_dd_trough_date})"
        )
    t.add_row("Max drawdown", dd_text)

    t.add_row(
        "Best day / Worst day",
        f"[green]${metrics.best_day_pnl:+,.0f}[/green] / "
        f"[red]${metrics.worst_day_pnl:+,.0f}[/red]",
    )
    console.print(t)


def _section_holdings(
    console: Console, holdings: list[HoldingRow], holdings_as_of: date | None,
) -> None:
    if not holdings:
        if holdings_as_of is None:
            console.print("\n[bold]Holdings[/bold]: No portfolio snapshots yet.")
        else:
            console.print(
                f"\n[bold]Holdings as of {holdings_as_of}[/bold]: "
                f"No open positions."
            )
        return

    total_mv = sum((h.market_value or 0.0) for h in holdings)
    total_unr = sum((h.unrealized_pnl or 0.0) for h in holdings)
    unr_color = "green" if total_unr >= 0 else "red"
    console.print(
        f"\n[bold]Holdings as of {holdings_as_of}[/bold] "
        f"({len(holdings)} positions, total market value ${total_mv:,.0f}, "
        f"unrealized [{unr_color}]${total_unr:+,.0f}[/{unr_color}])"
    )
    sorted_h = sorted(
        holdings, key=lambda h: (h.unrealized_pnl or 0.0), reverse=True,
    )
    winners = sorted_h[:5]
    losers = sorted(sorted_h[-5:], key=lambda h: (h.unrealized_pnl or 0.0))
    _render_holdings_table(console, "Top 5 winners", winners)
    _render_holdings_table(console, "Top 5 losers", losers)


def _render_holdings_table(
    console: Console, title: str, holdings: list[HoldingRow],
) -> None:
    t = Table(title=title)
    t.add_column("Symbol", style="cyan")
    t.add_column("Qty", justify="right")
    t.add_column("Avg cost", justify="right")
    t.add_column("Market", justify="right")
    t.add_column("Unrealized $", justify="right")
    t.add_column("Unrealized %", justify="right")
    for h in holdings:
        pnl = h.unrealized_pnl or 0.0
        color = "green" if pnl >= 0 else "red"
        pct_text = "-"
        if h.avg_cost and h.market_price and h.avg_cost > 0:
            pct_text = (
                f"[{color}]{(h.market_price / h.avg_cost - 1) * 100:+.1f}%"
                f"[/{color}]"
            )
        t.add_row(
            h.symbol,
            str(h.quantity),
            f"${h.avg_cost:.2f}" if h.avg_cost else "-",
            f"${h.market_price:.2f}" if h.market_price else "-",
            f"[{color}]${pnl:+,.0f}[/{color}]",
            pct_text,
        )
    console.print(t)


def _section_activity(
    console: Console, activity: dict[date, int],
    outliers: set[date], closed_stats: ClosedTradeStats,
) -> None:
    console.print("\n[bold]Trade activity[/bold]")
    # Merge activity dates with outlier dates so an outage day with zero
    # entries still surfaces in the day-by-day breakdown.
    all_dates = sorted(set(activity.keys()) | outliers)
    if all_dates:
        console.print("Trades opened by day:")
        parts = []
        for d in all_dates:
            mark = " [yellow]!warn[/yellow]" if d in outliers else ""
            count = activity.get(d, 0)
            parts.append(f"{d.strftime('%m-%d')}: {count}{mark}")
        # Print 4 entries per line for readability.
        for i in range(0, len(parts), 4):
            console.print("  " + "    ".join(parts[i:i + 4]))
        console.print(f"Total opened in window: {sum(activity.values())}")
    else:
        console.print("  No trade entries in window.")

    suffix = " [dim](no SELLs yet)[/dim]" if closed_stats.n == 0 else ""
    console.print(f"Trades closed in window: {closed_stats.n}{suffix}")

    if outliers:
        console.print(
            "[dim]!warn days where snapshot shows 0 open positions while "
            "realized P&L is non-zero -- typically a Gateway outage or "
            "reconcile-time anomaly. Included in metrics (note this "
            "inflates Best day / CAGR).[/dim]"
        )


def _section_trade_stats(
    console: Console, closed_stats: ClosedTradeStats,
) -> None:
    console.print("\n[bold]Trade stats[/bold]")
    if closed_stats.n == 0:
        console.print(
            "  Win rate / avg win / avg loss / profit factor / avg holding days\n"
            "  [dim]n/a -- no closed trades yet "
            "(becomes meaningful after the first SELL)[/dim]"
        )
        return

    wins = round(closed_stats.win_rate * closed_stats.n)
    losses = closed_stats.n - wins
    t = Table(show_header=False)
    t.add_column("Metric", style="cyan")
    t.add_column("Value")
    t.add_row("Closed trades in window", str(closed_stats.n))
    t.add_row(
        "Win rate",
        f"{closed_stats.win_rate * 100:.1f}%  ({wins}W / {losses}L)",
    )
    t.add_row(
        "Avg win / Avg loss",
        f"[green]${closed_stats.avg_win:+,.0f}[/green] / "
        f"[red]${closed_stats.avg_loss:+,.0f}[/red]",
    )
    pf = (
        "inf" if closed_stats.profit_factor == float("inf")
        else f"{closed_stats.profit_factor:.2f}"
    )
    t.add_row("Profit factor", pf)
    t.add_row("Avg holding days", f"{closed_stats.avg_holding_days:.1f}")
    console.print(t)
