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

**Deployment:** Linux headless server with crontab (prod) / Windows 11 manual CLI (dev). Timezone = `Asia/Jerusalem`.

**Key invariants:**
- Strategies evaluate yesterday's closed daily bar (`df.iloc[-1]`). No intraday, no lookahead.
- One order per ticker per day (structural — single-pass scan + universe dedup).
- Positions source at 16:00 = IB (not DB). DB is a history mirror.
- Partial fills → `PARTIALLY_FILLED`, no carry-over.

## Current status

- **Day 2 + 3** (V1 DB + broker): complete.
- **Session A** (V2 DB schema + repo): complete — Trade V2 columns, PortfolioSnapshot model, extended DailyPnL. Legacy `quantity`, V1 `trailing_stop`, redundant `total_pnl` removed. 92 tests total (31 DB).
- **Live paper scratches:** 7 scripts in `scratches/` covering every IB↔DB shape for submit/record/reconcile — see scripts list below.
- **Next:** Session B (broker: `get_order_status` for reconcile) then Session C (`submit` job).
- **V1 scanner/risk/orders:** still present under `src/vibe_trade/`, now broken at runtime (they reference removed `trailing_stop` / `quantity`). To be deleted in Session E.

## Project layout

```
src/vibe_trade/
├── broker/        # ib_async wrapper (IBBroker, base, models)
├── config.py      # pydantic config models + load_config
├── data/          # yfinance provider, S&P 500 universe loader
├── db/            # SQLAlchemy models, repositories, engine
├── notify/        # Telegram + console notifiers
├── orders/        # OrderExecutor (translates signals → broker orders)
├── risk/          # RiskManager, position sizer, trailing stops
├── scheduler/     # APScheduler (to be simplified/removed in V2)
├── strategy/      # BaseStrategy, ma_crossover, rsi_mean_revert, indicators
├── scanner.py     # V1 orchestrator (delete in Session E)
└── cli.py         # typer commands

tests/
├── TEST_REGISTRY.csv    # central list of all tests (update after every change)
├── conftest.py
├── test_broker.py, test_config.py, test_db.py, test_universe.py

docs/
└── ARCHITECTURE_V2.md   # full plan for the three-phase refactor

scratches/               # live IB-paper shape-discovery + DB-write scripts (not pytest)
├── scratch_positions.py       # get_positions + account summary
├── scratch_save_to_db.py      # account + positions → daily_pnl + portfolio_snapshot
├── scratch_orders_today.py    # ib.trades() + ib.openOrders() — raw shape
├── scratch_orders_save.py     # BUYs → SUBMITTED, SELLs flip OPEN→PENDING_CLOSE (record equivalent)
├── scratch_place_order.py     # list of (SIDE, TICKER, QTY) — places BUY/SELL via broker
├── scratch_fills_today.py     # ib.fills() raw + grouped by order_id
└── scratch_reconcile.py       # fills → status transitions + snapshot + daily_pnl (reconcile equivalent)
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

# CLI (V1 only for now; V2 commands land in Sessions C/D)
.venv/Scripts/python -m vibe_trade.cli --help
```

## Communication style

- Brief first, code after user says go
- Plain scenario walkthroughs, not dense technical dumps
- Don't over-engineer — prefer invariants from structure over runtime checks
- User decides architecture; present options with a recommendation

See also `~/.claude/projects/.../memory/` — user profile, session style, V2 decisions.
