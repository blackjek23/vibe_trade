# Session K — Performance Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `vibe-trade report` CLI command — a read-only summary of bot performance from `daily_pnl`, `portfolio_snapshot`, and `trades`. Five sections: header, equity & risk, current holdings, trade activity, trade stats. Pure DB read; no IB connection.

**Architecture:** New `src/vibe_trade/reports/` module with three files: `data.py` (DB → dataclasses), `metrics.py` (pure metric computation), `render.py` (rich terminal renderer). CLI command is a ~30-line glue layer. Computation layer is fully decoupled from rendering so a future web UI can reuse it. Spec: [docs/superpowers/specs/2026-05-26-session-k-performance-dashboard-design.md](../specs/2026-05-26-session-k-performance-dashboard-design.md).

**Tech Stack:** Python 3.11+, SQLAlchemy 2.0, rich, typer, pytest. No new dependencies.

**Pre-existing test failures (out of scope, don't touch):** 3× `test_backtest_plot` (matplotlib missing in venv), 1× `test_risk_manager` (`qty=0` divide). The "expected" pytest outcomes below say `passed` for new tests but the overall suite will still show those 4 failures. Don't try to fix them in this plan.

---

## File map

**New:**
- `src/vibe_trade/reports/__init__.py` — empty
- `src/vibe_trade/reports/data.py` — dataclasses + 5 DB loader functions
- `src/vibe_trade/reports/metrics.py` — `compute_metrics`, `compute_closed_trade_stats`, and their dataclasses
- `src/vibe_trade/reports/render.py` — `render_report` + helper

**Modified:**
- `src/vibe_trade/cli.py` — add `report` command
- `tests/TEST_REGISTRY.csv` — 19 new rows
- `tests/test_report.py` — new test file (counted as "new" but listed here for clarity)

---

## Task 1: Scaffold reports module + dataclasses

**Files:**
- Create: `src/vibe_trade/reports/__init__.py`
- Create: `src/vibe_trade/reports/data.py` (dataclasses only)
- Create: `src/vibe_trade/reports/metrics.py` (dataclasses + signatures only)

- [ ] **Step 1: Create empty `__init__.py`**

```python
# src/vibe_trade/reports/__init__.py
```

(File contains nothing — just the package marker.)

- [ ] **Step 2: Create `data.py` with dataclasses only**

Create `src/vibe_trade/reports/data.py`:

```python
"""Read-only DB loaders for `vibe-trade report`.

Returns simple dataclasses, never ORM objects, so metrics and render
have no SQLAlchemy dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


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

- [ ] **Step 3: Create `metrics.py` with dataclasses + stub signatures**

Create `src/vibe_trade/reports/metrics.py`:

```python
"""Pure metric computation for `vibe-trade report`.

No DB, no rich, no I/O. Reuses the sharpe/drawdown PATTERN from
`backtest/metrics.py` but does not import it -- different input shape
(daily_pnl rows vs an equity Series). Cheap duplication beats coupling
two reports that will evolve separately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from vibe_trade.reports.data import ClosedTrade, DailyRow

TRADING_DAYS_PER_YEAR: int = 252


@dataclass
class ReportMetrics:
    start_value: float | None
    end_value: float | None
    total_return_pct: float
    cagr_pct: float | None
    sharpe: float
    max_drawdown_pct: float
    max_dd_peak_date: date | None
    max_dd_trough_date: date | None
    best_day_pnl: float
    worst_day_pnl: float
    sample_size: int


@dataclass
class ClosedTradeStats:
    n: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    avg_holding_days: float


def compute_metrics(daily_rows: list[DailyRow]) -> ReportMetrics:
    raise NotImplementedError


def compute_closed_trade_stats(closed: list[ClosedTrade]) -> ClosedTradeStats:
    raise NotImplementedError
```

- [ ] **Step 4: Verify imports work**

Run: `.venv/Scripts/python -c "from vibe_trade.reports import data, metrics; print(data.DailyRow, metrics.ReportMetrics)"`

Expected: prints both class objects, no ImportError.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/reports/__init__.py src/vibe_trade/reports/data.py src/vibe_trade/reports/metrics.py
git commit -m "$(cat <<'EOF'
Session K: scaffold reports module + dataclasses

Empty package + DailyRow/HoldingRow/ClosedTrade dataclasses in data.py
and ReportMetrics/ClosedTradeStats + stub function signatures in
metrics.py. No logic yet -- subsequent tasks fill in implementations
TDD-style.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `compute_metrics` — degenerate cases

**Files:**
- Create: `tests/test_report.py`
- Modify: `src/vibe_trade/reports/metrics.py`

- [ ] **Step 1: Write failing tests for empty / single-row / all-flat input**

Create `tests/test_report.py`:

```python
"""Tests for vibe-trade report (reports/ module + CLI command)."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from vibe_trade.reports.data import ClosedTrade, DailyRow, HoldingRow
from vibe_trade.reports.metrics import (
    ClosedTradeStats,
    ReportMetrics,
    compute_closed_trade_stats,
    compute_metrics,
)


# ============================================================ compute_metrics


def _row(d: date, av: float | None, real: float = 0.0, unr: float = 0.0,
         pos: int | None = 50) -> DailyRow:
    return DailyRow(date=d, realized_pnl=real, unrealized_pnl=unr,
                    account_value=av, open_positions_count=pos)


def test_compute_metrics_empty_returns_zeroed_dataclass():
    m = compute_metrics([])
    assert m.sample_size == 0
    assert m.start_value is None
    assert m.end_value is None
    assert m.total_return_pct == 0.0
    assert m.cagr_pct is None
    assert m.sharpe == 0.0
    assert m.max_drawdown_pct == 0.0
    assert m.best_day_pnl == 0.0
    assert m.worst_day_pnl == 0.0


def test_compute_metrics_single_row_has_zero_return_no_nan():
    m = compute_metrics([_row(date(2026, 5, 1), 100_000.0)])
    assert m.sample_size == 1
    assert m.start_value == 100_000.0
    assert m.end_value == 100_000.0
    assert m.total_return_pct == 0.0
    assert m.cagr_pct is None  # span < 1 day
    assert m.sharpe == 0.0
    assert m.max_drawdown_pct == 0.0
    assert not math.isnan(m.sharpe)


def test_compute_metrics_flat_account_value_gives_zero_sharpe():
    rows = [_row(date(2026, 5, d), 100_000.0) for d in (1, 2, 3, 4, 5)]
    m = compute_metrics(rows)
    assert m.sharpe == 0.0
    assert m.total_return_pct == 0.0
    assert m.max_drawdown_pct == 0.0


def test_compute_metrics_drops_rows_with_null_account_value():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), None),  # should be skipped
        _row(date(2026, 5, 3), 101_000.0),
    ]
    m = compute_metrics(rows)
    assert m.sample_size == 2
    assert m.start_value == 100_000.0
    assert m.end_value == 101_000.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 4 FAIL with `NotImplementedError`.

- [ ] **Step 3: Implement the degenerate paths of `compute_metrics`**

Replace the `compute_metrics` stub in `src/vibe_trade/reports/metrics.py` with this skeleton that handles the empty/single-row/all-flat cases (and returns sensible zeros for the rest, to be filled in by Task 3):

```python
def _zero_metrics() -> ReportMetrics:
    return ReportMetrics(
        start_value=None,
        end_value=None,
        total_return_pct=0.0,
        cagr_pct=None,
        sharpe=0.0,
        max_drawdown_pct=0.0,
        max_dd_peak_date=None,
        max_dd_trough_date=None,
        best_day_pnl=0.0,
        worst_day_pnl=0.0,
        sample_size=0,
    )


def compute_metrics(daily_rows: list[DailyRow]) -> ReportMetrics:
    rows = sorted(
        [r for r in daily_rows if r.account_value is not None],
        key=lambda r: r.date,
    )
    if not rows:
        return _zero_metrics()

    start = rows[0].account_value
    end = rows[-1].account_value
    total_return_pct = (end / start - 1.0) * 100.0 if start else 0.0

    span_days = (rows[-1].date - rows[0].date).days
    if span_days > 0 and start and end and start > 0 and end > 0:
        years = span_days / 365.25
        cagr_pct = ((end / start) ** (1 / years) - 1) * 100.0
    else:
        cagr_pct = None

    return ReportMetrics(
        start_value=start,
        end_value=end,
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        sharpe=0.0,                 # filled in Task 3
        max_drawdown_pct=0.0,       # filled in Task 3
        max_dd_peak_date=None,      # filled in Task 3
        max_dd_trough_date=None,    # filled in Task 3
        best_day_pnl=0.0,           # filled in Task 3
        worst_day_pnl=0.0,          # filled in Task 3
        sample_size=len(rows),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 4 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/reports/metrics.py tests/test_report.py
git commit -m "$(cat <<'EOF'
Session K: compute_metrics degenerate paths + 4 tests

Handles empty list, single-row, all-flat, and null-account_value
rows without NaN/divide-by-zero. Sharpe/drawdown/best-worst-day
still stubbed at 0.0 -- next task adds the real math.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `compute_metrics` — full math (sharpe, drawdown, best/worst day)

**Files:**
- Modify: `tests/test_report.py`
- Modify: `src/vibe_trade/reports/metrics.py`

- [ ] **Step 1: Add failing tests for the real math**

Append to `tests/test_report.py` (below the existing tests):

```python
def test_compute_metrics_monotonically_increasing_positive_sharpe_no_dd():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), 101_000.0),
        _row(date(2026, 5, 3), 102_000.0),
        _row(date(2026, 5, 4), 103_000.0),
        _row(date(2026, 5, 5), 104_000.0),
    ]
    m = compute_metrics(rows)
    assert m.total_return_pct > 0
    assert m.sharpe > 0
    assert m.max_drawdown_pct == 0.0
    assert m.max_dd_peak_date is None
    assert m.max_dd_trough_date is None


def test_compute_metrics_monotonically_decreasing_negative_return_and_dd():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), 99_000.0),
        _row(date(2026, 5, 3), 98_000.0),
        _row(date(2026, 5, 4), 97_000.0),
    ]
    m = compute_metrics(rows)
    assert m.total_return_pct < 0
    assert m.max_drawdown_pct < 0
    assert m.max_dd_peak_date == date(2026, 5, 1)
    assert m.max_dd_trough_date == date(2026, 5, 4)


def test_compute_metrics_drawdown_identifies_correct_peak_and_trough():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), 105_000.0),  # peak
        _row(date(2026, 5, 3), 102_000.0),
        _row(date(2026, 5, 4), 95_000.0),   # trough (worst from 105k peak)
        _row(date(2026, 5, 5), 98_000.0),
        _row(date(2026, 5, 6), 110_000.0),  # new high, drawdown resets
    ]
    m = compute_metrics(rows)
    # peak is the 105k bar on 5/2; trough is the 95k bar on 5/4
    assert m.max_dd_peak_date == date(2026, 5, 2)
    assert m.max_dd_trough_date == date(2026, 5, 4)
    assert m.max_drawdown_pct == pytest.approx((95_000 / 105_000 - 1) * 100, abs=1e-6)


def test_compute_metrics_best_worst_day_pnl_from_realized_plus_unrealized():
    rows = [
        _row(date(2026, 5, 1), 100_000.0, real=0.0, unr=500.0),   # +500
        _row(date(2026, 5, 2), 101_000.0, real=100.0, unr=900.0), # +1000 BEST
        _row(date(2026, 5, 3), 100_500.0, real=-50.0, unr=-450.0),# -500 WORST
        _row(date(2026, 5, 4), 100_800.0, real=0.0, unr=300.0),   # +300
    ]
    m = compute_metrics(rows)
    assert m.best_day_pnl == 1000.0
    assert m.worst_day_pnl == -500.0


def test_compute_metrics_known_fixture_sharpe_and_drawdown():
    # Hand-computed: account_value [100, 102, 101, 103, 105]
    # daily returns: 0.02, -0.0098039, 0.0198, 0.01941
    # mean = 0.012501, std (sample) = 0.0131835
    # sharpe = mean/std * sqrt(252) ~= 15.05
    rows = [
        _row(date(2026, 5, 1), 100.0),
        _row(date(2026, 5, 2), 102.0),
        _row(date(2026, 5, 3), 101.0),
        _row(date(2026, 5, 4), 103.0),
        _row(date(2026, 5, 5), 105.0),
    ]
    m = compute_metrics(rows)
    assert m.sharpe == pytest.approx(15.05, abs=0.1)
    # drawdown: 101 vs peak 102 -> -0.98%
    assert m.max_drawdown_pct == pytest.approx(-0.9803921, abs=1e-4)
    # CAGR over 4 days, factor 1.05 -- huge annualized number
    assert m.cagr_pct is not None and m.cagr_pct > 100.0
```

- [ ] **Step 2: Run tests to confirm 5 new tests fail**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 4 PASSED (existing), 5 FAILED (the new ones — all because sharpe/dd stay at 0).

- [ ] **Step 3: Fill in the real math in `compute_metrics`**

In `src/vibe_trade/reports/metrics.py`, replace the four `# filled in Task 3` placeholder values inside `compute_metrics` with real computation. Replace the entire function body (after the existing CAGR block) so the final function reads:

```python
def compute_metrics(daily_rows: list[DailyRow]) -> ReportMetrics:
    rows = sorted(
        [r for r in daily_rows if r.account_value is not None],
        key=lambda r: r.date,
    )
    if not rows:
        return _zero_metrics()

    start = rows[0].account_value
    end = rows[-1].account_value
    total_return_pct = (end / start - 1.0) * 100.0 if start else 0.0

    span_days = (rows[-1].date - rows[0].date).days
    if span_days > 0 and start and end and start > 0 and end > 0:
        years = span_days / 365.25
        cagr_pct = ((end / start) ** (1 / years) - 1) * 100.0
    else:
        cagr_pct = None

    # ---- Sharpe on daily account_value returns (std-guarded, same shape
    # as backtest/metrics.py:72)
    values = [r.account_value for r in rows]
    returns: list[float] = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev and prev > 0:
            returns.append(values[i] / prev - 1.0)
    sharpe = 0.0
    if len(returns) >= 2:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var)
        if std > 0:
            sharpe = mean_r / std * math.sqrt(TRADING_DAYS_PER_YEAR)

    # ---- Max drawdown: walk forward, track running peak, find worst
    # (current/peak - 1). Peak date is the *running* peak at the trough,
    # not the eventual max.
    max_dd = 0.0
    dd_peak_date: date | None = None
    dd_trough_date: date | None = None
    cur_peak_value = values[0]
    cur_peak_date = rows[0].date
    for r in rows:
        v = r.account_value
        if v > cur_peak_value:
            cur_peak_value = v
            cur_peak_date = r.date
        dd = v / cur_peak_value - 1.0
        if dd < max_dd:
            max_dd = dd
            dd_peak_date = cur_peak_date
            dd_trough_date = r.date
    max_drawdown_pct = max_dd * 100.0

    # ---- Best/worst day P&L (realized + unrealized)
    day_pnls = [r.realized_pnl + r.unrealized_pnl for r in rows]
    best_day_pnl = max(day_pnls)
    worst_day_pnl = min(day_pnls)

    return ReportMetrics(
        start_value=start,
        end_value=end,
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        sharpe=float(sharpe),
        max_drawdown_pct=max_drawdown_pct,
        max_dd_peak_date=dd_peak_date,
        max_dd_trough_date=dd_trough_date,
        best_day_pnl=best_day_pnl,
        worst_day_pnl=worst_day_pnl,
        sample_size=len(rows),
    )
```

- [ ] **Step 4: Run tests to confirm all 9 pass**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 9 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/reports/metrics.py tests/test_report.py
git commit -m "$(cat <<'EOF'
Session K: compute_metrics full math + 5 tests

Sharpe (std-guarded, 252-day annualized), max drawdown with peak
and trough dates, best/worst day P&L from realized+unrealized.
Hand-computed fixture asserts sharpe~15.05 and drawdown ~-0.98%
for a known 5-row series.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `compute_closed_trade_stats`

**Files:**
- Modify: `tests/test_report.py`
- Modify: `src/vibe_trade/reports/metrics.py`

- [ ] **Step 1: Add 3 failing tests**

Append to `tests/test_report.py`:

```python
# ============================================================ compute_closed_trade_stats


def _ct(symbol: str, entry: date, exit_: date, pnl: float) -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        entry_time=datetime.combine(entry, datetime.min.time()),
        exit_time=datetime.combine(exit_, datetime.min.time()),
        pnl=pnl,
        pnl_pct=None,
    )


def test_compute_closed_trade_stats_empty_returns_zeros_no_nan():
    s = compute_closed_trade_stats([])
    assert s.n == 0
    assert s.win_rate == 0.0
    assert s.avg_win == 0.0
    assert s.avg_loss == 0.0
    assert s.profit_factor == 0.0
    assert s.avg_holding_days == 0.0


def test_compute_closed_trade_stats_mixed_wins_and_losses():
    trades = [
        _ct("AAPL", date(2026, 5, 1), date(2026, 5, 11), pnl=+200.0),   # 10d
        _ct("MSFT", date(2026, 5, 2), date(2026, 5, 22), pnl=+400.0),   # 20d
        _ct("GOOG", date(2026, 5, 3), date(2026, 5, 18), pnl=-100.0),   # 15d
        _ct("AMZN", date(2026, 5, 4), date(2026, 5, 19), pnl=-300.0),   # 15d
    ]
    s = compute_closed_trade_stats(trades)
    assert s.n == 4
    assert s.win_rate == 0.5
    assert s.avg_win == 300.0           # (200+400)/2
    assert s.avg_loss == -200.0         # (-100 + -300)/2
    assert s.profit_factor == pytest.approx(600.0 / 400.0)
    assert s.avg_holding_days == pytest.approx((10 + 20 + 15 + 15) / 4)


def test_compute_closed_trade_stats_all_wins_profit_factor_inf():
    trades = [
        _ct("AAPL", date(2026, 5, 1), date(2026, 5, 5), pnl=+100.0),
        _ct("MSFT", date(2026, 5, 2), date(2026, 5, 6), pnl=+200.0),
    ]
    s = compute_closed_trade_stats(trades)
    assert s.n == 2
    assert s.win_rate == 1.0
    assert s.profit_factor == float("inf")
```

- [ ] **Step 2: Run tests to confirm 3 fail**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 9 PASSED, 3 FAILED with `NotImplementedError`.

- [ ] **Step 3: Implement `compute_closed_trade_stats`**

In `src/vibe_trade/reports/metrics.py`, replace the `compute_closed_trade_stats` stub:

```python
def compute_closed_trade_stats(closed: list[ClosedTrade]) -> ClosedTradeStats:
    n = len(closed)
    if n == 0:
        return ClosedTradeStats(
            n=0, win_rate=0.0, avg_win=0.0, avg_loss=0.0,
            profit_factor=0.0, avg_holding_days=0.0,
        )

    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [t.pnl for t in closed if t.pnl < 0]

    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    if gross_losses > 0:
        profit_factor = gross_wins / gross_losses
    else:
        profit_factor = float("inf") if gross_wins > 0 else 0.0

    avg_holding_days = sum(
        (t.exit_time - t.entry_time).days for t in closed
    ) / n

    return ClosedTradeStats(
        n=n,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        avg_holding_days=avg_holding_days,
    )
```

- [ ] **Step 4: Run tests to confirm 12 pass**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 12 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/reports/metrics.py tests/test_report.py
git commit -m "$(cat <<'EOF'
Session K: compute_closed_trade_stats + 3 tests

Mirrors the formulas in backtest/metrics.py (win_rate, avg_win,
avg_loss, profit_factor, avg_holding_days). Profit factor = inf
when all wins, 0.0 when no trades -- no NaN paths.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `load_daily_pnl`

**Files:**
- Modify: `tests/test_report.py`
- Modify: `src/vibe_trade/reports/data.py`

- [ ] **Step 1: Add a failing test**

Append to `tests/test_report.py`:

```python
# ============================================================ load_daily_pnl


@pytest.fixture
def session():
    """Fresh in-memory SQLite session for each data-layer test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from vibe_trade.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_load_daily_pnl_respects_days_window(session):
    from vibe_trade.db.models import DailyPnL
    from vibe_trade.reports.data import load_daily_pnl

    today = date(2026, 5, 26)
    # Insert 10 rows, one every 6 calendar days, spanning ~54 days back
    for i in range(10):
        session.add(DailyPnL(
            date=today - timedelta(days=i * 6),
            realized_pnl=0.0, unrealized_pnl=0.0,
            account_value=100_000.0 + i * 100,
            open_positions_count=50,
        ))
    session.commit()

    rows = load_daily_pnl(session, days=30, today=today)
    # rows with date >= today-30 -> i*6 <= 30 -> i in {0,1,2,3,4,5} -> 6 rows
    assert len(rows) == 6
    # sorted by date ascending
    assert rows[0].date < rows[-1].date
    # all account_value is float
    assert all(isinstance(r.account_value, float) for r in rows)
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `.venv/Scripts/python -m pytest tests/test_report.py::test_load_daily_pnl_respects_days_window -v`

Expected: FAIL with `ImportError` (function doesn't exist).

- [ ] **Step 3: Implement `load_daily_pnl`**

Append to `src/vibe_trade/reports/data.py`:

```python
# ---------------------------------------------------------------- loaders


from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from vibe_trade.db.models import DailyPnL, PortfolioSnapshot, Trade


def load_daily_pnl(session: Session, days: int, today: date) -> list[DailyRow]:
    """Return daily_pnl rows where date >= today - `days`, oldest first."""
    cutoff = today - timedelta(days=days)
    rows = (
        session.query(DailyPnL)
        .filter(DailyPnL.date >= cutoff)
        .order_by(DailyPnL.date)
        .all()
    )
    return [
        DailyRow(
            date=r.date,
            realized_pnl=r.realized_pnl or 0.0,
            unrealized_pnl=r.unrealized_pnl or 0.0,
            account_value=r.account_value,
            open_positions_count=r.open_positions_count,
        )
        for r in rows
    ]
```

- [ ] **Step 4: Run test to confirm it passes**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 13 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/reports/data.py tests/test_report.py
git commit -m "$(cat <<'EOF'
Session K: load_daily_pnl + window test

Calendar-days-back filter on DailyPnL, sorted ascending by date.
Defaults realized/unrealized to 0.0 (DB columns default 0 anyway
but explicit guards survive null rows).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `load_latest_holdings`

**Files:**
- Modify: `tests/test_report.py`
- Modify: `src/vibe_trade/reports/data.py`

- [ ] **Step 1: Add a failing test**

Append to `tests/test_report.py`:

```python
# ============================================================ load_latest_holdings


def test_load_latest_holdings_returns_only_max_date_rows(session):
    from vibe_trade.db.models import PortfolioSnapshot
    from vibe_trade.reports.data import load_latest_holdings

    # Day 1: 2 holdings; Day 2: 3 holdings (the latest)
    older = date(2026, 5, 20)
    latest = date(2026, 5, 25)
    for sym in ("AAPL", "MSFT"):
        session.add(PortfolioSnapshot(
            date=older, symbol=sym, quantity=10,
            avg_cost=100.0, market_price=101.0,
            market_value=1010.0, unrealized_pnl=10.0,
        ))
    for sym in ("AAPL", "MSFT", "GOOG"):
        session.add(PortfolioSnapshot(
            date=latest, symbol=sym, quantity=20,
            avg_cost=100.0, market_price=105.0,
            market_value=2100.0, unrealized_pnl=100.0,
        ))
    session.commit()

    snapshot_date, holdings = load_latest_holdings(session)
    assert snapshot_date == latest
    assert len(holdings) == 3
    assert all(h.quantity == 20 for h in holdings)


def test_load_latest_holdings_empty_db_returns_none_and_empty_list(session):
    from vibe_trade.reports.data import load_latest_holdings

    snapshot_date, holdings = load_latest_holdings(session)
    assert snapshot_date is None
    assert holdings == []
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v -k load_latest_holdings`

Expected: 2 FAIL with `ImportError`.

- [ ] **Step 3: Implement `load_latest_holdings`**

Append to `src/vibe_trade/reports/data.py`:

```python
def load_latest_holdings(session: Session) -> tuple[date | None, list[HoldingRow]]:
    """Return (snapshot_date, rows) from the MAX(date) row of
    portfolio_snapshot. (None, []) when the table is empty."""
    latest = session.query(func.max(PortfolioSnapshot.date)).scalar()
    if latest is None:
        return (None, [])
    rows = (
        session.query(PortfolioSnapshot)
        .filter(PortfolioSnapshot.date == latest)
        .all()
    )
    holdings = [
        HoldingRow(
            symbol=r.symbol,
            quantity=r.quantity,
            avg_cost=r.avg_cost,
            market_price=r.market_price,
            market_value=r.market_value,
            unrealized_pnl=r.unrealized_pnl,
        )
        for r in rows
    ]
    return (latest, holdings)
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 15 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/reports/data.py tests/test_report.py
git commit -m "$(cat <<'EOF'
Session K: load_latest_holdings + 2 tests

Returns (MAX(date), rows). Older snapshot dates filtered out.
Empty DB returns (None, []) so callers branch cleanly.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: `load_trade_activity` + `load_closed_trades`

**Files:**
- Modify: `tests/test_report.py`
- Modify: `src/vibe_trade/reports/data.py`

- [ ] **Step 1: Add 2 failing tests**

Append to `tests/test_report.py`:

```python
# ============================================================ load_trade_activity / load_closed_trades


def _trade(session, symbol: str, entry: datetime, status: str = "OPEN",
           exit_: datetime | None = None, pnl: float | None = None):
    from vibe_trade.db.models import Trade
    session.add(Trade(
        symbol=symbol, side="BUY", strategy_name="donchian",
        entry_time=entry, exit_time=exit_,
        entry_price=100.0, exit_price=(110.0 if exit_ else None),
        requested_quantity=10, filled_quantity=10,
        status=status, pnl=pnl,
    ))


def test_load_trade_activity_groups_by_entry_date_within_window(session):
    from vibe_trade.reports.data import load_trade_activity

    today = date(2026, 5, 26)
    # 3 entries on 5/20, 2 entries on 5/25, 1 entry 60 days ago (outside window)
    for i in range(3):
        _trade(session, f"S{i}", datetime(2026, 5, 20, 14, i))
    for i in range(2):
        _trade(session, f"T{i}", datetime(2026, 5, 25, 14, i))
    _trade(session, "OLD", datetime(2026, 3, 1, 14, 0))
    session.commit()

    activity = load_trade_activity(session, days=30, today=today)
    assert activity == {date(2026, 5, 20): 3, date(2026, 5, 25): 2}


def test_load_closed_trades_filters_status_and_exit_time_window(session):
    from vibe_trade.reports.data import load_closed_trades

    today = date(2026, 5, 26)
    # CLOSED with exit_time inside window -> included
    _trade(session, "AAPL", datetime(2026, 5, 1, 14, 0),
           status="CLOSED",
           exit_=datetime(2026, 5, 20, 14, 0), pnl=200.0)
    # CLOSED but exit_time outside window -> excluded
    _trade(session, "OLD", datetime(2026, 3, 1, 14, 0),
           status="CLOSED",
           exit_=datetime(2026, 3, 20, 14, 0), pnl=100.0)
    # OPEN (no exit) -> excluded
    _trade(session, "MSFT", datetime(2026, 5, 10, 14, 0), status="OPEN")
    session.commit()

    closed = load_closed_trades(session, days=30, today=today)
    assert len(closed) == 1
    assert closed[0].symbol == "AAPL"
    assert closed[0].pnl == 200.0
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v -k "load_trade_activity or load_closed_trades"`

Expected: 2 FAIL with `ImportError`.

- [ ] **Step 3: Implement both loaders**

Append to `src/vibe_trade/reports/data.py`:

```python
def load_trade_activity(
    session: Session, days: int, today: date,
) -> dict[date, int]:
    """Count of `trades` rows grouped by entry_time::date, within window.

    Source of truth for activity -- the `daily_pnl.trades_opened` column
    is unreliable (often reads 0 even on days with confirmed entries).
    """
    cutoff = today - timedelta(days=days)
    cutoff_dt = datetime.combine(cutoff, datetime.min.time())
    rows = (
        session.query(Trade)
        .filter(Trade.entry_time.is_not(None))
        .filter(Trade.entry_time >= cutoff_dt)
        .all()
    )
    counts: dict[date, int] = {}
    for t in rows:
        d = t.entry_time.date()
        counts[d] = counts.get(d, 0) + 1
    return counts


def load_closed_trades(
    session: Session, days: int, today: date,
) -> list[ClosedTrade]:
    """`trades` rows with status='CLOSED' AND exit_time within the window."""
    cutoff = today - timedelta(days=days)
    cutoff_dt = datetime.combine(cutoff, datetime.min.time())
    rows = (
        session.query(Trade)
        .filter(Trade.status == "CLOSED")
        .filter(Trade.exit_time.is_not(None))
        .filter(Trade.exit_time >= cutoff_dt)
        .all()
    )
    return [
        ClosedTrade(
            symbol=t.symbol,
            entry_time=t.entry_time,
            exit_time=t.exit_time,
            pnl=t.pnl or 0.0,
            pnl_pct=t.pnl_pct,
        )
        for t in rows
    ]
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 17 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/reports/data.py tests/test_report.py
git commit -m "$(cat <<'EOF'
Session K: load_trade_activity + load_closed_trades + 2 tests

Activity is grouped by entry_time::date and replaces the
unreliable daily_pnl.trades_opened column. load_closed_trades
filters on status='CLOSED' AND exit_time within the window.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `detect_outlier_days` + empty-DB smoke

**Files:**
- Modify: `tests/test_report.py`
- Modify: `src/vibe_trade/reports/data.py`

- [ ] **Step 1: Add 2 failing tests**

Append to `tests/test_report.py`:

```python
# ============================================================ detect_outlier_days


def test_detect_outlier_days_flags_positions_zero_with_realized_nonzero():
    rows = [
        _row(date(2026, 5, 12), 100_000.0, real=0.0, unr=10.0, pos=50),     # normal
        _row(date(2026, 5, 13), 100_000.0, real=4056.0, unr=0.0, pos=0),    # OUTLIER
        _row(date(2026, 5, 14), 100_000.0, real=0.0, unr=20.0, pos=0),      # positions=0 but realized=0 -> not outlier
        _row(date(2026, 5, 15), 100_000.0, real=100.0, unr=0.0, pos=45),    # realized>0 but positions>0 -> not outlier
    ]
    from vibe_trade.reports.data import detect_outlier_days
    outliers = detect_outlier_days(rows)
    assert outliers == {date(2026, 5, 13)}


def test_data_loaders_on_empty_db_return_empty_containers(session):
    from vibe_trade.reports.data import (
        detect_outlier_days, load_closed_trades, load_daily_pnl,
        load_latest_holdings, load_trade_activity,
    )

    today = date(2026, 5, 26)
    assert load_daily_pnl(session, days=30, today=today) == []
    snap_date, holdings = load_latest_holdings(session)
    assert snap_date is None and holdings == []
    assert load_trade_activity(session, days=30, today=today) == {}
    assert load_closed_trades(session, days=30, today=today) == []
    assert detect_outlier_days([]) == set()
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v -k "detect_outlier_days or empty_db"`

Expected: outlier test fails (`ImportError`); empty-DB test fails on `detect_outlier_days` import.

- [ ] **Step 3: Implement `detect_outlier_days`**

Append to `src/vibe_trade/reports/data.py`:

```python
def detect_outlier_days(daily_rows: list[DailyRow]) -> set[date]:
    """Days that look like a Gateway/reconcile artifact:
    open_positions_count == 0 AND realized_pnl != 0.

    Returned dates are included in metrics; callers mark them in the
    output so the operator sees the anomaly.
    """
    return {
        r.date
        for r in daily_rows
        if r.open_positions_count == 0 and r.realized_pnl != 0
    }
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 19 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/reports/data.py tests/test_report.py
git commit -m "$(cat <<'EOF'
Session K: detect_outlier_days + empty-DB smoke test

Heuristic for Gateway-outage days (positions=0 AND realized!=0).
Empty-DB smoke covers all 5 loaders + outlier detector.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `render_report` + smoke tests

**Files:**
- Create: `src/vibe_trade/reports/render.py`
- Modify: `tests/test_report.py`

- [ ] **Step 1: Write 2 failing smoke tests**

Append to `tests/test_report.py`:

```python
# ============================================================ render_report


def _holding(symbol: str, pnl: float) -> HoldingRow:
    return HoldingRow(
        symbol=symbol, quantity=10,
        avg_cost=100.0, market_price=100.0 + pnl / 10,
        market_value=1000.0 + pnl, unrealized_pnl=pnl,
    )


def test_render_report_full_data_emits_key_sections(capsys):
    from rich.console import Console

    from vibe_trade.reports.render import render_report

    today = date(2026, 5, 26)
    daily_rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 5), 102_000.0),
    ]
    holdings = [_holding("AAPL", +300.0), _holding("MSFT", -200.0)]
    metrics = compute_metrics(daily_rows)
    closed_stats = compute_closed_trade_stats([])
    console = Console(force_terminal=False, no_color=True, width=120)

    render_report(
        metrics=metrics,
        daily_rows=daily_rows,
        holdings=holdings,
        holdings_as_of=date(2026, 5, 5),
        activity={date(2026, 5, 1): 2, date(2026, 5, 5): 1},
        closed_stats=closed_stats,
        outliers=set(),
        window_days=30,
        today=today,
        console=console,
    )
    out = capsys.readouterr().out
    assert "Account value" in out
    assert "Sharpe" in out
    assert "AAPL" in out
    assert "no closed trades" in out  # since closed_stats.n == 0


def test_render_report_empty_prints_no_daily_pnl_sentinel(capsys):
    from rich.console import Console

    from vibe_trade.reports.render import render_report

    today = date(2026, 5, 26)
    metrics = compute_metrics([])
    closed_stats = compute_closed_trade_stats([])
    console = Console(force_terminal=False, no_color=True, width=120)

    render_report(
        metrics=metrics,
        daily_rows=[],
        holdings=[],
        holdings_as_of=None,
        activity={},
        closed_stats=closed_stats,
        outliers=set(),
        window_days=30,
        today=today,
        console=console,
    )
    out = capsys.readouterr().out
    assert "No daily P&L data" in out
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v -k render_report`

Expected: 2 FAIL with `ImportError` (render module doesn't exist).

- [ ] **Step 3: Implement `render_report`**

Create `src/vibe_trade/reports/render.py`:

```python
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
    if activity:
        console.print("Trades opened by day:")
        parts = []
        for d in sorted(activity.keys()):
            mark = " [yellow]!warn[/yellow]" if d in outliers else ""
            parts.append(f"{d.strftime('%m-%d')}: {activity[d]}{mark}")
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
            "reconcile-time anomaly. Included in metrics.[/dim]"
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
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 21 PASSED.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/reports/render.py tests/test_report.py
git commit -m "$(cat <<'EOF'
Session K: render_report + 2 smoke tests

Five-section rich renderer: header (with small-sample caveat),
equity & risk, holdings (top/bottom 5), trade activity (4-per-line,
outlier-flagged), trade stats (n/a block when no closed trades).
Pure rendering -- all computation lives in metrics.py.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: CLI `report` command + integration test

**Files:**
- Modify: `src/vibe_trade/cli.py`
- Modify: `tests/test_report.py`

- [ ] **Step 1: Write a failing CLI integration test**

Append to `tests/test_report.py`:

```python
# ============================================================ CLI integration


def test_cli_report_exits_zero_and_emits_header(tmp_path, monkeypatch):
    """End-to-end: seed a tmp DB, run `vibe-trade report --days 7 --config X`."""
    from typer.testing import CliRunner

    from vibe_trade.cli import app
    from vibe_trade.db.engine import init_db
    from vibe_trade.db.models import DailyPnL

    # 1. Build a minimal config file pointing at a tmp DB.
    # AppConfig has default_factory for every sub-model, so only the
    # general.db_path override is required.
    db_path = tmp_path / "report.db"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[general]\ndb_path = "{db_path.as_posix()}"\n'
    )

    # 2. Seed the tmp DB with a couple of daily_pnl rows.
    factory = init_db(str(db_path))
    s = factory()
    today = date.today()
    for i, av in enumerate([100_000.0, 101_000.0]):
        s.add(DailyPnL(
            date=today - timedelta(days=(1 - i)),
            realized_pnl=0.0, unrealized_pnl=10.0,
            account_value=av, open_positions_count=10,
        ))
    s.commit()
    s.close()

    # 3. Invoke the CLI.
    runner = CliRunner()
    result = runner.invoke(
        app, ["report", "--days", "7", "--config", str(config_path)],
    )
    assert result.exit_code == 0, result.output
    assert "vibe_trade report" in result.output
    assert "Account value" in result.output
```

- [ ] **Step 2: Run test to confirm it fails**

Run: `.venv/Scripts/python -m pytest tests/test_report.py::test_cli_report_exits_zero_and_emits_header -v`

Expected: FAIL — typer reports `No such command 'report'`.

- [ ] **Step 3: Add the `report` command to `cli.py`**

Open `src/vibe_trade/cli.py`. Find the existing `trades` command (around line 829) — the new `report` command goes immediately after it, **before** the `config-check` command (around line 879).

Insert this block between `trades` and `config_check`:

```python
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
```

- [ ] **Step 4: Run the CLI test, then the full suite**

Run: `.venv/Scripts/python -m pytest tests/test_report.py -v`

Expected: 22 PASSED.

Then run the full suite to confirm no regressions:

Run: `.venv/Scripts/python -m pytest`

Expected: 308 passed, 4 failed (the 4 pre-existing failures: 3× `test_backtest_plot`, 1× `test_risk_manager`). If any *other* test fails, stop and investigate.

- [ ] **Step 5: Commit**

```bash
git add src/vibe_trade/cli.py tests/test_report.py
git commit -m "$(cat <<'EOF'
Session K: vibe-trade report CLI command + integration test

Thin glue (~40 LOC) between reports.data, reports.metrics, and
reports.render. Reuses init_db + load_config like status/trades.
End-to-end CliRunner test seeds a tmp DB, invokes the command,
asserts exit code 0 and header in output.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: Update TEST_REGISTRY.csv

**Files:**
- Modify: `tests/TEST_REGISTRY.csv`

- [ ] **Step 1: Append 22 rows to the CSV**

Open `tests/TEST_REGISTRY.csv` and append these lines at the end (no trailing newline before; one row per test, matching the 22 new tests we wrote):

```csv
test_report.py,compute_metrics,test_compute_metrics_empty_returns_zeroed_dataclass,Empty daily_rows list returns zeroed dataclass without NaN
test_report.py,compute_metrics,test_compute_metrics_single_row_has_zero_return_no_nan,Single-row input: total_return=0 sharpe=0 cagr=None no NaN
test_report.py,compute_metrics,test_compute_metrics_flat_account_value_gives_zero_sharpe,Flat account_value series: sharpe=0 via std-guard
test_report.py,compute_metrics,test_compute_metrics_drops_rows_with_null_account_value,Rows with null account_value are excluded from equity math
test_report.py,compute_metrics,test_compute_metrics_monotonically_increasing_positive_sharpe_no_dd,Monotonically rising equity: positive sharpe drawdown=0 no peak/trough dates
test_report.py,compute_metrics,test_compute_metrics_monotonically_decreasing_negative_return_and_dd,Monotonically falling equity: negative return and drawdown peak=first trough=last
test_report.py,compute_metrics,test_compute_metrics_drawdown_identifies_correct_peak_and_trough,Drawdown peak/trough correctly identified across local highs and lows
test_report.py,compute_metrics,test_compute_metrics_best_worst_day_pnl_from_realized_plus_unrealized,Best/worst day P&L = max/min of realized+unrealized per row
test_report.py,compute_metrics,test_compute_metrics_known_fixture_sharpe_and_drawdown,5-row hand-computed fixture: sharpe~15.05 drawdown~-0.98%
test_report.py,compute_closed_trade_stats,test_compute_closed_trade_stats_empty_returns_zeros_no_nan,Empty closed-trades list returns zeros without NaN
test_report.py,compute_closed_trade_stats,test_compute_closed_trade_stats_mixed_wins_and_losses,Hand-computed win_rate avg_win avg_loss profit_factor avg_holding_days on 4 trades
test_report.py,compute_closed_trade_stats,test_compute_closed_trade_stats_all_wins_profit_factor_inf,All-wins case yields profit_factor=inf
test_report.py,load_daily_pnl,test_load_daily_pnl_respects_days_window,10 rows across 60 days request 30 returns 6 sorted ascending
test_report.py,load_latest_holdings,test_load_latest_holdings_returns_only_max_date_rows,Returns only MAX(date) snapshot rows never older dates
test_report.py,load_latest_holdings,test_load_latest_holdings_empty_db_returns_none_and_empty_list,Empty portfolio_snapshot returns (None [])
test_report.py,load_trade_activity,test_load_trade_activity_groups_by_entry_date_within_window,Trades grouped by entry_time::date respecting --days window
test_report.py,load_closed_trades,test_load_closed_trades_filters_status_and_exit_time_window,Returns only status=CLOSED rows with exit_time in window
test_report.py,detect_outlier_days,test_detect_outlier_days_flags_positions_zero_with_realized_nonzero,Flags positions=0 AND realized!=0 only ignores positions=0+realized=0
test_report.py,data,test_data_loaders_on_empty_db_return_empty_containers,All 5 loaders + outlier detector return empty containers on empty DB
test_report.py,render_report,test_render_report_full_data_emits_key_sections,Full data renders Account value Sharpe holding symbol no-closed-trades line
test_report.py,render_report,test_render_report_empty_prints_no_daily_pnl_sentinel,Empty metrics prints "No daily P&L data" sentinel and returns early
test_report.py,cli,test_cli_report_exits_zero_and_emits_header,End-to-end CliRunner: seeds tmp DB invokes report exits 0 emits header
```

- [ ] **Step 2: Verify the CSV is still valid (4 columns per row, no parse errors)**

Run: `.venv/Scripts/python -c "import csv; rows = list(csv.reader(open('tests/TEST_REGISTRY.csv'))); print(f'rows={len(rows)} cols_per_row={set(len(r) for r in rows)}')"`

Expected: prints `rows=<existing+22> cols_per_row={4}`.

- [ ] **Step 3: Commit**

```bash
git add tests/TEST_REGISTRY.csv
git commit -m "$(cat <<'EOF'
Session K: TEST_REGISTRY.csv +22 rows for report tests

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: Manual smoke test against the real sample DB

**Files:** none touched in this task — sanity check only.

- [ ] **Step 1: Run the command against the sample DB**

Run: `.venv/Scripts/python -m vibe_trade report --days 30 --config config/config.example.toml`

Note: this uses the default `db_path = "data/vibe_trade.db"` from `config.example.toml`. If that file doesn't exist on the dev box, point at the sample copy instead:

```bash
# Make a one-shot config with the sample DB path
copy config\config.example.toml config\config.local.toml
# then edit config/config.local.toml so general.db_path = "data/vibe_trade_sample.db"
.venv/Scripts/python -m vibe_trade report --days 30 --config config/config.local.toml
```

Expected: the report renders all 5 sections without traceback. Specifically you should see:
- Header line `vibe_trade report -- 2026-04-26 -> 2026-05-26 (last 30 days, 12 daily_pnl rows)` and the small-sample caveat.
- Equity & Risk table with `Account value (start -> end) $95,528.11 -> $97,713.85` and a positive total return.
- Holdings table dated `2026-05-25` with `50 positions`, Top 5 winners and Top 5 losers.
- Trade activity with `Total opened in window: 80` and `Trades closed in window: 0 (no SELLs yet)`. Day `05-13` should carry the `!warn` mark; the outlier footnote should appear.
- Trade stats section showing the n/a placeholder.

- [ ] **Step 2: If any section looks wrong, capture the issue**

If the output looks broken (missing section, wrong number, traceback), stop and treat it as a bug — don't proceed to step 3. Add a regression test under the corresponding loader/render task and fix.

- [ ] **Step 3: No commit (no code change)**

This task is sanity-check-only. No commit unless step 2 found something.

---

## Task 13: Update PROJECT_MASTER_STATE.md + ROADMAP.md

**Files:**
- Modify: `PROJECT_MASTER_STATE.md`
- Modify: `docs/ROADMAP.md`

- [ ] **Step 1: Update `PROJECT_MASTER_STATE.md` header**

Open `PROJECT_MASTER_STATE.md`. Replace the three header fields at the top:

```markdown
**Last updated:** 2026-05-26 (Session K — performance dashboard — CLOSED)
**HEAD commit:** `<run: git rev-parse --short HEAD>`
**Tests:** 312 collected (308 passing; +22 net this session). 4 failures are
  pre-existing and unrelated to this work — 3× `test_backtest_plot` (matplotlib
  not installed in the venv) and 1× `test_risk_manager` (buggy test helper
  divides by `qty=0`). See §7.
```

(Replace `<run: git rev-parse --short HEAD>` with the actual commit hash from `git rev-parse --short HEAD`.)

- [ ] **Step 2: Append Session K row to "Done" table in §2**

In `PROJECT_MASTER_STATE.md`, find the "Done" table in section 2 ("Current Implementation Status"). Append this row at the bottom of the table:

```markdown
| **K** — Performance dashboard | `<commit hash from Task 10>` | New `vibe-trade report --days N` CLI command. Read-only against `daily_pnl` + `portfolio_snapshot` + `trades` — no IB connection. New `src/vibe_trade/reports/` module (data + metrics + render split). Five output sections: header (with small-sample caveat), equity & risk (sharpe / drawdown with peak+trough dates / CAGR / best+worst day), current holdings (top/bottom 5 by unrealized P&L), trade activity (per-day entries, outlier-flagged), trade stats (n/a block until first SELL fires). Derives activity from `trades.entry_time` because the `daily_pnl.trades_opened` column is unreliable. +22 tests. |
```

(Use the commit hash from Task 10's commit — `git log --oneline | grep "vibe-trade report CLI" | awk '{print $1}'`.)

- [ ] **Step 3: Remove Session K from "Not started" in §2**

In the same section, change:

```markdown
- **Session K** — Performance dashboard (`vibe-trade report`)
```

…to remove that line entirely from the "Not started" list (leave Sessions L, M, Phase 4, Phase 5 in place).

- [ ] **Step 4: Replace §7 (Session Hand-off) with the next deliverable**

In `PROJECT_MASTER_STATE.md`, find section 7 ("Session Hand-off — start here next time"). Replace the "Immediate next concrete deliverable" subsection contents with:

```markdown
### Immediate next concrete deliverable

**Session K is CLOSED** (2026-05-26). `vibe-trade report --days N` ships as
a read-only performance dashboard pulling from `daily_pnl` +
`portfolio_snapshot` + `trades`. +22 tests. Manually smoke-tested against
the May 11–25 paper-run DB sample: all five sections render correctly,
the 5/13 Gateway-outage day is flagged as an outlier.

**Session L — Multi-strategy:** Strategy registry V2 (Donchian + RSI mean
reversion + MA crossover) using `Order.orderRef = strategy_id`. Submit
sets the tag; record reads `fill.execution.orderRef` to populate
`strategy_name`. Position sizing gets a per-strategy override hook.

**Carry-forward notes from Session K:**
- The `daily_pnl.trades_opened` column is unreliable (reads 0 on days
  with confirmed entries). The report works around it by deriving
  activity from `trades.entry_time` — but the column being wrong is a
  reconcile defect worth fixing in a future session.
- A future web UI was mentioned by the user; the report's metrics layer
  (`reports/metrics.py` + `reports/data.py`) is pure and reusable. A web
  layer just renders the same dataclasses to HTML/JSON instead of calling
  `render.py`.
- The `vibe-trade report --days 30` default works fine on the current
  ~2-week dataset but the "small sample" caveat triggers until 60+ rows
  exist.
```

- [ ] **Step 5: Update `docs/ROADMAP.md`**

In `docs/ROADMAP.md`, find Session K under "Phase 2 — Operational maturity":

```markdown
### Session K — Performance dashboard
- `vibe-trade report --days N` — sharpe, drawdown, win rate from `daily_pnl` + `trades` tables
- Optional: simple HTML output in `reports/` for browser viewing
- Pure read-only against existing DB; no IB connection needed
```

Replace with:

```markdown
### Session K — Performance dashboard ✅ Done (2026-05-26)
- `vibe-trade report --days N` ships. Read-only against `daily_pnl` +
  `portfolio_snapshot` + `trades`. Five sections: header, equity & risk,
  current holdings (top/bottom 5), trade activity, trade stats. HTML
  output deferred — the user mentioned wanting a web UI separately, and
  `reports/metrics.py` + `reports/data.py` are designed to back it.
```

- [ ] **Step 6: Commit**

```bash
git add PROJECT_MASTER_STATE.md docs/ROADMAP.md
git commit -m "$(cat <<'EOF'
Session K: update PROJECT_MASTER_STATE + ROADMAP

Mark Session K closed, append "Done" row, refresh Hand-off section
with Session L as next deliverable. ROADMAP entry updated with
shipped status and the deferred-HTML note.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Run the full test suite one more time**

Run: `.venv/Scripts/python -m pytest`

Expected: `308 passed, 4 failed` (the same 4 pre-existing failures called out at the top of this plan). If anything else fails, stop and triage.

- [ ] **Confirm the commit history**

Run: `git log --oneline -15`

Expected: 12 new Session K commits on `main` (Tasks 1–11 + Task 13; Task 12 doesn't commit). All commits are co-authored by `Claude Opus 4.7`.

- [ ] **No push — leave for user to push**

Per project convention: don't `git push` unless asked.
