# vibe_trade — Operations Runbook

> Day-to-day operating guide for the swing-trading bot. For one-time deploy/setup
> (Docker, cron install, host config) see [`deploy/README.md`](../deploy/README.md).
> For project state and history see [`PROJECT_MASTER_STATE.md`](../PROJECT_MASTER_STATE.md).

All times are **Asia/Jerusalem**. US market opens 16:30 local, closes 23:00 local.

---

## 1. Daily cadence

Four OS-scheduled jobs, each short-lived (no long-running process). Cron drives timing.

| Time  | Command                  | Client ID | What it does                                                        |
| ----- | ------------------------ | --------- | ------------------------------------------------------------------ |
| 16:00 | `vibe-trade submit`      | 1         | Exits (strategy-scoped SELLs) → force-trim → entries (priority BUYs). Places market orders. **No DB writes.** |
| 16:25 | `vibe-trade record`      | 2         | Reads `ib.fills()`, persists BUYs as SUBMITTED, flips OPEN→PENDING_CLOSE. Stamps `strategy_name` from each order's `orderRef`. |
| 23:30 | `vibe-trade reconcile`   | 3         | Finalizes statuses (FILLED/CANCELLED/PARTIAL) + portfolio snapshot + daily P&L. |
| Sat 09:00 | `vibe-trade report-weekly` | 8     | Renders the last-7-day dashboard PNG and sends it to Telegram.     |

**Pre-flight:** IB Gateway / TWS must be up on the configured port (paper 7497 / live 7496) before `submit`/`record`/`reconcile`. If it's down, the job sends a `[CRITICAL]` Telegram alert and exits non-zero (Bug #6).

---

## 2. Strategy pool

The bot runs **multiple strategies at once** (Session L). The active pool lives in
`config/config.toml` under `[[strategies]]`. **List order = entry priority.**

Current pool (priority order):

| # | id         | type            | rule (regime/state on yesterday's daily close) |
|---|------------|-----------------|------------------------------------------------|
| 1 | `donchian` | breakout        | BUY close > prior-20-day high; SELL < prior-20-day low |
| 2 | `ema`      | trend crossover | BUY EMA(12) > EMA(26); SELL EMA(12) < EMA(26)  |
| 3 | `macd`     | momentum        | BUY MACD(12,26) > signal(9); SELL MACD < signal |

Two rules govern how they coexist:

- **Entry conflict → priority wins.** If more than one strategy signals BUY on the
  same un-held ticker, the highest-priority (first listed) strategy claims it. One
  order per ticker per day. The order is tagged `order_ref=<strategy id>`.
- **Exits are strategy-scoped.** A position is only exited by the strategy that
  opened it. `submit` reads the DB at 16:00 to map each held symbol → owning
  strategy (a read; submit still writes nothing). An untracked/orphan position is
  evaluated by the highest-priority strategy.

> ⚠️ **MACD churns.** In backtest it traded ~3.8× more than the others (~16-day
> avg hold). The backtest is frictionless; real IB commissions bite churny
> strategies hardest. Watch order volume and realized P&L after enabling.

> ⚠️ **One shared 50-position cap.** All strategies compete under a single cap with
> no per-strategy limits yet (per-strategy caps are Session M). Higher-priority
> strategies fill entry slots first.

### Add / enable / disable / tune a strategy

Edit `[[strategies]]` in `config/config.toml`, then validate:

```bash
.venv/Scripts/python -m vibe_trade config-check        # lists the resolved pool + priority
```

- **Enable/disable:** set `enabled = true|false` (omitted = enabled).
- **Reprioritize:** reorder the `[[strategies]]` blocks.
- **Tune:** edit `[strategies.params]` (e.g. `fast`, `slow`, `signal`, `period`).
- **Per-strategy size:** add `pct_per_position = 0.01` to a block (omit = global 1.8%).
- **New strategy:** add `src/vibe_trade/strategy/examples/<name>.py` (subclass
  `BaseStrategy`, return BUY/SELL/HOLD on `df.iloc[-1]`), register it in
  `STRATEGY_FACTORIES` in `strategy/registry.py`, add a test, then add a
  `[[strategies]]` block. **Constraint:** `evaluate(symbol, candles)` is stateless —
  it sees only the price series, never entry price/P&L/holding age. Stop-loss,
  take-profit, trailing, time-based, and intraday strategies need new machinery first.

### Always backtest before enabling live

```bash
.venv/Scripts/python -m vibe_trade backtest \
    --start 2018-01-01 --end 2026-01-01 --top-n 100 --strategy ema
```

Swap `--strategy` for any registered id (`donchian`, `sma`, `ema`, `macd`).
Outputs land in `backtests/<timestamp>/` (equity curve PNG, trades CSV, metrics
JSON) with SPY/QQQ buy-and-hold benchmarks. **Frictionless** — discount churny
strategies accordingly.

---

## 3. Config files

| File                       | Tracked? | Role                                                        |
| -------------------------- | -------- | ----------------------------------------------------------- |
| `config/config.toml`       | **no** (gitignored — holds secrets) | The **active** config `load_config()` reads. Telegram token/chat + the live `[[strategies]]` pool. |
| `config/config.example.toml` | yes    | Template. Ships sma/ema/macd `enabled=false` (conservative). |
| `config/config.local.toml` | no       | Ad-hoc override (e.g. sample-DB path for report smoke tests). |

`load_config()` precedence: explicit `--config` path → `$VIBE_TRADE_CONFIG` →
`config/config.toml`. Secrets can also come from env: `VIBE_TRADE_TELEGRAM_TOKEN`,
`VIBE_TRADE_TELEGRAM_CHAT_ID`.

---

## 4. Routine operator commands

```bash
.venv/Scripts/python -m vibe_trade status         # open positions + today's P&L
.venv/Scripts/python -m vibe_trade trades         # recent trade history (shows strategy_name)
.venv/Scripts/python -m vibe_trade report --days 30   # terminal performance dashboard
.venv/Scripts/python -m vibe_trade config-check   # validate config + list active strategy pool
```

---

## 5. Manual / emergency

```bash
# Market-close one ticker now (off-cycle). No DB write; next record/reconcile persists it.
.venv/Scripts/python -m vibe_trade close-position AAPL --yes

# List working orders, or cancel all of one ticker's. Connects as submit's client_id.
.venv/Scripts/python -m vibe_trade cancel-pending          # list
.venv/Scripts/python -m vibe_trade cancel-pending AAPL     # cancel AAPL's

# Close EVERYTHING immediately (panic).
.venv/Scripts/python -m vibe_trade panic --yes
```

---

## 6. Troubleshooting

| Symptom | Cause / action |
|---|---|
| `[CRITICAL]` Telegram + job exited non-zero | Uncaught error (often IB Gateway down). Fix Gateway, then **manually rerun that day's job**. |
| Wednesday ~16:00 Gateway outage | Known IB weekly paper maintenance (seen 5/13, 5/20). Rerun submit/record once Gateway is back, or shift Wednesday's cron later. |
| Missed a full day | `reconcile` back-fills orphan fills (Bug #5): permIds in `ib.fills()` with no DB row are inserted straight to OPEN. Re-running reconcile recovers most cases. IB can't serve *past-day* fills, so run same-day where possible. |
| Telegram says "N failed" on a Monday-style run | Should not happen: `PreSubmitted` (pre-RTH market orders) counts as success (Bug #1). If it recurs, check IB order status mapping. |
| Book stuck at 50 positions, no new entries | Expected — at cap, the entire BUY scan is skipped; entries resume when a strategy's SELL frees a slot. |
| Strategy not trading as expected | `config-check` to confirm it's enabled and in the pool; backtest it; remember regime semantics re-signal daily and held-dedup prevents re-buys. |

---

## 7. Invariants (don't break without cause)

- **Positions source at 16:00 = IB**, not the DB. DB is a history mirror.
- **Submit writes nothing to the DB.** Record (16:25) is the persistence step.
- **Strategies evaluate yesterday's closed daily bar** (`df.iloc[-1]`). No intraday, no lookahead.
- **One order per ticker per day.**
- **Cross-process dedup on `permId`**, not `orderId` (orderId resets to 0 across processes).
