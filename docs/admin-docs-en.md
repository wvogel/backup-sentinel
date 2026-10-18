# Backup Sentinel -- Admin / Deployment Documentation

## Table of Contents

1. [Requirements](#requirements)
2. [Quick Start](#quick-start)
3. [Environment Variables](#environment-variables)
4. [OAuth2-Proxy Setup](#oauth2-proxy-setup)
5. [Cluster Onboarding](#cluster-onboarding)
6. [Appearance](#appearance)
7. [Database Management](#database-management)
8. [HTTPS / Reverse Proxy](#https--reverse-proxy)
9. [GitLab CI/CD Deployment](#gitlab-cicd-deployment)
10. [Monitoring](#monitoring)
11. [Updating](#updating)

---

## Requirements

- **Docker** >= 24.0
- **Docker Compose** v2 (the `docker compose` plugin)
- A **reverse proxy** with TLS termination (e.g. Nginx Proxy Manager, Traefik, Caddy)
- An **OIDC-capable identity provider** (Keycloak, Azure Entra ID, Google, GitHub, etc.)
- A **Valkey / Redis** instance for OAuth2-Proxy session storage (can be shared)
- Outbound HTTPS connectivity to the Proxmox PVE/PBS API endpoints

### Proxmox Compatibility Matrix

Backup Sentinel is tested against the following Proxmox versions:

| Component                   | Tested versions   | Notes                                       |
|-----------------------------|-------------------|---------------------------------------------|
| Proxmox VE (PVE)            | 8.x, 9.x          | API v2 (`/api2/json`)                       |
| Proxmox Backup Server (PBS) | 3.x, 4.x          | API v2                                      |

Earlier versions may work but are not regularly tested. If you deploy Backup Sentinel against an older version, open an issue with your results.

API tokens used by Backup Sentinel (the bootstrap scripts set them up, see [Cluster Onboarding](#cluster-onboarding)):
- On PVE: user `backup-reports@pve`, token `<cluster-slug>-collector` (no privilege separation), role `PVEAuditor` on `/` plus the custom role `BackupSentinelBackupList` (`Datastore.AllocateSpace`, `VM.Backup`) on `/`. PVE has no read-only way to list backup volumes: a storage's content shows a backup only with `Datastore.Allocate` on the storage, or `Datastore.AllocateSpace` plus `VM.Backup` on the VM. The custom role takes the narrower second way — it cannot change the storage configuration or delete VM disks or ISOs, but it could start or delete backups. Backup Sentinel itself only sends read requests.
- On PBS: user `backup-reports@pbs`, token `<cluster-slug>-collector`, role `Audit` on `/` for both user and token (PBS privilege separation: the effective rights are the intersection of both). `Audit` is read-only and covers everything Backup Sentinel reads: datastores, namespaces, snapshots, usage, sync jobs, remotes and task logs. The certificate fingerprint is read from the TLS connection, not from the certificates API (that one needs `Sys.Modify`).
- Installations bootstrapped before v2.41.14 still have `PVEDatastoreAdmin` on `/storage` (PVE) or `Admin` on `/` (PBS). Run the bootstrap command from the settings again — once per cluster on a PVE node and once per PBS connection; it grants the roles above and takes the old ones back. The PBS user is shared by all clusters, so one run per PBS removes `Admin` for all of its tokens.
- PBS connection fields under Settings → Cluster: **Namespace** limits the connection to one namespace and its children (e.g. the target of a pull sync; empty = root only). **Aliases** are other host names or IPs PVE or a sync remote uses for this PBS. Backup Sentinel usually recognises a PBS itself (fingerprint, name, DNS, certificate over TLS); aliases are only needed when it cannot reach it under the other name, e.g. in a separate backup network.

---

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/wvogel/backup-sentinel.git
cd backup-sentinel

# 2. Create configuration files
cp .env.example .env
cp oauth2-proxy.env.example oauth2-proxy.env

# 3. Edit both files with your values
#    - .env              -> database password, secret key, timezone, APP_URL
#    - oauth2-proxy.env  -> OIDC client ID/secret, issuer URL, cookie secret

# 4. Make sure the external Docker networks exist
docker network create shared-npm    2>/dev/null || true
docker network create shared-valkey 2>/dev/null || true

# 5. Start the stack
docker compose up -d --build

# 6. Verify
docker compose ps
curl -s https://backup-sentinel.example.com/healthz
```

The stack consists of three containers:

| Container                | Port   | Purpose                        |
|--------------------------|--------|--------------------------------|
| `postgres`               | 5432   | Database (internal only)       |
| `backup-sentinel`        | 80     | FastAPI application            |
| `oauth2-backup-sentinel` | 4180   | OAuth2-Proxy (public-facing)   |

Your reverse proxy should forward traffic to the OAuth2-Proxy container on port **4180**.

---

## Environment Variables

### Application (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_URL` | `https://backup-sentinel.example.com` | Public URL of the application. Used in email notifications and bootstrap scripts. |
| `DB_NAME` | `backup_reports` | PostgreSQL database name. |
| `DB_USER` | `backup_reports` | PostgreSQL user. |
| `DB_PASSWORD` | `backup_reports` | PostgreSQL password. **Change this in production** -- and change `BSENTINEL_DATABASE_URL` along with it. |
| `BSENTINEL_DATABASE_URL` | `postgresql://backup_reports:backup_reports@postgres:5432/backup_reports` | Full PostgreSQL connection string. **Not** derived from `DB_*`: if you change `DB_NAME`, `DB_USER` or `DB_PASSWORD`, set this to match. Also used for an external database. |
| `BSENTINEL_SECRET_KEY` | *(empty)* | **Strongly recommended** — without it, secrets are stored unencrypted. Fernet key for encrypting secrets (SMTP password, Gotify token) in the database. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `BSENTINEL_DEFAULT_TIMEZONE` | `Europe/Berlin` | Timezone for display formatting and quiet-hour calculations. |
| `BSENTINEL_SYNC_INTERVAL_MINUTES` | `60` | Interval in minutes for the automatic background sync loop. |
| `BSENTINEL_API_TIMEOUT` | `60` | Timeout in seconds for Proxmox/PBS API requests. |
| `BSENTINEL_DEBUG` | `false` | Enable debug logging (`true` / `false`). |
| `BSENTINEL_INSECURE_SSL` | `false` | Disable SSL certificate verification for Proxmox/PBS APIs. Set to `true` if using self-signed certificates. |
| `BSENTINEL_BRAND_LOGO_LIGHT_URL` | *(empty = no logo)* | Logo URL for light theme. Also shown in the weekly report header (SVG is converted to PNG and embedded; relative URLs outside `/static/` need `APP_URL`). |
| `BSENTINEL_BRAND_LOGO_DARK_URL` | *(empty = no logo)* | Logo URL for dark theme. |
| `BSENTINEL_LOGOUT_URL` | `/oauth2/sign_out` | Logout redirect URL. |
| `FOOTER_LINKS` | *(empty)* | Footer links. Format: `Label1\|URL1,Label2\|URL2` |
| `COPYRIGHT_TEXT` | *(empty)* | Copyright text in the footer (e.g. `© 2026 Your Company`). |
| `BSENTINEL_REPORT_DIR` | `/reports` | Path for generated PDF/JSON reports. |
| `BSENTINEL_REPORT_LANGUAGE` | `de` | Server-side language (`de` or `en`) for everything generated without a browser: PDF reports, Gotify/email notifications and sync logs. The web UI follows each user's language switch instead. Any other value falls back to `de`. |
| `BSENTINEL_BACKUP_DAY_OFFSET_HOURS` | `12` | Boundary offset for the "backup day" in the sparkline. A long-running job whose last VMs finish the next morning still counts for the evening the job started. Set to `0` for wall-clock midnight. |

`BSENTINEL_DATA_DIR` (set to `/data` in the `Dockerfile`) is not read by the application; `./data` is mounted but currently unused.

### OAuth2-Proxy (`oauth2-proxy.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `OAUTH2_PROXY_PROVIDER` | `oidc` | OAuth2 provider type. Common values: `oidc`, `keycloak-oidc`, `google`, `github`. |
| `OAUTH2_PROXY_CLIENT_ID` | *(none)* | **Required.** Client ID from your identity provider. |
| `OAUTH2_PROXY_CLIENT_SECRET` | *(none)* | **Required.** Client secret from your identity provider. |
| `OAUTH2_PROXY_OIDC_ISSUER_URL` | *(none)* | **Required.** OIDC issuer URL (e.g. `https://keycloak.example.com/realms/your-realm`). |
| `OAUTH2_PROXY_REDIRECT_URL` | *(none)* | **Required.** OAuth2 callback URL: `https://<your-domain>/oauth2/callback`. |
| `OAUTH2_PROXY_COOKIE_SECRET` | *(none)* | **Required.** Random secret for session cookies. Generate with: `python -c "import secrets; print(secrets.token_hex(16))"` |
| `OAUTH2_PROXY_COOKIE_DOMAINS` | *(none)* | Your application domain (e.g. `backup-sentinel.example.com`). |
| `OAUTH2_PROXY_WHITELIST_DOMAINS` | *(none)* | Allowed redirect domains. Typically the same as `COOKIE_DOMAINS`. |
| `OAUTH2_PROXY_SESSION_STORE_TYPE` | `redis` | Session store type. Uses a shared Valkey/Redis instance. |
| `OAUTH2_PROXY_REDIS_CONNECTION_URL` | `redis://valkey:6379/0` | Redis/Valkey connection URL for session storage. |
| `OAUTH2_PROXY_SCOPE` | `openid profile email` | OIDC scopes to request. |
| `OAUTH2_PROXY_EMAIL_DOMAINS` | `*` | Allowed email domains. Use `*` to allow all. |
| `OAUTH2_PROXY_PASS_USER_HEADERS` | `true` | Pass authenticated user info as headers to the upstream. |
| `OAUTH2_PROXY_SET_XAUTHREQUEST` | `true` | Set X-Auth-Request headers. |

---

## OAuth2-Proxy Setup

Backup Sentinel delegates authentication entirely to [OAuth2-Proxy](https://oauth2-proxy.github.io/oauth2-proxy/). The proxy sits in front of the FastAPI application and handles the OIDC login flow.

### Provider Configuration

**Keycloak / Generic OIDC:**

```env
OAUTH2_PROXY_PROVIDER=oidc
OAUTH2_PROXY_OIDC_ISSUER_URL=https://keycloak.example.com/realms/your-realm
OAUTH2_PROXY_CLIENT_ID=backup-sentinel
OAUTH2_PROXY_CLIENT_SECRET=your-client-secret
OAUTH2_PROXY_REDIRECT_URL=https://backup-sentinel.example.com/oauth2/callback
```

**Azure Entra ID:**

```env
OAUTH2_PROXY_PROVIDER=oidc
OAUTH2_PROXY_OIDC_ISSUER_URL=https://login.microsoftonline.com/<tenant-id>/v2.0
OAUTH2_PROXY_CLIENT_ID=your-app-id
OAUTH2_PROXY_CLIENT_SECRET=your-client-secret
OAUTH2_PROXY_REDIRECT_URL=https://backup-sentinel.example.com/oauth2/callback
```

### Session Store

OAuth2-Proxy uses a Redis-compatible session store (Valkey) to persist sessions across container restarts. The default configuration expects a shared Valkey container accessible via the `shared-valkey` Docker network:

```env
OAUTH2_PROXY_SESSION_STORE_TYPE=redis
OAUTH2_PROXY_REDIS_CONNECTION_URL=redis://valkey:6379/0
```

### Bypassed Routes

These routes are excluded from authentication (`OAUTH2_PROXY_SKIP_AUTH_ROUTES` in `docker-compose.yml` and `oauth2-proxy.env.example`):

- `GET /healthz` and `GET /metrics` -- for monitoring (see [Monitoring](#monitoring)); restrict them at the reverse proxy if needed.
- `GET /bootstrap/proxmox-agent.sh` and `GET /bootstrap/pbs-agent.sh` -- the bootstrap scripts.
- `POST /api/bootstrap/finalize` and `GET /api/bootstrap/pbs-finalize` -- so that Proxmox nodes can call back without a session. These calls are checked against the cluster's enrollment secret instead.

---

## Cluster Onboarding

### PVE Cluster

1. Open the Settings page in the Backup Sentinel UI.
2. Enter a name and the PVE API URL, then click **Prepare Cluster**.
3. Copy the displayed bootstrap command and run it on **any node** of the Proxmox cluster:

```bash
bash <(curl -fsSL https://backup-sentinel.example.com/bootstrap/proxmox-agent.sh) \
  --server-url https://backup-sentinel.example.com \
  --cluster-slug pve-prod \
  --enrollment-secret <one-time-secret>
```

The script will:
- Create the user `backup-reports@pve` (if missing) and the API token `<cluster-slug>-collector` (an existing one is replaced).
- Assign the role `PVEAuditor` on `/` and the custom role `BackupSentinelBackupList` (`Datastore.AllocateSpace`, `VM.Backup`) on `/` (see [Requirements](#requirements) for why).
- Send the token back to the Backup Sentinel API to complete registration.

4. Once the bootstrap completes, the cluster appears in the UI and the first sync begins automatically.

### PBS Connection

1. On the Settings page, find the cluster and click **Add PBS**.
2. Copy the displayed bootstrap command. There is nothing to enter: the script detects name and API URL itself.
3. Run it on the PBS host as root:

```bash
bash <(curl -fsSL https://backup-sentinel.example.com/bootstrap/pbs-agent.sh) \
  --server-url https://backup-sentinel.example.com \
  --cluster-slug pve-prod \
  --enrollment-secret <one-time-secret>
```

The script creates the user `backup-reports@pbs` and the API token `<cluster-slug>-collector`, grants both the read-only role `Audit` on `/` (see [Requirements](#requirements)) and registers the connection. The PBS name is the short host name, the API URL `https://<fqdn>:8007`. Optional flags:

- `--api-url URL` -- use this API URL instead of `https://<fqdn>:8007`, e.g. when Backup Sentinel reaches the PBS under another name.
- `--namespace NS` -- limit the connection to this namespace and its children (e.g. the target of a pull sync); without it only the root namespace is read. A re-run without `--namespace` keeps the namespace set in the UI.

---

## Appearance

The default is the **smile eyes** design: Dark Blue `#141450` (topbar, text), Neon Yellow `#FFFF00` (the one main action per page, always with Dark Blue text), Street Blue `#4A7EB5` (running backups), Light Beige `#F8F5F2` (background), Beige `#EFE7DB` (borders) and Light Sage `#D8DDCE` (calm hints). Dark mode is derived from Dark Blue.

Under **Settings → Appearance** you can change:

- **Preset:** *smile eyes* or *Neutral* (the former cool blue-grey).
- **Four base colours:** primary, accent (main action), background and running backups. All other tones (soft surfaces, borders, dark mode) are derived. A preview shows the result before saving.
- **Contrast check:** saving checks WCAG contrast (text ≥ 4.5:1, running backups ≥ 3:1). Poor readability shows a warning with the measured ratio; the setting is saved anyway.
- **Reset to preset** removes custom colours.

Changes are recorded in the settings audit. The PDF report uses the primary, accent and running colours.

**Font:** the device's system font (macOS/iOS: San Francisco, Windows: Segoe UI, Android: Roboto). No font is downloaded, so nothing shifts while the page loads.

---

## Database Management

### Backup

The PostgreSQL data is stored in the `./postgres` volume. To create a logical backup:

```bash
docker compose exec postgres pg_dump -U backup_reports backup_reports > backup_$(date +%Y%m%d).sql
```

Alternatively run `./backup.sh` from the repository root (e.g. as a daily cron job). It reads `DB_USER` from `.env`, writes a `pg_dumpall` of the whole PostgreSQL instance to `./backup/<YYYY-MM-DD>.gz` and keeps the three most recent dumps. As a `pg_dumpall` it contains all databases and roles; restore it into a fresh instance (see [Reset](#reset), with the app stopped) via `gunzip -c backup/<date>.gz | docker compose exec -T postgres psql -U backup_reports postgres`.

### Restore

```bash
# Stop the app to prevent writes
docker compose stop backup-sentinel

# Restore
docker compose exec -T postgres psql -U backup_reports backup_reports < backup_20260401.sql

# Restart
docker compose up -d backup-sentinel
```

### Reset

To start with a fresh database, remove the volume and restart:

```bash
docker compose down
rm -rf ./postgres
docker compose up -d
```

The application will automatically create all tables on startup via `db.init_db()`.

---

## HTTPS / Reverse Proxy

Backup Sentinel expects a reverse proxy to handle TLS termination. The OAuth2-Proxy container listens on port **4180** and should be the public-facing endpoint.

### Nginx Proxy Manager

1. Add a new proxy host pointing to the `oauth2-backup-sentinel` container on port `4180`.
2. Enable SSL with a Let's Encrypt certificate.
3. Ensure the proxy host is on the `shared-npm` Docker network.

### Traefik (labels)

```yaml
services:
  oauth2-backup-sentinel:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.bsentinel.rule=Host(`backup-sentinel.example.com`)"
      - "traefik.http.routers.bsentinel.tls.certresolver=letsencrypt"
      - "traefik.http.services.bsentinel.loadbalancer.server.port=4180"
```

### Plain Nginx (manual)

```nginx
server {
    listen 443 ssl http2;
    server_name backup-sentinel.example.com;

    ssl_certificate     /etc/letsencrypt/live/backup-sentinel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/backup-sentinel.example.com/privkey.pem;

    # OAuth2-Proxy trusts these — must be set by the reverse proxy
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host  $host;

    # Don't buffer long-running sync log streams
    proxy_buffering off;
    proxy_read_timeout 300s;

    location / {
        proxy_pass http://127.0.0.1:4180;
    }
}
```

### Important Headers

OAuth2-Proxy sets `OAUTH2_PROXY_REVERSE_PROXY=true`, so it trusts `X-Forwarded-*` headers from the reverse proxy. Make sure your reverse proxy sets:

- `X-Forwarded-For`
- `X-Forwarded-Proto` (must be `https`)
- `X-Forwarded-Host`

---

## GitLab CI/CD Deployment

The repository includes a `.gitlab-ci.yml` with three stages.

### Pipeline Stages

| Stage | Job |
|-------|-----|
| `lint` | `ruff check` and `ruff format --check` on `app`, `tests` and `scripts`. |
| `test` | `pytest tests/` against a `postgres:18` service. `BSENTINEL_DB_TESTS=1` is set, so the database tests (which truncate tables) run as well. |
| `deploy` | Only on `main`: sends a `POST` to the URL of every CI/CD variable whose name starts with `DEPLOY_WEBHOOK`. |

### CI/CD Variables

Configure one or more webhook URLs in **GitLab > Settings > CI/CD > Variables**:

| Variable | Description |
|----------|-------------|
| `DEPLOY_WEBHOOK` (or `DEPLOY_WEBHOOK_<NAME>`) | URL that triggers the deploy on a server. Each variable is called once; a failing call fails the job. |

There is no SSH connection and no `SSH_KEY`, `DEPLOY_USER` or `DEPLOY_HOST`. What the webhook does on the server is up to you; typically it runs `deploy.sh` (see [Updating](#updating)).

### Version

The version is set by hand in `APP_VERSION` in `app/config.py` (format `vMAJOR.MINOR.PATCH`, e.g. `v2.41.12`) and matches `CHANGELOG.md` and the git tags. It is not derived from the commit.

### GitHub Actions

On GitHub, `.github/workflows/` contains `ci.yml` (lint and tests on push and pull requests to `main`), `codeql.yml` (code scanning) and `release.yml` (creates a GitHub Release with the notes from `CHANGELOG.md` when a `v*.*.*` tag is pushed).

---

## Monitoring

### Health Endpoint

The application exposes a `/healthz` endpoint that checks database connectivity:

```bash
curl -s https://backup-sentinel.example.com/healthz
```

**Responses:**

- `200 OK` with `{"status": "ok", "version": "v2.41.12"}` -- Application and database are healthy; `version` is the running release (handy to confirm a deploy).
- `503 Service Unavailable` with `{"status": "degraded", "version": "…", "database": "<error>"}` -- Database is unreachable.

### Docker Health Check

The `backup-sentinel` container has a built-in health check that polls `/healthz` every 5 seconds. Use `docker compose ps` to verify the container is `healthy`.

### Prometheus Metrics

Backup Sentinel exposes Prometheus-compatible metrics at `/metrics` (no authentication required). The sync metrics cover the PVE sync only; PBS syncs are not included.

Exposed metrics:

- `backup_sentinel_cluster_sync_ok{cluster="..."}` -- Whether last sync succeeded (1/0)
- `backup_sentinel_cluster_sync_age_seconds{cluster="..."}` -- Seconds since last sync
- `backup_sentinel_cluster_sync_failures_total{cluster="..."}` -- Consecutive failed PVE syncs; reset to 0 on the next successful sync (despite the name, not a monotonic counter)
- `backup_sentinel_vm_backup_severity_count{cluster="...",severity="..."}` -- VMs per severity
- `backup_sentinel_unencrypted_backups_count{cluster="..."}` -- VMs with unencrypted backups
- `backup_sentinel_restore_overdue_count{cluster="..."}` -- VMs with overdue restore tests

Add to your Prometheus `scrape_configs`:

```yaml
- job_name: backup-sentinel
  static_configs:
    - targets: ['backup-sentinel.example.com']
  metrics_path: /metrics
  scheme: https
```

### Logging

By default, the application logs at `INFO` level. Set `BSENTINEL_DEBUG=true` to enable `DEBUG` level logging. Logs are written to stdout and can be viewed with:

```bash
docker compose logs -f backup-sentinel
```

The OAuth2-Proxy container uses Docker's local logging driver with a 10 MB / 3 file rotation.

---

## Updating

To update Backup Sentinel to the latest version:

```bash
cd /path/to/backup-sentinel
git pull
docker compose up -d --build
```

Or use the included deploy script (as root):

```bash
DEPLOY_PATH=/path/to/backup-sentinel ./deploy.sh
```

`DEPLOY_PATH` must be set. The script runs `git fetch` and `git reset --hard origin/main` -- **local changes to tracked files are discarded** -- then `docker compose down`, `docker compose build --no-cache` and `docker compose up -d`. Files ignored by git such as `.env` stay.

The script also hands `data/` and `reports/` to the user the application runs as inside the container (UID/GID taken from the built image). Without that, e.g. the monthly report cannot be written ("Permission denied"). This needs root; otherwise the script prints a warning. When updating manually, fix the ownership once yourself: `chown -R 1000:1000 data reports`.

The application applies database migrations automatically on startup -- no manual migration steps are needed.

### Rollback

If an update causes issues:

```bash
git log --oneline -5          # find the previous commit
git checkout <commit-sha>     # switch to that version
docker compose up -d --build  # rebuild with the old version
```

### Version Check

The current build version is shown in the UI footer and can also be checked via the health endpoint response or container logs at startup. The format is `vMAJOR.MINOR.PATCH` (e.g. `v2.41.10`), as in `CHANGELOG.md` and the git tags; `/healthz` returns it as `version`.
