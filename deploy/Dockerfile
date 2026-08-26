# Digest-pinned, not just tag-pinned: `python:3.11-slim` and
# `ghcr.io/astral-sh/uv:latest` are both mutable tags that can point to a
# different image tomorrow with no diff in this file (PROJECT_EVALUATION.md).
# Digests resolved 2026-08-27 -- to refresh, run:
#   docker buildx imagetools inspect python:3.11-slim
#   docker buildx imagetools inspect ghcr.io/astral-sh/uv:latest
FROM python:3.11-slim@sha256:be1575ed968de893bd54f4c56315ff7c4736ce522c1bca08fd521731aafc0d76

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
    VIBE_TRADE_CONFIG=/config/config.toml \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:88bc6eb1ccd4b82efd0e1b530caffabddf50dc2bf612e66c14ea25b8ee8a4d3d /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src/ src/
# --frozen: install exactly what uv.lock pins, never re-resolve against PyPI
# at build time. The previous `uv pip install --system ".[plot]"` ignored
# the lockfile entirely and re-resolved live against pyproject.toml's
# unpinned `>=` ranges -- a compromised release of any of the ~70 locked
# packages would land straight in the trading image on the next build, with
# no diff, no lockfile change, and no CI signal to catch it.
# Include the `plot` extra (matplotlib): the weekly report job renders a PNG.
# The three trading jobs don't need it, but a single image serves all jobs.
RUN uv sync --frozen --no-dev --extra plot --no-editable

# Run as an unprivileged user, not root. Combined with `network_mode: host`
# in docker-compose.yml, a code-execution bug anywhere in the dependency
# tree would otherwise run as uid 0 holding the host's network namespace.
# UID 1000 matches the conventional first non-root Linux user -- if the
# host's bind-mounted `deploy/logs` or `deploy/config` are owned by a
# different uid, `chown -R 1000:1000` those directories on the host before
# deploying this image, or the container will fail to write logs.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin vibetrade \
    && mkdir -p /app/data /app/logs /app/reports \
    && chown -R vibetrade:vibetrade /app
USER vibetrade

ENTRYPOINT ["/app/.venv/bin/python", "-m", "vibe_trade"]
