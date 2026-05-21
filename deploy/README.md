# vibe-trade Deployment Guide

Docker-based deployment for the three V2 daily jobs (submit, record, reconcile).

## Prerequisites

- **Linux host** with Docker and Docker Compose v2 installed
- **IB Gateway** running natively on the host at `localhost:7497`
  - Must be logged in to your IB account (paper or live)
  - The bot does NOT manage Gateway lifecycle — check it's running before relying on cron
- **Timezone** set to `Asia/Jerusalem`:
  ```bash
  sudo timedatectl set-timezone Asia/Jerusalem
  timedatectl  # verify
  ```

## Quick Start

1. **Clone the repo:**
   ```bash
   git clone <repo-url> /opt/vibe-trade
   cd /opt/vibe-trade/deploy
   ```

2. **Set up secrets:**
   ```bash
   cp .env.example .env
   # Edit .env — fill in VIBE_TRADE_TELEGRAM_TOKEN and VIBE_TRADE_TELEGRAM_CHAT_ID
   ```

3. **Set up config:**
   ```bash
   mkdir -p config
   cp ../config/config.example.toml config/config.toml
   # Edit config/config.toml:
   #   - Set mode = "paper" (or "live" when ready)
   #   - Set telegram.enabled = true
   #   - Adjust risk settings if needed
   ```

4. **Create logs directory:**
   ```bash
   mkdir -p logs
   ```

5. **Build the Docker image:**
   ```bash
   docker compose build
   ```

6. **Run the smoke test:**
   ```bash
   ./smoke-test.sh
   ```
   All three jobs should complete without errors. If Telegram is enabled,
   you'll receive notifications for each job.

## Scheduling

Install the crontab to run jobs automatically on trading days:

```bash
crontab crontab.example
crontab -l  # verify
```

The crontab runs three jobs Mon-Fri:
| Time (Jerusalem) | Job | What it does |
|---|---|---|
| 16:00 | submit | Place exit + entry orders on IB |
| 16:25 | record | Persist today's fills to DB |
| 23:30 | reconcile | Finalize statuses, snapshot, daily P&L |

**Important:** The host timezone must be `Asia/Jerusalem`. Verify with `timedatectl`.
The containers also set `TZ=Asia/Jerusalem` (in `docker-compose.yml` and the
`Dockerfile`), so log timestamps match IB fill times without any host-side fix.

If your repo is not at `/opt/vibe-trade`, edit the paths in `crontab.example` before installing.

## Log Locations

| File | Content | Rotation |
|------|---------|----------|
| `logs/vibe_trade.log` | JSON app-level logs (trades, errors, IB calls) | Daily, 7-day retention |
| `logs/cron.log` | Cron stdout/stderr (container output) | Manual (grows slowly) |

View live:
```bash
tail -f logs/vibe_trade.log | python -m json.tool  # pretty-print JSON
tail -f logs/cron.log
```

## Missed-Run Recovery

If a job didn't run (Gateway down, host rebooted, etc.), run it manually:

```bash
cd /opt/vibe-trade/deploy
docker compose run --rm submit      # safe to re-run
docker compose run --rm record      # idempotent (dedup on permId)
docker compose run --rm reconcile   # idempotent (dedup on permId)
```

**Idempotency guarantees:**
- **submit:** Places the same orders IB would deduplicate for the day. Safe to re-run.
- **record:** Deduplicates on `permId` — re-running skips already-recorded fills.
- **reconcile:** Deduplicates on `permId` — re-running won't double-count.

**Order matters:** submit must run before record (record reads fills from submit's orders). Record must run before reconcile (reconcile finalizes what record persisted).

## Updating the Bot

After pulling new code:

```bash
cd /opt/vibe-trade
git pull
cd deploy
docker compose build   # rebuild image with new code
```

No container restart needed — each job is a fresh `docker compose run`.

## Troubleshooting

### IB Gateway not reachable

```bash
# Check Gateway is running
ss -tlnp | grep 7497

# Test from inside a container
docker compose run --rm submit config-check
```

`config-check` reads `/config/config.toml` via the `VIBE_TRADE_CONFIG` env var
baked into the image, so it sees the mounted config even though the `run`
override drops the service's `--config` flag.

If the port is open but the bot can't connect, check that `config.toml` has
`host = "127.0.0.1"` and the correct port (7497 for paper, 7496 for live).

### Inspecting the database

The SQLite DB lives in a Docker named volume:

```bash
# Find the volume path
docker volume inspect deploy_vibe-data

# Query directly (install sqlite3 on host)
sqlite3 "$(docker volume inspect deploy_vibe-data -f '{{.Mountpoint}}')/vibe_trade.db" \
  "SELECT * FROM trades ORDER BY created_at DESC LIMIT 10;"
```

### Container won't start

```bash
# Check image exists
docker images | grep vibe-trade

# Rebuild
docker compose build --no-cache

# Check for config errors
docker compose run --rm submit config-check
```

### Cron not firing

```bash
# Verify crontab is installed
crontab -l

# Check cron service is running
systemctl status cron

# Check timezone
timedatectl

# Check cron.log for errors
tail -20 logs/cron.log
```
