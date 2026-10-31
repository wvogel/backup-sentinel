#!/usr/bin/env bash
# Build the restore-helper LXC template for direction A (API-only, no SSH):
# Alpine + python3 + dnsmasq, with the probe/DHCP agent (restore_helper_agent.py)
# installed as a boot service. The agent listens on the helper's management IP;
# the app configures it and reads probe results over HTTP — no `pct exec`.
#
# Usage:
#   scripts/build-helper-template.sh [output.tar.gz]
# then upload the tarball to a `vztmpl` storage on the restore host — e.g. in the
# PVE UI (Datacenter > Storage > vztmpl > Upload) or via the API — and point
# restore_server.helper_template at the resulting volid (e.g. local:vztmpl/restore-helper.tar.gz).
#
# Requires Docker (used only to assemble the Alpine rootfs).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-restore-helper.tar.gz}"
build="$(mktemp -d)"
trap 'rm -rf "$build"' EXIT

cp "$HERE/app/services/restore_helper_agent.py" "$build/agent.py"
cat > "$build/Dockerfile" <<'DOCKER'
FROM alpine:3.20
RUN apk add --no-cache python3 dnsmasq iproute2 iputils openrc
COPY agent.py /opt/restore-helper-agent.py
RUN printf '#!/sbin/openrc-run\ncommand="/usr/bin/python3"\ncommand_args="/opt/restore-helper-agent.py"\ncommand_background=true\npidfile="/run/restore-helper.pid"\n' \
      > /etc/init.d/restore-helper \
 && chmod +x /etc/init.d/restore-helper \
 && rc-update add restore-helper default \
 && rc-update add networking boot \
 && rc-update add dnsmasq default
DOCKER

docker build -t restore-helper-template "$build"
cid="$(docker create restore-helper-template)"
docker export "$cid" | gzip > "$OUT"
docker rm "$cid" >/dev/null
echo "Built $OUT — upload it to a vztmpl storage and set restore_server.helper_template to its volid."
