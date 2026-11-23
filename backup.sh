#!/bin/bash
set -euo pipefail

# cd to the directory where this script lives (= project root)
cd "$(dirname "$(readlink -f "$0")")"

# Load DB credentials from .env
if [ -f .env ]; then
  export $(grep -E '^(DB_(NAME|USER)|BACKUP_AGE_RECIPIENT)=' .env | xargs)
fi

backup_name=$(date +'%Y-%m-%d')
backup_dir=./backup

mkdir -p "${backup_dir}"
chmod 700 "${backup_dir}"
umask 077

# With BACKUP_AGE_RECIPIENT (an age public key, "age1...") in .env the dump is
# encrypted to that key: the matching private key is kept somewhere else, so
# this host cannot read its own dumps. Without it the dump is plain — the API
# tokens in it are encrypted with BSENTINEL_SECRET_KEY, everything else is not.
if [ -n "${BACKUP_AGE_RECIPIENT:-}" ]; then
  command -v age >/dev/null 2>&1 || { echo "BACKUP_AGE_RECIPIENT is set but 'age' is not installed" >&2; exit 1; }
  docker compose exec -T postgres pg_dumpall -U "${DB_USER:-backup_reports}" | gzip \
    | age -r "${BACKUP_AGE_RECIPIENT}" > "${backup_dir}/${backup_name}.gz.age"
else
  echo "WARNING: BACKUP_AGE_RECIPIENT is not set, the database dump is not encrypted" >&2
  docker compose exec -T postgres pg_dumpall -U "${DB_USER:-backup_reports}" | gzip > "${backup_dir}/${backup_name}.gz"
fi

# Keep only the 3 most recent backups
cd "${backup_dir}"
ls -1t *.gz *.gz.age 2>/dev/null | tail -n +4 | xargs -r rm -f
