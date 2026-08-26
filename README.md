# vibe_trade

A Python swing-trading bot for Interactive Brokers. Trades the S&P 500 universe on daily bars using a Donchian-channel breakout strategy, executed by three short, OS-scheduled jobs per day.

**Live-trading since 2026-05-06.** A full audit and backtest rebuild on
2026-08-26 found the strategy actually trading barely beats zero after
realistic costs and loses to SPY/QQQ buy-and-hold — see
[Backtest results](#backtest-results) below before assuming this is a
proven edge. The engineering (scheduling, ledger correctness, ops) is solid;
whether the strategy itself is worth running is an open decision.

## Highlights

- **Four-phase daily flow** — `preflight` (15:50) → `submit` (16:00) → `record` (16:35) → `reconcile` (23:30), Asia/Jerusalem time. No long-running process; `deploy/systemd/` timers (DST-correct, recommended) or crontab (fallback) drive timing.
- **IB-first design** — IB account is the source of truth for positions at decision time; SQLite is a history mirror.
- **Cross-process correct** — order/fill bookkeeping deduplicates on `permId` (survives reconnects), driven from `ib.fills()`.
- **Backtested honestly, as of 2026-08-26** — point-in-time S&P 500 membership (not today's survivors), production settings, realistic frictions: +4.0% CAGR, Sharpe 0.38, max drawdown -24.75% — trails SPY/QQQ buy-and-hold on every axis. See [Backtest results](#backtest-results).
- **Telegram notifications** — every job reports submissions, fills, and a daily summary. Plus a healthchecks.io-compatible dead-man's switch on `preflight`.
- **Docker deployment** — single image, five compose services, `deploy/systemd/` timers or host crontab; `network_mode: host` for IB Gateway.
- **DB migrations** — hand-rolled `schema_version` + idempotent `ALTER TABLE`, not Alembic.
- **545 tests** covering broker, sizing, strategy, jobs, backtest, reconcile, drift-recovery and preflight flows.

## Strategy

- **Signal:** Donchian breakout, N=20, symmetric, excluding the bar being evaluated. Enter on close above the 20-day high; exit on close below the 20-day low.
- **Evaluation point:** yesterday's closed daily bar (`df.iloc[-1]`). No intraday data, no lookahead.
- **Sizing:** 1.8% of net liquidation per BUY, floor to whole shares (integer-cents arithmetic).
- **Risk gates:** max 50 open positions; one order per ticker per day; exits run before entries to free capital.

## Architecture

| Time (Asia/Jerusalem) | Command                | Client ID | Role                                                                    |
| --------------------- | ---------------------- | --------- | ----------------------------------------------------------------------- |
| 15:50                 | `vibe-trade preflight` | 1         | Verify Gateway is up + config sane. Read-only. Pings healthchecks.io.  |
| 16:00                 | `vibe-trade submit`    | 1         | Read positions from IB, exits then entries, submit market orders. **No DB writes.** |
| 16:35                 | `vibe-trade record`    | 2         | Read `ib.fills()`, persist as `SUBMITTED` / flip OPEN→`PENDING_CLOSE`.  |
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
├── data/        # yfinance provider, SP500 universe, sp100_top static list, market_calendar.py
├── db/          # SQLAlchemy models + repositories + engine + migrations.py
├── jobs/        # preflight.py, submit.py, record.py, reconcile.py, override.py
├── notify/      # Telegram + console notifiers + healthcheck.py (OPS-1)
├── risk/        # manager.py, position_sizer.py, panic.py
├── strategy/    # base.py + registry.py + examples/{donchian,sma_crossover,ema_crossover,macd_crossover}.py
├── backtest/    # data.py, engine.py, metrics.py, plot.py, membership.py (point-in-time S&P 500)
├── reports/     # data.py, metrics.py, render.py, plot.py
└── cli.py       # typer commands

tests/           # 545 tests + TEST_REGISTRY.csv index
deploy/          # Dockerfile, docker-compose.yml, crontab.example (fallback), systemd/ (recommended), smoke-test.sh
docs/            # ARCHITECTURE_V2.md, ROADMAP.md, SESSION_H_FINDINGS.md, playbooks/
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
.venv/Scripts/python -m pytest                  # full suite (~5s, 545 tests)
.venv/Scripts/python -m pytest tests/test_donchian.py -v

# Daily V2 commands (run manually or via the scheduler)
.venv/Scripts/python -m vibe_trade preflight    # 15:50 — Gateway/config check, read-only
.venv/Scripts/python -m vibe_trade submit       # 16:00 — exits then entries
.venv/Scripts/python -m vibe_trade record       # 16:35 — persist today's fills
.venv/Scripts/python -m vibe_trade reconcile    # 23:30 — finalize + snapshot
.venv/Scripts/python -m vibe_trade report-weekly # Sat 09:00 — dashboard PNG + Telegram

# Backtest — production-equivalent (point-in-time universe, real settings, frictions)
.venv/Scripts/python -m vibe_trade backtest \
    --start 2018-01-01 --end 2026-01-01 \
    --universe sp500 --pct 0.018 --max-positions 50 --friction median

# Operator queries
.venv/Scripts/python -m vibe_trade status       # open positions + today's P&L
.venv/Scripts/python -m vibe_trade trades       # recent trade history
.venv/Scripts/python -m vibe_trade report --days 30  # terminal performance dashboard
.venv/Scripts/python -m vibe_trade config-check # validate config

# Maintenance
.venv/Scripts/python -m vibe_trade refresh-sp100             # quarterly: refresh top-100 list
.venv/Scripts/python -m vibe_trade refresh-sp500-membership  # refresh point-in-time S&P 500 membership

# Emergency
.venv/Scripts/python -m vibe_trade panic --yes  # close all positions
```

## Docker deployment

```bash
cd deploy
cp .env.example .env            # fill in Telegram creds
docker compose build
docker compose run --rm submit  # one-off
./smoke-test.sh                 # run all jobs sequentially

# Recommended scheduler: systemd timers (DST-correct — see deploy/systemd/README.md)
sudo cp systemd/vibe-trade-*.service systemd/vibe-trade-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
for t in preflight submit record reconcile backup report-weekly; do sudo systemctl enable --now "vibe-trade-${t}.timer"; done

# Fallback: crontab (fixed Asia/Jerusalem clock — drifts against the US market during DST-mismatch windows)
crontab crontab.example
```

Full guide: [`docs/playbooks/deployment.md`](docs/playbooks/deployment.md).
All operational procedures: [`docs/playbooks/`](docs/playbooks/).

## Backtest results

**As of 2026-08-26**, replacing an earlier withdrawn number. 8-year run
(2018-01-01 → 2026-01-01) of `donchian` — the only strategy actually
trading — at **production settings** (1.8% per position, 50-position cap),
using **point-in-time S&P 500 membership** (not today's survivors) and
**realistic frictions** (measured slippage + IB's commission model):

| Metric        | `donchian` (median friction) | `donchian` (stress friction) | SPY B&H | QQQ B&H |
| ------------- | ----------------------------- | ----------------------------- | ------- | ------- |
| CAGR          | +4.00%                        | +2.80%                        | +14.12% | +19.25% |
| Sharpe        | 0.38                           | 0.29                           | 0.78    | 0.85    |
| Max drawdown  | -24.75%                        | -25.41%                        | —       | —       |
| Trades        | 1,893                          | —                              | —       | —       |
| Win rate      | 39.1%                          | —                              | —       | —       |

**The strategy trails both benchmarks on every axis — return, Sharpe, and
drawdown.** This is not a strong edge. The go-live decision this result
feeds into is open and unresolved — see
[`docs/playbooks/go-live-criteria.md`](docs/playbooks/go-live-criteria.md).
(An earlier headline number here — Sharpe 1.14 on a different strategy at
non-production settings with a survivorship-biased universe — was withdrawn
2026-07-30 and is superseded by this table, not corrected by it.)

## Status

Live-trading since 2026-05-06. A full five-agent audit (2026-08-26,
[`PROJECT_EVALUATION.md`](PROJECT_EVALUATION.md))
found and fixed 4 CRITICAL + 5 HIGH + several MEDIUM issues the same day,
plus rebuilt the backtest above. Engineering-wise the project is in good
shape; whether the strategy itself should keep running is an open decision,
not a coding task. See [PROJECT_MASTER_STATE.md](PROJECT_MASTER_STATE.md) for
current status and [docs/ROADMAP.md](docs/ROADMAP.md) for what's next.

## Disclaimer

This software is provided for educational and research purposes. It places real orders with whatever account it is configured against. Run on a paper account first. The author accepts no liability for trading losses, broker outages, bugs, or misconfiguration. Verify behavior end-to-end before pointing it at a live account.

## License

No license file is included — all rights reserved by the author unless a license is added. Open an issue if you'd like to use the code.
