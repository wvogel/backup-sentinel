#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export GIT_COMMIT="$(git rev-parse --short HEAD)"
echo "Deploying backup-sentinel v1.2.${GIT_COMMIT} ..."
docker compose up -d --build
