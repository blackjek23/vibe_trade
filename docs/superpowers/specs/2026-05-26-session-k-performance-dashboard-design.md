# Session K — Performance Dashboard

**Date:** 2026-05-26
**Status:** Approved design
**Scope:** New `vibe-trade report` CLI command + supporting `reports/` module. Pure read-only against the DB, no IB connection.

---

## Goal

A single command that summarises bot performance from `daily_pnl`, `trades`, and `portfolio_snapshot` tables. Designed to be useful from the very first paper week (when only a handful of `daily_pnl` rows exist) and grow more meaningful as data accumulates.

A future web UI (out of scope here) will reuse the same metrics module, so computation must be cleanly separated from rendering.

## Anchoring data (from `data/vibe_trade_sample.db`, May 11–25 paper run)

- `daily_pnl`: 12 rows, May 6 → May 25, with weekend gaps and one missing weekday (5/22).
- `trades`: 80 rows, **all `OPEN`**. Zero `CLOSED`. Donchian SELL hasn't fired.
- `portfolio_snapshot`: 521 rows across 11 distinct dates. Rich per-symbol per-day unrealized P&L.
- One known outlier day: 2026-05-13 has `realized=+4056`, `positions=0` — Gateway outage artifact, not a real trading day.
- The `daily_pnl.trades_opened` column is unreliable (reads 0 for days with confirmed entries). Activity must be derived from `trades.entry_time`, not this column.

The design accepts that current data is too thin for honest sharpe/drawdown numbers and surfaces that caveat in the output rather than hiding behind it.

## CLI shape

```
vibe-trade report [--days N] [--config PATH]
```

- `--days N` — int, default 30. Calendar days back from today. Same window applied to daily P&L, trade activity, and the snapshot lookup. Holdings always show the **latest** snapshot date regardless of window.
- `--config PATH` — standard config override, same as other commands.
- No `--html`, no `--plot`, no `--exclude-date`. Deferred until proven useful.
- Read-only: opens a SQLAlchemy session against `config.general.db_path`, never touches IB.

## Module layout

New module `src/vibe_trade/reports/`, parallel to `backtest/`:

```
src/vibe_trade/reports/
├── __init__.py
├── data.py        # DB → dataclasses
├── metrics.py     # Pure metric computation
└── render.py      # rich-based terminal renderer
```

### `reports/data.py` — read-only DB layer

- `load_daily_pnl(session, days: int, today: date) -> list[DailyRow]`
- `load_latest_holdings(session) -> tuple[date | None, list[HoldingRow]]` — returns the snapshot date and rows, or `(None, [])` if no snapshots exist.
- `load_trade_activity(session, days: int, today: date) -> dict[date, int]` — count of `trades` grouped by `entry_time::date`.
- `load_closed_trades(session, days: int, today: date) -> list[ClosedTrade]` — `trades` rows with `status='CLOSED'` AND `exit_time` within the window. Empty list when no SELLs have fired yet.
- `detect_outlier_days(daily_rows: list[DailyRow]) -> set[date]` — flags rows where `open_positions_count == 0` AND `realized_pnl != 0`.

Dataclasses:

```python
@dataclass
class DailyRow:
    date: date
    realized_pnl: float
    unrealized_pnl: float
    account_value: float | None
    open_positions_count: int | None

@dataclass
class HoldingRow:
    symbol: str
    quantity: int
    avg_cost: float | None
    market_price: float | None
    market_value: float | None
    unrealized_pnl: float | None

@dataclass
class ClosedTrade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    pnl: float
    pnl_pct: float | None
```

### `reports/metrics.py` — pure functions

No DB, no rich, no I/O. Reuses the sharpe/drawdown *pattern* from `backtest/metrics.py` but does **not** import it — different input shape (`daily_pnl` rows vs equity `pd.Series`); cheap duplication beats coupling two reports that will evolve separately.

```python
@dataclass
class ReportMetrics:
    start_value: float | None
    end_value: float | None
    total_return_pct: float
    cagr_pct: float | None        # None when span < 1 day
    sharpe: float                  # daily, 252-day annualized
    max_drawdown_pct: float
    max_dd_peak_date: date | None
    max_dd_trough_date: date | None
    best_day_pnl: float
    worst_day_pnl: float
    sample_size: int               # number of daily_pnl rows fed in

def compute_metrics(daily_rows: list[DailyRow]) -> ReportMetrics: ...


@dataclass
class ClosedTradeStats:
    n: int
    win_rate: float            # 0.0 when n == 0
    avg_win: float
    avg_loss: float
    profit_factor: float       # inf when no losses, 0.0 when no trades
    avg_holding_days: float

def compute_closed_trade_stats(closed: list[ClosedTrade]) -> ClosedTradeStats: ...
```

Day P&L = `realized_pnl + unrealized_pnl` per row. Sharpe uses `pct_change` on the `account_value` series, std-guarded (returns 0.0 when std==0 or n<2), matching `backtest/metrics.py:72`. `ClosedTradeStats` mirrors the corresponding fields in `backtest/metrics.py:BacktestMetrics` (same formulas, but the spec deliberately keeps them in their own dataclass — same reasoning as `ReportMetrics`: no cross-module import).

### `reports/render.py` — rich-based terminal renderer

```python
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
) -> None: ...
```

Pure rendering — no computation, no DB. Receives everything it needs as arguments.

### `cli.py` — new `report` command

Thin (~30 LOC) — load config, open session, call the four data functions, compute metrics, call render. Mirrors `status` / `trades` style.

## Data flow

```
config → session → reports.data (4 queries)
                 ↓
            reports.metrics.compute_metrics(daily_rows)  [pure]
                 ↓
            reports.render.render_report(...)            [stdout]
```

## Terminal output structure

Five sections, top to bottom.

### 1. Header

```
vibe_trade report — 2026-04-26 → 2026-05-26 (last 30 days, 12 daily_pnl rows)
⚠ Small sample — risk metrics below are indicative only.
```

The "small sample" warning shows when `sample_size < 60`.

### 2. Equity & risk

Single rich table:

```
Account value (start → end)    $95,528 → $97,713   (+2.29%)
CAGR (annualized)              +30.4%               ⚠ extrapolated from 19 calendar days
Sharpe (daily, 252-day annualized)   1.42          ⚠ thin sample
Max drawdown                   -1.26%               (peak 2026-05-14, trough 2026-05-19)
Best day / Worst day           +$1,243 / -$724
```

CAGR shows `n/a (span too short)` when span < 1 day. The "⚠ extrapolated" tag shows when span < 60 days.

### 3. Holdings — current

Header line:

```
Holdings as of 2026-05-25 (50 positions, total market value $80,051, unrealized +$538)
```

Two side-by-side or stacked rich tables: **Top 5 winners** and **Top 5 losers** by absolute unrealized P&L. Columns: symbol, qty, avg_cost, market_price, unrealized $, unrealized %.

Empty case: `No open positions.`

### 4. Trade activity

Derived from `trades.entry_time`, NOT `daily_pnl.trades_opened` (which is unreliable).

```
Trades opened by day:
  05-06: 14    05-08: 16    05-11: 13    05-12: 12    05-13: 1 ⚠
  ...
  Total opened in window: 80
Trades closed in window: 0    ← no SELLs yet
```

Days in the outlier set get a `⚠` next to the date, with a footnote:

```
⚠ days where snapshot shows 0 open positions while realized P&L is non-zero
  — typically a Gateway outage or reconcile-time anomaly. Included in metrics.
```

### 5. Trade stats

Placeholder block when `closed_stats.n == 0`:

```
Win rate / avg win / avg loss / profit factor / avg holding days
  n/a — no closed trades yet (becomes meaningful after the first SELL)
```

When `closed_stats.n > 0`, render a single rich table:

```
Closed trades in window      6
Win rate                     66.7%   (4W / 2L)
Avg win / Avg loss           +$312 / -$148
Profit factor                4.21
Avg holding days             18.3
```

The math mirrors `backtest/metrics.py` (`win_rate`, `profit_factor`, `avg_win`, `avg_loss`, `avg_holding_days`). *This computation is in scope for v1* — when SELLs eventually fire, the report becomes useful without a follow-up session.

## Error handling & edge cases

**Empty / sparse data:**
- 0 `daily_pnl` rows in window → print `No daily P&L data in last N days. Has reconcile run?` and exit 0.
- 0 holdings in latest snapshot → section 3 prints `No open positions.`
- No snapshots at all → section 3 prints `No portfolio snapshots yet.`

**Degenerate metric inputs:**
- Single `daily_pnl` row → total_return=0, sharpe=0, drawdown=0, CAGR=`n/a (span too short)`. No NaN.
- All-flat `account_value` → sharpe=0 via std-guard (same pattern as `backtest/metrics.py:72`).
- `account_value` is null on a row → skip that row for equity math, emit a one-line `[dim]warn:[/dim]` line to stdout. Don't crash.

**Outlier days:** included in math, marked with `⚠`, footnoted. No filtering.

**Config / DB errors:** propagate naturally — matches `status` / `trades`.

**Explicitly not handled:** retries, fallbacks, IB calls. Pure read-only.

## Testing

New file: `tests/test_report.py` — ~18 tests, all unit, all using `tmp_path` + `init_db` (no real DB file, no IB).

**`reports/metrics.py` — `compute_metrics` (~8 tests):**

1. Empty list → zero-metrics dataclass, no crash.
2. Single-row input → total_return=0, sharpe=0, drawdown=0, CAGR=None.
3. Two rows monotonically increasing → positive return, positive sharpe, drawdown=0.
4. Two rows monotonically decreasing → negative return, negative drawdown.
5. All-flat `account_value` → sharpe=0 (std-guard).
6. Five-row hand-computed fixture → sharpe/dd/cagr match expected to 2 decimals.
7. Best/worst day = max/min of `(realized + unrealized)`.
8. Max drawdown peak/trough dates correctly identify the worst peak-to-trough span.

**`reports/metrics.py` — `compute_closed_trade_stats` (~3 tests):**

9. Empty list → `n=0`, `win_rate=0`, `profit_factor=0`, no NaN, no crash.
10. Mixed wins and losses → `win_rate`, `avg_win`, `avg_loss`, `profit_factor` match hand-computed values.
11. All wins (no losses) → `profit_factor=inf` (matches `backtest/metrics.py:101`).

**`reports/data.py` (~5 tests):**

12. `load_daily_pnl` respects `--days` window (10 rows across 60 days, request 30 → 5 returned).
13. `load_latest_holdings` returns rows from MAX(date) only, never older.
14. `load_trade_activity` groups by `entry_time::date` and respects window.
15. `detect_outlier_days` flags positions=0 + realized≠0; ignores positions=0 + realized=0.
16. Empty DB → all loaders return empty containers, no crash.

**`reports/render.py` (~2 smoke tests):**

17. Render with full data (incl. non-zero `closed_stats`) → no exceptions, output contains "Account value", "Sharpe", "Win rate", first holding symbol.
18. Render with empty data → prints "No daily P&L data" sentinel, no exceptions.

**CLI integration (~1 test):**

19. `vibe-trade report --days 7 --config <tmp_config>` against a seeded tmp DB exits 0 and emits the report header.

**Not tested:** rich table layout (column widths, colors) — brittle and low-value.

**TEST_REGISTRY.csv:** 19 new rows.

## Files touched

**New:**
- `src/vibe_trade/reports/__init__.py`
- `src/vibe_trade/reports/data.py`
- `src/vibe_trade/reports/metrics.py`
- `src/vibe_trade/reports/render.py`
- `tests/test_report.py`

**Modified:**
- `src/vibe_trade/cli.py` — add `report` command (~30 LOC)
- `tests/TEST_REGISTRY.csv` — 19 new rows
- `PROJECT_MASTER_STATE.md` — session-K entry at end of session
- `docs/ROADMAP.md` — mark Session K done

## Out of scope (deferred)

- HTML / web UI (the user mentioned a future website — same metrics module will back it, no code change needed in v1).
- Matplotlib equity-curve PNG (`backtest/plot.py` exists; report can grow `--plot` later).
- `--exclude-date` flag (outliers are flagged, not filtered).
- Per-strategy breakdowns (one strategy today; revisit at Session L).
- Multi-day per-symbol P&L history (snapshots support it; YAGNI for v1).

## Open carry-forward notes (not blockers)

- The pre-existing `matplotlib` venv gap and the `test_risk_manager` `qty=0` bug remain. Not touched by this session.
- After this session, the `daily_pnl.trades_opened` column's unreliability should be triaged as a real reconcile bug in a future session — the report works around it but the column being wrong is a defect.
