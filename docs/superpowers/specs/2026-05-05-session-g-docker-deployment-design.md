# Session G — Docker Deployment Scaffolding

**Date:** 2026-05-05
**Status:** Approved design
**Scope:** Deployment config files, shell scripts, documentation. No Python code changes, no new tests.

---

## Goal

Enable automated daily execution of the three V2 jobs (submit, record, reconcile) on a Linux production server via Docker containers triggered by host crontab.

## Architecture

```
Linux host
├── IB Gateway (running natively, localhost:7497)
├── crontab (triggers docker compose run at 16:00, 16:25, 23:30 Asia/Jerusalem)
└── /opt/vibe-trade/                    (repo clone)
    ├── src/                            (source code — used at build time)
    ├── pyproject.toml                  (used at build time)
    └── deploy/                         (working directory for docker compose)
        ├── Dockerfile
        ├── docker-compose.yml
        ├── .env                        (secrets, not committed)
        ├── .env.example
        ├── crontab.example
        ├── smoke-test.sh
        ├── README.md
        ├── config/
        │   └── config.toml            (runtime config, bind-mounted read-only)
        └── logs/
            ├── vibe_trade.log         (JSON, app-level, rotated daily)
            └── cron.log               (cron stdout/stderr)
```

Note: `deploy/config/` and `deploy/logs/` are production-only directories (gitignored). The repo's `config/config.example.toml` serves as the template.

## Design Decisions

### Single image, three compose services

One `Dockerfile` builds the full `vibe_trade` package. `docker-compose.yml` defines three services (`submit`, `record`, `reconcile`) with different `command:` overrides. Host crontab triggers `docker compose run --rm <service>`.

**Rationale:** Simplest approach. One image to build/tag. Each job shares the same deps. Separate Dockerfiles would add build complexity with no real benefit (Python + deps dominate image size regardless).

### Package installation via `uv`

Uses `uv pip install --system --no-cache .` inside the container. No lockfile required (project uses `pyproject.toml` only). Fast installs, smaller image (no cache layer).

### Network mode: host

Containers use `network_mode: host` to reach IB Gateway at `127.0.0.1:7497` directly. No port mapping or `host.docker.internal` workarounds needed.

### Scheduling: host crontab

Host crontab runs `docker compose run --rm <service>` at the three scheduled times. Mon-Fri only.

**Why not systemd timers:** Adds complexity without meaningful benefit for three simple daily jobs. Missed-run handling is better served by Telegram error notification + manual re-run.

**Why not in-container cron:** Would require a long-running container, complicates restarts on code updates, mixes logs, prevents ad-hoc `docker compose run` usage.

### Persistence

| Data | Mechanism | Why |
|------|-----------|-----|
| SQLite DB (`data/vibe_trade.db`) | Docker named volume `vibe-data` | Survives container recreation |
| Logs (`logs/`) | Bind-mount `./logs` | Easy `tail -f` from host |
| Config (`config/config.toml`) | Bind-mount `./config:ro` | Secrets stay out of image |
| Telegram credentials | `.env` file (env vars) | Standard Docker secrets pattern |

### Compose profiles

Services use `profiles: ["job"]` to prevent accidental `docker compose up` from starting all three simultaneously. Must explicitly target a service: `docker compose run --rm submit`.

---

## Deliverables

### 1. `deploy/Dockerfile`

```dockerfile
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN uv pip install --system --no-cache .

ENTRYPOINT ["python", "-m", "vibe_trade"]
```

### 2. `deploy/docker-compose.yml`

Build context is the repo root (one level up from `deploy/`), so the Dockerfile can access `pyproject.toml` and `src/`:

```yaml
build:
  context: ..
  dockerfile: deploy/Dockerfile
```

Three services (submit, record, reconcile) sharing:
- `vibe-data` named volume mounted at `/app/data`
- `./logs` bind-mount at `/app/logs` (relative to `deploy/`)
- `./config` bind-mount at `/config` (read-only)
- `.env` file for secrets
- `network_mode: host`
- `profiles: ["job"]`

Each service differs only in `command:` (`submit --config /config/config.toml`, etc.)

### 3. `deploy/.env.example`

```env
VIBE_TRADE_TELEGRAM_TOKEN=
VIBE_TRADE_TELEGRAM_CHAT_ID=
```

### 4. `deploy/crontab.example`

```cron
# vibe-trade V2 — Asia/Jerusalem timezone (host TZ must be set)
# Mon-Fri only. IB Gateway must be running on localhost:7497.
00 16 * * 1-5  cd /opt/vibe-trade/deploy && docker compose run --rm submit  >> ../logs/cron.log 2>&1
25 16 * * 1-5  cd /opt/vibe-trade/deploy && docker compose run --rm record  >> ../logs/cron.log 2>&1
30 23 * * 1-5  cd /opt/vibe-trade/deploy && docker compose run --rm reconcile >> ../logs/cron.log 2>&1
```

### 5. `deploy/smoke-test.sh`

Sequential run of all three jobs with `set -euo pipefail`. Exits on first failure. No assertions beyond "it didn't crash" — Telegram notifications confirm correctness.

### 6. `deploy/README.md`

Sections:
1. Prerequisites (Linux, Docker, IB Gateway, timezone)
2. Quick start (clone, .env, config.toml, build, smoke test)
3. Scheduling (install crontab, verify timezone)
4. Log locations (app-level JSON vs cron-level stdout)
5. Missed-run recovery (manual re-run, idempotency guarantees)
6. Troubleshooting (connectivity, volume inspection, common errors)

---

## Out of Scope

- IB Gateway containerization (runs natively on host)
- IB Gateway auto-restart / headless setup (Phase 4)
- CI/CD pipeline for building the Docker image
- Multi-host / cloud deployment
- New Python code or tests
