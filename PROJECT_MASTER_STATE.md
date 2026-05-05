# vibe_trade — Project Master State

> **Hand-off file.** A new chat session should be able to read **only this file**
> and have everything needed to continue work. Updated at the end of every session
> per the protocol at the bottom.

**Last updated:** 2026-05-06 (end of Session G — Docker deployment scaffolding, merged to main)
**HEAD commit:** `8559adc` Merge Session G: Docker deployment scaffolding (231 tests)
**Tests:** 231 passing (no Python code changes in Session G)
**Branch:** `main` — synced with `origin/main`.

---

## 1. Project Blueprint

### What this is

A Python stock trading bot for Interactive Brokers. **Swing trading on daily bars, S&P 500 universe, runs as three short OS-scheduled jobs per day.** Built end-to-end against IB paper. Not yet validated by backtest, not yet running unattended.

### Three-phase architecture (V2 — current)

| Time (Asia/Jerusalem) | Command                | Client ID | Role                                                                  |
| --------------------- | ---------------------- | --------- | --------------------------------------------------------------------- |
| 16:00                 | `vibe-trade submit`    | 1         | Exits then entries. Places market orders. **No DB writes.**           |
| 16:25                 | `vibe-trade record`    | 2         | Read `ib.fills()`, persist as SUBMITTED rows / flip OPEN→PENDING_CLOSE.|
| 23:30                 | `vibe-trade reconcile` | 3         | Finalize statuses + portfolio_snapshot + daily_pnl with real counts.  |

Cron drives timing. Jobs are short-lived; no long-running process.

### Tech stack

- **Python 3.11+**
- **Broker:** `ib_async` (the maintained fork — not `ib_insync`)
- **Historical data:** `yfinance` (daily bars; not IB historical)
- **Database:** SQLite via SQLAlchemy 2.0 ORM
- **Config:** pydantic v2 + pydantic-settings (TOML + env vars + .env)
- **CLI:** typer + rich
- **Testing:** pytest + pytest-asyncio (`asyncio_mode = "auto"`)
- **Deployment:** Docker (single image, three compose services) + host crontab (prod) / Windows 11 manual (dev)

### Where things live

```
src/vibe_trade/
├── broker/          ib_async wrapper (IBBroker, base, models)
├── config.py        pydantic config + load_config
├── data/            yfinance provider, SP500 universe, sp100_top static list
├── db/              SQLAlchemy models + repositories + engine
├── jobs/            submit.py, record.py, reconcile.py (V2 jobs)
├── notify/          Telegram + console (currently only used by panic)
├── risk/            manager.py, position_sizer.py, panic.py
├── strategy/        base.py, examples/donchian.py
├── backtest/        data.py, engine.py, metrics.py, plot.py
└── cli.py           typer commands

tests/               231 tests across all modules + TEST_REGISTRY.csv index
docs/                ARCHITECTURE_V2.md, ROADMAP.md
scratches/           live IB-paper diagnostics + DB-write scripts (not pytest)
config/              config.example.toml
deploy/              Dockerfile, docker-compose.yml, crontab.example, smoke-test.sh, README.md
```

---

## 2. Current Implementation Status

Detailed roadmap: [`docs/ROADMAP.md`](docs/ROADMAP.md). Summary:

### Done

| Session | Commit | Outcome |
|---|---|---|
| **A** — DB schema | `925af28` | V2 `Trade` columns, new `PortfolioSnapshot`, extended `DailyPnL`. Removed legacy `quantity`, V1 `trailing_stop`, redundant `total_pnl`. |
| (scratches) | `4299a37` | 7 live IB-paper integration scripts in `scratches/`. |
| (cleanup) | `48c5bf9` | Scratches: ASCII output for Windows cp1252. |
| **B** — Risk/sizing + permId | `7c5bfe8` | Locked sizer (1.8% × 50). Simplified RiskManager. Stripped `TrailingStopConfig`. Added `perm_id`/`exit_perm_id` indexed columns. CLI crash fixes. |
| **C** — Submit job | `b46ef30` | `vibe-trade submit` + Donchian strategy. `jobs/submit.py` with exits-then-entries. |
| **D** — Record + Reconcile | `87c8596` | `vibe-trade record` + `vibe-trade reconcile`. Driven by `ib.fills()` not `ib.trades().order` (cross-process correctness). |
| **E** — Delete V1 | `65dcbb9` | Removed `scanner.py`, `scheduler.py`, `orders/`, `risk/trailing.py`, V1 strategy examples + registry + indicators. CLI commands `scan`/`start` removed. |
| **I** — Backtest framework | `991d257` | `src/vibe_trade/backtest/` (data + engine + metrics). `vibe-trade backtest` command. ROADMAP.md added. |
| (Session I follow-up) | `d610527` | Static `sp100_top.py` (top 100 by mcap, snapshot 2026-05-02) + `vibe-trade refresh-sp100` for quarterly refresh. |
| (Session I — backtest run) | `dc8dd3a` | First backtest run complete. SPY/QQQ benchmarks + backtrader-style equity curve plot. 212 tests. |
| **F** — Notifications + logging | `f1eb3b3` | Telegram messages from submit/record/reconcile (with daily summary table); JSON-to-file logs with daily rotation, 7-day retention. Fixed pre-existing `panic` `_get_notifier` and `config-check` strategy bugs. 231 tests. |
| (Session F follow-up) | `a5b5153` | Three notification scratches (`scratch_notify_submit/record/reconcile.py`) using notifier `client_id=8` and `data/test_paper.db`. Smoke test pending. |
| **G** — Docker deployment | `8559adc` | Single Docker image + three compose services (submit/record/reconcile). `network_mode: host` for IB Gateway. Host crontab triggers `docker compose run --rm`. Includes Dockerfile (uv), docker-compose.yml, crontab.example, smoke-test.sh, .env.example, deploy/README.md, .dockerignore. No Python code changes. |

### Not started (per ROADMAP)

- **Session H** — Live paper week (observation, no coding)
- **Session J** — Manual override CLI (`close-position`, `cancel-pending`, `replay-fills`)
- **Session K** — Performance dashboard (`vibe-trade report`)
- **Session L** — Multi-strategy (Donchian + RSI + MA crossover via `Order.orderRef`)
- **Session M** — Portfolio allocation rules (per-strategy caps)
- **Phase 4** — Resilience hardening (late-fill edge case, reconnect logic, DB migrations, disaster recovery)
- **Phase 5** — Live trading switch, multi-account, limit orders, shorts, universe expansion

---

## 3. Operational Rules

### Locked invariants — do not re-litigate without cause

- **Timezone:** Asia/Jerusalem. All cron schedules, all log timestamps. US market opens 16:30 local, closes 23:00 local.
- **Position source:** **IB at 16:00 is source of truth** for "what do I own." DB is a history mirror, not the system of record.
- **Strategy evaluation point:** yesterday's closed daily bar (`df.iloc[-1]`). No intraday, no lookahead.
- **One order per ticker per day** — structurally enforced (single-pass scan + universe dedup against held positions).
- **Submit has zero DB writes.** Record (16:25) is the persistence step. Regression-guarded by `test_run_submit_signature_has_no_db_param`.
- **Cross-process dedup:** record + reconcile dedup on `permId`, NOT `orderId`. `ib.trades().order.orderId` resets to 0 across processes; only `permId` survives. Pull quantities from `ib.fills()`, not `ib.trades().order.totalQuantity`.
- **Partial fills** → `PARTIALLY_FILLED` status, no carry-over re-issue. Not expected in practice on liquid SP500 names.

### Locked numbers (V2 first iteration)

| Setting | Value | Where |
|---|---|---|
| Strategy | Donchian, N=20, symmetric, excluding the bar being evaluated | `strategy/examples/donchian.py` |
| Per-position size | 1.8% of `net_liquidation` | `risk/position_sizer.py` `DEFAULT_PCT_PER_POSITION` |
| Max open positions | 50 | `risk/position_sizer.py` `DEFAULT_MAX_POSITIONS` |
| Share rounding | floor to whole shares (integer cents arithmetic) | `risk/position_sizer.py` |
| Skip rule | if `1 share > 1.8% of net_liq`: skip + log | `risk/position_sizer.py` |
| Fill model | next-day open (both production and backtest) | `jobs/submit.py` (production semantics; bars evaluated, orders fill at next session open) |
| Frictions (backtest) | zero commissions, zero slippage | `backtest/engine.py` |
| Client IDs | submit=1, record=2, reconcile=3 | `jobs/submit.py` constants |

### Backtest defaults (different from production deliberately)

| Setting | Backtest | Production |
|---|---|---|
| Universe | top 100 by market cap | full SP500 (~494 names) |
| Per-position size | 4% | 1.8% |
| Max positions | 25 | 50 |
| Date range | `2018-01-01 → 2026-01-01` (locked: 5y volatile + 3y relaxed) | n/a |

### Risk management rules

- **Already-holding check** before any BUY: `risk/manager.py:can_trade_symbol`.
- **At-cap check** at start of entries phase: `risk/manager.py:can_open_new_position`. If at 50, **skip entire BUY scan** that day; exits still run.
- **Per-position cap = 1.8%** is a hard constraint enforced by the sizer. Returns 0 (skip signal) rather than override.
- **Exits before entries** in `submit` — frees capital before evaluating new positions.

### Test discipline

- Every code change gets a test.
- Every test gets a row in [`tests/TEST_REGISTRY.csv`](tests/TEST_REGISTRY.csv) (4 columns: `test_file, test_type, test_name, test_objective`).
- Run before committing: `.venv/Scripts/python -m pytest`.

### Git discipline

- Stage files by name (`git add path1 path2`), **not** `-A` — `.claude/` and local files should stay untracked.
- Multi-line commit messages: what changed, why, test count delta, `Co-Authored-By` line.
- Don't push unless asked.
- `.gitignore` gotcha (already solved): `data/` (no leading slash) matches at any depth — anchor with `/data/` for root-only.

### IB paper testing convention

- Test client_id = main config client_id + 50 (e.g. main=1 → test=51).
- User confirms TWS is running on port 7497 before integration tests.
- Scratches in `scratches/` write to `data/test_paper.db` (separate from `data/vibe_trade.db`).

---

## 4. Code Map (where the trade logic lives)

| Concern | File |
|---|---|
| **Signal generation (BUY/SELL/HOLD)** | `src/vibe_trade/strategy/examples/donchian.py` |
| **Position sizing math** | `src/vibe_trade/risk/position_sizer.py` |
| **Risk gates (cap + already-held)** | `src/vibe_trade/risk/manager.py` |
| **Submit orchestration (16:00)** | `src/vibe_trade/jobs/submit.py` |
| **Record (16:25)** | `src/vibe_trade/jobs/record.py` |
| **Reconcile (23:30)** | `src/vibe_trade/jobs/reconcile.py` |
| **Broker abstraction** | `src/vibe_trade/broker/ib_broker.py` (concrete), `broker/base.py` (ABC) |
| **DB writes (lifecycle)** | `src/vibe_trade/db/repository.py` (`TradeRepository`, etc.) |
| **DB schema** | `src/vibe_trade/db/models.py` |
| **Backtest engine** | `src/vibe_trade/backtest/engine.py` (re-uses `donchian.py` + `position_sizer.py`) |
| **CLI commands** | `src/vibe_trade/cli.py` |
| **Docker deployment** | `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/smoke-test.sh` |

---

## 5. How to run things

```bash
# Tests
.venv/Scripts/python -m pytest                       # full suite (~2s, 212 tests)
.venv/Scripts/python -m pytest tests/test_donchian.py -v

# Daily V2 commands (per cron schedule, can also run manually)
.venv/Scripts/python -m vibe_trade submit            # 16:00 — exits then entries
.venv/Scripts/python -m vibe_trade record            # 16:25 — persist today's fills
.venv/Scripts/python -m vibe_trade reconcile         # 23:30 — finalize + snapshot

# Backtest (validation)
.venv/Scripts/python -m vibe_trade backtest \
    --start 2018-01-01 --end 2026-01-01 \
    --top-n 100 --pct 0.04 --max-positions 25

# Maintenance
.venv/Scripts/python -m vibe_trade refresh-sp100     # quarterly: refresh top-100 list

# Operator queries
.venv/Scripts/python -m vibe_trade status            # open positions + today's P&L
.venv/Scripts/python -m vibe_trade trades            # recent trade history
.venv/Scripts/python -m vibe_trade config-check      # validate config

# Emergency
.venv/Scripts/python -m vibe_trade panic --yes       # close all positions

# Live IB-paper diagnostics (requires TWS on 7497, mode=paper)
.venv/Scripts/python scratches/scratch_positions.py  # data-pull, safe anytime
.venv/Scripts/python scratches/scratch_reconcile.py  # writes to data/test_paper.db

# Docker deployment (Linux prod — see deploy/README.md)
cd deploy && docker compose build                    # build image
cd deploy && docker compose run --rm submit          # run one job
cd deploy && ./smoke-test.sh                         # run all three sequentially
```

---

## 6. Surprises / gotchas worth knowing

1. **Cross-process orderId loss** (verified live 2026-04-27): a fresh process sees `order.totalQuantity=0`, `orderId=0`, `clientId=0`. Drive record + reconcile from `ib.fills()` and dedup on `permId`.
2. **Float imprecision in sizing:** `100_000 * 0.018` evaluates to `1799.999...`, not `1800` exactly. `position_sizer.size_position` uses integer cents arithmetic to avoid off-by-one floor errors.
3. **yfinance for `Ticker.info` is slow** (~1 sec per ticker). The static SP100 list exists so backtests don't re-fetch market caps every run.
4. **Survivorship bias in backtest universe:** `sp100_top.py` is *today's* top 100 used across all historical dates. Documented; deferred to Phase 4 hardening.
5. **`SchedulerConfig` is dormant in V2.** Kept as a config section but not used by any V2 job. OS cron drives timing.
6. **Windows console codec (cp1252)** can't render `→` or `—` — scratches use ASCII (`->`, `--`).
7. **`ib_async` is the right import**, not `ib_insync` (the latter is unmaintained — `ib_async` is the active fork).
8. **Strategies must use ib_async/yfinance with `df.iloc[-1]` semantics:** that bar is yesterday's close at 16:00 Jerusalem cron time (US market not yet open). Don't rely on intraday data.

---

## 7. Session Hand-off — start here next time

### Backtest results (2018-01-01 → 2026-01-01, top-100, 4%, 25-cap)

| Metric | Strategy | SPY B&H | QQQ B&H |
|---|---|---|---|
| Total return | +253% | +187% | +308% |
| CAGR | +17.1% | +14.1% | +19.3% |
| Sharpe | 1.14 | 0.78 | 0.85 |
| Max drawdown | -20.5% | -33.7% | -35.1% |

**Verdict:** Strategy beats both benchmarks on risk-adjusted basis (Sharpe 1.14 vs 0.78/0.85) with half the drawdown. Trails QQQ on raw return but with far less pain. **Profitable — proceed forward.**

### Immediate next concrete deliverable

**Session H — Live paper week.** The bot is ready for its first live run. No coding — just observation and triage.

**Pre-flight (on Linux host):**
1. Ensure IB Gateway is running on `localhost:7497`, logged in to paper account
2. Install the bot: `git clone <repo> /opt/vibe-trade && cd /opt/vibe-trade && pip install .` (or use Docker: `cd deploy && docker compose build`)
3. Create `config/config.toml` from `config/config.example.toml` — set `mode = "paper"`, fill in Telegram creds
4. Manual bare-metal run first day: `python -m vibe_trade submit` at 16:00, `record` at 16:25, `reconcile` at 23:30
5. Once validated, switch to Docker + cron: `cd deploy && crontab crontab.example`

**What to observe over 5–10 trading days:**
- Orders placed vs strategy signals (any mismatches?)
- Partial fills (any on liquid SP500 names?)
- Reconcile drift (DB vs IB positions after reconcile)
- Telegram message formatting on mobile
- Log noise level
- IB Gateway login stability (does it drop overnight?)

**After the week:** triage findings into later sessions as needed.

### After Session H (per ROADMAP)

- **Session J:** Manual override CLI (`close-position`, `cancel-pending`, `replay-fills`)
- **Session K:** Performance dashboard (`vibe-trade report`)

### Open questions deferred to later sessions

- **Late-fill edge case** (Phase 4): a market order placed at 16:00 that fills *after* 16:25 — record misses it. Reconcile should auto-create the SUBMITTED row. Not yet implemented.
- **Multi-strategy attribution** (Session L): when a second strategy lands, use `Order.orderRef = strategy_id` so record can read `fill.execution.orderRef`.
- **Survivorship bias** (Phase 4): point-in-time SP500 membership instead of today's snapshot.

---

## 8. Companion documents

- [`CLAUDE.md`](CLAUDE.md) — Claude Code project context (style, conventions, running commands)
- [`PROJECT_MAP.md`](PROJECT_MAP.md) — module-level reference + Mermaid diagrams
- [`docs/ARCHITECTURE_V2.md`](docs/ARCHITECTURE_V2.md) — original V2 plan + implementation deltas
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — Sessions F → onward
- [`tests/TEST_REGISTRY.csv`](tests/TEST_REGISTRY.csv) — every test, one row each

User profile and session-style notes live in `~/.claude/projects/.../memory/` (not in the repo).

---

## 9. Future protocol — keep this file alive

> **At the end of every future session, the assistant MUST update**
> **`PROJECT_MASTER_STATE.md`** with the latest progress, decisions, and pending
> tasks to ensure a seamless hand-off.

Specifically, before ending a session, refresh:

- **Section header** — "Last updated", "HEAD commit", "Tests" counts
- **Section 2 (Current Implementation Status)** — append new rows to "Done", move items out of "Partially implemented" / "Not started"
- **Section 3 (Operational Rules)** — only if a locked decision changed; flag the change
- **Section 7 (Session Hand-off)** — replace with the next concrete deliverable
- **Update `tests/TEST_REGISTRY.csv`** count if tests were added/removed

The user should be able to start a fresh chat with **only**:

> "Read PROJECT_MASTER_STATE.md and let's continue."

…and have everything needed to pick up where we left off.
