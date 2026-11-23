# Security Policy

## Supported Versions

Security fixes are applied to the latest release only. If you are running an older version, please upgrade.

| Version | Supported |
|---------|-----------|
| 3.x     | ✅        |
| < 3.0   | ❌        |

## Reporting a Vulnerability

If you discover a security vulnerability in Backup Sentinel, please **do not** open a public GitHub issue.

Instead, report it privately via one of:

- **GitHub Security Advisories**: https://github.com/wvogel/backup-sentinel/security/advisories/new
- **Email**: open a GitHub issue asking for a private contact channel, or use the address on the maintainer's GitHub profile

Please include:

- A description of the vulnerability and its impact
- Steps to reproduce, ideally with a minimal proof-of-concept
- The affected version(s)
- Your contact information for follow-up questions

### What to expect

- **Acknowledgement** within 72 hours
- **Initial assessment** within 7 days
- **Fix or mitigation plan** within 30 days for confirmed vulnerabilities
- **Coordinated disclosure** — we will work with you on a timeline before publishing any advisory

## Scope

In scope:

- The Backup Sentinel application (Python code under `app/`)
- Docker image build (`Dockerfile`, `docker-compose.yml`)
- Database schema and queries
- OAuth2-Proxy integration as shipped in this repository

Out of scope:

- Vulnerabilities in upstream dependencies (report those to the respective projects)
- Vulnerabilities in Proxmox VE or Proxmox Backup Server
- Vulnerabilities in OAuth2-Proxy itself
- Social engineering attacks
- Denial-of-service attacks requiring unrealistic resources

## Security Practices

Backup Sentinel follows these practices:

- **Authentication** is delegated entirely to OAuth2-Proxy — the app trusts the `X-Forwarded-Email`, `X-Auth-Request-Email` and `X-Forwarded-User` headers it sets
- **CSRF protection** by origin check (`app/web/csrf.py`): a state-changing request whose `Origin` / `Referer` names another site is rejected; requests without either header (non-browser clients such as the bootstrap scripts) pass
- **Authorization** by role (`app/web/auth.py`): viewer, operator, admin, from the e-mail, user name or groups the proxy forwards. Every state-changing route needs admin unless it is listed for operators; with no role lists configured every signed-in user is admin
- **Secrets** (PVE/PBS API tokens, enrollment secrets, restore-server credentials, Gotify token, SMTP password) are encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256); a secret that cannot be encrypted is not stored, and one that cannot be decrypted is never sent out in its place
- **Audit log** is append-only (a database trigger refuses UPDATE / DELETE / TRUNCATE) and carries a hash chain keyed with `BSENTINEL_SECRET_KEY`; archived reports carry SHA-256 checksums that are checked on every download
- **Restore helper agent** accepts only requests with the bearer token the app derives for it, and builds its DHCP config itself from validated addresses
- **TLS to PVE/PBS** is verified against the system CAs or, for self-signed certificates, pinned to the certificate's SHA-256 fingerprint; a changed certificate is refused before any token is sent
- **Security headers** (CSP, X-Frame-Options, Referrer-Policy, etc.) are applied by middleware on every response
- **The Content-Security-Policy allows no inline code**: scripts and styles come from the app's own files only (`script-src 'self'; style-src 'self'`, no `unsafe-inline`), so markup injected into a page cannot run. The only exception are the two standalone previews (report, weekly mail), whose styles must be inline for PDF and mail clients; their policy forbids scripts altogether
- **Dependencies** are pinned to exact versions in `requirements.txt`, the base images by digest; `pip-audit` runs in the pipeline and CodeQL on GitHub
- **Deployment** only of release tags; with an allowed-signers file on the server only of tags signed by one of them, verified by the previously deployed `deploy.sh` before the new release is checked out
- **No user-supplied HTML** is rendered without Jinja2 auto-escaping
- **Parameterized queries** are used everywhere — no string-concatenated SQL
- **The app runs as a non-root user** (uid 1000) inside its container

## Known Limitations

- The app is designed to be deployed **behind OAuth2-Proxy**. Running it directly exposed to the internet is not supported and will allow unauthenticated access.
- `/healthz` and `/metrics` are intentionally unauthenticated. Restrict them at the reverse proxy level if needed.
- The bootstrap endpoints (`/bootstrap/proxmox-agent.sh`, `/bootstrap/pbs-agent.sh`, `/api/bootstrap/finalize`, `/api/bootstrap/pbs-finalize`) are unauthenticated by design — the scripts are public, and the finalize calls are checked against the per-cluster enrollment secret, which is valid for 24 hours and sent in the request body.
- The audit-log chain shows changed or removed entries, but not entries cut off at the very end by someone with full database access; the `report.generated` entries and the checksums in exported reports are the cross-check.
- The helper agent's token is claimed by the first caller; the app calls a new helper within seconds of its boot.
