# vibe_trade — Project Context for Claude Code

> A Python stock trading bot for Interactive Brokers. Swing trading on daily bars, S&P 500 universe, runs as three short OS-scheduled jobs per day.

## Stack

- **Language:** Python 3.11+
- **Broker:** `ib_async` (fork of ib_insync — use ib_async, not ib_insync)
- **Historical data:** `yfinance` (daily bars, no auth, free). Not IB historical.
- **Database:** SQLite via SQLAlchemy 2.0 ORM
- **Config:** pydantic v2 + pydantic-settings (TOML + env vars)
- **CLI:** typer + rich
- **Testing:** pytest + pytest-asyncio; runs via `.venv/Scripts/python -m pytest`
- **Windows gotcha:** pandas/numpy may hit DLL blocking — lazy-import inside fixtures that need them (see `tests/conftest.py`)

## Architecture (V2 — current target)

Three short-lived OS-scheduled jobs per trading day. Full details in `docs/ARCHITECTURE_V2.md`.

| Time (Asia/Jerusalem) | Command                   | Role                                                                    |
| --------------------- | ------------------------- | ----------------------------------------------------------------------- |
| 16:00                 | `vibe-trade submit`       | Exits first, then entries. Submits orders to IB. No DB writes.         |
| 16:25                 | `vibe-trade record`       | Save today's submissions to DB as SUBMITTED.                            |
| 23:30                 | `vibe-trade reconcile`    | Update statuses (FILLED/CANCELLED/PARTIALLY_FILLED) + portfolio snapshot. |

**Deployment:** Docker containers via `docker compose run` + host crontab (prod) / Windows 11 manual CLI (dev). Timezone = `Asia/Jerusalem`. See `deploy/README.md` for setup.

**Key invariants:**
- Strategies evaluate yesterday's closed daily bar (`df.iloc[-1]`). No intraday, no lookahead.
- One order per ticker per day (structural — single-pass scan + universe dedup).
- Positions source at 16:00 = IB (not DB). DB is a history mirror.
- Partial fills → `PARTIALLY_FILLED`, no carry-over.

## Current status

V2 implementation + Session I (backtest run, profitable) + Session F (notifications + JSON-rotating logs) + Session G (Docker deployment). **231 tests passing.** Ready for Session H (live paper week).

- **Sessions A–E** (DB schema, sizing/risk, submit, record + reconcile, V1 cleanup): all done.
- **Session I** (backtest framework + first run + benchmarks): done. Strategy beats SPY/QQQ on Sharpe (1.14 vs 0.78/0.85) with half the drawdown. Verdict: profitable, proceed forward.
- **Session F** (notifications + structured logging): done. All three V2 jobs send Telegram via the configured notifier (Console fallback when off). Logs are plain to stdout + JSON to `logs/vibe_trade.log` with daily rotation, 7-day retention. DB is the source of truth for analytics; logs are for short-term ops only.
- **Session G** (Docker deployment): done. Single image (python:3.11-slim + uv), three compose services, `network_mode: host` for IB Gateway. Host crontab triggers jobs Mon–Fri. Includes smoke-test script and full deploy README.
- **Strategy:** Donchian channel breakout, N=20, symmetric, excluding the bar being evaluated. Single strategy for first iteration; locked in `src/vibe_trade/strategy/examples/donchian.py`.
- **Sizing:** 1.8% of net_liquidation per BUY, 50-position cap, floor to whole shares (cents-based arithmetic to defeat float imprecision).
- **Client IDs:** submit=1, record=2, reconcile=3, notifier=8 (when needed for IB reads). Constants in `jobs/submit.py` and `scratches/scratch_notify_submit.py`.
- **Cross-process state:** record + reconcile drive from `ib.fills()` (intact across processes), with `permId` as the dedup key (`ib.trades().order` fields reset to 0 on reconnect).
- **Single source of truth:** `PROJECT_MASTER_STATE.md` at the repo root. Read this first when picking up the project. `docs/ROADMAP.md` covers Sessions H → onward.

## Project layout

```
src/vibe_trade/
├── broker/        # ib_async wrapper (IBBroker, base, models)
├── config.py      # pydantic config models + load_config
├── data/          # yfinance provider, SP500 universe, sp100_top static list
├── db/            # SQLAlchemy models, repositories, engine
├── jobs/          # V2 jobs: submit.py, record.py, reconcile.py, override.py
├── backtest/      # data.py, engine.py, metrics.py (Session I)
├── notify/        # Telegram + console notifiers (wired into submit/record/reconcile + panic)
├── risk/          # manager.py, position_sizer.py, panic.py
├── strategy/      # base.py, examples/donchian.py
└── cli.py         # typer: submit, record, reconcile, backtest, refresh-sp100, status, trades, config-check, close-position, cancel-pending, panic

tests/
├── TEST_REGISTRY.csv    # central list of all tests (update after every change)
├── conftest.py
└── test_broker.py, test_config.py, test_db.py, test_donchian.py, test_position_sizer.py,
    test_reconcile.py, test_record.py, test_risk_manager.py, test_submit.py, test_universe.py,
    test_backtest_data.py, test_backtest_engine.py, test_backtest_metrics.py

deploy/
├── Dockerfile                 # python:3.11-slim + uv, single image for all jobs
├── docker-compose.yml         # submit, record, reconcile services (network_mode: host)
├── .env.example               # Telegram secrets template
├── crontab.example            # Mon-Fri 16:00/16:25/23:30 Asia/Jerusalem
├── smoke-test.sh              # sequential run of all three jobs
└── README.md                  # full setup, scheduling, troubleshooting guide

docs/
├── ARCHITECTURE_V2.md         # original V2 plan + post-implementation deltas
├── ROADMAP.md                 # Sessions H → onward
└── superpowers/               # per-session design specs + implementation plans
    ├── specs/
    └── plans/

scratches/               # live IB-paper shape-discovery + DB-write scripts (not pytest)
├── scratch_positions.py       # get_positions + account summary
├── scratch_save_to_db.py      # account + positions → daily_pnl + portfolio_snapshot
├── scratch_orders_today.py    # ib.trades() + ib.openOrders() — raw shape
├── scratch_orders_save.py     # BUYs → SUBMITTED, SELLs flip OPEN→PENDING_CLOSE (record equivalent)
├── scratch_place_order.py     # list of (SIDE, TICKER, QTY) — places BUY/SELL via broker
├── scratch_fills_today.py     # ib.fills() raw + grouped by order_id
├── scratch_reconcile.py       # fills → status transitions + snapshot + daily_pnl (reconcile equivalent)
├── scratch_notify_submit.py    # IB → Submit-style Telegram message (notifier client_id=8)
├── scratch_notify_record.py    # test_paper.db → Record-style Telegram message (no IB)
└── scratch_notify_reconcile.py # test_paper.db → daily summary table (no IB)
```

## Conventions

- **Tests:** every code change gets a test. Every test gets a row in `tests/TEST_REGISTRY.csv` (4 columns: `test_file, test_type, test_name, test_objective`).
- **IB paper client_id for tests:** main config client_id + 50 (e.g. main=1 → test=51). User confirms TWS is running on port 7497 before integration tests.
- **Git:**
  - Stage files by name (`git add path1 path2`), not `-A` — `.claude/` and local files should stay untracked.
  - Multi-line commit messages: what changed, why, test count delta, `Co-Authored-By`.
  - Don't push unless user asks.
- **`.gitignore` gotcha (solved, but worth knowing):** `data/` (no leading slash) matches at any depth and once silently excluded `src/vibe_trade/data/`. Always anchor with `/data/` for root-only.

## Running things

```bash
# Tests
.venv/Scripts/python -m pytest              # full suite
.venv/Scripts/python -m pytest tests/test_broker.py -v

# Diagnostic against live IB paper (requires TWS running on 7497, mode=paper)
.venv/Scripts/python scratches/scratch_positions.py       # data-pull, safe anytime
.venv/Scripts/python scratches/scratch_reconcile.py       # writes to data/test_paper.db

# V2 CLI (paper or live per config.toml mode)
.venv/Scripts/python -m vibe_trade --help
.venv/Scripts/python -m vibe_trade submit       # 16:00 — exits then entries
.venv/Scripts/python -m vibe_trade record       # 16:25 — persist today's fills
.venv/Scripts/python -m vibe_trade reconcile    # 23:30 — finalize statuses + snapshot

# Backtest
.venv/Scripts/python -m vibe_trade backtest --start 2018-01-01 --end 2026-01-01

# Maintenance
.venv/Scripts/python -m vibe_trade refresh-sp100   # quarterly: refresh top-100 list

# Docker deployment (Linux prod — see deploy/README.md)
cd deploy && docker compose build                  # build image
cd deploy && docker compose run --rm submit        # run one job
cd deploy && ./smoke-test.sh                       # run all three sequentially
```

## Communication style

- Brief first, code after user says go
- Plain scenario walkthroughs, not dense technical dumps
- Don't over-engineer — prefer invariants from structure over runtime checks
- User decides architecture; present options with a recommendation

See also `~/.claude/projects/.../memory/` — user profile, session style, V2 decisions.
