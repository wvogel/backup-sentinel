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

## Documentation screenshots and diagram

The screenshots in `docs/` and the README use neutral demo data: four clusters
(`pve-berlin`, `pve-cologne`, `pve-frankfurt`, `pve-munich`) built from the seed.

```bash
./scripts/dev.sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec -T postgres \
  psql -U backup_reports -d backup_reports < scripts/docs_demo_data.sql
docker compose -f docker-compose.yml -f docker-compose.dev.yml exec -T backup-sentinel \
  python -c "from app.services import topology_store; topology_store.refresh_all()"
node scripts/docs_screenshot.mjs http://localhost:8081/ docs/screenshot-overview.png 1280 full en
node scripts/docs_screenshot.mjs http://localhost:8081/clusters/pve-berlin docs/screenshot-cluster.png 1280 "#vm-table" en
# German: same with -de file names and "de"
```

The architecture diagram is generated: `python scripts/architecture_diagram.py`
writes `docs/architecture-{en,de}.svg`; the PNGs are 2x renders of those (e.g.
an HTML page with the SVG as `<img>`, captured with `docs_screenshot.mjs` at 960 × 470).
