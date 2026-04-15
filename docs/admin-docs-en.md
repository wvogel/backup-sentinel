# Backup Sentinel -- Admin / Deployment Documentation

## Table of Contents

1. [Requirements](#requirements)
2. [Quick Start](#quick-start)
3. [Environment Variables](#environment-variables)
4. [OAuth2-Proxy Setup](#oauth2-proxy-setup)
5. [Cluster Onboarding](#cluster-onboarding)
6. [Database Management](#database-management)
7. [HTTPS / Reverse Proxy](#https--reverse-proxy)
8. [GitLab CI/CD Deployment](#gitlab-cicd-deployment)
9. [Monitoring](#monitoring)
10. [Updating](#updating)

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

API tokens used by Backup Sentinel require:
- On PVE: `Sys.Audit` on `/` and `VM.Audit` on `/vms` (read-only inventory) plus `Datastore.Audit` for backup storage discovery
- On PBS: `Datastore.Audit` on the datastore(s) to be monitored

---

## Quick Start

```bash
# 1. Clone the repository
git clone https://git.example.com/infra/backup-sentinel.git
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
| `DB_PASSWORD` | `backup_reports` | PostgreSQL password. **Change this in production.** |
| `BSENTINEL_DATABASE_URL` | *(derived from DB_*)* | Full PostgreSQL connection string. Override to use an external database. |
| `BSENTINEL_SECRET_KEY` | *(none)* | **Required.** Fernet key for encrypting secrets (SMTP password, Gotify token) in the database. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `BSENTINEL_DEFAULT_TIMEZONE` | `Europe/Berlin` | Timezone for display formatting and quiet-hour calculations. |
| `BSENTINEL_SYNC_INTERVAL_MINUTES` | `60` | Interval in minutes for the automatic background sync loop. |
| `BSENTINEL_API_TIMEOUT` | `60` | Timeout in seconds for Proxmox/PBS API requests. |
| `BSENTINEL_DEBUG` | `false` | Enable debug logging (`true` / `false`). |
| `BSENTINEL_INSECURE_SSL` | `false` | Disable SSL certificate verification for Proxmox/PBS APIs. Set to `true` if using self-signed certificates. |
| `BSENTINEL_BRAND_LOGO_LIGHT_URL` | `/static/logo.svg` | Logo URL for light theme. |
| `BSENTINEL_BRAND_LOGO_DARK_URL` | `/static/logo.svg` | Logo URL for dark theme. |
| `BSENTINEL_LOGOUT_URL` | `/oauth2/sign_out` | Logout redirect URL. |
| `FOOTER_LINKS` | *(empty)* | Footer links. Format: `Label1\|URL1,Label2\|URL2` |
| `COPYRIGHT_TEXT` | *(empty)* | Copyright text in the footer (e.g. `© 2026 Your Company`). |
| `BSENTINEL_DATA_DIR` | `/data` | Path for persistent application data. |
| `BSENTINEL_REPORT_DIR` | `/reports` | Path for generated PDF/JSON reports. |
| `BSENTINEL_REPORT_LANGUAGE` | `de` | Language for generated PDF reports (`de` or `en`). Any other value falls back to `de`. |
| `BSENTINEL_BACKUP_DAY_OFFSET_HOURS` | `6` | Boundary offset for the "backup day" in the sparkline. A job finishing at 00:30 counts for the previous evening's run. Set to `0` for wall-clock midnight. |

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

Bootstrap endpoints are excluded from authentication so that Proxmox nodes can call back without a session:

- `GET /bootstrap/proxmox-agent.sh`
- `GET /bootstrap/pbs-agent.sh`
- `POST /api/bootstrap/finalize`
- `POST /api/bootstrap/pbs-finalize`

These routes are secured by a one-time enrollment secret instead.

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
- Create a dedicated PVE API token (`backup-sentinel@pve!backup-sentinel`).
- Assign minimal read-only permissions.
- Send the token back to the Backup Sentinel API to complete registration.

4. Once the bootstrap completes, the cluster appears in the UI and the first sync begins automatically.

### PBS Connection

1. On the Settings page, find the cluster and click **Add PBS**.
2. Enter a name and the PBS API URL.
3. Run the displayed bootstrap command on the PBS host:

```bash
bash <(curl -fsSL https://backup-sentinel.example.com/bootstrap/pbs-agent.sh) \
  --server-url https://backup-sentinel.example.com \
  --cluster-slug pve-prod \
  --enrollment-secret <one-time-secret>
```

The script creates a PBS API token with audit/read permissions and registers the connection.

---

## Database Management

### Backup

The PostgreSQL data is stored in the `./postgres` volume. To create a logical backup:

```bash
docker compose exec postgres pg_dump -U backup_reports backup_reports > backup_$(date +%Y%m%d).sql
```

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

The repository includes a `.gitlab-ci.yml` that deploys to a server on every push to `main`.

### Required CI/CD Variables

Configure these three variables in **GitLab > Settings > CI/CD > Variables**:

| Variable | Description |
|----------|-------------|
| `SSH_KEY` | Private SSH key (ed25519) for the deployment user. Type: **File** or **Variable**. |
| `DEPLOY_HOST` | Target server hostname or IP. |
| `DEPLOY_PATH` | Absolute path to the project on the server (e.g. `/root/backup-sentinel`). |

### Pipeline Behavior

On each push to `main`, the pipeline:

1. Connects to the server via SSH.
2. Pulls the latest code with `git pull`.
3. Runs `docker compose down && docker compose up -d --build`.

The build version is derived from the git commit SHA (format: `1.2.<short-sha>`).

---

## Monitoring

### Health Endpoint

The application exposes a `/healthz` endpoint that checks database connectivity:

```bash
curl -s https://backup-sentinel.example.com/healthz
```

**Responses:**

- `200 OK` with `{"status": "ok"}` -- Application and database are healthy.
- `503 Service Unavailable` with `{"status": "degraded", "database": "<error>"}` -- Database is unreachable.

### Docker Health Check

The `backup-sentinel` container has a built-in health check that polls `/healthz` every 5 seconds. Use `docker compose ps` to verify the container is `healthy`.

### Prometheus Metrics

Backup Sentinel exposes Prometheus-compatible metrics at `/metrics` (no authentication required).

Exposed metrics:

- `backup_sentinel_cluster_sync_ok{cluster="..."}` -- Whether last sync succeeded (1/0)
- `backup_sentinel_cluster_sync_age_seconds{cluster="..."}` -- Seconds since last sync
- `backup_sentinel_cluster_sync_failures_total{cluster="..."}` -- Consecutive sync failures
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

Or use the included deploy script:

```bash
./deploy.sh
```

The application applies database migrations automatically on startup -- no manual migration steps are needed.

### Rollback

If an update causes issues:

```bash
git log --oneline -5          # find the previous commit
git checkout <commit-sha>     # switch to that version
docker compose up -d --build  # rebuild with the old version
```

### Version Check

The current build version is shown in the UI footer and can also be checked via the health endpoint response or container logs at startup. The format is `1.2.<git-short-sha>`.
