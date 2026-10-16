# PBS-Topologie mit Datastore-Ringen und Gast-Symbolen

Stand: 2026-10-16 · freigegeben im Chat: Variante „B mit Ringen“ + Gast-Symbole

## Ziel

Auf der Cluster-Seite soll auf einen Blick sichtbar sein:

1. wohin der Cluster sichert (PBS und Datastores, die PVE direkt beschreibt),
2. wohin diese Backups per Sync-Job weiter repliziert werden,
3. wie voll jeder beteiligte Datastore ist,
4. ob ein Gast eine VM oder ein Container ist – zusammen mit seinem Status.

Die heutigen Datastore-Chips unter den PBS-Sync-Zeilen werden dadurch ersetzt.

## Begriffe

- **Rolle hängt an der Kante, nicht am Server.** Ein PBS kann für einen Datastore
  Primärziel sein (PVE sichert direkt dorthin) und für einen anderen Ziel einer
  Replikation (Sync-Job zieht von einem anderen PBS). Zwei PBS, die sich
  gegenseitig sichern, sind zwei Kanten in Gegenrichtung. Es gibt kein Etikett
  „lokal/remote“ pro PBS.
- **Primär-Datastore:** ein Datastore (+ Namespace), auf den ein PBS-Storage der
  PVE-Konfiguration des Clusters zeigt.
- **Replikationskante:** ein Sync-Job auf einem registrierten PBS, Richtung
  Quelle-Datastore → Ziel-Datastore (+ Namespaces), mit Zeitplan und letztem Lauf.

## Daten

### 1. Gast-Typ

- Neue Spalte `vms.guest_type TEXT NOT NULL DEFAULT 'qemu'` (`qemu` | `lxc`).
- `fetch_cluster_inventory` übernimmt `type` aus `/cluster/resources`;
  `sync_cluster_inventory` schreibt ihn bei jedem Sync.
- `list_vm_rows`/Governance-Query liefern `guest_type` mit.

### 2. Primärziele aus PVE

- Beim PVE-Sync sind die PBS-Storage-Konfigurationen bereits bekannt
  (`proxmox.py`, `pbs_target`: Server, Fingerprint, Datastore, Namespace).
- Neue Tabelle `pve_pbs_targets (cluster_id, storage_id, pbs_id NULL, server,
  datastore, namespace)`, pro Cluster bei jedem erfolgreichen PVE-Sync ersetzt.
- `pbs_id` über das vorhandene `match_pbs_connection` gegen die PBS-Verbindungen
  **aller** Cluster (ein PBS kann bei einem anderen Cluster registriert sein).
  Nicht zuordenbare Ziele bleiben mit `pbs_id = NULL` und Hostnamen erhalten.

### 3. Sync-Jobs aus PBS

- Pro PBS-Verbindung beim PBS-Sync zusätzlich lesen:
  - `GET /api2/json/admin/sync` – Jobs inkl. Status (`id`, `store`, `ns`,
    `remote`, `remote-store`, `remote-ns`, `sync-direction` (fehlt = `pull`),
    `schedule`, `last-run-state`, `last-run-endtime`, `next-run`).
  - `GET /api2/json/config/remote` – `name`, `host`, `fingerprint`.
- Neue Tabelle `pbs_sync_jobs (pbs_id, job_id, direction, store, ns,
  remote_name, remote_host, remote_fingerprint, remote_store, remote_ns,
  schedule, last_run_ok, last_run_at, next_run_at)`, pro PBS bei erfolgreichem
  Abruf ersetzt. Schlägt der Abruf fehl (z. B. fehlende Rechte), bleiben die
  alten Zeilen stehen und das PBS-Sync-Log bekommt eine Zeile; der Sync gilt
  deshalb **nicht** als fehlgeschlagen.
- Lokale Sync-Jobs ohne Remote (PBS ≥ 3.x) sind Kanten innerhalb desselben PBS.
- Bei `push` ist der registrierte PBS die Quelle, sonst das Ziel.
- Der Remote wird zur Anzeige mit `match_pbs_connection` einem registrierten
  PBS zugeordnet (Fingerprint, Host, IP); sonst wird er mit Remote-Namen/Host
  als „nicht registriert“ gezeichnet.

### 4. Graph pro Cluster (Service, rein, testbar)

`app/services/pbs_topology.py` baut aus den drei Quellen einen Graphen:

- **Knoten** = Datastores (PBS, Store, Namespace), gruppiert nach PBS, mit
  Belegung aus `pbs_datastore_usage` (Ringfüllung, Status, „voll ca.“).
- **Kanten** = Primärkanten (Cluster → Datastore) und Replikationskanten.
- Enthalten sind: alle Primär-Datastores des Clusters, alle Datastores der
  PBS-Verbindungen des Clusters, und – transitiv – Replikationskanten, deren
  Quelle oder Ziel einer dieser Datastores ist (Namespace-Präfix zählt als
  Treffer, damit `pano` auch `pano/muc` abdeckt).
- Spalten links → rechts nach Abstand vom Cluster (Primär = 1, erste Kopie = 2,
  …); Zyklen (gegenseitige Sicherung) werden beim ersten Besuch abgeschnitten,
  die Rückkante wird trotzdem gezeichnet.

## Darstellung

### Cluster-Seite, rechte Spalte unter den Stat-Kästen

- Kästen pro PBS (Name, Sync-Punkt wie bisher), darin je Datastore ein Ring
  (`conic-gradient`, Farbe nach `assess_datastore_usage`: ok/warning/critical),
  Prozent in der Mitte, Store-Name (+ Namespace klein) darunter, bei
  warning/critical „voll ca. MM/JJ“. Tooltip wie heute (belegt/gesamt, Prognose).
- Primärkante: Pfeil vom Cluster-Kasten zum Datastore.
- Replikationskante: gestrichelter Pfeil Datastore → Datastore, Beschriftung
  Zeitplan + letzter Lauf („vor 3 h ✓“); letzter Lauf fehlgeschlagen → rot,
  noch nie gelaufen → grau. Tooltip: Job-ID, Namespaces, nächster Lauf.
- Nicht registrierte PBS: gestrichelter Kasten, keine Belegung.
- Gezeichnet als HTML/CSS-Grid mit Spalten; Pfeile als einfache Inline-SVG-Linien
  zwischen den Spalten (kein JS-Layout). Unter 900 px Breite stapeln die Spalten,
  Pfeile werden zu ↓ mit Beschriftung.
- Die Datastore-Chips unter den PBS-Sync-Zeilen links entfallen; die Sync-Zeilen
  (Punkt, Zeit, Log) bleiben.
- Dashboard bleibt unverändert.

### Gast-Symbole in der VM-Tabelle

- Der Statuspunkt wird durch ein Symbol ersetzt: VM = Monitor, CT = Box
  (Inline-SVG, keine neue Icon-Bibliothek). Farbe = bisherige Statusfarbe
  (`status-ok/warning/critical/...`), gelöschte Gäste ausgegraut wie bisher.
- `aria-label`: „VM · ok“ bzw. „Container · kritisch“.
- Filter/Sortierung nach Status unverändert.

Beide Darstellungen nutzen die vorhandenen Smile-Eyes-Tokens (hell/dunkel).

## Fehlerfälle

- Keine PBS-Verbindung, keine PVE-PBS-Storages: Topologie-Block entfällt.
- Datastore ohne Belegungsdaten: grauer Ring „?“.
- Sync-Job zeigt auf unbekannten Datastore: Knoten ohne Belegung.

## Tests

- `pbs.py`: Parsen von `/admin/sync` und `/config/remote` (pull, push, lokal,
  fehlende Felder).
- `pbs_topology.py`: Primär + Kopie, Namespace-Präfix, gegenseitige Sicherung
  (Zyklus), nicht registrierter Remote, PBS bei anderem Cluster registriert.
- DB: Ersetzen von `pbs_sync_jobs`/`pve_pbs_targets`, `guest_type`.
- Vorschau im Dev-Stack (hell/dunkel, schmal), Seed um Sync-Job + LXC-Gast
  ergänzen.

## Nicht im Umfang

- Dashboard-Karten, Wochenbericht, Benachrichtigungen bei fehlgeschlagenen
  Sync-Jobs (kann folgen, wenn die Daten da sind).
