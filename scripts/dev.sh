#!/usr/bin/env bash
# Local dev harness: build + start Postgres and the app (no oauth2 login wall),
# wait for health, and seed sample data. Then the app is at http://localhost:8081
# and the Claude preview server (scripts/dev_preview_proxy.py) can screenshot it.
#
# Usage:
#   ./scripts/dev.sh          # up + seed
#   ./scripts/dev.sh seed     # re-seed only
#   ./scripts/dev.sh down      # tear down (and drop the DB volume)
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.dev.yml)

seed() {
  echo "Seeding sample data…"
  "${COMPOSE[@]}" exec -T postgres psql -U "${DB_USER:-backup_reports}" -d "${DB_NAME:-backup_reports}" \
    < scripts/dev_seed.sql >/dev/null
  echo "Seeded. App: http://localhost:8081/clusters/pve-aboe-muc"
}

case "${1:-up}" in
  down)
    "${COMPOSE[@]}" down -v
    ;;
  seed)
    seed
    ;;
  up)
    if ! docker info >/dev/null 2>&1; then
      echo "Docker daemon not running. Start Docker Desktop first (open -a Docker)." >&2
      exit 1
    fi
    "${COMPOSE[@]}" up -d --build postgres backup-sentinel
    echo -n "Waiting for app health"
    for _ in $(seq 1 30); do
      if [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8081/healthz)" = "200" ]; then
        echo " — ready."
        seed
        exit 0
      fi
      echo -n "."; sleep 2
    done
    echo " — timed out. Check: ${COMPOSE[*]} logs backup-sentinel" >&2
    exit 1
    ;;
  *)
    echo "Usage: $0 [up|seed|down]" >&2
    exit 1
    ;;
esac
