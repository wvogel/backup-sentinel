# Local dev & browser testing

A lightweight harness for running the app locally to test changes in a real
browser. It runs Postgres + the app via Docker, **without** the oauth2-proxy
login wall, and seeds sample data.

> These files are dev-only and never affect the server deploy: the deploy runs
> plain `docker compose` (which loads only `docker-compose.yml`), while the dev
> overlay `docker-compose.dev.yml` is applied solely via an explicit `-f`.

## Prerequisites

- Docker Desktop running (`open -a Docker`).
- No local Python/Node needed — dependencies are installed inside the image.

## Start

```bash
./scripts/dev.sh          # build + start + wait for health + seed
```

The app is then at <http://localhost:8081/clusters/pve-aboe-muc>.

- `./scripts/dev.sh seed` — re-seed sample data only.
- `./scripts/dev.sh down` — stop everything and drop the DB volume.

## Sample data

`scripts/dev_seed.sql` creates one cluster (`pve-aboe-muc`) with long node names
(for column-wrap testing) and restore policies that exercise the warning logic:
a **yearly** policy in `WARNING` (tested 340 of 365 days ago) and a **quarterly**
one in `WARNING` (80 of 90 days ago), plus several never-tested criticals and 14
days of backup events for the sparkline.

## Screenshots (Claude preview)

The app holds port 8081, so the Claude preview tool can't own it directly.
`.claude/launch.json` instead launches `scripts/dev_preview_proxy.py`, a stdlib
reverse proxy on the preview-assigned port that forwards to 8081 and rewrites
`/` to the seeded cluster page — so a screenshot of `/` lands on the table.
Override the landing path with `PREVIEW_ROOT` if needed.

Set `PREVIEW_FORCE_EMAIL` to inject an `X-Forwarded-Email` header (the proxy
otherwise passes none, so the app shows the "logged in" fallback). Useful for
testing the header with a realistically long username, e.g.
`PREVIEW_FORCE_EMAIL=first.last@example.com`.
