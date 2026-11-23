#!/usr/bin/env bash
# Deploys the newest release tag on origin/main. Run on the server by the deploy
# webhook; this copy on disk is the one from the release deployed before, so it
# is the checked-in, already-running version that decides what comes next.
set -euo pipefail

cd "$DEPLOY_PATH"
git fetch --force --tags origin main

# Only releases are deployed: the newest vX.Y.Z tag reachable from main. A
# commit pushed to main without a release tag changes nothing here.
tag="$(git describe --tags --abbrev=0 --match 'v[0-9]*' origin/main)"

# Signed releases: once the file with the allowed signers exists on this server
# (outside the repository), a release is deployed only if its tag carries a
# valid signature of one of them. Checked BEFORE anything of the new release is
# checked out or run. Format: "<e-mail> ssh-ed25519 AAAA..." (git's
# gpg.ssh.allowedSignersFile); GPG-signed tags are checked against the keyring.
signers="${DEPLOY_ALLOWED_SIGNERS:-/etc/backup-sentinel/allowed_signers}"
if [ -f "$signers" ]; then
  if ! git -c gpg.ssh.allowedSignersFile="$signers" verify-tag "$tag"; then
    echo "REFUSED: release $tag has no valid signature of an allowed signer ($signers)" >&2
    exit 1
  fi
  echo "Signature of $tag verified"
else
  echo "NOTE: $signers not found, the signature of $tag is not checked" >&2
fi

git reset --hard "$tag"

# Build first, swap afterwards: a failed build leaves the running version up.
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

docker compose up -d --remove-orphans
echo "Deployed $tag"
