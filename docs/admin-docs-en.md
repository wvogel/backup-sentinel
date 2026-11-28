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
- On PVE: user `backup-reports@pve`, token `<cluster-slug>-collector` (no privilege separation), role `PVEAuditor` on `/` plus the custom role `BackupSentinelBackupList` (`Datastore.AllocateSpace`, `VM.Backup`) on `/`. PVE has no read-only way to list backup volumes: a storage's content shows a backup only with `Datastore.Allocate` on the storage, or `Datastore.AllocateSpace` plus `VM.Backup` on the VM. The custom role takes the narrower second way — it cannot change the storage configuration or delete VM disks or ISOs, but it could start or delete backups. Backup Sentinel itself only sends read requests. Optional: with `VM.Config.Disk` (or `Datastore.Allocate` on the storage) Backup Sentinel reads the allocated size of the VM disks of a running full read (`GET …/storage/<id>/content/<volid>`) and estimates the finish more precisely. PVE only returns this volume info with one of these privileges; both also allow changes, so the bootstrap does not grant them. Without them the estimate works without the allocation info.
- On PBS: user `backup-reports@pbs`, token `<cluster-slug>-collector`, role `Audit` on `/` for both user and token (PBS privilege separation: the effective rights are the intersection of both). `Audit` is read-only and covers everything Backup Sentinel reads: datastores, namespaces, snapshots, usage, sync jobs, remotes, running tasks (garbage collection, verify) and task logs. The certificate fingerprint is read from the TLS connection, not from the certificates API (that one needs `Sys.Modify`).
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
| `BSENTINEL_SECRET_KEY` | *(empty)* | **Required** — the app does not start without a valid key. Fernet key that encrypts the secrets in the database (PVE/PBS API tokens, enrollment secrets, restore-server credentials, SMTP password, Gotify token); it also keys the hash chain of the audit log. Keep a copy outside the server: without it the tokens cannot be read. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `BSENTINEL_SECRET_KEY_OLD` | *(empty)* | Previous keys, comma-separated — only for changing the key. **Never just replace the key:** the stored tokens can only be read with the key they were encrypted with. To change it, put the new key into `BSENTINEL_SECRET_KEY` and the previous one here, then restart; the start re-encrypts everything with the new key, after which this line can go. If the key does not fit the stored secrets they are left untouched, the log names them (`BSENTINEL_SECRET_KEY does not fit …`), and the affected connections work again only with the right key. |
| `BSENTINEL_ADMINS` | *(empty)* | Who may do everything: e-mail addresses, user names or IdP groups, comma-separated. See [Roles](#roles). |
| `BSENTINEL_ALLOW_ANONYMOUS` | `false` | Only for running the app locally **without** a proxy. By default a request without the user OAuth2-Proxy forwards is refused with 401 (except `/healthz`, `/metrics` and the bootstrap endpoints), so the app's port gives nothing when reached past the proxy. |
| `BSENTINEL_OPERATORS` | *(empty)* | Who runs day-to-day operation (start syncs and restore tests, record tests, create interim reports). See [Roles](#roles). |
| `BACKUP_AGE_RECIPIENT` | *(empty)* | age public key (`age1…`) that `backup.sh` encrypts the database dump to. |
| `BSENTINEL_DEFAULT_TIMEZONE` | `Europe/Berlin` | Timezone for display formatting and quiet-hour calculations. |
| `BSENTINEL_SYNC_INTERVAL_MINUTES` | `60` | Interval in minutes for the automatic background sync loop. |
| `BSENTINEL_API_TIMEOUT` | `60` | Timeout in seconds for Proxmox/PBS API requests. |
| `BSENTINEL_DEBUG` | `false` | Enable debug logging (`true` / `false`). |
| `BSENTINEL_INSECURE_SSL` | `false` | **No longer needed, not even with self-signed certificates** (see [TLS certificates of PVE and PBS](#tls-certificates-of-pve-and-pbs)). Set to `true`, a changed certificate is only reported instead of refused — meant for the switch-over; remove it afterwards. |
| `BSENTINEL_BRAND_LOGO_LIGHT_URL` | *(empty = no logo)* | Logo URL for light theme. Also shown in the weekly report header (SVG is converted to PNG and embedded; relative URLs outside `/static/` need `APP_URL`). |
| `BSENTINEL_BRAND_LOGO_DARK_URL` | *(empty = no logo)* | Logo URL for dark theme. |
| `BSENTINEL_BRAND_FONT_URL` | *(empty = system font)* | Font of the PDF report: a URL with `{weight}`, replaced by `Regular`, `Medium`, `SemiBold` and `Bold`, e.g. `https://example.com/fonts/Brand-{weight}.woff2`. The files are fetched and embedded when a report is generated; without them the report uses a system font. |
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
- `POST /api/bootstrap/finalize` and `POST /api/bootstrap/pbs-finalize` -- so that Proxmox nodes can call back without a session. These calls are checked against the cluster's enrollment secret instead; it is valid for 24 hours. The secrets travel in the request body, not in the URL.

### Roles

OAuth2-Proxy decides who may sign in; Backup Sentinel decides what a signed-in user may do:

| Role | May |
|------|-----|
| Viewer | See everything, download reports. Every signed-in user who is in neither list. |
| Operator (`BSENTINEL_OPERATORS`) | Also: start a sync, start, cancel and reorder restore tests, record manual restore tests, create interim reports. |
| Admin (`BSENTINEL_ADMINS`) | Everything: settings (the page itself too), add and delete clusters and PBS, change targets and intervals, delete archived reports. |

The lists hold e-mail addresses, user names or groups, comma-separated and case-insensitive, e.g. `BSENTINEL_ADMINS=it-admins,alice@example.com`. The user comes from `X-Forwarded-Email`, `X-Forwarded-User` and `X-Forwarded-Preferred-Username`, groups from `X-Forwarded-Groups` — the headers OAuth2-Proxy sets itself; `X-Auth-Request-*` headers are not read. For groups OAuth2-Proxy must receive them from the IdP (scope or `OAUTH2_PROXY_OIDC_GROUPS_CLAIM`; Entra ID sends group IDs). With **both lists empty** there are no roles and every signed-in user is admin. Every state-changing route not explicitly open to operators needs admin.

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

The command is valid for 24 hours. After that the settings page shows the button **Create a new command** instead; the old command stops working at once.

The script creates the user `backup-reports@pbs` and the API token `<cluster-slug>-collector`, grants both the read-only role `Audit` on `/` (see [Requirements](#requirements)) and registers the connection. The PBS name is the short host name, the API URL `https://<fqdn>:8007`. Optional flags:

- `--api-url URL` -- use this API URL instead of `https://<fqdn>:8007`, e.g. when Backup Sentinel reaches the PBS under another name.
- `--namespace NS` -- limit the connection to this namespace and its children (e.g. the target of a pull sync); without it only the root namespace is read. A re-run without `--namespace` keeps the namespace set in the UI.

**Space per guest.** A click on a datastore's ring in the backup diagram lists the guests with backups there and what their remaining backups take. PBS has no such figure (chunks are shared between all backups of a datastore), so this is an estimate from the backup task logs: per backup the uploaded data, after compression and without the chunks the server already had, summed over the snapshots that still exist. Data several guests share counts with the guest that wrote it first; the total therefore does not equal the datastore's usage. A snapshot a sync job copied counts with what it took where it was backed up to. Snapshots older than the PBS's task log have no figure; the list says for how many backups one is known.

Each sync reads the logs of the backup tasks not seen yet (one short API call each, at most 150 per PBS and sync, so a newly registered PBS is caught up over several syncs). No index file and no chunk is read: the storage behind the datastore sees nothing of it.

---

## Appearance

The default is the neutral preset: slate `#1E293B` (topbar, text), blue `#2563EB` (the one main action per page), teal `#0D9488` (running backups) and a light grey `#F5F7FA` background. Dark mode is derived from the primary colour.

Under **Settings → Appearance** you can change:

- **Preset:** *Neutral*.
- **Four base colours:** primary, accent (main action), background and running backups. All other tones (soft surfaces, borders, dark mode) are derived. A preview shows the result before saving.
- **Contrast check:** saving checks WCAG contrast (text ≥ 4.5:1, running backups ≥ 3:1). Poor readability shows a warning with the measured ratio; the setting is saved anyway.
- **Custom CSS:** added after the colours — for anything the four base colours don't cover, e.g. your own border or dark-mode tones (`html[data-theme="dark"] { --bg: …; }`). At most 20,000 characters, no `</`. Applies to the web interface, not the PDF report.
- **Reset to preset** removes custom colours; the custom CSS stays.

Changes are recorded in the settings audit. The PDF report uses the primary, accent and running colours.

**Font:** the device's system font (macOS/iOS: San Francisco, Windows: Segoe UI, Android: Roboto). No font is downloaded, so nothing shifts while the page loads.

---

## Database Management

### Backup

The PostgreSQL data is stored in the `./postgres` volume. To create a logical backup:

```bash
docker compose exec postgres pg_dump -U backup_reports backup_reports > backup_$(date +%Y%m%d).sql
```

Alternatively run `./backup.sh` from the repository root (e.g. as a daily cron job). It reads `DB_USER` from `.env`, writes a `pg_dumpall` of the whole PostgreSQL instance to `./backup/<YYYY-MM-DD>.gz` and keeps the three most recent dumps. With `BACKUP_AGE_RECIPIENT` in `.env` (an [age](https://age-encryption.org) public key; `age` must be installed) the dump is encrypted to that key (`<YYYY-MM-DD>.gz.age`); the private key does not belong on this server. Without the variable the dump stays unencrypted — enough when the directory is copied off the host by a tool that encrypts itself (e.g. restic). Copy the dump to another host in any case. As a `pg_dumpall` it contains all databases and roles; restore it into a fresh instance (see [Reset](#reset), with the app stopped) via `gunzip -c backup/<date>.gz | docker compose exec -T postgres psql -U backup_reports postgres`.

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

### TLS certificates of PVE and PBS

Proxmox hosts usually serve a self-signed certificate. Backup Sentinel does not switch verification off for that; it remembers the certificate, as SSH does with a host key:

- If a system certificate authority vouches for the certificate (right name, not expired), it is trusted as it is. The host is then listed as "verified by a certificate authority", with the fingerprint of that certificate; renewals update it by themselves. If such a host later presents a certificate **no** authority vouches for, that is a changed certificate (see below), not a first contact — otherwise anyone in the network path could present a self-signed certificate and receive the API token.
- Otherwise its SHA-256 fingerprint is remembered on first contact, per host and port. If it equals the one the bootstrap script read on the host itself, the origin says "confirmed by the bootstrap script", else "on first contact".
- From then on only that certificate is accepted. If the host presents another one, the connection is dropped **before** an API token is sent; the sync fails with a message saying so.

Under **Settings → Clusters** every PVE and PBS carries a lock beside its symbol (the restore host under **Restore server**, beside its API token): green once a certificate is remembered for it, grey before the first contact, red while it presents a different one. Hovering shows where the trust comes from, since when, and the fingerprint. A host with a red lock gets a line below it with the new fingerprint. After renewing a certificate on purpose, compare that fingerprint with the one on the host (`pvenode cert info` or `proxmox-backup-manager cert info`) and choose **Accept the new certificate**. Pinning, changes, accepting and dismissing are in the audit log.

**Switching over from `BSENTINEL_INSECURE_SSL=true`:** with the variable set everything keeps working as before, but certificates are already pinned and changes reported. Once the list shows every host without a report, remove the variable and restart — from then on a change is refused. If a host keeps showing different certificates, several nodes answer under its address; point the API URL at one node, or use one CA-signed certificate for all.

---

## GitLab CI/CD Deployment

The repository includes a `.gitlab-ci.yml` with three stages.

### Pipeline Stages

| Stage | Job |
|-------|-----|
| `lint` | `ruff check` and `ruff format --check` on `app`, `tests` and `scripts`. |
| `test` | `pytest tests/` against a `postgres:18` service. `BSENTINEL_DB_TESTS=1` is set, so the database tests (which truncate tables) run as well. |
| `test` (job `audit`) | `pip-audit` checks the dependencies in `requirements.txt` for known vulnerabilities. The job reports but does not block. |
| `deploy` | Release tags `vX.Y.Z` only: sends a `POST` to the URL of every CI/CD variable whose name starts with `DEPLOY_WEBHOOK`. A push to `main` without a tag deploys nothing. |

### Hardened deploy chain

What reaches the server is decided by `deploy.sh` on the server — the copy from the release deployed before:

1. It fetches `main` and the tags and takes the **newest release tag** (`vX.Y.Z`) on `main`.
2. If the file `/etc/backup-sentinel/allowed_signers` exists on the server (or the path in `DEPLOY_ALLOWED_SIGNERS`), the release is deployed only if its tag carries a **valid signature** of a key listed there. The check runs before anything of the new release is checked out or run. Without the file nothing is checked (note in the log).
3. It builds the image and swaps the containers only afterwards; if the build fails, the old version keeps running.

On the GitLab side this needs: `main` protected without force-push, tags `v*` protected (only maintainers may create them), `DEPLOY_WEBHOOK` as a protected variable.

**Setting up signed releases** (once): on the developer machine `git config gpg.format ssh`, `git config user.signingkey ~/.ssh/id_ed25519.pub`, `git config tag.gpgSign true`; on the server one line per authorised person in `/etc/backup-sentinel/allowed_signers`: `<e-mail> ssh-ed25519 AAAA…` (the public key). The file lives outside the repository on purpose: write access to the repository alone does not let anyone add themselves.

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

A running restore test survives an update: a restore that has already started keeps running on the restore host and is resumed after the restart. A run that was still before its restore at the restart (pre-flight, helper) is dropped and put back into the queue at its place.

### Rollback

If an update causes issues:

```bash
git log --oneline -5          # find the previous commit
git checkout <commit-sha>     # switch to that version
docker compose up -d --build  # rebuild with the old version
```

### Version Check

The current build version is shown in the UI footer and can also be checked via the health endpoint response or container logs at startup. The format is `vMAJOR.MINOR.PATCH` (e.g. `v2.41.10`), as in `CHANGELOG.md` and the git tags; `/healthz` returns it as `version`.


## Automated restore tests

Backup Sentinel can *prove* a restore, not just document it, by driving a **dedicated PVE-Restore scratch host** (Settings → Restore server) that holds the production PBS datastores locally (pull-synced, per datastore+namespace).

### Prerequisites

- **Dedicated, empty PVE host.** Nothing else may live on the host or its target storage — a hard reaper destroys, on startup and before/after every test, anything not owned by a running, known operation.
- **Local-only PBS datastores** that mirror the source exactly. Matching uses the exact 3-tuple `(PBS name, datastore, namespace)`. Anything the host does not see locally is *not auto-restorable* (no offsite backup) and is never queued.
- **SSH access** to the restore host (host, user, private key). That is all: the app derives the API URL from the SSH host and provisions the API token (`root@pam!b-sentinel`, no privilege separation) itself over SSH — the same SSH connection also drives the bridge, LXC/dnsmasq and reaper. The host key is pinned automatically on the first connection test. "Renew token" re-creates it at any time.
- **Guest-agent audit** (`VM.GuestAgent.Audit` or `VM.Monitor`) on the production cluster tokens for IP discovery; otherwise set the primary IP per VM manually.
  Only IPs on the VM's **real NICs** (MAC listed in the VM config) are probed; Docker bridges, `wg0` and other guest-internal interfaces are skipped. If the helper's DHCP handed out the IP, it is probed first.
- **Management network** (static, no DHCP): interface/bridge, optional VLAN id and an IP range (from–to). One IP per helper; when exhausted the queue waits. The sandbox never talks outside, so no gateway and always /24.
- **Sandbox bridge**: a portless, VLAN-aware bridge (default `vmbr9`, MTU 1500) the app ensures idempotently over SSH — traffic never leaves the host. The restored VM's NICs are pinned to MTU 1500 on it, so a jumbo-frame source VM (`mtu=9000`) still starts. Before the start the app also removes PCI passthrough (`hostpciN`) and CPU and RAM affinity (`affinity`, `numaN` with `hostnodes`): the restore host has a different CPU, so the pinned cores or NUMA nodes may not exist there. `numa: 1` (NUMA emulation without host binding) stays.

### Operation

A global single-slot queue drains due, enabled VMs one at a time (never two active tests at once). The **cadence** is also the due interval (month/quarter/half-year/year → 30/91/182/365 days); the former criticality-based class is gone. Before each run the scheduler checks the VM fits the free space — **disk** on the target storage and host **RAM**. The host always keeps 10% of its RAM reserved, at least 10 GB; a VM that doesn't fit right now is skipped and the next one tried (no busy-spin — a pass ends once nothing fits and is retried on the next sync). The restore phase has no fixed timeout: it aborts only on a task error or a genuine stall (adaptive backstop). A `passed`/`limited` result resets the due clock, `failed`/`error` does not (retry cadence, default 1 week) and raises a notification. Optionally a failed environment is **held** for manual testing; multiple holds coexist (bounded by host RAM/disk). Live progress (percent, ETA, throughput) and host capacity (RAM bar, one ring per image storage) are shown at `/restore`; the menu bar tints the restore icon by queue state (green/amber/red, animated while a restore runs).

**VMs and LXC containers.** Both guest types are tested. A container is restored with `pct restore` onto a container-capable target storage (`content=rootdir`), booted with `pct start`, and probed exactly like a VM — liveness (ICMP/ARP) plus its configured TCP ports (for example a Corosync **Qdevice** on 5403). Containers have no QEMU agent, so their live IP comes from the `netN` config (a static `ip=`, otherwise the sandbox DHCP). In **Settings → Restore server** each target storage is marked **VM** (holds VM disks, `content=images`) and/or **CT** (holds a container rootfs, `content=rootdir`) so you can tick the right one per guest type. Spreading disks across several storages stays VM-only.

### Protecting the restore host

The restore host is the most sensitive system of the installation: it holds a full copy of the backups including their keys, and it boots restored production systems. It needs the same protection as the PBS.

**What is on it:**

- the local PBS datastores (all mirrored backups) and, for encrypted backups, their keys under `/etc/pve/priv/storage/`;
- during a test, the restored VM with its real data; a **held** environment stays until it is released;
- an API token `root@pam!b-sentinel` without privilege separation.

**What Backup Sentinel takes care of:**

- Restored guests are attached only to the portless sandbox bridge; their traffic never leaves the host. The helper probes a guest only if the route goes out the sandbox leg (otherwise it aborts instead of reaching the production system).
- After every test and at every start the reaper deletes everything that does not belong to a running operation, disks included.
- The SSH host key is remembered on first connect; a changed key is refused. Token and SSH key are stored encrypted in the database.
- The helper agent accepts only requests carrying the app's token, which is a different one for every run.

**What operations must ensure:**

- Management access (SSH 22, API 8006) only from the management network and from Backup Sentinel; none from client networks.
- No other service and no other user on the host; updates as on the production nodes.
- The sandbox bridge has no physical port and no IP address on the host.
- Release held environments promptly — they contain real data.
- The SSH key is used for this host only; if in doubt, replace key and token (Settings → Restore server).
- The disks are encrypted or the host stands in an access-controlled room; they are wiped when it is decommissioned.
- The host belongs in the inventory of systems holding sensitive data (same protection level as the PBS).
