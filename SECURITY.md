# Security Policy

## Supported Versions

Security fixes are applied to the latest release only. If you are running an older version, please upgrade.

| Version | Supported |
|---------|-----------|
| 2.x     | ✅        |
| < 2.0   | ❌        |

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

- **Authentication** is delegated entirely to OAuth2-Proxy — the app trusts `X-Forwarded-Email` / `X-Forwarded-User` headers only
- **Secrets** (Gotify token, SMTP password) are encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256)
- **Security headers** (CSP, X-Frame-Options, Referrer-Policy, etc.) are applied by middleware on every response
- **Dependencies** are pinned to exact versions in `requirements.txt`
- **No user-supplied HTML** is rendered without Jinja2 auto-escaping
- **Parameterized queries** are used everywhere — no string-concatenated SQL
- **The app runs as a non-root user** (uid 1000) inside its container

## Known Limitations

- The app is designed to be deployed **behind OAuth2-Proxy**. Running it directly exposed to the internet is not supported and will allow unauthenticated access.
- `/healthz` and `/metrics` are intentionally unauthenticated. Restrict them at the reverse proxy level if needed.
- The bootstrap endpoints (`/bootstrap/proxmox-agent.sh`, `/api/bootstrap/finalize`) are unauthenticated by design — they use per-cluster enrollment secrets for authentication.
