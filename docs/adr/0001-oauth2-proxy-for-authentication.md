# ADR-0001: OAuth2-Proxy for authentication

## Status

Accepted

## Context

Backup Sentinel is a compliance tool that provides administrative access to backup infrastructure. Authentication is a hard requirement — the app must not be exposed without a login.

Options considered:
1. **Implement authentication inside the app** — requires user management, password hashing, session handling, MFA, password reset, etc. Significant surface area and ongoing maintenance.
2. **Rely on a reverse proxy with basic auth** — insufficient for compliance needs (NIS2 Art. 21 requires access control with audit trail); no SSO integration.
3. **Delegate to an OAuth2 / OIDC proxy** — authentication is handled externally against an existing IdP (Keycloak, Entra ID, Authentik, Google, etc.).

## Decision

Use [OAuth2-Proxy](https://github.com/oauth2-proxy/oauth2-proxy) as a sidecar container that handles all authentication before requests reach the app. The app trusts the `X-Forwarded-Email` / `X-Forwarded-User` headers set by OAuth2-Proxy.

The `docker-compose.yml` bundles OAuth2-Proxy by default. Credentials are configured via `oauth2-proxy.env` which is excluded from version control.

## Consequences

**Positive:**
- No user management code in the app — entire SSO stack is a dependency
- Existing organizational IdPs (Entra ID, Keycloak) integrate immediately
- MFA, password policy, account lifecycle are handled by the IdP
- Audit trail lives in the IdP, not the app
- `/healthz` and `/metrics` can bypass auth via `OAUTH2_PROXY_SKIP_AUTH_ROUTES`

**Negative:**
- One more container to deploy and maintain
- Adds a dependency on a separate session store (Redis / Valkey) for HA deployments
- Developers running locally must either set up the proxy or disable auth — increases setup friction
- Header-based trust is only safe behind a properly configured reverse proxy; misconfiguration can bypass authentication entirely

**Neutral:**
- Session cookies are managed by OAuth2-Proxy, not the app — the app has no control over session lifetime or invalidation
