# Architecture V2 — Three-Phase Daily Trading Flow

**Status:** Implemented (Sessions A–E merged on `main`, last update 2026-05-02)
**Owner:** Jeki

> **Reading note:** the original planning document is preserved below for historical
> context. Reality diverged from the plan in a few places during implementation —
> see "Implementation deltas" immediately below. For the as-built picture, read
> `PROJECT_MAP.md` and `PROJECT_MASTER_STATE.md`.

---

## Implementation deltas (where reality diverged from the plan)

| Plan said | What actually shipped | Why |
|---|---|---|
| Trailing stops evaluated at 16:00 against yesterday's low | **No trailing stops at all.** Exits come purely from strategy SELL signals on the daily bar. | Simpler. Donchian's symmetric breakdown signal is the exit. Removed `trailing_stop` columns + `TrailingStopConfig` in Session B. |
| Add `broker.get_order_status(ib_order_id)` for reconcile | **Not added.** Reconcile drives off `ib.trades().orderStatus` + `ib.fills()` directly. | Live diagnostic showed it's redundant — `orderStatus.status` survives reconnect. |
| Dedup orders by `ib_order_id` | **Dedup by `permId`** via new indexed `perm_id` / `exit_perm_id` columns on `Trade`. | Live diagnostic on 2026-04-27: a fresh process sees `order.totalQuantity=0`, `orderId=0`, `clientId=0` for completed orders — only `permId` survives. Forced a schema addition. |
| Generic strategy registry, multiple strategies in `StrategyConfig.active` | **Single hardcoded strategy:** Donchian breakout (N=20, symmetric, excluding eval bar). `StrategyConfig` deleted. | First iteration is single-strategy. Session L (deferred) restores multi-strategy via `Order.orderRef`. |
| `total_pnl` column on `daily_pnl` | **Removed.** Compute `realized + unrealized` on read. | Redundant. Session A. |
| Single shared client_id | **Three hardcoded:** submit=1, record=2, reconcile=3 (constants in `jobs/submit.py`). | Audit trail via `fill.execution.clientId`. IB scopes today's trades/fills by account, not client_id, so cross-client visibility is fine. |
| Position sizing tied to risk-per-share (entry − stop) | **Fixed % of net_liquidation:** 1.8% × 50-position cap. Floor to whole shares via integer cents arithmetic. | No trailing stops = no risk-per-share input. Locked decision in Session B. |
| Three jobs, cron-driven | **Four jobs** — a `preflight` step (15:50) was added later: read-only Gateway/config health check, Telegrams either way, plus (as of the 2026-08-26 audit-fix session) a dead-man's-switch ping to healthchecks.io. Scheduling moved from raw crontab to `deploy/systemd/*.timer` with `OnCalendar=... America/New_York` for the market-tied jobs — a plain Asia/Jerusalem crontab drifts against the US market by up to an hour during DST-mismatch windows (Israel/US flip DST on different dates); systemd's `OnCalendar=` computes each zone's DST correctly. | Ten missed trading days (IB Gateway down, nothing alerted) plus the DST-drift finding (H-1/H-1b) in the 2026-08-26 audit. |

The original plan below describes the architecture as designed. The deltas above
describe what was actually built. Both are useful — the plan reads as intent,
PROJECT_MAP.md reads as implementation.

---

## Why this change

The original architecture had a single long-running scanner that placed orders, polled briefly for fills, and recorded only successful fills to the database. This has three problems:

1. **Lost orders.** If a market order doesn't fill within the 5s poll window, the DB forgets it ever happened — but IB still has it. Bot's internal state diverges from reality.
2. **No pre-market workflow.** Swing trading runs on daily bars; the bot should evaluate *yesterday's* close once per day and submit orders before market open, not scan intraday.
3. **Brittle scheduling.** A long-running Python process with APScheduler is harder to deploy, monitor, and recover from than discrete cron jobs.

V2 replaces the single scanner with **three short-lived jobs** that each do one thing and exit.

## The three jobs

All times are **Asia/Jerusalem** (US market opens 16:30 local, closes 23:00 local).

### Bot 1 — `vibe-trade submit` — 16:00

Submits all orders for the day. Runs 30 min before market open so orders are queued in IB when the bell rings.

**Internally it's two phases in one CLI command:**

1. **Exits phase** (`run_exits`):
   - Pull open positions from IB (`broker.get_positions()`)
   - For each held ticker:
     - Check trailing stop against yesterday's low
     - Check strategy-level exit signal on yesterday's close
     - If either fires → submit SELL order for the full position
2. **Entries phase** (`run_entries`):
   - Load universe
   - Skip tickers already held
   - For each remaining ticker:
     - Fetch daily candles (yfinance, ending at yesterday)
     - Run strategy
     - If BUY signal → size position against current available cash → submit BUY order

**No DB writes.** Orders live only in IB's queue between Bot 1 and Bot 2.

**Exits before entries** by design: frees capital before evaluating new positions.

### Bot 2 — `vibe-trade record` — 16:35

Records every submission from Bot 1 into the DB.

- Connect to IB
- Query today's fills via `ib.fills()` (**not** `ib.trades()` — order fields reset
  to 0 across processes; see §6)
- BUY: insert a `Trade` row with `status=SUBMITTED`. SELL: flip the matching OPEN
  row to `PENDING_CLOSE`
- Exit

> ⚠️ **Timing corrected 2026-07-30 — the original rationale below was wrong.**
>
> This section used to read *"**Why 16:25** (not 16:29 as first drafted): gives a
> 5-minute safety buffer **before market open** in case IB's API is slow to reflect
> the submissions."*
>
> That premise held only while record read *submissions* from `ib.trades()`. Once
> it moved to `ib.fills()`, running before the open became self-defeating: the US
> open is **16:30 Asia/Jerusalem**, market orders placed at 16:00 fill *at* the
> open, and `ib.fills()` is empty until then. At 16:25 record reliably sees zero
> fills.
>
> The live box has run **16:35** all along — that is why prod worked while the repo
> documented a schedule that cannot. `deploy/crontab.example` is now 16:35 and
> carries the full reasoning plus the October/November DST caveat.
>
> Record is a fast path, not the only one: reconcile at 23:30 back-fills orphan
> BUY *and* orphan SELL fills, so a record no-op degrades to a later pickup rather
> than lost data.

### Bot 3 — `vibe-trade reconcile` — 23:30

Final daily reconciliation + portfolio snapshot.

1. **Reconcile:**
   - Query DB for today's `SUBMITTED` / `PENDING_CLOSE` rows
   - For each: ask IB for the order's final status (`broker.get_order_status(ib_order_id)`)
   - Update DB:
     - Fully filled → `FILLED` (BUY) or `CLOSED` (SELL, compute P&L)
     - Partially filled → `PARTIALLY_FILLED` (safety net; not expected to fire often)
     - Never filled / IB cancelled → `CANCELLED`
2. **Portfolio snapshot:**
   - Fetch `account.get_account_summary()` and `account.get_positions()` from IB
   - Insert one row into `daily_pnl` (numbers) + N rows into `portfolio_snapshot` (one per held position)
3. Exit

---

## Data flow through a day

```
Mon 00:00 ─────────────── DB idle ────────────────────
Mon 16:00  Bot 1 runs
           IB ⇢ orders queued
           DB unchanged
Mon 16:35  Bot 2 runs
           IB → ask "today's orders"
           DB ← insert SUBMITTED rows
Mon 16:30  Market opens — IB fills orders over the day
           Bot sleeps. DB unchanged.
Mon 23:00  Market closes
Mon 23:30  Bot 3 runs
           IB → ask final status per order_id
           DB ← update SUBMITTED rows to FILLED / CANCELLED / PARTIALLY_FILLED
           DB ← insert daily_pnl row + portfolio_snapshot rows
Tue 00:00 ─────────────── DB idle ────────────────────
```

---

## Data sources

| Question | Source |
|---|---|
| What positions do we hold? | IB at Bot 1 start (`broker.get_positions()`), not the DB |
| Candles for strategy evaluation | yfinance, daily timeframe, ending at yesterday |
| Order status | IB — DB is a mirror, IB is source of truth |
| Portfolio state history | DB (`daily_pnl` + `portfolio_snapshot`) — IB doesn't keep it |

---

## DB schema diff

### `trades` table

| Column | V1 | V2 | Notes |
|---|---|---|---|
| `entry_price` | float, nullable | float, nullable | unchanged; NULL while SUBMITTED |
| `entry_time` | datetime, nullable | datetime, nullable | unchanged; NULL while SUBMITTED |
| `submitted_at` | — | **datetime, nullable** | when we sent the order to IB |
| `requested_quantity` | — | **int** | what we asked for (renamed from `quantity`) |
| `filled_quantity` | — | **int, nullable** | what actually filled |
| `exit_ib_order_id` | — | **int, nullable** | IB order_id for the closing SELL |
| `exit_submitted_at` | — | **datetime, nullable** | when we sent the close |
| `status` | str | str | new values: `SUBMITTED`, `PENDING_CLOSE`, `PARTIALLY_FILLED` |
| `quantity` | int | removed (replaced by `requested_quantity` + `filled_quantity`) |

**Status lifecycle:**

```
BUY:   SUBMITTED → OPEN              (fully filled)
                 → PARTIALLY_FILLED  (partial, no carry-over)
                 → CANCELLED         (unfilled at close)

SELL:  OPEN → PENDING_CLOSE → CLOSED    (fully filled)
                            → PARTIALLY_FILLED
                            → CANCELLED (reverts to OPEN — position stays)

Recovery paths (added 2026-07-30, reconcile):
       OPEN → CLOSED / PARTIALLY_FILLED   orphan-SELL recovery: the exit filled at
                                          IB but `record` never ran, so no row
                                          carried its exit_perm_id. Matched by
                                          symbol (FIFO) while the fill is still in
                                          `ib.fills()`.
       OPEN → NEEDS_REVIEW                drift sweep: IB does not hold the symbol
                                          and the exit fill is gone from
                                          `ib.fills()` (only the current session is
                                          returned). Terminal until a human
                                          resolves it — we never invent an exit
                                          price.
```

**Why `NEEDS_REVIEW` exists.** Every other transition keys off an order id:
`get_pending_orders` returns only SUBMITTED + PENDING_CLOSE, and the orphan-fill
path only sees permIds in *today's* fills. So a row that reached OPEN and then lost
its exit fill (missed `record`, then a missed `reconcile` the next day) was never
examined by anything again — it stayed OPEN while IB held nothing, and the next
breakout on that symbol opened a *second* OPEN row. That is how 23 phantom rows and
14 duplicated symbols accumulated in prod between 2026-05 and 2026-07. See
`_sweep_open_rows` in `jobs/reconcile.py` and `scripts/audit_drift.py`.

### `portfolio_snapshot` table (new)

One row per held position per day. Records the end-of-day state.

| Column | Type | Notes |
|---|---|---|
| `id` | int, PK | autoincrement |
| `date` | date, indexed | snapshot date (composite index with symbol) |
| `symbol` | str | |
| `quantity` | int | |
| `avg_cost` | float | |
| `market_price` | float | close price at snapshot time |
| `market_value` | float | qty × market_price |
| `unrealized_pnl` | float | |
| `created_at` | datetime | server_default=now() |

### `daily_pnl` table (extended)

Adds:

| Column | Type | Notes |
|---|---|---|
| `total_cash` | float | from IB account summary |
| `open_positions_count` | int | # distinct tickers held |

(Existing columns `realized_pnl`, `unrealized_pnl`, `account_value` stay. `total_pnl` removed — redundant, compute on read as `realized + unrealized`.)

---

## Module changes

| File | Change |
|---|---|
| `src/vibe_trade/config.py` | **Scheduler config rewrite.** Remove `interval_minutes`, `market_open`, `market_close`, `trading_days`. The OS scheduler owns timing now. Keep `timezone` (used by jobs for logging). |
| `src/vibe_trade/db/models.py` | Update `Trade` per schema diff. Add `PortfolioSnapshot` model. Extend `DailyPnL`. |
| `src/vibe_trade/db/repository.py` | New methods: `create_submitted_buy`, `mark_pending_close`, `confirm_buy_fill`, `confirm_close_fill`, `mark_cancelled`, `get_pending_orders_for_today`. New `PortfolioSnapshotRepository`. |
| `src/vibe_trade/broker/models.py` | New `OrderStatus` dataclass (status, filled_quantity, avg_fill_price, fill_time). |
| `src/vibe_trade/broker/ib_broker.py` | Add `get_order_status(ib_order_id)`. Simplify `place_market_order` — submit + return `order_id` immediately, no polling. Add `get_todays_orders()` for Bot 2. |
| `src/vibe_trade/strategy/*` | Force daily timeframe. Assert strategy evaluates against `df.iloc[-1]` (yesterday's closed bar). Exit-signal path (new). |
| `src/vibe_trade/scanner.py` | **Delete.** |
| `src/vibe_trade/jobs/__init__.py` | New. |
| `src/vibe_trade/jobs/submit.py` | `run_exits()`, `run_entries()`, `run_submit()` (calls both). |
| `src/vibe_trade/jobs/record.py` | `run_record()`. |
| `src/vibe_trade/jobs/reconcile.py` | `run_reconcile()` (statuses + snapshot). |
| `src/vibe_trade/cli.py` | Three new commands: `submit`, `record`, `reconcile`. |
| `config/config.example.toml` | New `[scheduler]` block (timezone only), remove obsolete keys. |
| `deploy/crontab` | **New.** Three cron lines for Linux production. |
| `docs/ARCHITECTURE_V2.md` | This file. |

---

## Test plan

### Session A tests — DB layer

- `Trade` schema migration: existing OPEN rows upgrade cleanly (nullable new columns)
- New repo methods tested individually (happy path + edge cases)
- `PortfolioSnapshot` insert/query
- Status transition guards (can't go FILLED → SUBMITTED, etc.) — optional, raise or silently allow? Pick one.

### Session B tests — Broker

- `place_market_order` returns immediately with `order_id` and `status="SUBMITTED"`; no wait loop
- `get_order_status` mapped correctly from `ib.trades()` (fake IB stub)
- `get_todays_orders` filters to today's date

### Session C tests — Submit job

- Exits phase: for each open position, exit signal evaluated; SELL submitted only when triggered
- Entries phase: held tickers skipped; BUY only on strategy signal
- Integration test: mock broker + fake yfinance → verify N orders submitted, zero DB writes

### Session D tests — Record + Reconcile

- Record: IB returns 3 orders → 3 SUBMITTED rows in DB
- Reconcile: SUBMITTED rows of various outcomes → correct status/price updates
- Portfolio snapshot: 5 positions → 5 snapshot rows + 1 daily_pnl row

### Session E tests — Strategy daily-close

- Strategies refuse non-daily timeframe
- No lookahead: `df.iloc[-1]` is yesterday, never today
- Exit-signal path returns SELL when trailing stop breached

---

## Sessions (implementation order)

Each session ends with a commit + all tests green + a clear exit criterion.

### Session A — DB foundation

**Exit criterion:** new schema migrated, 15+ new tests pass, no code outside `db/` changed yet.

- Update `Trade` columns per schema diff
- Add `PortfolioSnapshot` model
- Extend `DailyPnL`
- New repo methods with tests
- Blow away existing `data/vibe_trade.db` (user confirmed no real data in it — **confirm once more at start of session**)

### Session B — Broker

**Exit criterion:** can round-trip an order via a manual scratch script against IB paper: submit → record order_id → fetch status → see FILLED.

- `get_order_status`, `get_todays_orders`
- Simplify `place_market_order` (no polling)
- `OrderStatus` dataclass
- Unit tests with fake IB
- Manual verification script against paper account

### Session C — Submit job

**Exit criterion:** `vibe-trade submit` dry-run against paper account pre-market; orders visible in IB, nothing in DB yet.

- `jobs/submit.py`: `run_exits()`, `run_entries()`, `run_submit()`
- CLI command
- Strategy updates (daily timeframe, exit-signal path)
- Tests with mock broker

### Session D — Record + Reconcile jobs

**Exit criterion:** full end-to-end cycle runs against paper account:
1. 16:00: run submit → orders in IB
2. 16:35: run record → DB has SUBMITTED rows
3. 23:30: run reconcile → DB has FILLED/CANCELLED + snapshot

- `jobs/record.py`, `jobs/reconcile.py`
- CLI commands
- Integration tests
- `deploy/crontab` file

### Session E — Cleanup & polish

**Exit criterion:** V1 scanner deleted, config cleaned up, docs updated, TEST_REGISTRY.csv current.

- Delete `src/vibe_trade/scanner.py`
- Remove obsolete scheduler config keys
- Update `config.example.toml`
- Update `PROJECT_MAP.md` (new Mermaid flow diagram for V2)
- Final TEST_REGISTRY.csv pass

---

## Decisions locked in (do not re-litigate without cause)

| Question | Decision |
|---|---|
| Timezone | Asia/Jerusalem |
| Bot 1 split | Code split into two functions; CLI/cron stays as one command |
| Scheduler | OS-level. Linux prod = crontab; Windows dev = manual CLI invocations |
| Partial fills | Record as `PARTIALLY_FILLED`, no carry-over re-issue. Not expected to occur in practice. |
| DB wipe | OK to wipe `data/vibe_trade.db` at Session A start (user confirmed no real data) |
| Snapshot schema | Separate `portfolio_snapshot` table + extend `daily_pnl` |
| Data source for positions | IB at Bot 1 start (not DB) |
| Historical data | yfinance, daily bars only |
| Strategy evaluation point | Yesterday's closed daily bar (`df.iloc[-1]`), never intraday |

---

## Non-goals / deferred

- **Trailing stop recalculation mid-day.** V2 evaluates stops only at 16:00 against yesterday's low. No intraday monitoring.
- **Order modification.** V2 only places market orders; no limit order support, no amend/replace.
- **Live notifications during market hours.** Telegram only fires from the three scheduled jobs.
- **Backtesting integration.** Session Z or later.
- **Multi-account support.** Single IB account assumed.
- **Options / futures.** Stocks only.

---

## Rough sizing

| Session | Effort | Risk |
|---|---|---|
| A | 2–3 hours | Low — mechanical schema + repo work |
| B | 2 hours | Medium — IB API shape for `get_order_status` needs verification |
| C | 2–3 hours | Medium — strategy rewrite touches correctness |
| D | 2 hours | Low — mostly glue code |
| E | 1 hour | Low — cleanup |

Total: ~10 hours across ~5 sessions.
