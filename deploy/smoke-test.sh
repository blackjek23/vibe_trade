#!/usr/bin/env bash
set -euo pipefail

# Smoke test: run all four V2 daily jobs sequentially against paper mode.
#
# Prerequisites:
#   - IB Gateway running on localhost:7497 (paper account)
#   - deploy/.env populated with Telegram creds (or telegram.enabled = false)
#   - deploy/config/config.toml exists with mode = "paper"
#   - Docker image built: docker compose build
#
# Usage:
#   cd deploy && ./smoke-test.sh

echo "=== Smoke test: preflight (15:50 job) ==="
docker compose run --rm preflight

echo ""
echo "=== Smoke test: submit (16:00 job) ==="
docker compose run --rm submit

echo ""
echo "=== Smoke test: record (16:35 job) ==="
docker compose run --rm record

echo ""
echo "=== Smoke test: reconcile (23:30 job) ==="
docker compose run --rm reconcile

echo ""
echo "=== Smoke test: report-weekly (Saturday job) ==="
docker compose run --rm report-weekly

echo ""
echo "=== All jobs completed successfully ==="
