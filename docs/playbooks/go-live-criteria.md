# Go-live criteria

> 🔴 **The August gate (item 2 below) is done, and it came back negative.**
> `donchian` at production settings (`0.018/50`, full point-in-time S&P 500,
> 2018–2026), median friction: **CAGR +4.00%, Sharpe 0.38, max DD -24.75%** —
> badly trailing SPY (+14.12% CAGR, Sharpe 0.78) and QQQ (+19.25%, Sharpe 0.85)
> buy-and-hold on every axis. This is the exact scenario the plan below calls
> out: *"If step 2 fails — donchian is not profitable net of friction —
> stop. Do not reset and run four months of a strategy the backtest
> rejects."* Full verdict in `PROJECT_MASTER_STATE.md` §7 and
> `backtests/c2_donchian_prod_median/`. **The user has not yet decided**
> whether to cancel/delay the Sept 1 reset, run a different strategy, or
> proceed anyway — this page's plan of record below is unchanged until they
> do. Don't act on it unilaterally; flag it and ask.

**Plan of record (set 2026-07-30, not yet revised for the result above).**
Reset the paper account **2026-09-01**, run clean through year end, decide on
`mode = "live"` in **January 2027**.

Sept 1 → Dec 31 is **four** months, not three — roughly 85 trading days.

Criteria are written down *now*, before any results exist, because thresholds
chosen after seeing the outcome are not thresholds. Numbers below are proposals —
override them, but override them in August, not in December.

---

## What four months can and cannot tell you

This is the most important thing on this page.

At the observed rate — 25 closed trades in three months with a 50-position cap and
a ~60-day average hold — four months yields roughly **35 closed trades**.

To distinguish a real edge from noise at 2 standard errors, using the actual
per-trade P&L distributions from the saved backtests:

| Strategy | mean P&L/trade | std dev | trades needed | at ~8.3/month |
|---|---|---|---|---|
| `sma` | $98.85 | $840.72 | **289** | 35 months |
| `ema` | $143.34 | $1,059.26 | **218** | 26 months |
| `macd` | $71.15 | $660.89 | **345** | 41 months |

**A four-month paper run delivers ~12% of the sample needed.** This is not a close
call to be argued about — it is off by an order of magnitude. Four profitable months
would be weak evidence; four flat months would be equally weak evidence. Neither
should move the decision much.

So split the question in two, and use the right instrument for each:

| Question | Instrument | Why |
|---|---|---|
| Does the strategy have an edge? | **Backtest** — 8 years, 1,300+ trades | Only thing with the sample size |
| Does the bot execute correctly and unattended? | **Paper run** | Answerable in weeks |
| What do frictions actually cost? | **Paper run** (`measure_slippage.py`) | Answerable in weeks — see below |

The paper run's job is **operational validation plus friction measurement**. The
backtest is the profitability instrument. Don't ask either to do the other's job.

## Why friction is measurable when profitability isn't

Same four months, same ~35 trades — but the signal-to-noise differs by ~50x:

```
edge:      mean ~$143/trade, sd ~$1,059   ratio 0.14  -> ~218 trades
slippage:  mean ~27 bps/leg, sd ~74 bps   ratio 0.36  -> a few dozen legs
```

You are measuring a **cost** (small variance relative to its mean) rather than an
**edge** (enormous variance relative to its mean). That's the whole difference.

### Already measured, from the 2026-05 → 07 run

`python scripts/measure_slippage.py <db> --since 2026-05-14` over 111 legs
(excluding the early-May manual scratch placements, which were placed at arbitrary
times of day and dragged the mean badly):

| | per leg | round trip |
|---|---|---|
| **median** | **26.6 bps** | **53.3 bps** ← use this |
| mean | 40.9 bps | 81.7 bps (stress case) |

The backtest currently models **zero**. First-order impact on per-trade gross
return, adding ~11 bps commission:

| Strategy | gross bps/trade | net @ median | net @ mean | edge eaten |
|---|---|---|---|---|
| `sma` | 201.9 | 137.6 | 109.2 | 32–46% |
| `ema` | 226.7 | 162.4 | 134.0 | 28–41% |
| `macd` | 95.4 | **31.1** | **2.7** | **67–97%** |
| `donchian` | **unmeasured** | ? | ? | ? |

**Two immediate consequences:**

1. **`macd` is probably not viable.** It holds ~16 days versus 47–62 for the others,
   so it pays friction 3–4x as often on a thinner gross edge. At the stress friction
   its edge is 2.7 bps — indistinguishable from zero. It is currently **enabled** in
   `config/config.toml`. Disable it unless a friction-aware backtest rescues it.
2. **`donchian` — the only strategy actually trading — has no backtest at all**, so
   its friction tolerance is unknown. Breakouts hold longer than MACD, which argues
   for tolerance, but that is an assumption, not a measurement.

---

## August: do this before the reset

The month before Sept 1 is not dead time. It is when the profitability question gets
answered, so that the paper run starts as *confirmation* of a validated strategy
rather than *discovery* of an unvalidated one.

| # | Task | Gate |
|---|---|---|
| 1 | ~~**Rotate the Telegram token**~~ | **DONE 2026-08-26** — new bot (`Vibe_trade_claude_bot`), new token wired into `deploy/.env`, verified end-to-end (`getMe` + a real `sendMessage` delivered). Old bot/token should be revoked in BotFather if not already. |
| 2 | ~~**Backtest `donchian` at `0.018 / 50`** with median/stress friction~~ | **DONE 2026-08-26** — point-in-time S&P 500 membership (not today's snapshot), full 2018-2026 range. Result: **fails this gate.** See the banner at the top of this page. |
| 3 | Same for `ema`; re-check `macd` | **Still open** — only `donchian` (the strategy actually trading) was rebuilt for C-2. `ema`/`macd` still have no production-settings, point-in-time, frictioned backtest. Moot unless the strategy pool changes. |
| 4 | ~~Add friction params to `backtest/engine.py`~~ | **DONE** — `Frictions` dataclass (slippage bps + IB commission model), `--friction {none,median,stress}` on `vibe-trade backtest`. |
| 5 | ~~Deploy `deploy/ibgateway/` + verify `preflight`~~ | **Partially done, differently than planned.** `deploy/ibgateway/` (Gateway keepalive via systemd+IBC) is still unexecuted — see its own README. What *is* done and live-verified: `deploy/systemd/*.timer` (job scheduling, `OnCalendar=... America/New_York` — fixes the DST drift that caused the 10 missed days, confirmed with `systemd-analyze calendar` and now installed+enabled on `jeki-MINIPC`) and OPS-1 (`preflight` pings healthchecks.io on every run, verified live). Together these close the *detection and scheduling* side of the 10-missed-days problem; Gateway still needing a human restart is the one piece left. |
| 6 | ~~Purge the 7 dead tickers from `SP500_SYMBOLS`~~ | **DONE** (commit `c6c8628`) — FISV, MRO, LUMN, DXC, ILMN, NWL, ENPH removed. |

**Per this page's own rule, step 2 failing means: stop.** Do not reset and run
four months of a strategy the backtest rejects. That decision has not been
made yet — see the banner at the top of this page.

---

## Gates for the January decision

### A. Operational — all must pass, these are the ones four months *can* answer

| Gate | Threshold | How |
|---|---|---|
| Missed job runs | **0** | `audit_drift.py` check 5 |
| DB/IB drift | **0** phantom rows, **0** duplicate OPEN symbols | `audit_drift.py` checks 1–2 |
| Ledger agreement | `sum(trades.pnl)` = `sum(daily_pnl.realized_pnl)` within commission | check 4 |
| P&L basis integrity | **0** rows whose `pnl` contradicts their own prices | check 3 |
| Gateway uptime | 0 unplanned outages that a human had to fix | `preflight` history |
| Unresolved `NEEDS_REVIEW` | **0** at year end | `vibe-trade review-trades` |

Any operational gate failing is a **hard no**. If the plumbing can't keep records
straight on paper, it will not do better with real money.

### B. Strategy — from the backtest, not the paper run

| Gate | Threshold | Result (2026-08-26) |
|---|---|---|
| Donchian backtested at production settings with measured friction | Done, artifact on disk | ✅ Done |
| Sharpe net of friction | **> 0.8** (SPY was 0.78 over the same window) | ❌ **0.38** (median), 0.29 (stress) |
| Max drawdown | **< 25%** | ❌ **-24.75%** (median), -25.41% (stress) — borderline-fails on its own, and does so while trailing both benchmarks |
| Net per-trade edge at the *stress* friction (82 bps) | **> 0** with margin | ⚠️ Positive but thin — CAGR +2.80% at stress, no real margin |
| Positive across sub-periods | Not carried by one year | Not separately checked — the aggregate result already fails without needing this |

**Gate B fails on every row that has a result.** This is what the banner at
the top of this page is flagging.

### C. Paper-run sanity — weak evidence, use as a veto only

| Gate | Threshold |
|---|---|
| Measured slippage | Within ~1.5x of the 53 bps assumption, i.e. **< 80 bps** round trip |
| Live win rate | Within ~15pp of backtested — a *gross* mismatch signals a modelling error |
| Realized vs expected fill prices | No systematic bias beyond measured slippage |
| Four-month P&L | **Not a gate.** ~35 trades cannot rule in or out. Only a catastrophic outlier (say worse than -15%) should veto |

Treat C as a smoke detector, not a scale. It catches "the model is wrong about
mechanics." It cannot catch "the edge is absent."

### D. Before flipping `mode = "live"`

| Item | Note |
|---|---|
| **2FA blocks unattended login** | Live + IBKR Mobile 2FA needs a phone approval per login. `deploy/ibgateway/` stops being unattended. Solve before, not after |
| Live-mode banner verified | Already implemented; confirm it fires |
| Position sizing re-checked | 1.8% × 50 = 90% invested, ~97% exposure, **no stop-loss and no drawdown circuit breaker** |
| Start smaller than the paper account | The paper account's equity is not your risk tolerance |
| `panic` tested against the live account | Once, deliberately, with one small position |

**On the missing stop-loss:** exits depend entirely on strategy signals. The worst
observed paper trade rode to **-25.6%** before its 20-day-low exit. That was
acceptable on paper. Decide explicitly whether it's acceptable with real money —
`strategy/base.py` is stateless (`evaluate(symbol, candles)`, no entry-price
awareness), so stops need new machinery and a design session, not a patch.

---

## During the run

Weekly, ~5 minutes:

```bash
python scripts/audit_drift.py data/vibe_trade.db          # expect CLEAN
vibe-trade review-trades                                  # expect none
```

Monthly:

```bash
python scripts/measure_slippage.py data/vibe_trade.db --since 2026-09-01
```

Watch the friction estimate tighten as legs accumulate. By November it should be
solid enough to re-run the backtest with a confident number — **that** re-run, not
the four-month P&L, is what the January decision should rest on.

Log each month's numbers here so December compares against what was written in
August rather than against memory:

| Month | Closed trades | `audit_drift` | Slippage (median rt) | Notes |
|---|---|---|---|---|
| Sept | | | | |
| Oct | | | | |
| Nov | | | | |
| Dec | | | | |
