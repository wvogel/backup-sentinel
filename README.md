# Backup Sentinel

[![CI](https://github.com/wvogel/backup-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/wvogel/backup-sentinel/actions/workflows/ci.yml)
[![CodeQL](https://github.com/wvogel/backup-sentinel/actions/workflows/codeql.yml/badge.svg)](https://github.com/wvogel/backup-sentinel/actions/workflows/codeql.yml)
[![Release](https://img.shields.io/github/v/release/wvogel/backup-sentinel)](https://github.com/wvogel/backup-sentinel/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.116-009688.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL 18](https://img.shields.io/badge/PostgreSQL-18-336791.svg)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED.svg)](https://www.docker.com/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)
[![Last commit](https://img.shields.io/github/last-commit/wvogel/backup-sentinel)](https://github.com/wvogel/backup-sentinel/commits/main)
[![Stars](https://img.shields.io/github/stars/wvogel/backup-sentinel?style=social)](https://github.com/wvogel/backup-sentinel/stargazers)

Compliance-focused monitoring for Proxmox backup infrastructure. Track backup status, encryption, verification, and document restore tests across PVE clusters and PBS instances — aligned with NIST CSF 2.0 and NIS2 requirements.

![Screenshot](docs/screenshot-overview.png)

![Architecture](docs/architecture-en.svg)

## Features

- **Dashboard** — Health bar, KPI cards (restore coverage, overdue tests), cluster overview with sync status
- **Cluster Detail** — Per-VM backup status with 30-day sparklines, backup policy overrides, encryption audit
- **Restore Test Documentation** — Record and track recovery tests (full, partial, file-based) for compliance evidence
- **Monthly Reports** — Auto-generated PDF/JSON reports with per-cluster and per-VM breakdown, archive for audit
- **Notifications** — Gotify push and SMTP email alerts for failed syncs, critical backups, and anomalies
- **Multi-cluster** — Monitor multiple PVE clusters and PBS instances from a single dashboard
- **Governance** — NIST CSF 2.0 / NIS2 mapping (Identify, Protect, Detect, Recover)
- **Internationalization** — Full German and English UI with one-click language switching
- **Theme** — Light, dark, and auto (system) modes

## Requirements

- Docker & Docker Compose
- A reverse proxy such as [Nginx Proxy Manager](https://github.com/NginxProxyManager/nginx-proxy-manager)
- [OAuth2-Proxy](https://github.com/oauth2-proxy/oauth2-proxy) for authentication (included in `docker-compose.yml`)
- An OIDC-compatible identity provider (Keycloak, Azure Entra ID, Google, etc.)

## Quick Start

```bash
# Clone and configure
git clone https://github.com/wvogel/backup-sentinel.git
cd backup-sentinel
cp .env.example .env
cp oauth2-proxy.env.example oauth2-proxy.env

# Edit .env and oauth2-proxy.env with your values
# Then start the stack
docker compose up -d
```

The app will be available behind the OAuth2-Proxy on port 4180.

## Configuration

See [`.env.example`](.env.example) for all available environment variables.

| Variable | Description | Default |
|----------|-------------|---------|
| `APP_URL` | Public URL of the application | `https://backup-sentinel.example.com` |
| `DB_NAME` | PostgreSQL database name | `backup_reports` |
| `DB_USER` | PostgreSQL user | `backup_reports` |
| `DB_PASSWORD` | PostgreSQL password | `backup_reports` |
| `BSENTINEL_SECRET_KEY` | Fernet key for encrypting secrets | `changeme` |
| `BSENTINEL_DEFAULT_TIMEZONE` | Timezone for display | `Europe/Berlin` |
| `BSENTINEL_SYNC_INTERVAL_MINUTES` | Auto-sync interval | `60` |

For OAuth2-Proxy configuration, see [`oauth2-proxy.env.example`](oauth2-proxy.env.example).

## Documentation

- [User Guide (English)](docs/user-docs-en.md) · [Benutzerhandbuch (Deutsch)](docs/user-docs-de.md)
- [Admin Guide (English)](docs/admin-docs-en.md) · [Admin-Handbuch (Deutsch)](docs/admin-docs-de.md)
- [Architecture (English)](docs/architecture-en.svg) · [Architektur (Deutsch)](docs/architecture-de.svg)

## Deployment

### GitLab CI/CD

Set these variables in your GitLab project:

| Variable | Description |
|----------|-------------|
| `DEPLOY_USER` | SSH user for deployment |
| `DEPLOY_HOST` | Target server hostname/IP |
| `DEPLOY_PATH` | Path on the server |

All application secrets (`DB_PASSWORD`, `BSENTINEL_SECRET_KEY`, OAuth2 credentials) are configured in `.env` and `oauth2-proxy.env` directly on the server — they are never stored in CI/CD or the repository.

## Project Structure

```
backup-sentinel/
├── app/                  # FastAPI application
│   ├── i18n/             # Translations (en.json, de.json)
│   ├── web/              # Route handlers
│   └── ...
├── templates/            # Jinja2 HTML templates
├── static/               # CSS, JS, favicon
├── scripts/              # Bootstrap shell scripts
├── docs/                 # Documentation and architecture diagrams
├── docker-compose.yml    # Container orchestration
├── Dockerfile            # Application container
├── .env.example          # Environment variable template
└── oauth2-proxy.env.example  # OAuth2-Proxy configuration template
```

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run locally (requires PostgreSQL)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## License

[MIT](LICENSE) — Copyright (c) 2026 Backup Sentinel Contributors
