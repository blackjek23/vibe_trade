# Playbooks

Operational procedures for vibe_trade. Everything you'd need with the bot running
and something to do or something wrong.

All times **Asia/Jerusalem**. US market opens 16:30 local, closes 23:00 local.

| Playbook | Use when |
|---|---|
| [daily-operations.md](daily-operations.md) | Normal running: cadence, strategy pool, config, routine commands, troubleshooting |
| [paper-reset.md](paper-reset.md) | Starting a clean paper run from scratch |
| [go-live-criteria.md](go-live-criteria.md) | **The Sept 2026 → Jan 2027 plan and the gates for going live** |
| [data-recovery.md](data-recovery.md) | DB and IB disagree, phantom positions, `NEEDS_REVIEW` rows, restoring a backup |
| [ib-gateway.md](ib-gateway.md) | Gateway keeps needing a human — systemd + IBC unattended setup |
| [deployment.md](deployment.md) | First-time deploy, Docker, cron install, updating the bot |
| [linux-bringup.md](linux-bringup.md) | Standing up a fresh Linux prod host end to end |

## Not playbooks

Design and history live outside this directory on purpose:

- [`../ARCHITECTURE_V2.md`](../ARCHITECTURE_V2.md) — why the three-job split exists, status lifecycle, DB schema
- [`../ROADMAP.md`](../ROADMAP.md) — what's planned
- [`../SESSION_H_FINDINGS.md`](../SESSION_H_FINDINGS.md) — numbered bug/incident history
- [`../../PROJECT_MASTER_STATE.md`](../../PROJECT_MASTER_STATE.md) — current state, read this first when picking the project up

## Tools these playbooks call

| Command | What it does | Safe to run anytime? |
|---|---|---|
| `vibe-trade preflight` | Is Gateway up and can submit work? | Yes — read-only |
| `vibe-trade status` / `trades` | Current positions / recent trades | Yes — read-only |
| `python scripts/audit_drift.py <db>` | DB-vs-IB consistency report, exit 1 if dirty | Yes — read-only |
| `python scripts/verify_db.py <db>` | `PRAGMA integrity_check` + row counts | Yes — read-only |
| `vibe-trade review-trades` | List / resolve `NEEDS_REVIEW` rows | Listing yes; `--resolve` writes |
| `vibe-trade cancel-pending [SYM]` | List working orders, or cancel one ticker's | Listing yes; cancelling acts at IB |
| `vibe-trade close-position SYM` | Market-close one ticker off-cycle | **No** — places an order |
| `vibe-trade panic --yes` | Close everything immediately | **No** — liquidates |
| `python scripts/reset_paper_db.py` | Archive + wipe the DB (paper only) | Dry run by default; `--yes` destroys |
| `python scripts/measure_slippage.py <db>` | Realized fill-vs-open slippage in bps | Yes — read-only (needs network) |
