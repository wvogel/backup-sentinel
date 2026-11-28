#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  pbs-bootstrap.sh --server-url URL --cluster-slug SLUG --enrollment-secret SECRET \
                   [--api-url URL] [--namespace NS]

Run this script on a Proxmox Backup Server node (as root).
--namespace limits the cluster to that namespace and its children, e.g. the
target of a pull sync; without it only the root namespace is read.
The user and token receive read-only access: role Audit on / and role
DatastoreReader on /datastore (to read the snapshots' index files).
EOF
}

SERVER_URL=""
CLUSTER_SLUG=""
ENROLLMENT_SECRET=""
API_URL_OVERRIDE=""
NAMESPACE=""
NAMESPACE_SET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-url)        SERVER_URL="$2";         shift 2 ;;
    --cluster-slug)      CLUSTER_SLUG="$2";       shift 2 ;;
    --enrollment-secret) ENROLLMENT_SECRET="$2";  shift 2 ;;
    --api-url)           API_URL_OVERRIDE="$2";   shift 2 ;;
    --namespace)         NAMESPACE="$2"; NAMESPACE_SET=1; shift 2 ;;
    *) usage; exit 1 ;;
  esac
done

if [[ -z "$SERVER_URL" || -z "$CLUSTER_SLUG" || -z "$ENROLLMENT_SECRET" ]]; then
  usage
  exit 1
fi

if ! command -v proxmox-backup-manager >/dev/null 2>&1; then
  echo "proxmox-backup-manager not found. Run this on a Proxmox Backup Server node." >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl not found." >&2
  exit 1
fi

API_USER="backup-reports@pbs"
TOKEN_NAME="${CLUSTER_SLUG}-collector"
PBS_HOST="$(hostname -f)"
if [[ -n "$API_URL_OVERRIDE" ]]; then
  PBS_API_URL="$API_URL_OVERRIDE"
else
  PBS_API_URL="https://${PBS_HOST}:8007"
fi

# ── Create PBS user ──────────────────────────────────────────────────────────
USER_OUT="$(proxmox-backup-manager user create "$API_USER" \
  --comment "Backup Sentinel collector" 2>&1 || true)"
if [[ -n "$USER_OUT" ]] && [[ "$USER_OUT" != *"already exists"* ]] && [[ "$USER_OUT" != *"duplicate"* ]]; then
  printf '%s\n' "$USER_OUT" >&2
  exit 1
fi

# ── Create API token ─────────────────────────────────────────────────────────
# Remove existing token first (idempotent re-run)
proxmox-backup-manager user delete-token "$API_USER" "$TOKEN_NAME" 2>/dev/null || true

TOKEN_OUTPUT=""
TOKEN_SECRET=""
TOKEN_OUTPUT="$(proxmox-backup-manager user generate-token "$API_USER" "$TOKEN_NAME" 2>&1 || true)"

# Output looks like:  Result: { "tokenid": "...", "value": "UUID" }
TOKEN_SECRET="$(printf '%s' "$TOKEN_OUTPUT" | \
  sed -n 's/.*"value"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"

if [[ -z "$TOKEN_SECRET" ]]; then
  echo "Failed to extract token secret from proxmox-backup-manager output:" >&2
  printf '%s\n' "$TOKEN_OUTPUT" >&2
  exit 1
fi

TOKEN_ID="${API_USER}!${TOKEN_NAME}"

# ── Grant read-only access for user AND token ────────────────────────────────
# PBS uses privilege separation: effective perms = user perms ∩ token perms
# Both need the role, otherwise the intersection is empty. Audit covers all
# that Backup Sentinel reads: datastores, namespaces, snapshots, usage, sync
# jobs, remotes and task logs. DatastoreReader on the datastores adds the one
# thing Audit does not allow: downloading a snapshot's index files, from which
# the space per guest is counted (it also allows downloading backup contents).
# Earlier versions granted Admin; a re-run takes it back.
for AUTH_ID in "${API_USER}" "${TOKEN_ID}"; do
  proxmox-backup-manager acl update / Audit \
    --auth-id "${AUTH_ID}" --propagate 1
  proxmox-backup-manager acl update /datastore DatastoreReader \
    --auth-id "${AUTH_ID}" --propagate 1
  proxmox-backup-manager acl update / Admin \
    --auth-id "${AUTH_ID}" --delete true >/dev/null 2>&1 || true
done

# ── TLS fingerprint ──────────────────────────────────────────────────────────
CERT_FILE="/etc/proxmox-backup/proxy.pem"
if [[ ! -f "$CERT_FILE" ]]; then
  CERT_FILE="/etc/proxmox-backup/tls/server.pem"
fi
if [[ ! -f "$CERT_FILE" ]]; then
  CERT_FILE="/etc/proxmox-backup/ssl/pve-ssl.pem"
fi
FINGERPRINT=""
if [[ -f "$CERT_FILE" ]]; then
  FINGERPRINT="$(openssl x509 -in "$CERT_FILE" -noout -fingerprint -sha256 | cut -d= -f2)"
fi

# ── PBS display name ─────────────────────────────────────────────────────────
PBS_NAME="$(hostname -s)"

# ── Register with backup-sentinel ─────────────────────────────────────────────
# Re-runs without --namespace keep the namespace configured in the UI.
# Sent as a JSON body on stdin: the secrets appear neither in the URL (proxies
# log it) nor in the process list.
json_str() {
  local s="${1//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}
NAMESPACE_JSON=""
if [[ "$NAMESPACE_SET" == 1 ]]; then
  NAMESPACE_JSON=", \"namespace\": $(json_str "$NAMESPACE")"
fi
if ! curl -fsSL -X POST "${SERVER_URL}/api/bootstrap/pbs-finalize" \
  -H "Content-Type: application/json" \
  --data-binary @- <<JSON
{
  "cluster_slug": $(json_str "$CLUSTER_SLUG"),
  "enrollment_secret": $(json_str "$ENROLLMENT_SECRET"),
  "name": $(json_str "$PBS_NAME"),
  "api_url": $(json_str "$PBS_API_URL"),
  "api_token_id": $(json_str "$TOKEN_ID"),
  "api_token_secret": $(json_str "$TOKEN_SECRET"),
  "fingerprint": $(json_str "$FINGERPRINT")${NAMESPACE_JSON}
}
JSON
then
  echo "Registration refused. The onboarding command is valid for 24 hours:" >&2
  echo "create a new one in Backup Sentinel under Settings and run it again." >&2
  exit 1
fi

echo ""
echo "PBS ${PBS_NAME} registered for cluster '${CLUSTER_SLUG}'."
