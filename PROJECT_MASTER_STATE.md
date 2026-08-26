# vibe_trade — Project Master State

> **Hand-off file.** A new chat session should be able to read **only this file**
> and have everything needed to continue work. Updated at the end of every session
> per the protocol at the bottom.

**Last updated:** 2026-08-26 (full audit + same-day remediation session, including the docs restructuring pass)
**HEAD commit:** `a54b785` Add: OPS-1 dead-man's switch (healthchecks.io-compatible ping)
**Tests:** 545 passing, 0 failing, `ruff check src tests` clean — verified in the
  real `.venv` against the pinned `ruff==0.15.10` toolchain. `test_report_plot`
  still requires matplotlib (`plot` extra).
**Branch:** `main` — synced with `origin/main`.

> 🔴 **Read this before anything else.** A full five-agent audit
> (`PROJECT_EVALUATION.md`, C+ overall grade) ran on 2026-08-26 and
> its entire remediation backlog — Tier 0 through Tier 3, all cheap wins — was
> closed the **same day**. What's below is the current state, not a to-do list.
>
> **What was found and fixed:** 4 CRITICALs (submit had no guard against an empty
> IB position read; the backtest universe was survivorship-biased on today's
> market cap; no DB schema migration path; record could pick the wrong OPEN row
> under a race), 5 HIGHs (bar-freshness / DST trading-safety gap, a
> `requested_quantity` inversion that could silently drop exits, entry/exit fill
> quantities sharing one column, a holiday double-run guard that ate real trading
> days, a live-but-unconfirmed order counted as a failure past the position cap),
> the exposed Telegram token, and a pile of MEDIUM/cheap-win items (OPS-1
> dead-man's switch, hardened backup, digest-pinned Dockerfile, `panic.py` tests,
> CI hardening). Plus one bug found live while installing the fix for H-1b: `get_account_summary`
> resolved `account_id` as the literal string `"All"` instead of the real account,
> which made SEC-2's mode-match check fail closed on every preflight run.
>
> **The one finding whose fix is a *result*, not a patch: C-2, the backtest
> rebuild.** It's done — point-in-time S&P 500 membership (not today's survivor
> snapshot), production settings (`0.018/50`, not the old `0.04/25`), realistic
> frictions. The verdict (§7 below): **`donchian` — the only strategy actually
> trading — barely clears zero after costs and loses to SPY/QQQ buy-and-hold on
> every axis.** That is not something more code can fix; it's a decision the user
> has flagged and deliberately not made yet. See `docs/playbooks/go-live-criteria.md`.
>
> **What's genuinely still open, nothing else:** (1) the live-bot verdict decision
> above, (2) the docs restructuring pass — **in progress as of this update**, and
> (3) `deploy/ibgateway/` (Gateway keepalive via systemd+IBC) is still unexecuted
> on a live box — separate from `deploy/systemd/` (job *scheduling* timers), which
> **is** confirmed installed and running (dev host `jeki-MINIPC`, not yet the
> eventual `/opt/vibe-trade` prod host these files assume).
>
> Run `python scripts/audit_drift.py <db>` before trusting any DB-derived report —
> its last known damage report (2026-07-30 export) found 23 phantom OPEN rows, 14
> duplicated symbols, 3 rows whose `pnl` contradicts their own prices, and two
> ledgers disagreeing by $2,878, all traced to a now-fixed orphan-SELL bug
> (`orphan_sells_recovered` + `_sweep_open_rows`). Re-run it against the current DB
> before relying on this number — it predates several of this session's fixes.

---

## 1. Project Blueprint

### What this is

A Python stock trading bot for Interactive Brokers. **Swing trading on daily bars, S&P 500 universe, runs as four short OS-scheduled jobs per day.** Built end-to-end against IB paper and **running unattended since 2026-05-06**. Backtest validation is done — **not** withdrawn; the verdict is negative, see §7.

### Three-phase architecture (V2 — current)

| Time (Asia/Jerusalem) | Command                | Client ID | Role                                                                  |
| --------------------- | ---------------------- | --------- | --------------------------------------------------------------------- |
| 15:50                 | `vibe-trade preflight` | 1         | Verify Gateway is up + logged in, universe loads, a strategy is on. Read-only. Pings healthchecks.io (OPS-1). |
| 16:00                 | `vibe-trade submit`    | 1         | Exits then entries. Places market orders. **No DB writes.**           |
| 16:35                 | `vibe-trade record`    | 2         | Read `ib.fills()`, persist as SUBMITTED rows / flip OPEN→PENDING_CLOSE. **Must be after the 16:30 US open.** |
| 23:30                 | `vibe-trade reconcile` | 3         | Finalize statuses + portfolio_snapshot + daily_pnl with real counts.  |

Scheduled by `deploy/systemd/*.timer` (`OnCalendar=... America/New_York` for
these four — DST-correct, confirmed installed and running on the dev host) or
`deploy/crontab.example` as a fallback. Jobs are short-lived; no long-running
process.

### Tech stack

- **Python 3.11+**
- **Broker:** `ib_async` (the maintained fork — not `ib_insync`)
- **Historical data:** `yfinance` (daily bars; not IB historical)
- **Market calendar:** `pandas_market_calendars` (real NYSE sessions/holidays, used by preflight + submit)
- **Database:** SQLite via SQLAlchemy 2.0 ORM + a hand-rolled migration mechanism (`db/migrations.py`, deliberately not Alembic)
- **Config:** pydantic v2 + pydantic-settings (TOML + env vars + .env)
- **CLI:** typer + rich
- **Testing:** pytest + pytest-asyncio (`asyncio_mode = "auto"`)
- **Deployment:** Docker (single image, five compose services) + `deploy/systemd/` timers (recommended, DST-correct) or host crontab (fallback), prod; Windows 11 manual (dev)

### Where things live

```
src/vibe_trade/
├── broker/          ib_async wrapper (IBBroker, base, models)
├── config.py        pydantic config + load_config
├── data/            yfinance provider, SP500 universe, sp100_top static list, market_calendar.py
├── db/              SQLAlchemy models + repositories + engine + migrations.py
├── jobs/            preflight.py, submit.py, record.py, reconcile.py, override.py
├── notify/          Telegram + console (submit/record/reconcile/panic/report-weekly) + healthcheck.py (OPS-1)
├── risk/            manager.py, position_sizer.py, panic.py
├── strategy/        base.py, registry.py, examples/{donchian,sma_crossover,ema_crossover,macd_crossover}.py
├── backtest/        data.py, engine.py, metrics.py, plot.py, membership.py (point-in-time S&P 500, C-2)
├── reports/         data.py, metrics.py, render.py, plot.py
└── cli.py           typer commands

tests/               545 tests across all modules + TEST_REGISTRY.csv index
docs/                ARCHITECTURE_V2.md, ROADMAP.md, SESSION_H_FINDINGS.md, playbooks/, superpowers/
scratches/           live IB-paper diagnostics + DB-write scripts (not pytest)
config/              config.example.toml
deploy/              Dockerfile, docker-compose.yml, crontab.example (fallback), smoke-test.sh, README.md,
                     systemd/ (recommended scheduler — confirmed running on the dev host),
                     ibgateway/ (systemd + IBC so Gateway starts itself — still unexecuted on a live box)
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
| **H-hygiene** — Tier-3 cleanup | `session-h-hygiene` branch | The four deferred Tier-3 items from the Saturday triage. **#1** curated `SP500_SYMBOLS` (16 delistings removed, `BRK.B`/`BF.B` → hyphen form) + `normalize_symbol` `.`→`-` helper + one-retry-with-backoff in `DataProvider.get_candles` + `data_unavailable` skip-summary counter on `SubmitResult`. **#2** `TZ=Asia/Jerusalem` committed to all three compose services + `tzdata`/`ENV TZ` in the `Dockerfile`. **#3** `load_config` falls back to `$VIBE_TRADE_CONFIG`; `ENV VIBE_TRADE_CONFIG=/config/config.toml` baked into the image so `docker compose run … config-check` finds the mounted config. **#4** `DataProvider.get_candles_batch` (bounded-concurrency `asyncio.gather`, 10 workers); submit's entries phase prefetches the whole universe in one batch — ~5 min → ~30 s. +15 net tests. |
| **J** — Manual override CLI | `1594bf3` + `488c20b` | Two operator commands off the cron cycle. `close-position SYMBOL` market-SELLs the full IB position (`order_ref="manual"`, confirmation prompt + `--yes`, `OVERRIDE_CLIENT_ID=4`); `cancel-pending [SYMBOL]` lists working orders or cancels all of one ticker's. **No DB writes** — next record/reconcile persists fills (submit invariant). New `jobs/override.py` (`run_close_position`/`run_cancel_pending`), new `OpenOrder` broker model, broker gains `get_open_orders`/`cancel_orders_for_symbol`. `replay-fills` dropped — IB can't serve past-day fills and reconcile Bug #5 orphan back-fill already covers missed runs. **Live-paper verified:** caught a cross-client bug — `ib.openTrades()` and `cancelOrder` are client-scoped, so `cancel-pending` could not see/cancel a `submit` order. Fix: `get_open_orders`/`cancel_orders_for_symbol` call `reqAllOpenOrders` first (visibility), and `cancel-pending` connects as **submit's client_id (1)**, not 4, so `cancelOrder` is honoured (proven live). +17 tests. `close-position` not yet live-verified (needs market hours + a held position). |
| **K** — Performance dashboard | `a985525` (CLI) + `0d99e9d` (outlier fix) | New `vibe-trade report --days N` CLI command. Read-only against `daily_pnl` + `portfolio_snapshot` + `trades` — no IB connection. New `src/vibe_trade/reports/` module (data + metrics + render split). Five output sections: header (with small-sample caveat), equity & risk (sharpe / drawdown with peak+trough dates / CAGR / best+worst day), current holdings (top/bottom 5 by unrealized P&L), trade activity (per-day entries, outlier-flagged), trade stats (n/a block until first SELL fires). Derives activity from `trades.entry_time` because the `daily_pnl.trades_opened` column is unreliable. Smoke-tested against the May 11–25 paper-run DB sample — caught a UX bug where outlier days with zero activity (e.g. 5/13 Gateway outage) didn't surface in the activity table; fixed by merging activity-dates with outlier-dates. Pure metrics layer designed to back a future web UI. +23 tests. |
| **L** — Multi-strategy registry | working tree | Strategy registry V2. New `strategy/registry.py` (`STRATEGY_FACTORIES` + `build_strategy`/`build_strategies`). Three new strategies, all **regime/state** (BUY fast>slow, SELL fast<slow): SMA crossover (`"sma"` 20/50) + EMA crossover (`"ema"` 12/26) sharing `_crossover.py`'s base, + MACD crossover (`"macd"` 12/26/9). New config `[[strategies]]` list (`StrategyConfig`): list order = entry **priority**, optional per-strategy `pct_per_position` (else global fallback), `params` dict; default-when-absent = single donchian. Submit reworked: priority conflict resolution (first BUY wins, `order_ref=<id>`), **strategy-scoped exits** (owner map read from DB by the CLI, passed into the still-DB-free `run_submit`; orphan → highest-priority), per-strategy sizing, dynamic lookback sizing. Record reads `fill.execution.orderRef` → `strategy_name` (fallback for empty/legacy). Backtest gains `--strategy <id>`. config-check now lists + validates active strategies. RSI/Bollinger/ROC and any stop/target/trailing/time/intraday exits deferred (don't fit the stateless interface). +55 tests (376 total). |
| **H** — Live paper week + Tier-1 fixes + Enhancement #1 | `2de11e1` | Five paper days (Mon–Fri 2026-05-11..15) exposed 3 blockers + 1 enhancement, fixed Saturday 2026-05-16: **Bug #1** `PreSubmitted` now counts as a successful placement (no more "9 failed" Telegram on Monday-style runs). **Bug #5** reconcile back-fills orphan fills (late-fill recovery): permIds in `ib.fills()` with no DB row are inserted straight to OPEN via new `repository.create_filled_buy_from_fill`. **Bug #6** all three job entrypoints wrapped in `_run_with_crash_alert` — uncaught exception sends `[CRITICAL]` Telegram via a fresh notifier then re-raises (non-zero exit for cron). **Enhancement #1** force-trim phase between Donchian exits and entries: when held > max, sell the worst-performing positions by unrealized $ P&L, tagged `orderRef="trim"`, mirrored in `backtest/engine.py`. **Validated live Mon–Wed 2026-05-18..20:** Bug #1 confirmed (`Failed=0`), Bug #5 confirmed (`opened` == `placed`, zero drift), Bug #6 proven on the 5/20 Gateway outage (two `[CRITICAL]` alerts delivered where 5/13 was silent), force-trim deployed (never triggered — book never exceeded the 50 cap). 22 new tests. Full findings + validation log in `docs/SESSION_H_FINDINGS.md`. Tier-3 hygiene deferred. |

| **Audit hardening** — Tier-1/2 correctness + ops | `39ac4d8` + `837cd8e` + `59597f2` | Full-project audit (3 parallel reviews + graphify graph), then fixed the verified findings. **Money-path:** late-SELL-fill recovery — `get_pending_orders_for_today`→`get_pending_orders` (drop date filter so a SELL that fills after 23:30 isn't lost forever; SELL-side twin of Bug #5) + stale day-order resolution in reconcile (stale BUY→CANCELLED, stale SELL→reverted OPEN if still held, else flagged `resolve manually`). Submit **double-run guard** — new `broker.get_today_order_refs()`; submit aborts if a strategy/`trim` ref is already at IB (cron retry / manual re-run protection), `--force` overrides; keeps no-DB-writes invariant. **permId=0 guard** in record+reconcile (IB quirk — distinct orders no longer collapse). `daily_pnl.open_positions_count` now counts longs only (explains the 61-on-50-cap §7 anomaly). **Robustness:** per-symbol `asyncio.wait_for` timeout in `get_candles_batch` (a hung yfinance call no longer wedges submit). **Ops:** GitHub Actions CI (ruff + pytest), live-mode warning banner, DB backup procedure (deploy README + crontab), backtest↔submit parity cross-reference comments, RUNBOOK troubleshooting rows. Lint 22→0. +17 tests (393 total). |
| **Tier-0/Tier-1/Tier-2 audit fixes** (`PROJECT_EVALUATION.md`) | `0e817c8` | Fixing the bot before it goes live again, per the 2026-08-26 five-agent audit. **C-1** — `run_submit` now aborts before any order when `get_positions()` comes back empty *and* `total_cash` sits well below `net_liquidation` (a suspected failed/racing IB read, not a flat account) — mirrors the same instinct already live read-only in `reconcile._sweep_open_rows`. **SEC-2** — both `run_submit` and `run_preflight` now take `mode` and hard-check the connected account's id prefix (`DU`/`DF`=paper, `U`=live) against it; submit aborts on mismatch, preflight reports it as a new `account_mode_match` check. **C-4** — record's SELL-fill matching now calls `repo.find_open_by_symbol` (FIFO-ordered) instead of taking `get_open_trades()[0]` from a query with no `ORDER BY`. **H-5** — flipped submit's placement-status check from whitelisting successes (missed `PendingSubmit`/`ApiPending`/empty-string, undercounting the position count and breaching the cap on a busy open) to blacklisting the three genuine failure statuses (`Cancelled`/`ApiCancelled`/`Inactive`). **C-3** — new `db/migrations.py`: a `schema_version` singleton row + idempotent `ALTER TABLE`/`CREATE INDEX` steps run by `init_db` right after `create_all` (deliberately not Alembic — no new dependency, fits a single-writer SQLite file). Verified end-to-end against a hand-built "legacy" DB missing `perm_id`/`exit_perm_id`/`total_cash`/`open_positions_count` and the `schema_version` table entirely: columns/indexes get added, existing data is untouched, re-running `init_db` is a no-op. **H-3** — new `trades.exit_filled_quantity` column (migration v2); `confirm_close_fill`/`close_from_open` now write the exit leg's shares there instead of clobbering the entry-leg `filled_quantity`, so a partial exit no longer destroys the cost basis or hides the un-sold remainder from every OPEN-based query. **H-2** — record now prefers IB's live `totalQuantity` (via `get_open_orders`, cross-process) as `requested_quantity` for a BUY still working at record time, falling back to summed fills only once the order has fully resolved; before this, a still-partially-filled order recorded whatever had filled *as* the request, inverting reconcile's later `filled == requested` check in both directions (false-partial swallowing the exit forever, false-full masking real missing exposure). **H-1a** — new `_last_bar_is_closed` guard in `run_submit`: holds/skips a symbol instead of evaluating it whenever the last candle's date is today's US trading date rather than a closed prior day (the ~3-week Israel/US DST-mismatch window where 16:00 IDT lands after the 09:30 ET open); surfaced in the submit Telegram/console summary and escalates to `notify_error`. **H-4** — new `data/market_calendar.py` (backed by `pandas_market_calendars`, a new runtime dependency — **`uv.lock` needs regenerating** with `uv lock` or `uv sync`, not done here, no `uv` binary in this sandbox): `_run_submit_cli` checks the real NYSE calendar before even connecting to IB and skips cleanly (non-error Telegram) on a weekend/holiday, closing the root cause of the Thanksgiving-orders-abort-Friday failure mode; `run_submit` also gates on an explicit `is_trading_day` param as defense-in-depth (never computed from wall-clock time inside the job itself, to keep it test-deterministic); preflight reports the same calendar check as a new informational `market_session` line (never fails preflight — a holiday is a valid day to do nothing). `deploy/crontab.example`'s DST comment corrected (it had the US-open shift backwards: EARLIER at 15:30 IDT, not later at 17:30, and considered only the November gap not the longer March one) and documents both new guards; `deploy/smoke-test.sh` now runs preflight too and drops the stale "16:25" label. H-1b (moving cron itself to `CRON_TZ=America/New_York`) is intentionally left as a documented follow-up — the code-level guards now cover the actual trading-safety risk. CLAUDE.md's playbook list gets `go-live-criteria.md` (was missing — the one file that tracks the open Telegram-token rotation and go-live gates). **SEC-1 closed 2026-08-26**: a new bot (`Vibe_trade_claude_bot`) replaces the exposed one, new token wired into `deploy/.env`, verified end-to-end (`getMe` + a real delivered `sendMessage`) — the old token should still be revoked in BotFather if that wasn't done as part of creating the new bot. **Still open at the time:** C-2 backtest rebuild, H-1b cron rescheduling (both closed same day — see the row below). +51 tests (473 total at the time), verified green in a fresh `.venv` against the `uv.lock`-pinned toolchain. |
| **Audit backlog closed out** (C-2, H-1b, OPS-1, dead tickers, `account_id` bug) | `0965b6a`..`a54b785` | Same day, later. **C-2 done** (`0e817c8` + verdict run): new `backtest/membership.py` (Wikipedia-scraped point-in-time S&P 500 membership, `MembershipTimeline`), `run_backtest(membership=...)` filters entries (not exits) to the historically-real index each day, `vibe-trade backtest --universe {top100,sp500}`; new `Frictions` dataclass (slippage bps + IB per-share commission model) wired via `--friction {none,median,stress}`. Verdict run: `donchian` at `pct=0.018/max_positions=50`, full point-in-time S&P 500 (580 symbols touched across the range), 2018-01-01→2026-01-01 — median friction **CAGR +4.00%, Sharpe 0.38, max DD -24.75%, 1893 trades, win rate 39.1%, profit factor 1.16**; stress friction CAGR +2.80%, Sharpe 0.29, max DD -25.41%. Both badly trail SPY (+14.12% CAGR, Sharpe 0.78) and QQQ (+19.25%, Sharpe 0.85) B&H on every axis. Outputs at `backtests/c2_donchian_prod_median/` and `backtests/c2_donchian_prod_stress/` (gitignored). **H-1b turned out to already be solved differently than planned**: `CRON_TZ` doesn't work on Ubuntu cron at all (only sets an env var for the spawned process, not the firing time — confirmed empirically); the real fix is `deploy/systemd/*.timer` with `OnCalendar=... America/New_York`, which tracks each zone's own DST rules (verified with `systemd-analyze calendar`) — these were already written in the earlier "cheap wins" batch and are now **confirmed installed and running** on `jeki-MINIPC` (host-specific unit files generated for that box; the checked-in `deploy/systemd/*.service` still target the eventual `/opt/vibe-trade` prod host, deliberately untouched). **Dead-ticker purge** (`c6c8628`): 7 delisted/acquired tickers (FISV, MRO, LUMN, DXC, ILMN, NWL, ENPH) removed from `data/universe.py`'s live-trading `SP500_SYMBOLS` (477→470). **Found and fixed while live-verifying H-1b** (`2df439c`): `IBBroker.get_account_summary()` resolved `account_id` as the literal string `"All"` instead of the real account id (`accountSummaryAsync()` called with no account filter, loop overwrote `account_id` from every row); this made SEC-2's mode-match check fail closed on *every* preflight run against the real account. Fixed by resolving the real id from `ib.managedAccounts()` and guarding against a stray `"All"` row; re-verified against the real Gateway (`NOT READY (1 failure)` → `READY (7/7)`). Same failure shape as the AIZ panic false-negative earlier in the project: passed its own unit tests, only surfaced by actually running it. **OPS-1 done** (`a54b785`): `config.HealthcheckConfig` (opt-in, disabled by default) + `notify/healthcheck.py::ping_healthcheck` (stdlib `urllib`, avoids touching `uv.lock`) wired into preflight, firing on READY or NOT READY. **User then signed up for healthchecks.io, created a Cron Schedule check** (`50 15 * * 1-5`, Asia/Jerusalem, 30 min grace — matches preflight's actual cadence, not a simple period, so weekends don't false-alarm) and set `ping_url` in both `config/config.toml` and `deploy/config/config.toml` (gitignored); verified live end-to-end in-session (`load_config` parses it, a real ping reached healthchecks.io and returned success). +72 tests total across this stretch (545 total). The audit backlog (Tier 0-3, H-1b, OPS-1, dead tickers) is now **fully closed**. The only genuinely open item from the audit is the live-bot verdict decision above — not a bug, a call the user has flagged and not yet made. |

### Not started (per ROADMAP)

- **Session K-plus monthly follow-up** — `report-monthly` (~30-day window) reusing `save_report_plot(period_label="Monthly")` + a monthly cron line; trivial.
- **Session M** — Portfolio allocation rules (per-strategy caps + per-strategy daily_pnl/snapshot rollups, now that record attributes `strategy_name` from orderRef)
- **More strategies (anytime)** — RSI / Bollinger / ROC slot into the registry the same way the crossovers did (new `examples/<name>.py` + `STRATEGY_FACTORIES` entry + `[[strategies]]` block). Stop/target/trailing/time-based/intraday exits need new machinery first (the `evaluate(symbol, candles)` interface is stateless).
- **Phase 4** — Resilience hardening (late-fill edge case, reconnect logic, DB migrations, disaster recovery)
- **Phase 5** — Live trading switch, multi-account, limit orders, shorts, universe expansion
- **Phase 6 (planned)** — BI web project: point Metabase or Grafana at the SQLite DB for headless dashboards. Replaces the throwaway PNGs from Session K-plus. Trigger: after the bot has run headless for "some time" and accumulated enough data to make charts meaningful.

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
| Frictions (backtest) | default `--friction none` (zero); `median`/`stress` model measured slippage + IB commission (C-2) | `backtest/engine.py`, `Frictions` dataclass |
| Client IDs | submit=1, record=2, reconcile=3 | `jobs/submit.py` constants |

### Backtest defaults (different from production deliberately)

| Setting | Backtest CLI default | Production |
|---|---|---|
| Universe | top 100 by market cap (`--universe top100`) | full SP500 (~470 names); `--universe sp500` gives point-in-time membership |
| Per-position size | 4% | 1.8% |
| Max positions | 25 | 50 |
| Date range | `2018-01-01 → 2026-01-01` (locked: 5y volatile + 3y relaxed) | n/a |

The CLI defaults above are deliberately conservative/fast for exploration.
**The C-2 verdict run used explicit `--universe sp500 --pct 0.018
--max-positions 50 --friction median/stress`** to match production exactly —
don't confuse a default-flags run with a production-equivalent one.

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
| **DB migrations** | `src/vibe_trade/db/migrations.py` (schema_version + idempotent ALTER TABLE, run by `init_db`) |
| **Backtest engine** | `src/vibe_trade/backtest/engine.py` (re-uses `donchian.py` + `position_sizer.py`) |
| **CLI commands** | `src/vibe_trade/cli.py` |
| **Docker deployment** | `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/smoke-test.sh` |

---

## 5. How to run things

```bash
# Tests
.venv/Scripts/python -m pytest                       # full suite (~5s, 545 tests)
.venv/Scripts/python -m pytest tests/test_donchian.py -v

# Daily V2 commands (per scheduler, can also run manually)
.venv/Scripts/python -m vibe_trade preflight         # 15:50 — Gateway/config check, read-only, pings healthchecks.io
.venv/Scripts/python -m vibe_trade submit            # 16:00 — exits then entries
.venv/Scripts/python -m vibe_trade record            # 16:35 — persist today's fills
.venv/Scripts/python -m vibe_trade reconcile         # 23:30 — finalize + snapshot
.venv/Scripts/python -m vibe_trade report-weekly     # Sat 09:00 — dashboard PNG + Telegram

# Backtest — production-equivalent (point-in-time universe, real settings, frictions)
.venv/Scripts/python -m vibe_trade backtest \
    --start 2018-01-01 --end 2026-01-01 \
    --universe sp500 --pct 0.018 --max-positions 50 --friction median

# Maintenance
.venv/Scripts/python -m vibe_trade refresh-sp100              # quarterly: refresh top-100 list
.venv/Scripts/python -m vibe_trade refresh-sp500-membership    # refresh point-in-time S&P 500 membership (C-2)

# Operator queries
.venv/Scripts/python -m vibe_trade status            # open positions + today's P&L
.venv/Scripts/python -m vibe_trade trades            # recent trade history
.venv/Scripts/python -m vibe_trade report --days 30  # terminal performance dashboard
.venv/Scripts/python -m vibe_trade config-check      # validate config

# Manual / emergency
.venv/Scripts/python -m vibe_trade close-position AAPL --yes  # market-close one ticker off-cycle
.venv/Scripts/python -m vibe_trade cancel-pending             # list/cancel working orders
.venv/Scripts/python -m vibe_trade panic --yes       # close all positions

# Live IB-paper diagnostics (requires TWS on 7497, mode=paper)
.venv/Scripts/python scratches/scratch_positions.py  # data-pull, safe anytime
.venv/Scripts/python scratches/scratch_reconcile.py  # writes to data/test_paper.db

# Docker deployment (Linux prod — see docs/playbooks/deployment.md)
cd deploy && docker compose build                    # build image
cd deploy && docker compose run --rm submit          # run one job
cd deploy && ./smoke-test.sh                         # run all jobs sequentially

# Scheduler install (recommended: systemd; crontab.example is the fallback)
sudo cp deploy/systemd/vibe-trade-*.service deploy/systemd/vibe-trade-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
for t in preflight submit record reconcile backup report-weekly; do sudo systemctl enable --now "vibe-trade-${t}.timer"; done
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

### Plan of record (set 2026-07-30, superseded by the result below — not yet revised)

| When | What |
|---|---|
| **August 2026** | ~~Rotate the Telegram token~~ **DONE.** ~~Backtest `donchian` at `0.018/50` with friction~~ **DONE — result is negative, see below.** ~~Deploy Gateway keepalive~~ **partially done differently** — job *scheduling* is fixed (`deploy/systemd/`), Gateway keepalive itself (`deploy/ibgateway/`) is still unexecuted. Deciding the September strategy pool on evidence is **blocked on the user's decision below**, not on more data. |
| **2026-09-01** | Reset the paper account (`docs/playbooks/paper-reset.md`) — **plan unchanged, but its own prerequisite doc now says "stop" given the backtest result. Not yet revisited by the user.** |
| **Sept → Dec** | Clean run. Weekly `audit_drift.py`, monthly `measure_slippage.py` |
| **January 2027** | Go / no-go on `mode = "live"` against the gates in `docs/playbooks/go-live-criteria.md` — **Gate B already fails today**, months before this date |

**The four-month paper run still cannot answer whether the strategy is
profitable** — at ~8.3 closed trades/month it yields ~35 trades, an order of
magnitude short of the ~218-345 needed to distinguish edge from noise at 2 SE.
That was always the backtest's job, not the paper run's, and the backtest now
has an answer: see below.

### Backtest results — the real one (2018-01-01 → 2026-01-01, point-in-time S&P 500, production settings)

✅ **This is the C-2 rebuild, done 2026-08-26 — no longer withdrawn.** Universe
is `backtest/membership.py`'s point-in-time S&P 500 (real historical
membership, not today's survivor snapshot); settings are production's own
(`pct=0.018`, `max_positions=50`, not the old `0.04/25`); frictions are the
measured slippage + IB's commission model, not zero.

| Metric | `donchian`, median friction | `donchian`, stress friction | SPY B&H | QQQ B&H |
|---|---|---|---|---|
| CAGR | **+4.00%** | +2.80% | +14.12% | +19.25% |
| Sharpe | **0.38** | 0.29 | 0.78 | 0.85 |
| Max drawdown | **-24.75%** | -25.41% | (not directly comparable — different window slicing) | — |
| Trades | 1,893 | — | — | — |
| Win rate | 39.1% | — | — | — |
| Profit factor | 1.16 | — | — | — |

Outputs on disk (gitignored, local only): `backtests/c2_donchian_prod_median/`
and `backtests/c2_donchian_prod_stress/`.

**The verdict: `donchian` — the only strategy that has traded live since
2026-05-06 — barely clears zero after realistic costs and loses to both
benchmarks on every axis: return, Sharpe, *and* drawdown.** This is the first
honest, non-survivorship-biased, cost-adjusted read of the strategy the bot
has been running for over three months. The old "Sharpe 1.14" headline this
section used to carry (withdrawn 2026-07-30) was a different strategy (`ema`)
at different, non-production settings — this replaces it outright, not just
corrects it.

**This is a decision, not a bug.** When asked, the user chose "flag it, no
action yet" over discussing pausing or stopping the bot — that decision is
still open and is explicitly **not** something to act on unilaterally. See
`docs/playbooks/go-live-criteria.md` for the full gate table (Gate B now
fails) and the banner at its top.

### What's actually next (as of 2026-08-26)

There is no coding backlog left from the audit. What's open is:

1. **The live-bot decision.** Given the C-2 verdict above, does the Sept 1
   paper reset go ahead as planned, get delayed, run a different strategy, or
   something else? Flagged, not decided. Don't act unilaterally — ask.
2. **The docs restructuring pass** — this file, CLAUDE.md, README.md,
   PROJECT_MAP.md, and the playbooks were updated 2026-08-26 to reflect
   current state (this update). Deferred per user request until now.
3. `deploy/ibgateway/` (Gateway keepalive) is still unexecuted on a live box
   — separate from `deploy/systemd/` (job scheduling), which **is** confirmed
   running.
4. Backtesting `sma`/`ema`/`macd` at the same C-2 rigor (point-in-time
   universe, production settings, frictions) — only `donchian` got this
   treatment since it's the only one actually trading. Moot unless the
   strategy pool changes.
5. Everything under "Older session closures" below (Session M, monthly
   report, BI web project) is unstarted and lower priority than 1-4.

### Older session closures (context — not open action items)

**Session K is CLOSED** (2026-05-26). `vibe-trade report --days N` ships as
a read-only performance dashboard pulling from `daily_pnl` +
`portfolio_snapshot` + `trades`. +23 tests. Manually smoke-tested against the
May 11–25 paper-run DB sample (extracted from the `deploy_vibe-data` Docker
volume on the prod server): all five sections render correctly; the 5/13
Gateway-outage day is flagged as an outlier with a `!warn` mark in the
activity table and a footnote noting it inflates Best day / CAGR.

**Session K-plus is CLOSED** (2026-06-06). Scope refined from an on-demand
`--plot` flag to a **scheduled weekly job**: `vibe-trade report-weekly`
(Saturday 09:00) renders one dashboard PNG to `general.reports_dir`
(`reports/<date>-weekly.png`) — equity curve + holdings bar chart + key-metrics
text panel, strict last-7-day window. Read-only; reuses `reports/data.py` +
`reports/metrics.py`. Delivered to Telegram via a new `notify_report_image`
(`send_photo`) and written to disk regardless; empty window emits a sentinel
PNG. New `reports/plot.py` mirrors `backtest/plot.py`. Deploy: `report-weekly`
compose service + `./reports` volume + Saturday cron line; Dockerfile installs
the `.[plot]` extra (matplotlib) so the report job runs in the image. +8 tests.
Manually verified against the sample DB (renders correctly, x-ticks pinned to
data dates). **Monthly is the trivial follow-up:**
`save_report_plot(period_label="Monthly")` + a `report-monthly` command (~30-day
window) + a monthly cron line.

**Session L is CLOSED** (2026-06-06). Multi-strategy registry shipped:
Donchian + SMA(20/50) + EMA(12/26) + MACD(12/26/9), all regime/state. Registry,
`[[strategies]]` config (priority = list order), priority entry conflict
resolution, strategy-scoped exits (DB owner map passed into a still-DB-free
`run_submit`), per-strategy sizing, dynamic lookback, record orderRef
attribution, `backtest --strategy`, config-check strategy validation. In the
example config only `donchian` is enabled; flip `enabled=true` on sma/ema/macd
to activate (and tune `params` / per-strategy `pct_per_position`). +55 tests.

**Validation done:** full suite 376 green; `config-check` parses + lists the
strategies; backtest selector rejects unknown ids. **Not yet done:** standalone
backtests of sma/ema/macd to vet their edge (needs network — run
`backtest --start 2018-01-01 --end 2026-01-01 --top-n 100 --strategy sma`, then
`ema`, `macd`); no live-paper run with a second strategy enabled yet.

**Next candidates:** (a) backtest the three new strategies and decide which to
enable; (b) **Session M** — portfolio allocation rules (per-strategy % caps) +
per-strategy daily_pnl/snapshot rollups (record now persists `strategy_name`);
(c) more strategies (RSI/Bollinger/ROC) drop into the registry trivially.

**Long-term — BI web project (Phase 6):** Metabase or Grafana pointed at
the SQLite DB. The user plans this *after* the bot has run headless for
"some time", once there are months of `daily_pnl` and a meaningful pool
of closed trades. `reports/metrics.py` stays valuable as the canonical
metric definitions to mirror inside whichever BI tool wins.

**Carry-forward notes from Session K:**
- The `daily_pnl.trades_opened` column is unreliable (reads 0 on days with
  confirmed entries — visible in the sample DB on 5/11, 5/12, 5/13). The
  report works around it by deriving activity from `trades.entry_time`, but
  the column being wrong is a real reconcile defect. **Still open** after the
  audit session: `trades_opened` counts `result.opened`, which only
  increments when reconcile itself flips SUBMITTED→OPEN. If record already
  persisted the fill same-day (or it back-filled as an orphan), reconcile
  finds nothing pending and the counter stays 0. Fix needs the May sample DB
  to confirm; left for a future session.
- ~~The 5/12 row shows `open_positions_count=61` (above the 50 cap)~~ —
  **RESOLVED** (audit session, `39ac4d8`): `reconcile` counted
  `len(positions)` including shorts/zero-qty rows; now counts longs only
  (`quantity > 0`) to match submit's cap definition.
- The user mentioned plans for a future web UI to display the dashboard.
  The metrics layer (`reports/metrics.py` + `reports/data.py`) is pure and
  reusable; a web layer renders the same dataclasses to HTML/JSON instead
  of calling `render.py`.
- The 4 "failing" tests from Session J's hand-off (3× `test_backtest_plot`,
  1× `test_risk_manager`) are no longer failing — full suite is
  `309 passed, 1 skipped, 0 failing`. Either matplotlib was installed or
  the tests now skip cleanly. Worth verifying before declaring it solved.
- A throwaway `config/config.local.toml` was created during smoke testing
  (sample-DB path override). It's untracked and gitignored implicitly via
  the `config/` directory convention — leave alone.

### Known operational notes (carry forward)

- **Wednesday Gateway outages** — IB paper Gateway went down ~16:00 on both
  2026-05-13 and 2026-05-20. Likely IB weekly paper maintenance. The bot now
  alerts via `[CRITICAL]` Telegram and exits non-zero (Bug #6 fix); operator
  should manually rerun that day or shift Wednesday's cron later.
- **Portfolio at the 50 cap** — since 2026-05-19 the book is full; entries are
  skipped until Donchian SELL signals free up slots. Expected, not a bug.

### After the above (per ROADMAP)

- **Session K-plus monthly:** `report-monthly` (~30-day window) — trivial reuse of `save_report_plot(period_label="Monthly")`
- **Session M:** Portfolio allocation rules (per-strategy caps) + per-strategy P&L rollups
- **Phase 6:** BI web project (Metabase/Grafana on SQLite) once data has accumulated

### Open questions deferred to later sessions

- **Multi-strategy attribution** (Session L — DONE): submit tags each BUY with `order_ref=<strategy_id>`; record reads `fill.execution.orderRef` → `strategy_name`. Strategy-scoped exits resolve the owner from the DB. Per-strategy P&L rollups in daily_pnl/snapshot remain for Session M.
- **Survivorship bias** (Phase 4) — **DONE** (C-2, 2026-08-26): `backtest/membership.py` gives point-in-time S&P 500 membership instead of today's snapshot; see the verdict in §7.
- **Reconnect logic** during a job: current crash-alert wrapper notifies and exits; no in-process recovery from mid-run disconnect. Still open.

---

## 8. Companion documents

- [`CLAUDE.md`](CLAUDE.md) — Claude Code project context (style, conventions, running commands)
- [`PROJECT_MAP.md`](PROJECT_MAP.md) — module-level reference + Mermaid diagrams
- [`docs/ARCHITECTURE_V2.md`](docs/ARCHITECTURE_V2.md) — original V2 plan + implementation deltas
- [`PROJECT_EVALUATION.md`](PROJECT_EVALUATION.md) (+ `.pdf`) — the 2026-08-26 five-agent full-codebase audit that drove the Tier 0-3/C-2/OPS-1 remediation (fully closed, kept as historical record with a resolution banner at the top)
- [`docs/playbooks/`](docs/playbooks/) — **all operational procedures, centralised.** Start at the index:
  daily operations, paper reset, data recovery, IB Gateway, deployment, Linux bring-up
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
