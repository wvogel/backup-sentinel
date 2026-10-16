# Backup Sentinel -- Admin- / Deployment-Dokumentation

## Inhaltsverzeichnis

1. [Voraussetzungen](#voraussetzungen)
2. [Schnellstart](#schnellstart)
3. [Umgebungsvariablen](#umgebungsvariablen)
4. [OAuth2-Proxy-Konfiguration](#oauth2-proxy-konfiguration)
5. [Cluster-Onboarding](#cluster-onboarding)
6. [Erscheinungsbild](#erscheinungsbild)
7. [Datenbank-Verwaltung](#datenbank-verwaltung)
8. [HTTPS / Reverse Proxy](#https--reverse-proxy)
9. [GitLab-CI/CD-Deployment](#gitlab-cicd-deployment)
10. [Monitoring](#monitoring)
11. [Aktualisierung](#aktualisierung)

---

## Voraussetzungen

- **Docker** >= 24.0
- **Docker Compose** v2 (das `docker compose`-Plugin)
- Ein **Reverse Proxy** mit TLS-Terminierung (z. B. Nginx Proxy Manager, Traefik, Caddy)
- Ein **OIDC-fähiger Identity Provider** (Keycloak, Azure Entra ID, Google, GitHub etc.)
- Eine **Valkey-/Redis-Instanz** für OAuth2-Proxy-Session-Storage (kann geteilt werden)
- Ausgehende HTTPS-Konnektivität zu den Proxmox-PVE/PBS-API-Endpunkten

### Proxmox-Kompatibilität

Backup Sentinel wird gegen folgende Proxmox-Versionen getestet:

| Komponente                  | Getestete Versionen | Hinweise                                    |
|-----------------------------|---------------------|---------------------------------------------|
| Proxmox VE (PVE)            | 8.x, 9.x            | API v2 (`/api2/json`)                       |
| Proxmox Backup Server (PBS) | 3.x, 4.x            | API v2                                      |

Ältere Versionen funktionieren möglicherweise, werden aber nicht regelmäßig getestet. Falls Sie Backup Sentinel gegen eine ältere Version einsetzen, öffnen Sie gerne ein Issue mit Ihren Ergebnissen.

API-Tokens benötigen:
- PVE: `Sys.Audit` auf `/` und `VM.Audit` auf `/vms` (Read-Only-Inventory) sowie `Datastore.Audit` für Backup-Storage-Erkennung
- PBS: `Datastore.Audit` auf die zu überwachenden Datastores; für das Replikations-Schaubild zusätzlich Lesen der Sync-Jobs und Remotes (`Datastore.Audit` auf `/datastore`, `Remote.Audit` auf `/remote`). Fehlen diese Rechte, bleibt der PBS-Sync grün, das Sync-Log nennt den Grund.
- PBS-Verbindung, Felder in Einstellungen → Cluster: **Namespace** beschränkt die Verbindung auf einen Namespace samt Unter-Namespaces (z. B. das Ziel eines Pull-Syncs; leer = nur Root). **Aliase** sind weitere Hostnamen oder IPs, unter denen PVE oder ein Sync-Remote diesen PBS kennt. Normalerweise erkennt Backup Sentinel einen PBS selbst (Fingerprint, Name, DNS, Zertifikat per TLS); Aliase braucht es nur, wenn er unter dem anderen Namen nicht erreichbar ist, etwa in einem getrennten Backup-Netz.

---

## Schnellstart

```bash
# 1. Repository klonen
git clone https://git.example.com/infra/backup-sentinel.git
cd backup-sentinel

# 2. Konfigurationsdateien erstellen
cp .env.example .env
cp oauth2-proxy.env.example oauth2-proxy.env

# 3. Beide Dateien mit Ihren Werten bearbeiten
#    - .env              -> Datenbank-Passwort, Secret Key, Zeitzone, APP_URL
#    - oauth2-proxy.env  -> OIDC Client-ID/Secret, Issuer-URL, Cookie-Secret

# 4. Externe Docker-Netzwerke sicherstellen
docker network create shared-npm    2>/dev/null || true
docker network create shared-valkey 2>/dev/null || true

# 5. Stack starten
docker compose up -d --build

# 6. Pruefen
docker compose ps
curl -s https://backup-sentinel.example.com/healthz
```

Der Stack besteht aus drei Containern:

| Container                | Port   | Zweck                          |
|--------------------------|--------|--------------------------------|
| `postgres`               | 5432   | Datenbank (nur intern)         |
| `backup-sentinel`        | 80     | FastAPI-Anwendung              |
| `oauth2-backup-sentinel` | 4180   | OAuth2-Proxy (öffentlich)     |

Ihr Reverse Proxy sollte den Traffic an den OAuth2-Proxy-Container auf Port **4180** weiterleiten.

---

## Umgebungsvariablen

### Anwendung (`.env`)

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `APP_URL` | `https://backup-sentinel.example.com` | Öffentliche URL der Anwendung. Wird in E-Mail-Benachrichtigungen und Bootstrap-Skripten verwendet. |
| `DB_NAME` | `backup_reports` | PostgreSQL-Datenbankname. |
| `DB_USER` | `backup_reports` | PostgreSQL-Benutzer. |
| `DB_PASSWORD` | `backup_reports` | PostgreSQL-Passwort. **In Produktion ändern.** |
| `BSENTINEL_DATABASE_URL` | *(abgeleitet von DB_*)* | Vollständiger PostgreSQL-Connection-String. Überschreiben für externe Datenbank. |
| `BSENTINEL_SECRET_KEY` | *(keiner)* | **Pflicht.** Fernet-Schlüssel für die Verschlüsselung von Secrets (SMTP-Passwort, Gotify-Token) in der Datenbank. Generieren mit: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `BSENTINEL_DEFAULT_TIMEZONE` | `Europe/Berlin` | Zeitzone für Anzeigeformatierung und Ruhezeiten-Berechnung. |
| `BSENTINEL_SYNC_INTERVAL_MINUTES` | `60` | Intervall in Minuten für die automatische Hintergrund-Synchronisation. |
| `BSENTINEL_API_TIMEOUT` | `60` | Timeout in Sekunden für Proxmox/PBS-API-Anfragen. |
| `BSENTINEL_DEBUG` | `false` | Debug-Logging aktivieren (`true` / `false`). |
| `BSENTINEL_INSECURE_SSL` | `false` | SSL-Zertifikatsprüfung für Proxmox/PBS-APIs deaktivieren. Auf `true` setzen bei selbstsignierten Zertifikaten. |
| `BSENTINEL_BRAND_LOGO_LIGHT_URL` | `/static/logo.svg` | Logo-URL für helles Design. Erscheint auch im Kopf des Wochenberichts (SVG wird dafür in PNG umgewandelt und eingebettet; relative URLs außerhalb von `/static/` brauchen `APP_URL`). |
| `BSENTINEL_BRAND_LOGO_DARK_URL` | `/static/logo.svg` | Logo-URL für dunkles Design. |
| `BSENTINEL_LOGOUT_URL` | `/oauth2/sign_out` | Logout-Weiterleitungs-URL. |
| `FOOTER_LINKS` | *(leer)* | Footer-Links. Format: `Label1\|URL1,Label2\|URL2` |
| `COPYRIGHT_TEXT` | *(leer)* | Copyright-Text in der Fußzeile (z.B. `© 2026 Ihre Firma`). |
| `BSENTINEL_DATA_DIR` | `/data` | Pfad für persistente Anwendungsdaten. |
| `BSENTINEL_REPORT_DIR` | `/reports` | Pfad für generierte PDF/JSON-Berichte. |
| `BSENTINEL_REPORT_LANGUAGE` | `de` | Serverseitige Sprache (`de` oder `en`) für alles, was ohne Browser entsteht: PDF-Berichte, Gotify-/E-Mail-Benachrichtigungen und Sync-Logs. Die Weboberfläche folgt stattdessen der Sprachwahl des jeweiligen Nutzers. Andere Werte fallen auf `de` zurück. |
| `BSENTINEL_BACKUP_DAY_OFFSET_HOURS` | `12` | Verschiebung der "Backup-Tag"-Grenze in der Sparkline. Lange Jobs, deren letzte VMs erst am nächsten Morgen fertig werden, zählen für den Abend, an dem der Job startete. `0` = echter Mitternachtswechsel. |

### OAuth2-Proxy (`oauth2-proxy.env`)

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `OAUTH2_PROXY_PROVIDER` | `oidc` | OAuth2-Provider-Typ. Häufige Werte: `oidc`, `keycloak-oidc`, `google`, `github`. |
| `OAUTH2_PROXY_CLIENT_ID` | *(keiner)* | **Pflicht.** Client-ID vom Identity Provider. |
| `OAUTH2_PROXY_CLIENT_SECRET` | *(keiner)* | **Pflicht.** Client-Secret vom Identity Provider. |
| `OAUTH2_PROXY_OIDC_ISSUER_URL` | *(keiner)* | **Pflicht.** OIDC-Issuer-URL (z. B. `https://keycloak.example.com/realms/your-realm`). |
| `OAUTH2_PROXY_REDIRECT_URL` | *(keiner)* | **Pflicht.** OAuth2-Callback-URL: `https://<ihre-domain>/oauth2/callback`. |
| `OAUTH2_PROXY_COOKIE_SECRET` | *(keiner)* | **Pflicht.** Zufalls-Secret für Session-Cookies. Generieren mit: `python -c "import secrets; print(secrets.token_hex(16))"` |
| `OAUTH2_PROXY_COOKIE_DOMAINS` | *(keiner)* | Ihre Anwendungsdomain (z. B. `backup-sentinel.example.com`). |
| `OAUTH2_PROXY_WHITELIST_DOMAINS` | *(keiner)* | Erlaubte Redirect-Domains. Typischerweise identisch mit `COOKIE_DOMAINS`. |
| `OAUTH2_PROXY_SESSION_STORE_TYPE` | `redis` | Session-Store-Typ. Verwendet eine geteilte Valkey/Redis-Instanz. |
| `OAUTH2_PROXY_REDIS_CONNECTION_URL` | `redis://valkey:6379/0` | Redis/Valkey-Verbindungs-URL für Session-Storage. |
| `OAUTH2_PROXY_SCOPE` | `openid profile email` | Anzufordernde OIDC-Scopes. |
| `OAUTH2_PROXY_EMAIL_DOMAINS` | `*` | Erlaubte E-Mail-Domains. `*` für alle. |
| `OAUTH2_PROXY_PASS_USER_HEADERS` | `true` | Authentifizierte Benutzerinformationen als Header an Upstream weiterleiten. |
| `OAUTH2_PROXY_SET_XAUTHREQUEST` | `true` | X-Auth-Request-Header setzen. |

---

## OAuth2-Proxy-Konfiguration

Backup Sentinel delegiert die Authentifizierung vollständig an [OAuth2-Proxy](https://oauth2-proxy.github.io/oauth2-proxy/). Der Proxy sitzt vor der FastAPI-Anwendung und übernimmt den OIDC-Login-Flow.

### Provider-Konfiguration

**Keycloak / Generisches OIDC:**

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

### Session-Store

OAuth2-Proxy verwendet einen Redis-kompatiblen Session-Store (Valkey), um Sessions über Container-Neustarts hinweg zu persistieren. Die Standardkonfiguration erwartet einen geteilten Valkey-Container, erreichbar über das Docker-Netzwerk `shared-valkey`:

```env
OAUTH2_PROXY_SESSION_STORE_TYPE=redis
OAUTH2_PROXY_REDIS_CONNECTION_URL=redis://valkey:6379/0
```

### Ausgenommene Routen

Bootstrap-Endpunkte sind von der Authentifizierung ausgenommen, damit Proxmox-Nodes ohne Session zurückrufen können:

- `GET /bootstrap/proxmox-agent.sh`
- `GET /bootstrap/pbs-agent.sh`
- `POST /api/bootstrap/finalize`
- `POST /api/bootstrap/pbs-finalize`

Diese Routen sind stattdessen durch ein einmaliges Enrollment-Secret gesichert.

---

## Cluster-Onboarding

### PVE-Cluster

1. Öffnen Sie die Einstellungsseite in der Backup-Sentinel-Oberfläche.
2. Geben Sie einen Namen und die PVE-API-URL ein, dann klicken Sie auf **Cluster vorbereiten**.
3. Kopieren Sie den angezeigten Bootstrap-Befehl und führen Sie ihn auf **einem beliebigen Node** des Proxmox-Clusters aus:

```bash
bash <(curl -fsSL https://backup-sentinel.example.com/bootstrap/proxmox-agent.sh) \
  --server-url https://backup-sentinel.example.com \
  --cluster-slug pve-prod \
  --enrollment-secret <einmal-secret>
```

Das Skript wird:
- Einen dedizierten PVE-API-Token erstellen (`backup-sentinel@pve!backup-sentinel`).
- Minimale Leserechte zuweisen.
- Den Token an die Backup-Sentinel-API zurücksenden, um die Registrierung abzuschließen.

4. Nach Abschluss des Bootstraps erscheint der Cluster in der Oberfläche und die erste Synchronisation startet automatisch.

### PBS-Verbindung

1. Suchen Sie auf der Einstellungsseite den Cluster und klicken Sie auf **PBS hinzufügen**.
2. Geben Sie einen Namen und die PBS-API-URL ein.
3. Führen Sie den angezeigten Bootstrap-Befehl auf dem PBS-Host aus:

```bash
bash <(curl -fsSL https://backup-sentinel.example.com/bootstrap/pbs-agent.sh) \
  --server-url https://backup-sentinel.example.com \
  --cluster-slug pve-prod \
  --enrollment-secret <einmal-secret>
```

Das Skript erstellt einen PBS-API-Token mit Audit/Lese-Berechtigungen und registriert die Verbindung.

---

## Erscheinungsbild

Standard ist das **smile eyes**-Design: Dark Blue `#141450` (Kopfzeile, Text), Neon Yellow `#FFFF00` (wichtigste Aktion pro Seite, immer mit Dark-Blue-Schrift), Street Blue `#4A7EB5` (laufende Backups), Light Beige `#F8F5F2` (Hintergrund), Beige `#EFE7DB` (Rahmen) und Light Sage `#D8DDCE` (ruhige Hinweise). Der Dark Mode wird aus Dark Blue abgeleitet.

Unter **Einstellungen → Erscheinungsbild** lassen sich anpassen:

- **Vorlage:** *smile eyes* oder *Neutral* (das frühere, kühle Blau-Grau).
- **Vier Grundfarben:** Primärfarbe, Akzent (Hauptaktion), Hintergrund und laufende Backups. Alle übrigen Töne (zarte Flächen, Rahmen, Dark Mode) werden daraus berechnet. Eine Vorschau zeigt die Wirkung vor dem Speichern.
- **Kontrastprüfung:** Beim Speichern wird nach WCAG geprüft (Text ≥ 4,5:1, laufende Backups ≥ 3:1). Bei schlechter Lesbarkeit erscheint eine Warnung mit dem gemessenen Wert; gespeichert wird trotzdem.
- **Auf Vorlage zurücksetzen** entfernt die eigenen Farben.

Änderungen landen im Einstellungs-Audit. Der PDF-Report übernimmt Primär-, Akzent- und Laufend-Farbe.

**Schrift:** [Instrument Sans](https://github.com/Instrument/instrument-sans) (SIL Open Font License 1.1), lokal ausgeliefert aus `static/fonts/` samt `OFL.txt` – die Anwendung lädt nichts von Google.

---

## Datenbank-Verwaltung

### Sicherung

Die PostgreSQL-Daten liegen im Volume `./postgres`. Für ein logisches Backup:

```bash
docker compose exec postgres pg_dump -U backup_reports backup_reports > backup_$(date +%Y%m%d).sql
```

### Wiederherstellung

```bash
# Anwendung stoppen, um Schreibzugriffe zu verhindern
docker compose stop backup-sentinel

# Wiederherstellen
docker compose exec -T postgres psql -U backup_reports backup_reports < backup_20260401.sql

# Neustarten
docker compose up -d backup-sentinel
```

### Zurücksetzen

Um mit einer frischen Datenbank zu beginnen, entfernen Sie das Volume und starten neu:

```bash
docker compose down
rm -rf ./postgres
docker compose up -d
```

Die Anwendung erstellt alle Tabellen beim Start automatisch über `db.init_db()`.

---

## HTTPS / Reverse Proxy

Backup Sentinel erwartet einen Reverse Proxy für die TLS-Terminierung. Der OAuth2-Proxy-Container lauscht auf Port **4180** und sollte der öffentlich erreichbare Endpunkt sein.

### Nginx Proxy Manager

1. Neuen Proxy-Host erstellen, der auf den Container `oauth2-backup-sentinel` auf Port `4180` zeigt.
2. SSL mit einem Let's-Encrypt-Zertifikat aktivieren.
3. Sicherstellen, dass der Proxy-Host im Docker-Netzwerk `shared-npm` ist.

### Traefik (Labels)

```yaml
services:
  oauth2-backup-sentinel:
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.bsentinel.rule=Host(`backup-sentinel.example.com`)"
      - "traefik.http.routers.bsentinel.tls.certresolver=letsencrypt"
      - "traefik.http.services.bsentinel.loadbalancer.server.port=4180"
```

### Nginx (manuell)

```nginx
server {
    listen 443 ssl http2;
    server_name backup-sentinel.example.com;

    ssl_certificate     /etc/letsencrypt/live/backup-sentinel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/backup-sentinel.example.com/privkey.pem;

    # OAuth2-Proxy vertraut diesen Headern - muessen vom Reverse Proxy gesetzt werden
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host  $host;

    # Sync-Log-Streams nicht puffern
    proxy_buffering off;
    proxy_read_timeout 300s;

    location / {
        proxy_pass http://127.0.0.1:4180;
    }
}
```

### Wichtige Header

OAuth2-Proxy setzt `OAUTH2_PROXY_REVERSE_PROXY=true`, sodass es `X-Forwarded-*`-Headern vom Reverse Proxy vertraut. Stellen Sie sicher, dass Ihr Reverse Proxy folgende Header setzt:

- `X-Forwarded-For`
- `X-Forwarded-Proto` (muss `https` sein)
- `X-Forwarded-Host`

---

## GitLab-CI/CD-Deployment

Das Repository enthält eine `.gitlab-ci.yml`, die bei jedem Push auf `main` auf einen Server deployed.

### Benötigte CI/CD-Variablen

Konfigurieren Sie diese drei Variablen unter **GitLab > Settings > CI/CD > Variables**:

| Variable | Beschreibung |
|----------|--------------|
| `SSH_KEY` | Privater SSH-Schlüssel (ed25519) für den Deployment-Benutzer. Typ: **File** oder **Variable**. |
| `DEPLOY_HOST` | Zielserver-Hostname oder IP. |
| `DEPLOY_PATH` | Absoluter Pfad zum Projekt auf dem Server (z. B. `/root/backup-sentinel`). |

### Pipeline-Verhalten

Bei jedem Push auf `main` führt die Pipeline folgende Schritte aus:

1. Verbindung zum Server per SSH.
2. Neuesten Code mit `git pull` holen.
3. `docker compose down && docker compose up -d --build` ausführen.

Die Build-Version wird aus dem Git-Commit-SHA abgeleitet (Format: `1.2.<short-sha>`).

---

## Monitoring

### Health-Endpunkt

Die Anwendung stellt einen `/healthz`-Endpunkt bereit, der die Datenbank-Konnektivität prüft:

```bash
curl -s https://backup-sentinel.example.com/healthz
```

**Antworten:**

- `200 OK` mit `{"status": "ok", "version": "v2.4.7"}` -- Anwendung und Datenbank sind gesund; `version` ist das laufende Release (praktisch, um einen Deploy zu prüfen).
- `503 Service Unavailable` mit `{"status": "degraded", "version": "…", "database": "<fehler>"}` -- Datenbank nicht erreichbar.

### Docker Health Check

Der `backup-sentinel`-Container hat einen eingebauten Health Check, der `/healthz` alle 5 Sekunden abfragt. Verwenden Sie `docker compose ps`, um zu prüfen, ob der Container `healthy` ist.

### Prometheus-Metriken

Backup Sentinel stellt Prometheus-kompatible Metriken unter `/metrics` bereit (keine Authentifizierung erforderlich).

Verfügbare Metriken:

- `backup_sentinel_cluster_sync_ok{cluster="..."}` -- Ob die letzte Synchronisation erfolgreich war (1/0)
- `backup_sentinel_cluster_sync_age_seconds{cluster="..."}` -- Sekunden seit der letzten Synchronisation
- `backup_sentinel_cluster_sync_failures_total{cluster="..."}` -- Aufeinanderfolgende Sync-Fehlschläge
- `backup_sentinel_vm_backup_severity_count{cluster="...",severity="..."}` -- VMs pro Schweregrad
- `backup_sentinel_unencrypted_backups_count{cluster="..."}` -- VMs mit unverschlüsselten Backups
- `backup_sentinel_restore_overdue_count{cluster="..."}` -- VMs mit überfälligen Restore-Tests

Ergänzen Sie in Ihrer Prometheus-Konfiguration unter `scrape_configs`:

```yaml
- job_name: backup-sentinel
  static_configs:
    - targets: ['backup-sentinel.example.com']
  metrics_path: /metrics
  scheme: https
```

### Logging

Standardmäßig protokolliert die Anwendung auf `INFO`-Level. Setzen Sie `BSENTINEL_DEBUG=true` für `DEBUG`-Level. Logs werden auf stdout geschrieben und können eingesehen werden mit:

```bash
docker compose logs -f backup-sentinel
```

Der OAuth2-Proxy-Container verwendet Dockers lokalen Logging-Treiber mit 10 MB / 3 Dateien Rotation.

---

## Aktualisierung

Um Backup Sentinel auf die neueste Version zu aktualisieren:

```bash
cd /pfad/zu/backup-sentinel
git pull
docker compose up -d --build
```

Oder verwenden Sie das mitgelieferte Deploy-Skript:

```bash
./deploy.sh
```

Die Anwendung führt Datenbank-Migrationen beim Start automatisch durch -- manuelle Migrationsschritte sind nicht erforderlich.

### Rollback

Falls ein Update Probleme verursacht:

```bash
git log --oneline -5          # vorherigen Commit finden
git checkout <commit-sha>     # auf diese Version wechseln
docker compose up -d --build  # mit der alten Version neu bauen
```

### Versionsprüfung

Die aktuelle Build-Version wird in der Fußzeile der Oberfläche angezeigt und kann auch über den Health-Endpunkt oder die Container-Logs beim Start geprüft werden. Das Format ist `1.2.<git-short-sha>`.
