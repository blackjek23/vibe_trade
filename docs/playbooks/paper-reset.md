# Paper reset — clean-slate run

Wipes local trade history and flattens the paper account so the next run starts
from zero. Use this to validate the whole pipeline end to end without three months
of accumulated drift in the way.

**Takes about 15 minutes**, plus one overnight wait if you reset IB's cash balance.

---

## Read this first

> **The plan of record is [go-live-criteria.md](go-live-criteria.md)**: reset
> 2026-09-01, run to year end, decide in January. Read it before resetting — it sets
> the August prerequisites, and one of them can cancel the reset.

**A clean paper run proves the plumbing, not the edge.** It will tell you that
submit places orders, record captures fills, reconcile closes rows, and the audit
comes back clean. It will *not* tell you the strategy makes money — that is what a
backtest is for, and the backtest verdict is currently **withdrawn**:

- production runs `donchian`, which has **never been backtested**
- the four saved runs used `pct=0.04 / 25-cap`; production is `0.018 / 50`
- the engine models **zero** commission and slippage

So: reset and run, by all means. But treat "the pipeline is clean for two weeks" as
a *plumbing* result. Going to `mode = "live"` is a separate decision that should
wait on a donchian backtest at production settings with frictions.

**What this procedure does and does not touch:**

| | |
|---|---|
| Wipes the SQLite DB (trades, snapshots, daily P&L) | yes, after archiving |
| Closes open positions at IB | yes, step 3 — a separate explicit command |
| Resets IB paper cash balance | optional, step 4, manual via Client Portal |
| Touches live-mode anything | **no** — the wipe script refuses unless `mode = "paper"` |
| Deletes your archive | no — kept in `backups/`, plus the `.wiped` file |

---

## 1. Archive and see what you'd destroy

Dry run is the default, so this is safe:

```bash
python scripts/reset_paper_db.py --config config/config.toml
```

```
mode:    paper
Will DESTROY 2,547 row(s) across 5 table(s):
  trades                    112
  daily_pnl                  50
  portfolio_snapshot      2,385
DRY RUN - nothing changed. Re-run with --yes to archive and wipe.
```

If `mode` prints anything other than `paper`, **stop** — you're pointed at the
wrong config. The script will refuse anyway.

> Keep the archive. It is the only record of the 2026-05 → 2026-07 run, and it's
> what the four bugs found in that period were diagnosed from. `scripts/` already
> holds one export with a checksum; the wipe adds another to `backups/`.

## 2. Snapshot the prod DB too (if resetting the prod box)

The DB lives in a Docker volume there, not on the filesystem:

```bash
cd /opt/vibe-trade/deploy && ../scripts/export_db.sh
```

## 3. Flatten the account

```bash
vibe-trade cancel-pending                 # list working orders first
vibe-trade panic --yes                    # close every position, market orders
```

`panic` cancels working orders then market-sells everything. It writes nothing to
the DB by design.

**Do this while the US market is open** (16:30–23:00 local). Market orders sent
outside RTH sit as `PreSubmitted` until the next open, which leaves you in a
half-flat state that's confusing to verify.

Confirm you're actually flat:

```bash
vibe-trade status          # expect: No open positions
```

## 4. Reset the IB paper cash balance — optional

Only if you want round starting equity rather than whatever the account drifted to.

IB Client Portal → **Settings → Account Settings → Paper Trading Account Reset**.

Two caveats worth knowing before you rely on it:

- IB usually processes paper resets **overnight**, not instantly. Don't schedule
  your first clean run for the same day.
- The reset also clears positions, which makes step 3 redundant — but step 3 is
  immediate and uses code you control, so do it anyway and treat the portal reset
  as cash-only housekeeping.

Verify afterwards rather than assuming:

```bash
vibe-trade preflight       # shows net_liq and held count
```

## 5. Wipe the DB

```bash
python scripts/reset_paper_db.py --config config/config.toml --yes
```

Archives (online `.backup` + `.sha256`, integrity-checked, row counts compared
against source), then moves the old file to `*.wiped` rather than deleting it. It
refuses to wipe if the archive doesn't verify.

On the prod box the DB is inside the Docker volume, so run it in a container:

```bash
cd /opt/vibe-trade/deploy
docker compose run --rm --entrypoint python submit \
    scripts/reset_paper_db.py --config /config/config.toml --yes
```

## 6. Verify the clean state

```bash
python -m vibe_trade status --config config/config.toml    # recreates empty schema
python scripts/verify_db.py data/vibe_trade.db             # 5 tables, 0 rows, integrity ok
python scripts/audit_drift.py data/vibe_trade.db           # expect: CLEAN, exit 0
```

All three must pass before the first run. `audit_drift.py` reports `CLEAN` on an
empty DB — if it says anything else, the wipe didn't finish.

---

## First clean day

Run the jobs in order and check after each. Manually first — don't let cron do it
until one manual cycle has worked.

```bash
vibe-trade preflight     # 15:50 — expect READY
vibe-trade submit        # 16:00 — expect N entries placed, 0 exits (nothing held)
vibe-trade record        # 16:35 — expect N BUYs recorded; MUST be after 16:30
vibe-trade reconcile     # 23:30 — expect N opened, snapshot rows = N
```

Then the check that matters:

```bash
python scripts/audit_drift.py data/vibe_trade.db
```

**Expect `CLEAN`.** Day one is the easy case — no exits, no re-buys, so nothing
can drift yet.

### What to actually watch for, and when

| Day | What becomes possible | Green means |
|---|---|---|
| 1 | First entries | `audit_drift` CLEAN, snapshot rows = positions held |
| 2+ | First exits → the orphan-SELL path | `orphan_sells_recovered` fires if record is missed, and rows reach `CLOSED` not stuck `OPEN` |
| any | A re-bought symbol | **no duplicate `OPEN` rows** — this is the bug that produced 14 duplicated symbols |
| ~2 weeks | Enough history to trust reports | `audit_drift` CLEAN every day; `sum(trades.pnl)` matches `sum(daily_pnl.realized_pnl)` |

Run `audit_drift.py` daily for the first two weeks. It exits non-zero when dirty,
so cron it if you like:

```cron
40 23 * * 1-5  cd /opt/vibe-trade && python scripts/audit_drift.py data/vibe_trade.db >> deploy/logs/audit.log 2>&1
```

A single dirty day in the first fortnight is worth stopping for — that's the window
where the drift fixes are unproven against real fills.

---

## Rolling back

The old DB is still there:

```bash
mv data/vibe_trade.db.wiped data/vibe_trade.db
```

Or from the archive, verifying the checksum first:

```bash
sha256sum -c backups/vibe_trade-prewipe-<stamp>.db.sha256
cp backups/vibe_trade-prewipe-<stamp>.db data/vibe_trade.db
```

Restoring records does **not** restore positions. If you already ran `panic`,
those closes really happened at IB and the restored DB will disagree with the
account — run `vibe-trade reconcile`, then `audit_drift.py`, and expect to resolve
the difference via [data-recovery.md](data-recovery.md).
