# Session H — Linux Live Paper Runbook

> **Goal:** Run the bot on Linux against IB paper for 5–10 trading days,
> observe behavior, triage findings into later sessions.
>
> **Assumptions:** Linux host with Docker + Compose v2. IB Gateway running
> natively on the host at `localhost:4002` (Gateway paper port), logged in
> to the paper account. You have shell access and sudo.
>
> **Important port note:** This bot's defaults (`config.example.toml`,
> `docs/playbooks/deployment.md`) reference TWS ports (`7497` paper / `7496` live).
> **IB Gateway uses different ports: `4002` paper / `4001` live.** Every
> step below reflects Gateway = 4002.

---

## Phase 0 — Host prep (one-time)

### 0.1 Set timezone to Asia/Jerusalem

All cron schedules and log timestamps assume Jerusalem time.

```bash
sudo timedatectl set-timezone Asia/Jerusalem
timedatectl    # verify "Time zone: Asia/Jerusalem (IDT, ...)"
```

The containers carry their own `TZ=Asia/Jerusalem` (set in `docker-compose.yml`
and the `Dockerfile`), so in-container log timestamps are correct regardless of
the host. Setting the host timezone still matters — cron fires in host time.

### 0.2 Confirm Docker + Compose v2

```bash
docker --version            # 20.10+ ideal
docker compose version      # v2.x — note "compose" is a subcommand, not "docker-compose"
```

If missing, install via your distro's package manager (`docker.io` + `docker-compose-plugin` on Debian/Ubuntu).

### 0.3 Confirm IB Gateway is reachable

```bash
ss -tlnp | grep 4002
# Expect a line like: LISTEN 0 ... 127.0.0.1:4002 ...
```

If nothing listens, start IB Gateway and log in to the paper account before continuing. The bot does **not** manage Gateway lifecycle.

In Gateway's API settings, verify:
- **Enable ActiveX and Socket Clients** = ON
- **Socket port** = `4002`
- **Trusted IPs** includes `127.0.0.1`
- **Read-Only API** = OFF (the bot needs to place orders)
- **Master API client ID** = blank (or set to a value the bot won't collide with — bot uses 1/2/3/8)

---

## Phase 1 — Get the code on the host

### 1.1 Clone

```bash
sudo mkdir -p /opt/vibe-trade
sudo chown $USER:$USER /opt/vibe-trade
git clone <your-repo-url> /opt/vibe-trade
cd /opt/vibe-trade
git log -1                  # confirm you're on the expected commit
```

### 1.2 Move to deploy dir

All commands from here run from `/opt/vibe-trade/deploy` unless noted.

```bash
cd /opt/vibe-trade/deploy
```

---

## Phase 2 — Configure

### 2.1 Secrets (Telegram)

```bash
cp .env.example .env
nano .env       # or vim
```

Fill in:
- `VIBE_TRADE_TELEGRAM_TOKEN` — from @BotFather
- `VIBE_TRADE_TELEGRAM_CHAT_ID` — from your Telegram chat (use @userinfobot or curl the bot's `getUpdates` endpoint)

### 2.2 Bot config — **port 4002 override required**

```bash
mkdir -p config logs data
cp ../config/config.example.toml config/config.toml
nano config/config.toml
```

Required edits:

```toml
[general]
mode = "paper"                  # MUST be "paper" for Session H

[broker]
host = "127.0.0.1"
paper_port = 4002               # CHANGED from 7497 — IB Gateway, not TWS
live_port = 4001                # CHANGED from 7496 — for completeness
client_id = 1                   # submit uses this; record=2, reconcile=3 are derived
timeout = 30
account = ""                    # blank = first available paper account

[telegram]
enabled = true                  # CHANGED from false
notify_on_trade = true
notify_on_error = true
daily_summary = true
```

Leave `[risk]` defaults alone (`pct_per_position = 0.018`, `max_open_positions = 50`) — those are the locked V2 numbers.

### 2.3 Sanity-check the config

```bash
docker compose build              # first build, ~2–5 min
docker compose run --rm submit config-check
```

Expect a clean exit with the resolved config printed. If it complains about port, db path, or Telegram creds, fix before moving on.

---

## Phase 3 — Smoke test (no market hours required)

`smoke-test.sh` runs all three jobs sequentially. Safe to run anytime — `submit` will just find no signals outside market days, `record`/`reconcile` are idempotent.

```bash
./smoke-test.sh
```

Expected:
- Three Telegram messages (one per job), even if "no orders placed today"
- A new `logs/vibe_trade.log` with JSON entries
- No tracebacks in stdout / `logs/cron.log`

If anything errors, see Troubleshooting at the bottom.

---

## Phase 4 — Day 1: manual sequenced run

**Run each job at its scheduled time** to validate the full daily loop before handing it to cron.

| Time (Jerusalem) | Command | What you're checking |
|---|---|---|
| 16:00 | `docker compose run --rm submit` | Orders placed against IB paper, Telegram message lists them |
| 16:25 | `docker compose run --rm record` | Fills persisted, status = SUBMITTED, Telegram confirms count |
| 23:30 | `docker compose run --rm reconcile` | Statuses finalized (FILLED/CANCELLED), portfolio_snapshot row written, daily P&L Telegram message |

Between runs, peek at the DB:

```bash
# Find the DB inside the container's volume
docker volume inspect deploy_vibe-data -f '{{.Mountpoint}}'
# Or query directly if you have sqlite3 on the host:
sudo sqlite3 "$(docker volume inspect deploy_vibe-data -f '{{.Mountpoint}}')/vibe_trade.db" \
  "SELECT symbol, side, quantity, status, perm_id FROM trades ORDER BY created_at DESC LIMIT 10;"
```

Cross-check IB Gateway's order log against the `trades` table — same tickers, same quantities.

---

## Phase 5 — Hand off to cron (Day 2 onward)

Once Day 1 looks correct:

```bash
crontab crontab.example
crontab -l                  # verify three lines (16:00, 16:25, 23:30 Mon–Fri)
```

If your repo isn't at `/opt/vibe-trade`, edit `crontab.example` first to fix the `cd` paths.

**Verify cron service is running:**

```bash
systemctl status cron       # Debian/Ubuntu
# or
systemctl status crond      # RHEL/Fedora
```

---

## Phase 6 — Daily observation checklist (5–10 trading days)

Each evening after 23:30, take 5 minutes and check:

| Check | How |
|---|---|
| All three Telegram messages arrived | Scroll the bot chat |
| No tracebacks in app log | `tail -200 logs/vibe_trade.log \| grep -i error` |
| Cron actually fired | `tail -50 logs/cron.log` |
| DB has today's rows | `sqlite3 ... "SELECT date(created_at), count(*) FROM trades GROUP BY 1 ORDER BY 1 DESC LIMIT 5;"` |
| IB positions == DB open positions | Compare Gateway portfolio panel against `SELECT symbol FROM trades WHERE status='OPEN';` |
| Strategy signals match orders | Spot-check 1–2 tickers: did Donchian breakout actually trigger? |
| Telegram formatting on mobile | Open on phone, check daily summary table renders |

Things to log (informally — a notes file is fine):
- **Partial fills** — any? On which tickers? (Liquid SP500 should rarely partial-fill.)
- **Reconcile drift** — any rows in `trades` whose status doesn't match IB's view?
- **Late fills** — any fills timestamped after 16:25 that record missed?
- **Gateway disconnects** — any "connection refused" / "API error" in the log?
- **Log noise** — too verbose? Too quiet? Anything you wish was logged but isn't?

These observations feed Session H's exit triage — they decide what Phase 4 (resilience hardening) prioritizes.

---

## Phase 7 — End of paper week

After 5–10 trading days, summarize findings into `docs/session_h_findings.md` (create new) or directly into a new spec under `docs/superpowers/specs/`. Then triage:

- Bugs / crashes → fix immediately
- Edge cases (late fills, disconnects) → Phase 4 hardening session
- "Wish I had X" tooling → Session J (overrides) or K (dashboard)
- Strategy concerns → revisit backtest, possibly Session L (multi-strategy)

Update `PROJECT_MASTER_STATE.md`:
- Section 2: mark Session H "Done" with date range
- Section 7: replace hand-off with the next session's pre-flight

---

## Troubleshooting

### "Connection refused" / can't reach Gateway

```bash
# Confirm Gateway is listening on 4002
ss -tlnp | grep 4002

# Test from inside a container (host networking)
docker compose run --rm submit config-check

# If you see "Cannot connect to 127.0.0.1:4002" but ss shows it listening:
# Gateway's "Trusted IPs" list may not include 127.0.0.1 — open Gateway,
# Configuration -> API -> Settings, add 127.0.0.1, restart Gateway.
```

The compose file uses `network_mode: host`, so `127.0.0.1` inside the container == host's `127.0.0.1`. No port-forwarding needed.

### Telegram messages not arriving

```bash
# Re-check creds were loaded
docker compose run --rm submit config-check | grep -i telegram

# Check .env is being read by compose
grep TELEGRAM .env
docker compose config | grep -i telegram

# Smoke-test the bot's notifier directly
docker compose run --rm submit python -c "
from vibe_trade.notify import get_notifier
from vibe_trade.config import load_config
n = get_notifier(load_config())
n.send('test from runbook')
"
```

### Container can't write to `data/` or `logs/`

```bash
# Ensure host dirs exist and are writable
ls -la /opt/vibe-trade/deploy/data /opt/vibe-trade/deploy/logs

# If permissions are wrong:
sudo chown -R $USER:$USER /opt/vibe-trade/deploy/data /opt/vibe-trade/deploy/logs
```

### Cron didn't fire at 16:00

```bash
# Did cron see the job?
grep CRON /var/log/syslog | tail -20      # Debian/Ubuntu
journalctl -u cron --since "today"         # systemd

# Is the timezone right?
timedatectl

# Did the job run but fail silently?
tail -100 /opt/vibe-trade/deploy/logs/cron.log
```

### "Database is locked"

Two of the three containers tried to run simultaneously. Cron schedule prevents this in normal operation; if it happened, check whether you manually re-ran a job that overlapped a cron-fired one. The DB is a Docker named volume — only one writer at a time.

### Need to wipe and start over

```bash
# Stop anything running
docker compose down

# WARNING: deletes the SQLite DB
docker volume rm deploy_vibe-data

# Rebuild
docker compose build --no-cache
```

---

## Quick reference — daily commands

```bash
cd /opt/vibe-trade/deploy

# Manual run of any job (idempotent — safe to re-run)
docker compose run --rm submit
docker compose run --rm record
docker compose run --rm reconcile

# Watch logs live
tail -f logs/vibe_trade.log | python3 -m json.tool
tail -f logs/cron.log

# Update bot to latest code
cd /opt/vibe-trade && git pull && cd deploy && docker compose build

# Inspect DB
sudo sqlite3 "$(docker volume inspect deploy_vibe-data -f '{{.Mountpoint}}')/vibe_trade.db"
# Useful queries:
#   SELECT * FROM trades ORDER BY created_at DESC LIMIT 20;
#   SELECT date, net_liquidation, total_pnl FROM portfolio_snapshot ORDER BY date DESC LIMIT 5;
#   SELECT date, realized_pnl, unrealized_pnl FROM daily_pnl ORDER BY date DESC LIMIT 5;
```

---

## Companion files

- [`deployment.md`](deployment.md) — original Docker setup notes (uses TWS port 7497, override to 4002)
- [`deploy/crontab.example`](../../deploy/crontab.example) — the three cron lines
- [`deploy/smoke-test.sh`](../../deploy/smoke-test.sh) — sequential test script
- [`PROJECT_MASTER_STATE.md`](../../PROJECT_MASTER_STATE.md) section 7 — short pre-flight summary
- [`docs/ROADMAP.md`](../ROADMAP.md) — Session H scope + what comes after
