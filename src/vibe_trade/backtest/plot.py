"""Backtrader-style equity curve + drawdown plot with benchmark overlay."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def save_backtest_plot(
    equity_curve: pd.Series,
    benchmarks: dict[str, pd.Series],
    output_path: Path,
    starting_equity: float = 100_000.0,
) -> Path:
    """Save a two-panel PNG: equity curve (top) + drawdown (bottom).

    `benchmarks` is {symbol: close_prices} — each gets normalized to
    `starting_equity` so all lines share the same y-axis scale.

    Returns the path to the saved PNG.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import matplotlib.ticker as mticker

    fig, (ax_eq, ax_dd) = plt.subplots(
        2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]},
        sharex=True,
    )
    fig.suptitle("Backtest: Donchian Breakout vs Benchmarks", fontsize=14, fontweight="bold")

    # --- top panel: equity curve + benchmarks
    ax_eq.plot(equity_curve.index, equity_curve.values, label="Strategy", linewidth=1.5, color="#2563eb")

    bench_colors = {"SPY": "#f59e0b", "QQQ": "#10b981"}
    for sym, closes in benchmarks.items():
        if closes.empty:
            continue
        normalized = closes / closes.iloc[0] * starting_equity
        aligned = normalized.reindex(equity_curve.index, method="ffill")
        color = bench_colors.get(sym, "#6b7280")
        ax_eq.plot(aligned.index, aligned.values, label=f"{sym} B&H", linewidth=1.0, linestyle="--", color=color)

    ax_eq.set_ylabel("Portfolio Value ($)")
    ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax_eq.legend(loc="upper left", framealpha=0.9)
    ax_eq.grid(True, alpha=0.3)

    # --- bottom panel: drawdown
    running_peak = equity_curve.cummax()
    drawdown_pct = (equity_curve / running_peak - 1.0) * 100.0
    ax_dd.fill_between(drawdown_pct.index, drawdown_pct.values, 0, alpha=0.4, color="#ef4444")
    ax_dd.plot(drawdown_pct.index, drawdown_pct.values, linewidth=0.8, color="#ef4444")
    ax_dd.set_ylabel("Drawdown (%)")
    ax_dd.set_xlabel("Date")
    ax_dd.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax_dd.grid(True, alpha=0.3)

    ax_dd.xaxis.set_major_locator(mdates.YearLocator())
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.autofmt_xdate(rotation=0, ha="center")

    plt.tight_layout()
    png_path = output_path / "backtest.png"
    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return png_path
