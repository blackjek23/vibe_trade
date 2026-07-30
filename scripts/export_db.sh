#!/usr/bin/env bash
# Export a CONSISTENT snapshot of the live vibe_trade SQLite DB so it can be
# carried to another machine (USB / manual copy) for offline work.
#
# Run this ON the working bot host (the machine running `docker compose`).
# It pulls the DB out of the Docker named volume using SQLite's online
# `.backup` (safe even if a job is mid-write or the DB is in WAL mode) and
# writes a timestamped file + a .sha256 checksum into the current directory.
#
# Usage:
#   ./export_db.sh                  # default volume "deploy_vibe-data"
#   ./export_db.sh my_volume_name   # override the volume name
#   VIBE_VOLUME=foo ./export_db.sh  # or via env var
#
# Not running under Docker? If the bot runs the venv directly and the DB is a
# plain file, just snapshot it with:
#   sqlite3 /path/to/vibe_trade.db ".backup vibe_trade-snapshot.db"
set -euo pipefail

VOLUME="${1:-${VIBE_VOLUME:-deploy_vibe-data}}"
DB_IN_VOLUME="vibe_trade.db"
OUT_DIR="$(pwd)"
STAMP="$(date -u +%Y%m%d-%H%M%S)"   # UTC, sortable
OUT_FILE="vibe_trade-${STAMP}.db"

echo "Volume:    ${VOLUME}"
echo "Source DB: /data/${DB_IN_VOLUME} (inside the volume)"
echo "Output:    ${OUT_DIR}/${OUT_FILE}"
echo

if ! docker volume inspect "${VOLUME}" >/dev/null 2>&1; then
  echo "ERROR: docker volume '${VOLUME}' not found." >&2
  echo "Pick the right one from:" >&2
  docker volume ls >&2
  exit 1
fi

# Consistent online backup via sqlite3 inside a throwaway alpine container.
# (Host needs neither sqlite nor the DB on its filesystem — only Docker.)
docker run --rm \
  -v "${VOLUME}:/data" \
  -v "${OUT_DIR}:/out" \
  alpine:3.20 sh -c "
    apk add --no-cache -q sqlite &&
    sqlite3 /data/${DB_IN_VOLUME} \".backup '/out/${OUT_FILE}'\" &&
    echo -n 'integrity_check: ' &&
    sqlite3 /out/${OUT_FILE} 'PRAGMA integrity_check;'
  "

# Checksum so the manual copy can be verified on the other side.
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${OUT_FILE}" | tee "${OUT_FILE}.sha256"
fi

echo
ls -lh "${OUT_FILE}"
echo
echo "Done. Copy BOTH files to the dev machine:"
echo "  ${OUT_FILE}"
echo "  ${OUT_FILE}.sha256   (optional, for verification)"
echo "Then run on the dev machine:"
echo "  scripts\\import_prod_db.ps1 -Source <path-to>\\${OUT_FILE}"
