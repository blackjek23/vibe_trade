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

---

## Phase 3 — Multi-strategy (deferred until after live paper validation)

### Session L — Strategy registry V2
- Restore registry pattern with new shape (Donchian + RSI mean-reversion + MA crossover)
- Each strategy declares `strategy_id` (e.g. `"don"`, `"rsi"`, `"ma"`)
- Submit places orders with `Order.orderRef = strategy_id`
  (designed in memory: project_v2_next_sessions.md → "strategy_name handoff")
- Record reads `fill.execution.orderRef` to set `strategy_name` correctly
- Position sizing: per-strategy `pct_per_position` overrides, or one global

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
- Web dashboard or Telegram bot for ad-hoc queries

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
