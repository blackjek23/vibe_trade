"""Matplotlib dashboard image for the scheduled `report-weekly` job.

One self-contained PNG: equity curve (top), holdings bar chart (bottom-left),
and a key-metrics text panel (bottom-right). Consumes the SAME dataclasses
that `render.py` renders to the terminal -- no new data plumbing.

Mirrors the style of `backtest/plot.py` (Agg backend, hex palette, dpi=150,
bbox_inches="tight"). `period_label` keys the title + filename so a future
`report-monthly` is a one-line caller change.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from vibe_trade.reports.data import DailyRow, HoldingRow
from vibe_trade.reports.metrics import ClosedTradeStats, ReportMetrics

# Shared palette with backtest/plot.py.
EQUITY_COLOR = "#2563eb"
GAIN_COLOR = "#10b981"
LOSS_COLOR = "#ef4444"

SMALL_SAMPLE_THRESHOLD = 60
MAX_HOLDINGS_BARS = 10


def save_report_plot(
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
    output_path: Path,
    period_label: str = "Weekly",
) -> Path:
    """Render the dashboard PNG and return its path.

    Filename is ``{today}-{period_label.lower()}.png`` under ``output_path``
    (which is created if missing). An empty window still produces a sentinel
    PNG so the scheduled job always emits a file.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_path.mkdir(parents=True, exist_ok=True)
    png_path = output_path / f"{today.isoformat()}-{period_label.lower()}.png"

    span_start = today - timedelta(days=window_days)
    title = (
        f"vibe_trade -- {period_label} Report -- {span_start} -> {today} "
        f"(last {window_days} days)"
    )

    # Empty window: single-panel sentinel so the operator still gets a file.
    if metrics.sample_size == 0:
        fig, ax = plt.subplots(figsize=(14, 8))
        fig.suptitle(title, fontsize=14, fontweight="bold")
        ax.axis("off")
        ax.text(
            0.5, 0.5,
            f"No daily P&L data in last {window_days} days.\nHas reconcile run?",
            ha="center", va="center", fontsize=16, color=LOSS_COLOR,
        )
        fig.savefig(png_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        return png_path

    fig = plt.figure(figsize=(14, 8))
    fig.suptitle(title, fontsize=14, fontweight="bold")
    gs = fig.add_gridspec(2, 2, height_ratios=[3, 2], hspace=0.3, wspace=0.2)
    ax_eq = fig.add_subplot(gs[0, :])
    ax_hold = fig.add_subplot(gs[1, 0])
    ax_text = fig.add_subplot(gs[1, 1])

    _panel_equity(ax_eq, daily_rows, outliers)
    _panel_holdings(ax_hold, holdings, holdings_as_of)
    _panel_metrics(ax_text, metrics, activity, closed_stats)

    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return png_path


def _panel_equity(ax, daily_rows: list[DailyRow], outliers: set[date]) -> None:
    import matplotlib.ticker as mticker

    rows = sorted(
        [r for r in daily_rows if r.account_value is not None],
        key=lambda r: r.date,
    )
    ax.set_title("Account value", fontsize=11, loc="left")
    ax.set_ylabel("Value ($)")
    ax.grid(True, alpha=0.3)

    if len(rows) < 2:
        ax.text(
            0.5, 0.5, "Insufficient data for an equity curve",
            ha="center", va="center", fontsize=12, color="#6b7280",
            transform=ax.transAxes,
        )
        ax.set_xticks([])
        return

    dates = [r.date for r in rows]
    values = [r.account_value for r in rows]
    ax.plot(dates, values, linewidth=1.5, color=EQUITY_COLOR, marker="o", markersize=4)

    # Label each point with its value just above the marker.
    ax.set_ymargin(0.18)  # headroom so top labels aren't clipped
    for d, v in zip(dates, values):
        ax.annotate(
            f"${v:,.0f}", (d, v),
            textcoords="offset points", xytext=(0, 7),
            ha="center", fontsize=8, color="black",
        )

    # Flag outlier days (snapshot shows 0 positions but realized P&L != 0).
    out_pts = [(r.date, r.account_value) for r in rows if r.date in outliers]
    if out_pts:
        ax.scatter(
            [d for d, _ in out_pts], [v for _, v in out_pts],
            color=LOSS_COLOR, zorder=5, s=40, label="outlier day",
        )
        ax.legend(loc="upper left", framealpha=0.9)

    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    # Pin ticks to the actual data dates so a short window doesn't produce
    # interpolated duplicate-looking labels. Thin to ~12 ticks for longer windows.
    step = max(1, len(dates) // 12)
    ticks = dates[::step]
    ax.set_xticks(ticks)
    ax.set_xticklabels([d.strftime("%m-%d") for d in ticks], rotation=0)


def _panel_holdings(
    ax, holdings: list[HoldingRow], holdings_as_of: date | None,
) -> None:
    import matplotlib.ticker as mticker

    as_of = f" (as of {holdings_as_of})" if holdings_as_of else ""
    ax.set_title(f"Holdings unrealized P&L{as_of}", fontsize=11, loc="left")

    if not holdings:
        ax.axis("off")
        ax.text(
            0.5, 0.5, "No open positions",
            ha="center", va="center", fontsize=12, color="#6b7280",
        )
        return

    top = sorted(
        holdings, key=lambda h: abs(h.unrealized_pnl or 0.0), reverse=True,
    )[:MAX_HOLDINGS_BARS]
    # Plot ascending so the largest bar sits at the top.
    top = sorted(top, key=lambda h: (h.unrealized_pnl or 0.0))
    symbols = [h.symbol for h in top]
    pnls = [h.unrealized_pnl or 0.0 for h in top]
    colors = [GAIN_COLOR if p >= 0 else LOSS_COLOR for p in pnls]

    bars = ax.barh(symbols, pnls, color=colors)
    ax.axvline(0, color="#9ca3af", linewidth=0.8)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.grid(True, axis="x", alpha=0.3)

    # Label each bar with its P&L. Place inside the bar end when the bar is
    # long enough; otherwise just outside the tip so short bars stay readable.
    span = max((abs(p) for p in pnls), default=1.0) or 1.0
    for bar, p in zip(bars, pnls):
        inside = abs(p) > span * 0.18
        if p >= 0:
            x = p - span * 0.02 if inside else p + span * 0.02
            ha = "right" if inside else "left"
        else:
            x = p + span * 0.02 if inside else p - span * 0.02
            ha = "left" if inside else "right"
        ax.text(
            x, bar.get_y() + bar.get_height() / 2, f"${p:,.0f}",
            va="center", ha=ha, fontsize=8,
            color="white" if inside else "#374151",
        )
    ax.margins(x=0.15)  # room for outside labels on short bars


def _panel_metrics(
    ax, metrics: ReportMetrics, activity: dict[date, int],
    closed_stats: ClosedTradeStats,
) -> None:
    ax.axis("off")
    ax.set_title("Summary", fontsize=11, loc="left")

    thin = metrics.sample_size < SMALL_SAMPLE_THRESHOLD
    lines: list[str] = []

    if metrics.start_value is not None and metrics.end_value is not None:
        lines.append(
            f"Account: ${metrics.start_value:,.0f} -> ${metrics.end_value:,.0f}"
            f"  ({metrics.total_return_pct:+.2f}%)"
        )
    lines.append(f"Sharpe (annualized): {metrics.sharpe:.2f}")
    lines.append(f"Max drawdown: {metrics.max_drawdown_pct:.2f}%")
    lines.append(
        f"Best / Worst day: ${metrics.best_day_pnl:+,.0f} / "
        f"${metrics.worst_day_pnl:+,.0f}"
    )
    lines.append("")
    lines.append(f"Trades opened: {sum(activity.values())}")
    if closed_stats.n == 0:
        lines.append("Trades closed: 0 (no SELLs yet)")
    else:
        wins = round(closed_stats.win_rate * closed_stats.n)
        lines.append(
            f"Trades closed: {closed_stats.n}  "
            f"(win rate {closed_stats.win_rate * 100:.0f}%, {wins}W)"
        )
    lines.append(f"Daily P&L rows: {metrics.sample_size}")
    if thin:
        lines.append("")
        lines.append("(thin sample -- risk metrics indicative only)")

    ax.text(
        0.0, 1.0, "\n".join(lines),
        ha="left", va="top", fontsize=12, family="monospace",
        transform=ax.transAxes,
    )
