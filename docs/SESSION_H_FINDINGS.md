# Session H — Live Paper Findings

> **Status:** Open. Live paper week in progress.
> **Plan:** accumulate findings Mon–Fri → fix them Saturday → re-run two more days of paper to validate fixes.
>
> **Severity scale:**
> - 🔴 **Blocker** — incorrect behavior, wrong P&L, missed fills, data corruption
> - 🟠 **Bug** — misleading output / wrong status reporting, but no real economic impact
> - 🟡 **Hygiene** — noisy logs, stale config, UX papercut
> - ⚪ **Observation** — neutral note for future reference

---

## 🟠 Bug #1 — `PreSubmitted` is treated as a placement error

**Found:** 2026-05-11 (Monday) at 16:00 cron run
**Evidence:** [logs/cron.log line 332–344 from that run]

```
│ Entries │       460 │        9 │      0 │      9 │
9 error(s):
  - entry BA:    order status=PreSubmitted err=None
  - entry BLK:   order status=PreSubmitted err=None
  - entry CBOE:  order status=PreSubmitted err=None
  - entry EXPD:  order status=PreSubmitted err=None
  - entry FTNT:  order status=PreSubmitted err=None
  - entry GOOGL: order status=PreSubmitted err=None
  - entry HPE:   order status=PreSubmitted err=None
  - entry NTAP:  order status=PreSubmitted err=None
  - entry NWS:   order status=PreSubmitted err=None
```

**Root cause:** All 9 orders reached IB successfully (each got a permId — BA=497123114, …, NWS=497123122). IB assigned them `PreSubmitted` status because the cron job fires at 16:00 IDT, 30 minutes before US market open (16:30 IDT). `PreSubmitted` is the correct IB state for a market order queued pre-RTH — it will fill automatically at the open.

The bot's `place_order` confirmation logic in `src/vibe_trade/broker/ib_broker.py` treats anything not in `{Submitted, Filled, PartiallyFilled}` as a failure. `PreSubmitted` should also be a success state.

**Economic impact:** None. The orders are at IB and filled normally at 16:30. Reconcile at 23:30 picked them up correctly.

**Telegram impact:** Misleading. The submit notification said "0 placed, 9 failed" when in reality 9 orders were live at IB.

**Suggested fix:**
- Treat `PreSubmitted` as success in `place_order` confirmation.
- Acceptable terminal-success states: `{Submitted, PreSubmitted, Filled, PartiallyFilled}`.
- The poll loop can still wait briefly for `PendingSubmit → PreSubmitted` transition, but should not block waiting for `PreSubmitted → Submitted` (that only happens at market open).
- Add a test in `tests/test_submit.py` that mocks IB returning `PreSubmitted` and asserts `placed=1, failed=0`.

**Status:** ✅ Fixed Saturday 2026-05-16 — `PLACEMENT_SUCCESS_STATUSES` frozenset in submit.py now includes `PreSubmitted`; 3 tests in test_submit.py::TestPlacementStatuses.

---

## 🟡 Hygiene #1 — yfinance failures for stale/renamed tickers

**Found:** 2026-05-08 (smoke test), persists in 2026-05-11 (cron run)
**Evidence:** ~30 tickers fail per run across `logs/vibe_trade.log` ERROR lines.

| Category | Tickers |
|---|---|
| Truly delisted | ATVI, FRC, SIVB, FBHS, CDAY, CTLT, DFS, PARA, PEAK, WRK, PXD, FLT, PKI, RE, DISH, ANSS (deal-closed) |
| Stale symbol format | BRK.B → `BRK-B`, BF.B → `BF-B` |
| Listed but yfinance flaky | NEE (curl timeout), HES, JNPR, K, MMC, MRO, IPG, CMA, WBA |

**Impact:** ~6% of the universe is silently un-tradable each run. Bot logs the warning, skips, and moves on — no crash, no bad data. But it's persistent log noise and means missed signal opportunities for misnamed tickers.

**Suggested fix:**
1. Refresh the static SP500 universe list (`src/vibe_trade/data/sp500.py` or wherever) — remove confirmed delistings.
2. Normalize `.` → `-` for tickers yfinance expects with hyphens (BRK-B, BF-B).
3. Add a single retry with 1–2 sec backoff for transient yfinance failures (curl timeout, 503).
4. Bonus: have submit log a one-line "skipped N tickers due to data unavailability" summary at the end.

**Status:** Open. Will fix Saturday.

---

## 🟡 Hygiene #2 — Container defaulted to UTC instead of Asia/Jerusalem

**Found:** 2026-05-08 (Friday) during smoke-test review
**Evidence:** Log prefix vs IB fill UTC timestamps were inconsistent — log read as UTC, not IDT.

**Root cause:** `deploy/Dockerfile` does not set `TZ`, and `deploy/docker-compose.yml` did not pass `TZ` env. Docker containers default to UTC. Cron at host runs in host TZ, so if host is also UTC the schedule fires 3 hours late (16:00 UTC = 19:00 IDT = after market open instead of before).

**Mitigation already applied:** User edited `docker-compose.yml` to add `environment: - TZ=Asia/Jerusalem` to all three services on 2026-05-10. Verified container `date` now prints IDT.

**Suggested fix:**
- Commit the `TZ=Asia/Jerusalem` change to `deploy/docker-compose.yml`.
- Update `deploy/Dockerfile` to install `tzdata` and set `ENV TZ=Asia/Jerusalem` as a default (belt + suspenders).
- Update `deploy/README.md` and `docs/SESSION_H_LINUX_RUNBOOK.md` to mention this explicitly.

**Status:** Patched on the box. Repo not yet updated.

---

## 🟡 Hygiene #3 — `config-check` silently uses defaults when invoked via `docker compose run`

**Found:** 2026-05-08 (config setup)
**Evidence:** `docker compose run --rm submit config-check` reported `port=7497, telegram=disabled` even after the user edited `deploy/config/config.toml` to set `port=4002, telegram=true`.

**Root cause:** Compose service's command is `["submit", "--config", "/config/config.toml"]`. When you override the entire command with `docker compose run --rm submit config-check`, the `--config` flag is dropped. `config-check` then falls back to its default path (`config/config.toml` relative to `/app` CWD), which doesn't exist → loads pydantic defaults.

**Workaround:** explicitly pass `--config /config/config.toml`:
```bash
docker compose run --rm submit config-check --config /config/config.toml
```

**Suggested fix:** make `config-check` default to `/config/config.toml` when running inside the container (e.g. via env var `VIBE_TRADE_CONFIG`), so the override-friendly form just works. Or document the workaround prominently in the runbook.

**Status:** Open. Annoying UX, no data risk.

---

## 🟡 Hygiene #4 — Submit takes ~5 minutes scanning the universe

**Found:** every day this week.
**Evidence:** submit start at 16:00:06 → submit done at ~16:05:30 every cron day.

**Root cause:** sequential yfinance fetch over ~494 tickers, ~600ms per ticker (network round-trip + yfinance overhead).

**Why this matters:**
- Orders are placed throughout the 16:00–16:05 window, not all at 16:00.
- The last few orders (~16:04) only have 21 minutes before record runs at 16:25 → much narrower window for them to fill, exacerbating Bug #5.
- If yfinance slows further (NEE timeout was 10s on Mon), submit could exceed 16:25.

**Suggested fix:** parallel yfinance fetches with bounded concurrency (e.g. 10 workers via `asyncio.gather` + semaphore). Should cut universe scan from ~5min to ~30sec.

**Status:** 🟡 Not blocking, but related to Bug #5. Lower priority than the orphan-fill fix.

---

## ⚪ Observation #3 — Paper account fill timing is inconsistent

**Found:** comparing Mon/Tue (0 fills by 16:25) vs Thu/Fri (all fills by 16:25)
**Hypothesis:** IB paper simulator's fill timing for market orders placed pre-RTH varies. Some days it fills near-instantly against last quote; other days it waits for actual market open.

**Why this matters:** confirms that Bug #5 (orphan fills) isn't a Mon/Tue fluke — any day with slower paper fills will leak fills out of the DB unless we fix reconcile.

**Action:** None for the bug itself (already covered by Bug #5). But worth noting that **in live trading**, fill timing is determined by real exchange behavior — market orders placed pre-open will fill at or just after 16:30 IDT every day. So the orphan-fill rate in live will be 100%, not 50%. **Bug #5 is even more urgent for live trading.**

---

## ⚪ Observation #4 — Paper account positions wiped between Wed close and Thu open

**Found:** 2026-05-13 23:30 reconcile = 61 positions, 2026-05-14 16:00 submit = 0 held.
**Hypothesis:** Either:
1. IB paper-side scheduled reset (some IB paper environments reset weekly)
2. Side-effect of Wed's Gateway outage
3. Manual close by user

**Action:** None — not a bot bug. Note it so we know that the Thu numbers aren't a continuation of the Mon–Wed state.

---

## ⚪ Observation #1 — Smoke test fills came from prior runs

**Found:** 2026-05-08 (Friday)
**Evidence:** Friday's `record` saw 16 fills for the day, but that same run's `submit` only placed 10 entries. The extra 6 (permIds 1221725839–1221725844) were from a separate manual run earlier that day.

**Why this matters:** `record` reads `ib.fills()` — *all* fills on the IB side for the day, irrespective of which process placed them. This is correct cross-process behavior (one of the V2 design invariants), but it means smoke-test counts can look inconsistent if you re-run during the same day.

**Action:** None. Working as designed. Note in the runbook so future-you doesn't waste time investigating.

---

## 🔴 Bug #5 — Late-fill edge case: DB loses fills that land between 16:25 and 23:30

**Found:** 2026-05-11 (Mon) and 2026-05-12 (Tue) — confirmed two days in a row.
**Evidence:**

| Day | 16:25 record `fills seen` | 23:30 reconcile `ib_fills` | reconcile `opened` |
|---|---|---|---|
| Mon | 0 | 9 | **0** |
| Tue | 0 | 15 | **0** |
| Thu | 34 | 34 | 34 ✓ |
| Fri | 11 | 11 | 11 ✓ |

**Root cause:** This is the documented Phase 4 edge case (PROJECT_MASTER_STATE.md § 7, ROADMAP Phase 4):

> A market order placed at 16:00 that fills *after* 16:25 — record misses it. Reconcile should auto-create the SUBMITTED row. Not yet implemented.

When market orders sit `PreSubmitted` past 16:25:
- `record` reads `ib.fills()` → sees 0 fills → inserts 0 SUBMITTED rows
- `reconcile` reads `ib.fills()` → sees N fills, queries DB for "pending rows" (SUBMITTED or PENDING_CLOSE), finds none, so cannot transition anything
- IB has the positions; DB does not. Permanent drift unless we backfill.

**Why Mon/Tue but not Thu/Fri:** IB paper simulator fill timing is inconsistent (see Observation #3). On Thu/Fri orders apparently filled by 16:25.

**Economic impact:** Severe in production-grade environments. Paper week's drift is 24 positions worth ~$40k. All future records/reconciles will be unaware of these positions:
- Exits won't trigger via Donchian (the strategy doesn't know the position exists in DB)
- Sizing assumes 50 - DB.held; if DB shows 0 but IB shows 24, bot would happily place 50 more entries → over-trade.
- Performance reports will undercount realized P&L.

**Suggested fix:** Modify `reconcile` to handle "orphan fills" — fills present in `ib.fills()` with no matching SUBMITTED row in DB:

```python
for fill in ib_fills:
    if fill.permId not in db_pending_perm_ids:
        # Late fill — insert a backfill row in one step
        trade_repo.create_trade(
            symbol=fill.symbol, side="BUY",
            quantity=fill.cumQty, avg_price=fill.avgPrice,
            perm_id=fill.permId, status="OPEN",  # straight to OPEN
            strategy_name="donchian",  # or fill.execution.orderRef
            created_at=fill.execution.time,
        )
```

**Backfill required for Mon+Tue:** before resuming, we need a one-shot script that scans last week's fills and inserts the missing 24 rows. Otherwise the 50-position cap arithmetic is wrong tomorrow.

**Status:** ✅ Fixed Saturday 2026-05-16 — see reconcile.py orphan-fill loop + `create_filled_buy_from_fill()` in repository.py. 4 tests in test_reconcile.py::TestOrphanFills.

---

## 🔴 Bug #6 — Cron crash on Gateway disconnect, no Telegram notification

**Found:** 2026-05-13 (Wednesday)
**Evidence:** [cron.log lines 1285–1503]

```
2026-05-13 16:00:05,133 | ERROR | ib_async.client | API connection failed: ConnectionRefusedError(111)
... 4 retries with exponential backoff ...
2026-05-13 16:00:19,153 | ERROR | vibe_trade.broker.ib_broker | Failed to connect to IB after 4 attempts
╭───────── Traceback ──────────╮
│ ... cli.py:180 in submit     │
│ ❱ 180 asyncio.run(...)       │
... uncaught exception, process exits with non-zero code ...
```

**What went wrong:**
1. Gateway was unreachable at 16:00 — could be IB-side outage, user's machine reboot, Gateway login session expired, etc.
2. Bot retried 4 times (good — connect_retries logic worked).
3. After exhausting retries, raised exception **all the way to top-level**, crashing the process.
4. The Telegram notifier is constructed *inside* `_run_submit_cli` — it never got constructed because the exception preceded notifier setup. **Result: no notification on the most important failure mode.**

**User noticed manually** ~1 hour later and reran `submit` at 17:04 after restarting Gateway. That was operationally good but lucky.

**Suggested fix:**
1. **Wrap each job's entrypoint** (`submit`, `record`, `reconcile`) in a top-level try/except that:
   - Sends a `[CRITICAL]` Telegram message via a *bootstrap* notifier (constructed from env vars before any other code runs)
   - Logs the traceback to file
   - Exits non-zero so cron's MAILTO (if configured) also fires
2. **Add a "heartbeat" check** at top of each job: if Gateway not reachable, send Telegram alert immediately rather than after 4 retries.
3. **Consider auto-recovery:** if submit fails at 16:00 but Gateway comes back by 16:25, record could detect the gap and either:
   - Skip (current behavior, safe)
   - Run a "catch-up" submit pass first (risky; deferred)

**Status:** ✅ Fixed Saturday 2026-05-16 — `_run_with_crash_alert` wrapper in cli.py; 5 tests in test_cli_crash_alert.py.

---

## ⚪ Observation #2 — Telegram bot token committed to chat history

**Found:** 2026-05-08 (setup)
**Evidence:** User pasted `.env` contents containing live token `8656886469:...` directly in conversation.

**Status:** User has acknowledged. Will revoke and rotate at end of Session H.

---

## 🟢 Enhancement #1 — Force-trim positions when over `max_open_positions`

**Requested:** 2026-05-11 by user
**Current behavior:** `risk/manager.py:can_open_new_position` — if `held >= max_open_positions`, the entries phase is skipped entirely. Existing surplus positions are left untouched until they hit a normal Donchian exit signal.

**Requested behavior:** in `submit`, after the regular Donchian exits but before entries, if `held > max_open_positions`, force-sell the **N lowest-performing positions** until `held == max_open_positions`.

Example: held = 60, max = 50 → force-sell the 10 worst performers.

**Why this matters:** without it, if the user lowers the cap mid-cycle, or adds positions manually, or after a config change, the bot can sit above the cap indefinitely. This makes the cap an *active rebalance target* instead of a *passive gate*.

**Design decisions (locked 2026-05-11 by user):**

1. **"Lowest-performing" = lowest unrealized P&L in dollars.** Most negative `unrealizedPNL` from `ib.portfolio()` goes first. Not return %, not time-weighted. Simple, direct.

2. **Flow:** Donchian exits → **force-trim if over cap** → entries.
   - User note: when force-trim runs, the entries phase will see `held == max` and the existing `can_open_new_position` check skips entries naturally. So in practice, an over-cap day = no new entries. That's the intended outcome; no special handling needed.

3. **Tag trim sells with `Order.orderRef = "trim"`.** Distinguishes them from Donchian exits in record/analytics later.

4. **Edge cases — keep simple, no warnings/alerts:**
   - If after Donchian exits we're already at/below cap, skip force-trim.
   - No Telegram `[WARNING]` for severe over-cap. Just trim to `max` and move on.

5. **Telegram messaging:** keep simple for now. Submit summary may report the trim count combined with Donchian exits; refine later if it becomes confusing.

6. **Backtest parity:** apply same logic to `backtest/engine.py` so backtest stays a faithful preview of production.

7. **Tests to add Saturday:**
   - `tests/test_submit.py::test_force_trim_lowest_dollar_pnl` — held=60 positions with varied unrealized $ P&L, max=50 → exactly 10 trim sells, on the 10 with most-negative `unrealizedPNL`.
   - `tests/test_submit.py::test_force_trim_at_cap_no_op` — held=50, max=50 → 0 trim signals.
   - `tests/test_submit.py::test_force_trim_after_donchian_exits_brings_under_cap` — held=55, max=50, Donchian signals 5 exits → 0 additional trim signals.
   - `tests/test_submit.py::test_force_trim_tags_orderref` — verify the SELL orders carry `orderRef="trim"`.
   - `tests/test_backtest_engine.py::test_force_trim_matches_production` — same scenario in backtest, same result.
   - One mocked-IB integration in `tests/test_submit.py` with a 60-position portfolio.

**Status:** ✅ Implemented Saturday 2026-05-16 — `RiskManager.select_force_trim_candidates` + force-trim phase in `submit.py` + backtest parity helper. 11 tests across test_risk_manager.py, test_submit.py, test_backtest_engine.py.

---

## 📋 Saturday triage checklist (re-ordered after Fri 2026-05-15 review)

### Tier 1 — blockers, must fix before resuming paper

1. **Bug #5** — reconcile auto-creates rows for orphan fills (DB ↔ IB drift)
   - Plus one-shot backfill script to recover Mon/Tue's 24 missed fills
   - Tests: orphan-fill insertion, idempotent re-reconcile, backfill script
2. **Bug #6** — crash-resistant job entrypoints + Telegram alert on Gateway down
   - Wrap each job in top-level try/except with bootstrap notifier
   - Test: simulate ConnectionRefusedError → assert Telegram fire + non-zero exit
3. **Bug #1** — `PreSubmitted` accepted as success
   - Smaller code change but related: also clean up the misleading "N failed" reporting

### Tier 2 — Enhancement #1 (force-trim over-cap)

4. **Force-trim implementation** in submit + backtest, with the 5 unit tests already speced
   - More urgent now: backfilling 24 fills (Bug #5 recovery) likely pushes us over 50-cap
     immediately on the first submit run post-fix, exercising the force-trim path on Day 1

### Tier 3 — hygiene + DX (do if time permits Saturday)

5. **Hygiene #4** — parallel yfinance fetches (cut universe scan ~5min → ~30s)
   - Important because Bug #5 fix doesn't fully solve "last orders fill late"
6. **Hygiene #2** — commit `TZ=Asia/Jerusalem` to compose + Dockerfile
7. **Hygiene #1** — universe refresh + `.` → `-` + yfinance retry
8. **Hygiene #3** — `config-check` default path

### Tier 4 — bookkeeping

9. Update `tests/TEST_REGISTRY.csv` (expect +10 to +15 new test rows)
10. Update `PROJECT_MASTER_STATE.md` Section 2 (Session H done with date range)
11. Update `PROJECT_MASTER_STATE.md` Section 7 to point to next session

### Re-deploy + re-validate

After commits land on `main`:
```bash
# On Linux box
cd /opt/vibe-trade && git pull
cd deploy && docker compose build
# Backfill Mon/Tue missing rows (Tier 1 #1 will define the exact command):
docker compose run --rm submit backfill-fills --start 2026-05-11 --end 2026-05-12
# Verify DB now reflects IB:
docker compose run --rm submit status
```

Then two more validation days (Mon 2026-05-18, Tue 2026-05-19) with the fixes in place.
Expected outcome: 0 orphan fills, 0 silent crashes, all fills tracked, force-trim engages
naturally if the backfill pushes us over 50 positions.

---

## Notes added after each cron day

> Append new findings below as the week progresses.

### 2026-05-11 (Monday)
- **Bug #1** surfaced (9 entries reported PreSubmitted/failed). All 9 reached IB.
- 16:25 record: **0 fills seen**.
- 23:30 reconcile: `ib_fills=9` but `opened=0` — the 9 fills happened between 16:25 and 23:30, after record had already run. **Bug #5 (late-fill) confirmed in production.**
- DB now missing 9 OPEN positions that exist in IB.

### 2026-05-12 (Tuesday)
- Same pattern as Monday. 15 entries (AIZ, BIIB, CNC, DD, ENPH, FOX, FOXA, GLW, IVZ, NVDA, +5 more) all PreSubmitted.
- 16:25 record: **0 fills seen** again.
- 23:30 reconcile: `ib_fills=15`, `opened=0`. **Another 15 fills missed by DB.**
- Running drift after Tuesday: 24 IB positions not tracked in DB.

### 2026-05-13 (Wednesday) — IB Gateway outage
- 16:00 submit: **`ConnectionRefusedError(111)` × 4 retries → traceback → job crashed.**
- 16:25 record: same failure mode → crashed.
- **No Telegram notification was sent** because the bot died before the notification step.
- ~17:04 manual re-run of submit by user after fixing Gateway: `61 held, 0 signals, 0 placed`.
- 23:30 reconcile ran successfully: `ib_trades=61 ib_fills=61, opened=0`. (No SUBMITTED rows to open — DB still missing Mon/Tue fills.)
- **Bug #6 (no notification on cron failure)** confirmed.

### 2026-05-14 (Thursday) — paper account wiped overnight
- 16:00 submit: `0 held` (Wed end had 61 positions; Thu morning IB shows 0). **Paper account was reset between Wed close and Thu morning** — not a bot bug, likely IB paper-side cleanup after Wed's outage or routine weekly reset.
- 34 entry signals placed, all PreSubmitted.
- 16:25 record: **`fills seen = 34`** (this time all orders filled before record ran).
- 23:30 reconcile: **`opened=34`** ✓ — DB synced.
- Why fills hit by 16:25 here but not Mon/Tue: paper simulator fill timing is inconsistent. See Observation #3.

### 2026-05-15 (Friday)
- 16:00 submit: 34 held → 11 entries (AES, AIZ, AVGO, BLK, FFIV, GNRC, GWW, LUMN, PM, TTWO, WMB), all PreSubmitted.
- 16:25 record: `fills seen = 11` ✓.
- 23:30 reconcile: `opened=11` ✓ — DB synced for Fri.

### Week totals
- Signals generated: 69
- Properly tracked in DB: 45 (Thu + Fri)
- **Missing from DB but exist in IB ledger: 24 (Mon + Tue)**
- Cron crashes: 2 (Wed submit + record)
- Notification failures: 2 (Wed crashes)
- Paper account reset events: 1 (Wed→Thu)
