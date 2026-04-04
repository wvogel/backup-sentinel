#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  proxmox-bootstrap.sh --server-url URL --cluster-slug SLUG --enrollment-secret SECRET
EOF
}

SERVER_URL=""
CLUSTER_SLUG=""
ENROLLMENT_SECRET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-url)
      SERVER_URL="$2"
      shift 2
      ;;
    --cluster-slug)
      CLUSTER_SLUG="$2"
      shift 2
      ;;
    --enrollment-secret)
      ENROLLMENT_SECRET="$2"
      shift 2
      ;;
    *)
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$SERVER_URL" || -z "$CLUSTER_SLUG" || -z "$ENROLLMENT_SECRET" ]]; then
  usage
  exit 1
fi

if ! command -v pveum >/dev/null 2>&1; then
  echo "pveum not found. Run this on a Proxmox VE node." >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl not found." >&2
  exit 1
fi

API_USER="backup-reports@pve"
TOKEN_NAME="${CLUSTER_SLUG}-collector"
PROXMOX_NODE="$(hostname -s)"
FINGERPRINT="$(openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256 | cut -d= -f2)"

USER_ADD_OUTPUT="$(pveum user add "$API_USER" --comment "Backup Sentinel collector" 2>&1 || true)"
if [[ -n "$USER_ADD_OUTPUT" ]] && [[ "$USER_ADD_OUTPUT" != *"already exists"* ]]; then
  printf '%s\n' "$USER_ADD_OUTPUT" >&2
  exit 1
fi

pveum aclmod / -user "$API_USER" -role PVEAuditor
pveum aclmod /storage -user "$API_USER" -role PVEDatastoreAdmin

TOKEN_REMOVE_OUTPUT="$(pveum user token remove "$API_USER" "$TOKEN_NAME" 2>&1 || true)"
if [[ -n "$TOKEN_REMOVE_OUTPUT" ]] && [[ "$TOKEN_REMOVE_OUTPUT" != *"does not exist"* ]] && [[ "$TOKEN_REMOVE_OUTPUT" != *"not exist"* ]] && [[ "$TOKEN_REMOVE_OUTPUT" != *"no such token"* ]]; then
  printf '%s\n' "$TOKEN_REMOVE_OUTPUT" >&2
  exit 1
fi

TOKEN_OUTPUT=""
TOKEN_VALUE=""
if TOKEN_OUTPUT="$(pveum user token add "$API_USER" "$TOKEN_NAME" --privsep 0 --output-format json 2>/dev/null)"; then
  if command -v python3 >/dev/null 2>&1; then
    TOKEN_VALUE="$(printf '%s' "$TOKEN_OUTPUT" | python3 -c 'import json, sys; print(json.load(sys.stdin)["value"])')"
  else
    TOKEN_VALUE="$(printf '%s\n' "$TOKEN_OUTPUT" | sed -n 's/.*"value"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  fi
else
  TOKEN_OUTPUT="$(pveum user token add "$API_USER" "$TOKEN_NAME" --privsep 0 2>&1 || true)"
  TOKEN_VALUE="$(printf '%s\n' "$TOKEN_OUTPUT" | awk -F'│' '/value/ {gsub(/^[[:space:]]+|[[:space:]]+$/, "", $3); if ($3 != "") {print $3; exit}}')"
fi

if [[ -z "$TOKEN_VALUE" ]]; then
  echo "Failed to extract token secret from pveum output:" >&2
  printf '%s\n' "$TOKEN_OUTPUT" >&2
  exit 1
fi

curl -fsSL -X POST "${SERVER_URL}/api/bootstrap/finalize" \
  -H "Content-Type: application/json" \
  -d "$(cat <<EOF
{
  "cluster_slug": "${CLUSTER_SLUG}",
  "enrollment_secret": "${ENROLLMENT_SECRET}",
  "proxmox_node": "${PROXMOX_NODE}",
  "api_user": "${API_USER}",
  "token_id": "${TOKEN_NAME}",
  "token_secret": "${TOKEN_VALUE}",
  "fingerprint": "${FINGERPRINT}"
}
EOF
)"

echo "Cluster ${CLUSTER_SLUG} registered successfully."
