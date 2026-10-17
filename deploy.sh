#!/usr/bin/env bash
set -euo pipefail

cd "$DEPLOY_PATH"
git fetch origin main
git reset --hard origin/main
docker compose down
docker compose build --no-cache

# The app runs as an unprivileged user; the mounted host directories must
# belong to it, or e.g. the monthly report cannot be written. Its UID/GID
# come from the image just built, so a change in the Dockerfile follows.
owner="$(docker compose run --rm --no-deps -T --entrypoint sh backup-sentinel -c 'echo "$(id -u):$(id -g)"')"
for dir in data reports; do
  mkdir -p "$dir"
  if ! chown -R "$owner" "$dir"; then
    echo "WARNING: cannot hand $DEPLOY_PATH/$dir to $owner (run deploy.sh as root)" >&2
  fi
done

docker compose up -d
