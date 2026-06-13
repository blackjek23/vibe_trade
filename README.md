# vibe_trade

A Python swing-trading bot for Interactive Brokers. Trades the S&P 500 universe on daily bars using a Donchian-channel breakout strategy, executed by three short, OS-scheduled jobs per day.

## Highlights

- **Three-phase daily flow** — `submit` (16:00) → `record` (16:25) → `reconcile` (23:30), Asia/Jerusalem time. No long-running process; OS cron drives timing.
- **IB-first design** — IB account is the source of truth for positions at decision time; SQLite is a history mirror.
- **Cross-process correct** — order/fill bookkeeping deduplicates on `permId` (survives reconnects), driven from `ib.fills()`.
- **Backtested** — 2018–2026 on top-100 by market cap: +17.1% CAGR, Sharpe 1.14, max drawdown -20.5% (beats SPY/QQQ on Sharpe with half the drawdown).
- **Telegram notifications** — every job reports submissions, fills, and a daily summary.
- **Docker deployment** — single image, three compose services, host crontab; `network_mode: host` for IB Gateway.
- **393 tests** covering broker, sizing, strategy, jobs, backtest, and reconcile flows.

## Strategy

- **Signal:** Donchian breakout, N=20, symmetric, excluding the bar being evaluated. Enter on close above the 20-day high; exit on close below the 20-day low.
- **Evaluation point:** yesterday's closed daily bar (`df.iloc[-1]`). No intraday data, no lookahead.
- **Sizing:** 1.8% of net liquidation per BUY, floor to whole shares (integer-cents arithmetic).
- **Risk gates:** max 50 open positions; one order per ticker per day; exits run before entries to free capital.

## Architecture

| Time (Asia/Jerusalem) | Command                | Client ID | Role                                                                    |
| --------------------- | ---------------------- | --------- | ----------------------------------------------------------------------- |
| 16:00                 | `vibe-trade submit`    | 1         | Read positions from IB, exits then entries, submit market orders. **No DB writes.** |
| 16:25                 | `vibe-trade record`    | 2         | Read `ib.fills()`, persist as `SUBMITTED` / flip OPEN→`PENDING_CLOSE`.  |
| 23:30                 | `vibe-trade reconcile` | 3         | Finalize statuses (FILLED/CANCELLED/PARTIALLY_FILLED) + portfolio + daily P&L. |

Detailed design: [docs/ARCHITECTURE_V2.md](docs/ARCHITECTURE_V2.md). Module-level map: [PROJECT_MAP.md](PROJECT_MAP.md).

## Stack

- Python 3.11+
- [`ib_async`](https://github.com/ib-api-reloaded/ib_async) — Interactive Brokers (the maintained fork, not `ib_insync`)
- `yfinance` — daily historical bars (no auth, free)
- SQLAlchemy 2.0 + SQLite
- pydantic v2 + pydantic-settings (TOML + env vars)
- typer + rich (CLI)
- pytest + pytest-asyncio
- Docker (deployment)

## Project layout

```
src/vibe_trade/
├── broker/      # ib_async wrapper (IBBroker + ABC + models)
├── config.py    # pydantic config + load_config
├── data/        # yfinance provider, SP500 universe, sp100_top static list
├── db/          # SQLAlchemy models + repositories + engine
├── jobs/        # submit.py, record.py, reconcile.py
├── notify/      # Telegram + console notifiers
├── risk/        # manager.py, position_sizer.py, panic.py
├── strategy/    # base.py + examples/donchian.py
├── backtest/    # data.py, engine.py, metrics.py, plot.py
└── cli.py       # typer commands

tests/           # 393 tests + TEST_REGISTRY.csv index
deploy/          # Dockerfile, docker-compose.yml, crontab.example, smoke-test.sh
docs/            # ARCHITECTURE_V2.md, ROADMAP.md
scratches/       # live IB-paper diagnostics + DB-write scripts (not pytest)
config/          # config.example.toml
```

## Setup

### Prerequisites

- Python 3.11+
- An Interactive Brokers account with TWS or IB Gateway installed (paper account recommended for first runs)
- (Optional) Docker + Docker Compose for production deployment
- (Optional) Telegram bot token + chat ID for notifications

### Install (development, Windows)

```bash
git clone <repo-url> vibe_trade
cd vibe_trade
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
```

### Install (production, Linux)

```bash
git clone <repo-url> /opt/vibe-trade
cd /opt/vibe-trade
pip install .
```

### Configure

```bash
cp config/config.example.toml config/config.toml
# edit config/config.toml — set mode = "paper", IB host/port/client_id, Telegram creds
```

Environment variables (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) override TOML values. See [`.env.example`](.env.example).

## Running

```bash
# Tests
.venv/Scripts/python -m pytest                  # full suite (~5s, 393 tests)
.venv/Scripts/python -m pytest tests/test_donchian.py -v

# Daily V2 commands (run manually or via cron)
.venv/Scripts/python -m vibe_trade submit       # 16:00 — exits then entries
.venv/Scripts/python -m vibe_trade record       # 16:25 — persist today's fills
.venv/Scripts/python -m vibe_trade reconcile    # 23:30 — finalize + snapshot

# Backtest
.venv/Scripts/python -m vibe_trade backtest \
    --start 2018-01-01 --end 2026-01-01 \
    --top-n 100 --pct 0.04 --max-positions 25

# Operator queries
.venv/Scripts/python -m vibe_trade status       # open positions + today's P&L
.venv/Scripts/python -m vibe_trade trades       # recent trade history
.venv/Scripts/python -m vibe_trade config-check # validate config

# Maintenance
.venv/Scripts/python -m vibe_trade refresh-sp100  # quarterly: refresh top-100 list

# Emergency
.venv/Scripts/python -m vibe_trade panic --yes  # close all positions
```

## Docker deployment

```bash
cd deploy
cp .env.example .env            # fill in Telegram creds
docker compose build
docker compose run --rm submit  # one-off
./smoke-test.sh                 # run all three jobs sequentially
crontab crontab.example         # install Mon-Fri schedule
```

Full guide: [`deploy/README.md`](deploy/README.md).

## Backtest results

8-year run (2018-01-01 → 2026-01-01), top-100 by market cap, 4% per position, 25-position cap, zero frictions:

| Metric        | Strategy | SPY B&H | QQQ B&H |
| ------------- | -------- | ------- | ------- |
| Total return  | +253%    | +187%   | +308%   |
| CAGR          | +17.1%   | +14.1%  | +19.3%  |
| Sharpe        | 1.14     | 0.78    | 0.85    |
| Max drawdown  | -20.5%   | -33.7%  | -35.1%  |

Strategy beats both benchmarks on risk-adjusted basis with roughly half the drawdown. Trails QQQ on raw return but with far less pain.

## Status

V2 architecture complete; backtest profitable; Docker deployment scaffolded. Next milestone is **Session H — Live paper week** (5–10 trading days of observation, no coding). See [PROJECT_MASTER_STATE.md](PROJECT_MASTER_STATE.md) for current status and [docs/ROADMAP.md](docs/ROADMAP.md) for upcoming sessions (manual-override CLI, performance dashboard, multi-strategy support).

## Disclaimer

This software is provided for educational and research purposes. It places real orders with whatever account it is configured against. Run on a paper account first. The author accepts no liability for trading losses, broker outages, bugs, or misconfiguration. Verify behavior end-to-end before pointing it at a live account.

## License

No license file is included — all rights reserved by the author unless a license is added. Open an issue if you'd like to use the code.
