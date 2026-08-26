# vibe_trade — Project Context for Claude Code

> A Python stock trading bot for Interactive Brokers. Swing trading on daily bars, S&P 500 universe, runs as three short OS-scheduled jobs per day.

## Stack

- **Language:** Python 3.11+
- **Broker:** `ib_async` (fork of ib_insync — use ib_async, not ib_insync)
- **Historical data:** `yfinance` (daily bars, no auth, free). Not IB historical.
- **Market calendar:** `pandas_market_calendars` (NYSE sessions/holidays — used by preflight + submit, not a hand-maintained date list)
- **Database:** SQLite via SQLAlchemy 2.0 ORM + a hand-rolled migration mechanism (`db/migrations.py` — `schema_version` + idempotent `ALTER TABLE`, deliberately not Alembic)
- **Config:** pydantic v2 + pydantic-settings (TOML + env vars)
- **CLI:** typer + rich
- **Testing:** pytest + pytest-asyncio; runs via `.venv/Scripts/python -m pytest`
- **Windows gotcha:** pandas/numpy may hit DLL blocking — lazy-import inside fixtures that need them (see `tests/conftest.py`)

## Architecture (V2 — current target)

Three short-lived OS-scheduled jobs per trading day. Full details in `docs/ARCHITECTURE_V2.md`.

| Time (Asia/Jerusalem) | Command                   | Role                                                                    |
| --------------------- | ------------------------- | ----------------------------------------------------------------------- |
| 15:50                 | `vibe-trade preflight`    | Verify IB Gateway is up + config sane. Read-only. Telegrams either way. |
| 16:00                 | `vibe-trade submit`       | Exits first, then entries. Submits orders to IB. No DB writes.         |
| 16:35                 | `vibe-trade record`       | Save today's fills to DB as SUBMITTED. Must be AFTER the 16:30 US open.  |
| 23:30                 | `vibe-trade reconcile`    | Update statuses (FILLED/CANCELLED/PARTIALLY_FILLED) + portfolio snapshot. |

**Deployment:** Docker containers via `docker compose run` + host crontab (prod) / Windows 11 manual CLI (dev). Timezone = `Asia/Jerusalem`.
**All operational procedures live in `docs/playbooks/`** (index at `docs/playbooks/README.md`): daily operations, paper reset, data recovery, IB Gateway unattended setup, deployment, Linux bring-up, go-live criteria.

**Key invariants:**
- Strategies evaluate yesterday's closed daily bar (`df.iloc[-1]`). No intraday, no lookahead.
- One order per ticker per day (structural — single-pass scan + universe dedup).
- Positions source at 16:00 = IB (not DB). DB is a history mirror.
- Partial fills → `PARTIALLY_FILLED`, no carry-over.

## Current status

V2 implementation + Sessions I/F/G/H/J/K + Session K-plus (weekly report image) + Session L (multi-strategy) + audit-hardening session + a full five-agent audit (`PROJECT_EVALUATION.md`) with its entire remediation backlog closed same-day. **545 tests passing, `ruff check` clean.** See `PROJECT_MASTER_STATE.md` for the live status.

> **The bot is live** (since 2026-05-06). Both prior open items are now closed:
> the 10-missed-run problem (IB Gateway outages, nothing alerted) is fixed —
> `deploy/systemd/*.timer` (DST-correct `America/New_York` scheduling, installed
> and verified) + OPS-1 (healthchecks.io dead-man's switch, live) — and the
> backtest verdict is no longer withdrawn, it's **in and it's bad**: `donchian`
> at production settings, point-in-time S&P 500 membership, realistic frictions
> — CAGR +4.00%, Sharpe 0.38, max DD -24.75% (median case), badly trailing
> SPY/QQQ buy-and-hold on every axis. That's a decision now, not a data gap —
> see `PROJECT_MASTER_STATE.md` §7 and `docs/playbooks/go-live-criteria.md`.
> Run `python scripts/audit_drift.py <db>` before trusting any DB-derived report.

- **Sessions A–E** (DB schema, sizing/risk, submit, record + reconcile, V1 cleanup): all done.
- **Session I** (backtest framework) + **C-2** (2026-08-26 rebuild): done. Universe is now point-in-time S&P 500 membership (not today's snapshot), production runs are backtested at production settings (`0.018/50`, not the old `0.04/25`), and frictions (slippage + IB commission model) are wired in via `--friction {none,median,stress}`. See verdict above.
- **Session F** (notifications + structured logging): done. All three V2 jobs send Telegram via the configured notifier (Console fallback when off). Logs are plain to stdout + JSON to `logs/vibe_trade.log` with daily rotation, 7-day retention. DB is the source of truth for analytics; logs are for short-term ops only.
- **Session G** (Docker deployment): done. Single image (python:3.11-slim + uv), three compose services, `network_mode: host` for IB Gateway. `deploy/systemd/*.timer` is the recommended scheduler (DST-correct); `crontab.example` is a documented fallback. Includes smoke-test script and full deploy README.
- **Strategies:** Multi-strategy registry exists (Session L) but production runs **donchian only** — `sma`/`ema`/`macd` are registered and available, `enabled=false` by default. `strategy/registry.py` maps ids → strategies, built from the config `[[strategies]]` list (order = entry priority). Submit: priority conflict resolution + strategy-scoped exits (`order_ref=<id>`); record reads `orderRef`→`strategy_name`. Strategies are stateless (`evaluate(symbol, candles)` — no entry-price/stop awareness), so stop/target/trailing/intraday strategies need new machinery.
- **Sizing:** 1.8% of net_liquidation per BUY, 50-position cap, floor to whole shares (cents-based arithmetic to defeat float imprecision).
- **Client IDs:** submit=1, record=2, reconcile=3, notifier=8 (when needed for IB reads). Constants in `jobs/submit.py` and `scratches/scratch_notify_submit.py`.
- **Cross-process state:** record + reconcile drive from `ib.fills()` (intact across processes), with `permId` as the dedup key (`ib.trades().order` fields reset to 0 on reconnect).
- **DB migrations:** `db/migrations.py` — `schema_version` singleton row + idempotent `ALTER TABLE`/`CREATE INDEX` steps run by `init_db`, deliberately hand-rolled instead of Alembic.
- **Ops:** `[healthcheck]` config (OPS-1, opt-in) pings a healthchecks.io-compatible URL from `preflight` on every run — closes the "silent scheduler death" gap Telegram alerts can't cover.
- **Single source of truth:** `PROJECT_MASTER_STATE.md` at the repo root. Read this first when picking up the project. `docs/ROADMAP.md` covers Sessions H → onward.

## Project layout

```
src/vibe_trade/
├── broker/        # ib_async wrapper (IBBroker, base, models)
├── config.py      # pydantic config models + load_config
├── data/          # yfinance provider, SP500 universe, sp100_top static list, market_calendar (NYSE session check)
├── db/            # SQLAlchemy models, repositories, engine, migrations (schema_version + idempotent ALTER TABLE)
├── jobs/          # V2 jobs: preflight.py, submit.py, record.py, reconcile.py, override.py
├── backtest/      # data.py, engine.py, metrics.py, plot.py (Session I) + membership.py (C-2: point-in-time S&P 500)
├── reports/       # data.py, metrics.py, render.py (Session K) + plot.py (Session K-plus weekly PNG)
├── notify/        # Telegram + console notifiers (wired into submit/record/reconcile + panic + report-weekly image) + healthcheck.py (OPS-1)
├── risk/          # manager.py, position_sizer.py, panic.py
├── strategy/      # base.py, registry.py, examples/{donchian,sma_crossover,ema_crossover,macd_crossover}.py
└── cli.py         # typer: preflight, submit, record, reconcile, report, report-weekly, backtest, refresh-sp100, status, trades, config-check, close-position, cancel-pending, review-trades, panic

tests/
├── TEST_REGISTRY.csv    # central list of all tests (update after every change) — the list of
│                        # test files below drifted stale before (audit finding); don't hand-enumerate
│                        # them here again, just `ls tests/test_*.py` or read the registry
├── conftest.py
└── 31 test_*.py files, 545 tests total, one row per test in TEST_REGISTRY.csv

deploy/
├── Dockerfile                 # python:3.11-slim + uv, single image for all jobs
├── docker-compose.yml         # preflight, submit, record, reconcile, report-weekly services (network_mode: host)
├── .env.example               # Telegram secrets template
├── crontab.example            # Mon-Fri 15:50/16:00/16:35/23:30 + Sat 09:00 report-weekly, Asia/Jerusalem (fallback scheduler)
├── systemd/                   # RECOMMENDED scheduler: OnCalendar=...America/New_York timers (DST-correct); confirmed running on the dev host
├── ibgateway/                 # systemd + IBC assets so Gateway starts itself (still untested on a live box)
├── smoke-test.sh              # sequential run of all jobs
└── README.md                  # full setup, scheduling, troubleshooting guide

docs/
├── playbooks/                 # ALL operational procedures (start at README.md)
│   ├── README.md              # index / router
│   ├── daily-operations.md    # cadence, strategy pool, config, troubleshooting
│   ├── paper-reset.md         # clean-slate paper run
│   ├── data-recovery.md       # drift audit, NEEDS_REVIEW, backup restore
│   ├── ib-gateway.md          # systemd + IBC unattended Gateway
│   ├── deployment.md          # Docker, cron install, updating
│   ├── linux-bringup.md       # fresh prod host end to end
│   └── go-live-criteria.md    # scale-to-live gates — the C-2 verdict lives here too
├── ARCHITECTURE_V2.md         # original V2 plan + post-implementation deltas
├── ROADMAP.md                 # Sessions H → onward
├── SESSION_H_FINDINGS.md      # numbered bug/incident history (Session H week specifically)
└── superpowers/               # per-session design specs + implementation plans
    ├── specs/
    └── plans/

scripts/                       # operator tools (not pytest)
├── audit_drift.py             # DB-vs-IB consistency report; exit 1 if dirty
├── measure_slippage.py        # realized fill-vs-open slippage in bps (needs network)
├── reset_paper_db.py          # archive + wipe the DB (refuses unless mode=paper)
├── verify_db.py               # integrity_check + row counts
├── export_db.sh               # consistent snapshot out of the Docker volume
└── import_prod_db.ps1         # pull a prod snapshot to the dev machine

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
.venv/Scripts/python -m vibe_trade preflight    # 15:50 — is Gateway up? read-only
.venv/Scripts/python -m vibe_trade submit       # 16:00 — exits then entries
.venv/Scripts/python -m vibe_trade record       # 16:35 — persist today's fills (after 16:30 open)
.venv/Scripts/python -m vibe_trade reconcile    # 23:30 — finalize statuses + snapshot
.venv/Scripts/python -m vibe_trade report-weekly # Sat 09:00 — weekly dashboard PNG + Telegram (needs .[plot])

# Backtest — production-equivalent run (point-in-time universe, real settings, frictions)
.venv/Scripts/python -m vibe_trade backtest --start 2018-01-01 --end 2026-01-01 \
    --universe sp500 --pct 0.018 --max-positions 50 --friction median

# Maintenance
.venv/Scripts/python -m vibe_trade refresh-sp100              # quarterly: refresh top-100 list
.venv/Scripts/python -m vibe_trade refresh-sp500-membership    # refresh point-in-time S&P 500 membership (C-2)

# Docker deployment (Linux prod — see docs/playbooks/deployment.md)
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
