FROM python:3.11-slim

# tzdata so the container clock and log timestamps are Asia/Jerusalem, not UTC.
# Without this, cron-relative log analysis and IB fill-time comparisons drift
# 3 h (Hygiene #2, docs/SESSION_H_FINDINGS.md).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# TZ: belt-and-suspenders default; docker-compose.yml also sets it per service.
# VIBE_TRADE_CONFIG: lets `docker compose run` invocations that override the
# service command (e.g. config-check) still find the mounted config (Hygiene #3).
ENV TZ=Asia/Jerusalem \
    VIBE_TRADE_CONFIG=/config/config.toml

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
# Include the `plot` extra (matplotlib): the weekly report job renders a PNG.
# The three trading jobs don't need it, but a single image serves all jobs.
RUN uv pip install --system --no-cache ".[plot]"

ENTRYPOINT ["python", "-m", "vibe_trade"]
