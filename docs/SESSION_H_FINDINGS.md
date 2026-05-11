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

**Status:** Open. Will fix Saturday.

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

## ⚪ Observation #1 — Smoke test fills came from prior runs

**Found:** 2026-05-08 (Friday)
**Evidence:** Friday's `record` saw 16 fills for the day, but that same run's `submit` only placed 10 entries. The extra 6 (permIds 1221725839–1221725844) were from a separate manual run earlier that day.

**Why this matters:** `record` reads `ib.fills()` — *all* fills on the IB side for the day, irrespective of which process placed them. This is correct cross-process behavior (one of the V2 design invariants), but it means smoke-test counts can look inconsistent if you re-run during the same day.

**Action:** None. Working as designed. Note in the runbook so future-you doesn't waste time investigating.

---

## ⚪ Observation #2 — Telegram bot token committed to chat history

**Found:** 2026-05-08 (setup)
**Evidence:** User pasted `.env` contents containing live token `8656886469:...` directly in conversation.

**Status:** User has acknowledged. Will revoke and rotate at end of Session H.

---

## 📋 Saturday triage checklist

When we fix Saturday, do them in this order to minimize re-test churn:

1. **Bug #1** (`PreSubmitted` as success) — biggest functional impact, needs a unit test
2. **Hygiene #2** (commit `TZ` to compose + Dockerfile) — re-deploy fixes config drift between repo and prod
3. **Hygiene #1** (universe refresh + dot-to-dash + yfinance retry) — quality of life
4. **Hygiene #3** (config-check default path) — UX polish, low priority
5. Bump tests in `tests/TEST_REGISTRY.csv`, update `PROJECT_MASTER_STATE.md` test count

Re-deploy on Linux box: `git pull && cd deploy && docker compose build`.

Then two more validation days (Mon–Tue of week 2) to confirm everything is clean before declaring Session H done.

---

## Notes added after each cron day

> Append new findings below as the week progresses.

### 2026-05-11 (Monday)
- Bug #1 surfaced (see above)
- 9 entry signals: BA, BLK, CBOE, EXPD, FTNT, GOOGL, HPE, NTAP, NWS
- All 9 orders confirmed at IB Gateway, expected to fill at 16:30 open
- Awaiting 23:30 reconcile to confirm fills are recorded

### 2026-05-12 (Tuesday)
_pending_

### 2026-05-13 (Wednesday)
_pending_

### 2026-05-14 (Thursday)
_pending_

### 2026-05-15 (Friday)
_pending_
