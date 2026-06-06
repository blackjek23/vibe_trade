# vibe_trade Roadmap — post-V2

**As of 2026-04-29.** V2 implementation (Sessions A–E) complete; commits `925af28..65dcbb9` on `main`. 164 tests passing. Bot can run end-to-end against IB paper via three cron-triggered commands: `vibe-trade submit / record / reconcile`.

This document maps what comes next. Sessions are numbered F+ to continue the A–E sequence, grouped by phase. Order is a recommendation, not a hard sequence — re-prioritize based on findings.

---

## Phase 1 — Production readiness (before live paper)

### Session F — Notifications & logging polish
- Wire existing `notify/telegram.py` into submit/record/reconcile job results
- Daily summary message at 23:35 (post-reconcile): trades placed, P&L, errors
- Structured log format (JSON to file, plain to stdout)
- Log rotation so the cron jobs don't fill disk
- ~15 tests, ~1 hour

### Session G — Cron deployment on Linux prod
- `deploy/crontab.example` with three cron lines (16:00, 16:25, 23:30 Asia/Jerusalem)
- `deploy/systemd-timer/` alternative (more robust than cron for retries)
- Docs: install steps, log locations, missed-run recovery
- Smoke test script that runs all three jobs in sequence against paper
- Mostly docs + one shell script. No new tests.

### Session H — Live paper week (not a coding session)
- Run the bot daily for 5–10 trading days, observe behavior
- Track: orders placed vs strategy signals, partial fills (any?), reconcile drift, log noise
- After the week: triage findings into later sessions as needed

---

## Phase 2 — Operational maturity

### Session I — Backtesting framework
- New `src/vibe_trade/backtest/` module
- Pull 5 years of S&P 500 daily bars via yfinance, simulate Donchian + 1.8% × 50 sizing
- Output: trade log, equity curve, sharpe, max drawdown, win rate
- **Validates whether the strategy/sizing combo is actually profitable before risking money.**
- Recommend running this **before** Session H if you want confidence in the strategy.

### Session J — Manual override CLI
- `vibe-trade close-position SYMBOL` — emergency exit one ticker
- `vibe-trade cancel-pending [perm-id]` — cancel a working order
- `vibe-trade replay-fills DATE` — re-run record for a missed day
- Smaller than it looks; thin typer wrappers + tests

### Session K — Performance dashboard ✅ Done (2026-05-26)
- `vibe-trade report --days N` ships. Read-only against `daily_pnl` +
  `portfolio_snapshot` + `trades`. Five sections: header, equity & risk,
  current holdings (top/bottom 5), trade activity (outlier-flagged),
  trade stats (n/a until first SELL fires). HTML output deferred — the
  user mentioned wanting a web UI separately, and `reports/metrics.py` +
  `reports/data.py` are pure modules designed to back it.

### Session K-plus — Weekly report image ✅ Done (2026-06-06)
- Scope refined from an on-demand `--plot` flag to a **scheduled weekly job**.
  `vibe-trade report-weekly` (Saturday 09:00 Asia/Jerusalem) renders a single
  dashboard PNG to `general.reports_dir` (`reports/<date>-weekly.png`):
  equity curve + holdings bar chart + key-metrics text panel. Strict last-7-day
  window. Read-only (no IB); reuses `reports/data.py` + `reports/metrics.py`.
- Delivered to Telegram via a new `notify_report_image` (`send_photo`); writes
  the file regardless. Empty window still emits a sentinel PNG so the job
  always reports it ran.
- New `reports/plot.py` (mirrors `backtest/plot.py` style), `report-weekly` CLI
  command, Docker `report-weekly` service + Saturday cron line. Dockerfile now
  installs the `.[plot]` extra (matplotlib) for the report job.
- **Monthly is the trivial follow-up:** `save_report_plot(period_label="Monthly")`
  + a `report-monthly` command with a ~30-day window + a monthly cron line.
- Deliberately disposable. Bridges the gap until the BI web project (Phase 6).

---

## Phase 3 — Multi-strategy (deferred until after live paper validation)

### Session L — Strategy registry V2 ✅ Done (2026-06-06)
- New `src/vibe_trade/strategy/registry.py`: `STRATEGY_FACTORIES` (id → factory)
  + `build_strategy(id, params)` + `build_strategies(configs, default_pct)`.
- Three new strategies, all **regime/state** semantics (BUY when fast > slow,
  SELL when fast < slow), tunable via config `params`:
  Donchian (`"donchian"`, existing) + SMA crossover (`"sma"`, 20/50) +
  EMA crossover (`"ema"`, 12/26) + MACD crossover (`"macd"`, 12/26/9).
  SMA/EMA share `_crossover.py`'s `_CrossoverStrategy` base.
- New config `[[strategies]]` list (`config.StrategyConfig`): order = entry
  **priority**; optional per-strategy `pct_per_position` (else global fallback);
  `params` dict. Default-when-absent = single donchian.
- Submit reworked: priority conflict resolution (first strategy to BUY a ticker
  wins, tagged `order_ref=<id>`), **strategy-scoped exits** (each position
  exited only by its owner — symbol→owner map read from DB by the CLI and passed
  in so `run_submit` stays DB-free), per-strategy sizing, and dynamic lookback
  sizing (SMA(50)/EMA/MACD need > the old 60-day window).
- Record reads `fill.execution.orderRef` → `strategy_name` (fallback default for
  empty/legacy refs). Reconcile unchanged.
- Backtest gains `--strategy <id>` to vet each strategy standalone.
- Orphan/unknown-owner positions default to the highest-priority strategy.
- **Deferred** (don't fit the stateless `evaluate(symbol, candles)` interface):
  RSI / Bollinger / ROC (buildable later); stop-loss / take-profit / ATR
  trailing / time-based exits (need entry price or per-position state); intraday.
- +55 tests (376 total).

### Session M — Portfolio allocation rules
- Max % per strategy (e.g. don ≤ 60%, rsi ≤ 30%, ma ≤ 20%)
- Sub-cap on positions per strategy
- Risk manager gets new check methods

---

## Phase 4 — Resilience hardening (driven by Session H findings)

- **Late-fill edge case:** order placed at 16:00, fills after 16:25 — record misses it. Solution: reconcile auto-creates DB rows for fills with no matching trade row.
- **Reconnect logic** for transient IB API drops mid-run
- **DB schema migration tool** (alembic or simple deltas) — current `init_db` only creates missing tables, not new columns
- **Disaster recovery:** rebuild DB from IB history when local data is lost

---

## Phase 5 — Beyond paper

- Live trading switch (`mode = "live"` in config)
- Multi-account support
- Limit orders / OCO brackets
- Short positions
- Universe expansion (Russell 2000, sector ETFs, custom lists)
- Telegram bot for ad-hoc queries (web dashboard moved to Phase 6)

---

## Phase 6 — BI web project (planned)

After the bot has run headless for "some time" and accumulated enough
data to make charts meaningful, replace the Session K terminal report
and Session K-plus PNGs with a real BI dashboard.

- Point **Metabase** or **Grafana** at the existing SQLite DB via their
  SQLite plugins. Zero custom Python — chart `daily_pnl`,
  `portfolio_snapshot`, and `trades` directly.
- Run as an additional `docker compose` service alongside the existing
  submit/record/reconcile stack. `network_mode: host` already gives it
  the DB volume mount.
- `reports/metrics.py` definitions become the canonical formulas
  mirrored in the BI tool (sharpe, drawdown, win rate, profit factor).
- Headless access via SSH tunnel or a port exposed behind auth.
- This replaces both the terminal-rendered report (kept as a CLI
  fallback) and the disposable PNGs.

---

## Recommended order

**I → F → G → H → triage**

Rationale:
1. **I (backtest)** first — no point wiring Telegram for a strategy you haven't validated.
2. **F (notifications)** so you get told when something happens before letting it run unattended.
3. **G (cron)** so the bot runs without you.
4. **H (paper week)** to gather real-world signal/issue data.
5. After H, the rest of the phases re-rank based on what actually broke or proved valuable.

Phases 3–5 are explicitly deferred until live paper has been observed. Don't build multi-strategy on a strategy you don't trust yet.
