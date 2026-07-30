# Data recovery — when the DB and IB disagree

The DB is a **mirror** of IB, never the source of truth. Submit reads positions from
IB, so trading stays correct even when the mirror is wrong — but every report,
weekly dashboard and P&L number comes from the DB, so drift makes your *analytics*
lie while your *trading* carries on fine.

That distinction sets the urgency: drift is not an emergency, but don't trust a
report until `audit_drift.py` is clean.

---

## Start here

```bash
python scripts/audit_drift.py data/vibe_trade.db
```

Five checks, exit 1 if any fail:

| # | Check | Means |
|---|---|---|
| 1 | OPEN rows the latest snapshot doesn't back | **phantom positions** — DB thinks you hold something IB doesn't |
| 2 | Symbols with >1 OPEN row | **duplicate lots** — a phantom got re-bought |
| 3 | CLOSED rows whose `pnl` contradicts their own prices | **basis mismatch** — IB's average cost mixed with a per-order row |
| 4 | `sum(trades.pnl)` vs `sum(daily_pnl.realized_pnl)` | two ledgers disagreeing |
| 5 | Weekdays with no `daily_pnl` row | **missed job runs** |

## How drift happens

One mechanism produces 1–4, and it's worth understanding because the symptoms look
unrelated:

1. Submit places a SELL at 16:00.
2. `record` doesn't run, or runs before the fill exists — the row stays `OPEN` and
   never gets an `exit_perm_id`.
3. The SELL fills at IB. `reconcile` sees a fill whose permId matches no DB row.
4. **Before 2026-07-30** that orphan SELL was counted and discarded. The row stayed
   `OPEN` forever while IB held nothing.
5. The next breakout on that symbol re-buys it → a *second* `OPEN` row.
6. IB now reports realized P&L against its own average cost across both lots, which
   no longer matches either row's `entry_price` → check 3 fires.

Two fixes closed this:

- **orphan-SELL recovery** — an untracked SELL fill is matched to an `OPEN` row by
  symbol (FIFO) and closed while the fill is still in `ib.fills()`. Only the current
  session is returned, so this must happen the same day or the exit is gone.
- **`_sweep_open_rows`** — any `OPEN` row IB doesn't back becomes `NEEDS_REVIEW`
  rather than silently persisting.

`record` remains a fast path, not the only path: reconcile at 23:30 back-fills both
orphan BUYs and orphan SELLs.

---

## Resolving `NEEDS_REVIEW` rows

Reconcile parks a row here when IB doesn't hold the symbol *and* the exit fill is no
longer in `ib.fills()`. It refuses to invent an exit price — that's your call.

```bash
vibe-trade review-trades
```

```
| id | Symbol | Qty |  Entry | Entered    | Why                            |
| 27 | TSLA   |   3 | 429.71 | 2026-05-08 | OPEN row not held at IB        |
```

Find the real exit price in **IB Client Portal → Reports → Trade Confirmations** (or
Activity Statement) for that symbol, then:

```bash
vibe-trade review-trades --resolve 27 --exit-price 415.50
```

P&L is computed from the row's own entry and exit, so the result always reconciles
against the numbers stored beside it.

If the position was never really yours — a mis-recorded row, not a real trade:

```bash
vibe-trade review-trades --write-off 27      # -> CANCELLED
```

Prefer `--exit-price` whenever you can find the fill. A write-off silently removes
the trade from P&L, so an unrecoverable loss just disappears from your statistics.

## Duplicate OPEN rows

Reported but never auto-fixed — a share-count mismatch can also be a legitimate
`PARTIALLY_FILLED` sibling or a manual trade, and guessing would be worse than
reporting. Resolve by hand: keep the row matching IB's current lot, and
`--resolve`/`--write-off` the other.

## Missed job runs

Check 5 counts weekdays with no `daily_pnl` row. Confirm whether orders were
actually placed on those dates before assuming damage:

```sql
SELECT COUNT(*) FROM trades WHERE date(submitted_at) = '2026-07-21';
```

Zero BUYs *and* zero SELLs means nothing was placed — the day is benign, costing
only opportunity. Non-zero means orders went out unrecorded, and reconcile's
recovery paths should have caught them; verify with `audit_drift.py`.

For prevention rather than diagnosis see [ib-gateway.md](ib-gateway.md).

---

## Restoring a backup

Prod keeps 14 days of nightly snapshots (`/opt/vibe-trade/backups`, cron 23:45), and
`reset_paper_db.py` writes its own pre-wipe archive.

```bash
# 1. Stop cron so nothing writes mid-restore (comment out the crontab lines).
# 2. Verify the snapshot BEFORE trusting it.
sha256sum -c backups/vibe_trade-prewipe-<stamp>.db.sha256
python scripts/verify_db.py backups/vibe_trade-prewipe-<stamp>.db

# 3. Copy it back into the Docker volume.
docker run --rm -v deploy_vibe-data:/data -v /opt/vibe-trade/backups:/backup \
    alpine cp /backup/<snapshot>.db /data/vibe_trade.db

# 4. Re-enable cron. The next reconcile self-heals the gap — it's idempotent and
#    resolves stale pending rows.
# 5. Confirm.
python scripts/audit_drift.py data/vibe_trade.db
```

**Restoring records never restores positions.** A restored DB describes the account
as it was at snapshot time; anything that filled since is missing. Run `reconcile`
first, then `audit_drift.py`, and resolve what's left with `review-trades`.

## Deliberately not automated

Repairing history means deciding what happened, and only you have the IB statement
that says so. The tools report and let you decide:

- `audit_drift.py` never writes
- the drift sweep flags but never invents an exit price
- share-count mismatches are warn-only
- **if IB reports zero positions while OPEN rows exist, the sweep refuses to run** —
  a failed account read would otherwise flag your entire book irreversibly
