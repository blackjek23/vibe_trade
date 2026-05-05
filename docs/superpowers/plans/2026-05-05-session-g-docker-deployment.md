# Session G — Docker Deployment Scaffolding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create Docker deployment scaffolding so the three V2 jobs (submit, record, reconcile) can run as short-lived containers on a Linux host, triggered by crontab.

**Architecture:** Single Docker image built from `deploy/Dockerfile` using `uv` for fast installs. `docker-compose.yml` defines three services sharing the same image with different `command:` overrides. Host crontab triggers `docker compose run --rm <service>` at 16:00, 16:25, and 23:30 Asia/Jerusalem, Mon–Fri. Containers reach IB Gateway on the host via `network_mode: host`.

**Tech Stack:** Docker, Docker Compose, uv, bash, cron

**Spec:** `docs/superpowers/specs/2026-05-05-session-g-docker-deployment-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `deploy/Dockerfile` | Create | Build image with Python 3.11 + uv + vibe_trade package |
| `.dockerignore` | Create | Exclude tests, docs, .venv, .git from build context |
| `deploy/docker-compose.yml` | Create | Three services (submit, record, reconcile) with volumes, networking, profiles |
| `deploy/.env.example` | Create | Template for Telegram secrets |
| `deploy/crontab.example` | Create | Three cron lines for Mon–Fri scheduling |
| `deploy/smoke-test.sh` | Create | Sequential run of all three jobs |
| `deploy/README.md` | Create | Install steps, prerequisites, scheduling, troubleshooting |
| `.gitignore` | Modify | Add `deploy/config/`, `deploy/logs/`, `deploy/.env` |

---

### Task 1: Dockerfile + .dockerignore

**Files:**
- Create: `deploy/Dockerfile`
- Create: `.dockerignore`

- [ ] **Step 1: Create `deploy/Dockerfile`**

```dockerfile
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN uv pip install --system --no-cache .

ENTRYPOINT ["python", "-m", "vibe_trade"]
```

Notes:
- `COPY --from=` pulls the `uv` binary from the official image (no curl/pip install step).
- `pyproject.toml` is copied first so dependency installs are cached in a Docker layer. Source changes only invalidate the final `COPY src/` layer — but `uv pip install .` re-runs because it depends on `src/`. For a pure-config session this is acceptable; a future optimization could split deps from package install.
- `--system` installs into the container's system Python (no venv inside the container).
- `ENTRYPOINT` is the CLI; each compose service appends its command (`submit`, `record`, etc.).

- [ ] **Step 2: Create `.dockerignore`**

This file goes at the **repo root** (same level as `pyproject.toml`), because the Docker build context is set to `..` (repo root) in `docker-compose.yml`. It controls what gets sent to the Docker daemon.

```
.venv/
venv/
.git/
.claude/
tests/
docs/
scratches/
backtests/
data/
logs/
*.db
*.pyc
__pycache__/
.mypy_cache/
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
deploy/config/
deploy/logs/
deploy/.env
*.egg-info/
dist/
build/
```

- [ ] **Step 3: Verify the build works**

Run from the `deploy/` directory:

```bash
docker compose build
```

If running outside compose (standalone check):

```bash
cd deploy && docker build -f Dockerfile -t vibe-trade:dev ..
```

Expected: image builds successfully, final line shows image ID.

- [ ] **Step 4: Verify the entrypoint works**

```bash
docker run --rm vibe-trade:dev --help
```

Expected: typer help output showing `submit`, `record`, `reconcile`, `backtest`, etc.

- [ ] **Step 5: Commit**

```bash
git add deploy/Dockerfile .dockerignore
git commit -m "Add Dockerfile and .dockerignore for Docker deployment

- python:3.11-slim base with uv for fast installs
- .dockerignore excludes tests, docs, .venv, .git from build context
- ENTRYPOINT is 'python -m vibe_trade'; compose services append command

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 2: docker-compose.yml

**Files:**
- Create: `deploy/docker-compose.yml`

- [ ] **Step 1: Create `deploy/docker-compose.yml`**

```yaml
services:
  submit:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    command: ["submit", "--config", "/config/config.toml"]
    volumes:
      - vibe-data:/app/data
      - ./logs:/app/logs
      - ./config:/config:ro
    env_file: .env
    network_mode: host
    profiles: ["job"]

  record:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    command: ["record", "--config", "/config/config.toml"]
    volumes:
      - vibe-data:/app/data
      - ./logs:/app/logs
      - ./config:/config:ro
    env_file: .env
    network_mode: host
    profiles: ["job"]

  reconcile:
    build:
      context: ..
      dockerfile: deploy/Dockerfile
    command: ["reconcile", "--config", "/config/config.toml"]
    volumes:
      - vibe-data:/app/data
      - ./logs:/app/logs
      - ./config:/config:ro
    env_file: .env
    network_mode: host
    profiles: ["job"]

volumes:
  vibe-data:
```

Key details:
- `context: ..` sets the build context to the repo root so the Dockerfile can access `pyproject.toml` and `src/`.
- `profiles: ["job"]` prevents `docker compose up` from starting all three at once. Each must be targeted explicitly: `docker compose run --rm submit`.
- `vibe-data` is a named volume for SQLite persistence (`data/vibe_trade.db`).
- `./logs` and `./config` are bind-mounts relative to the compose file location (`deploy/`).
- `network_mode: host` lets containers reach IB Gateway at `127.0.0.1:7497`.

- [ ] **Step 2: Verify compose parses correctly**

```bash
cd deploy && docker compose config
```

Expected: valid YAML output with all three services expanded. No errors.

- [ ] **Step 3: Commit**

```bash
git add deploy/docker-compose.yml
git commit -m "Add docker-compose.yml with three job services

- submit, record, reconcile share one image with different commands
- network_mode: host for IB Gateway connectivity
- Named volume for DB, bind-mounts for logs + config
- profiles: ['job'] prevents accidental 'docker compose up'

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 3: .env.example + crontab.example

**Files:**
- Create: `deploy/.env.example`
- Create: `deploy/crontab.example`

- [ ] **Step 1: Create `deploy/.env.example`**

```env
# Telegram notifications — fill in your bot token and chat ID.
# These override the [telegram] section in config.toml when set.
VIBE_TRADE_TELEGRAM_TOKEN=
VIBE_TRADE_TELEGRAM_CHAT_ID=
```

- [ ] **Step 2: Create `deploy/crontab.example`**

```cron
# vibe-trade V2 — three daily jobs
# Timezone: Asia/Jerusalem (set host TZ or use timedatectl)
# Prereq: IB Gateway running on localhost:7497
#
# Install: crontab deploy/crontab.example
# Verify:  crontab -l

00 16 * * 1-5  cd /opt/vibe-trade/deploy && docker compose run --rm submit   >> ./logs/cron.log 2>&1
25 16 * * 1-5  cd /opt/vibe-trade/deploy && docker compose run --rm record   >> ./logs/cron.log 2>&1
30 23 * * 1-5  cd /opt/vibe-trade/deploy && docker compose run --rm reconcile >> ./logs/cron.log 2>&1
```

Notes:
- `1-5` = Monday through Friday.
- `>> ./logs/cron.log 2>&1` appends both stdout and stderr to `deploy/logs/cron.log` (same bind-mount directory the app writes JSON logs to).
- The path `/opt/vibe-trade/` is a convention — README documents how to adjust.

- [ ] **Step 3: Commit**

```bash
git add deploy/.env.example deploy/crontab.example
git commit -m "Add .env.example and crontab.example

- .env.example: Telegram token + chat_id placeholders
- crontab.example: three cron lines Mon-Fri (16:00, 16:25, 23:30 Jerusalem)
- Logs cron output to deploy/logs/cron.log

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 4: smoke-test.sh

**Files:**
- Create: `deploy/smoke-test.sh`

- [ ] **Step 1: Create `deploy/smoke-test.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Smoke test: run all three V2 jobs sequentially against paper mode.
#
# Prerequisites:
#   - IB Gateway running on localhost:7497 (paper account)
#   - deploy/.env populated with Telegram creds (or telegram.enabled = false)
#   - deploy/config/config.toml exists with mode = "paper"
#   - Docker image built: docker compose build
#
# Usage:
#   cd deploy && ./smoke-test.sh

echo "=== Smoke test: submit (16:00 job) ==="
docker compose run --rm submit

echo ""
echo "=== Smoke test: record (16:25 job) ==="
docker compose run --rm record

echo ""
echo "=== Smoke test: reconcile (23:30 job) ==="
docker compose run --rm reconcile

echo ""
echo "=== All three jobs completed successfully ==="
```

- [ ] **Step 2: Make executable**

```bash
chmod +x deploy/smoke-test.sh
```

- [ ] **Step 3: Commit**

```bash
git add deploy/smoke-test.sh
git commit -m "Add smoke-test.sh for sequential job validation

Runs submit -> record -> reconcile against paper mode.
Exits on first failure (set -e). No assertions beyond 'it ran'.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 5: README.md

**Files:**
- Create: `deploy/README.md`

- [ ] **Step 1: Create `deploy/README.md`**

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add deploy/README.md
git commit -m "Add deployment README with setup, scheduling, and troubleshooting

Covers: prerequisites, quick start, crontab install, log locations,
missed-run recovery (idempotency guarantees), updating, and
common troubleshooting scenarios.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 6: Update .gitignore

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add deploy-specific ignores to `.gitignore`**

Append to the existing `.gitignore`:

```
# Deploy runtime (not committed)
deploy/config/
deploy/logs/
deploy/.env
```

These directories are created on the production host during setup and should never be committed (they contain runtime config with secrets and log files).

- [ ] **Step 2: Verify nothing unexpected is tracked**

```bash
git status
```

Expected: only the new `deploy/` files and modified `.gitignore` show up. No `deploy/config/`, `deploy/logs/`, or `deploy/.env` should appear.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "Gitignore deploy runtime dirs (config, logs, .env)

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 7: Final verification

- [ ] **Step 1: Verify all files exist**

```bash
ls -la deploy/
```

Expected output shows:
```
Dockerfile
docker-compose.yml
.env.example
crontab.example
smoke-test.sh
README.md
```

- [ ] **Step 2: Verify compose config is valid**

```bash
cd deploy && docker compose config
```

Expected: valid expanded YAML, no errors.

- [ ] **Step 3: Verify git log shows all Session G commits**

```bash
git log --oneline -7
```

Expected: 6 Session G commits (Tasks 1–6) on top of the existing history.
