# Backup Sentinel -- Benutzerhandbuch

## Inhaltsverzeichnis

1. [Dashboard](#dashboard)
2. [Cluster-Detailseite](#cluster-detailseite)
3. [Berichte](#berichte)
4. [Einstellungen](#einstellungen)
5. [Design & Sprache](#design--sprache)

---

## Dashboard

Das Dashboard (`/`) bietet eine Gesamt-Uebersicht ueber alle ueberwachten Proxmox-Cluster.

### Gesundheitsleiste (Health Bar)

Am oberen Seitenrand zeigt eine farbige Leiste jeden Cluster als Segment an:

| Farbe  | Bedeutung                                         |
|--------|---------------------------------------------------|
| Gruen  | Alle Backups OK, keine Warnungen                  |
| Gelb   | Mindestens eine Warnung (z. B. veralteter Sync)   |
| Rot    | Kritische Probleme (fehlgeschlagene Backups, Sync-Fehler) |
| Grau   | Nie synchronisiert / unbekannter Status           |

Ein **stale**-Badge erscheint, wenn Syncs ueber laengere Zeit fehlschlagen. Klicken Sie auf ein Segment, um zur Cluster-Detailseite zu gelangen.

### KPI-Karten

Unterhalb der Gesundheitsleiste werden vier Zusammenfassungskarten angezeigt:

- **Restore-Abdeckung** -- Ein Ringdiagramm mit dem Prozentsatz der VMs, die einen dokumentierten Restore-Test haben (z. B. 85 %). Die Farbe wechselt von Gruen (>= 80 %) ueber Gelb (>= 50 %) zu Rot (< 50 %).
- **Restore ueberfaellig** -- Anzahl der VMs, deren Restore-Test-Intervall abgelaufen ist.
- **Restore kritisch** -- Anzahl der VMs im kritischen Restore-Test-Status.
- **Diesen Monat getestet** -- Anzahl der im laufenden Kalendermonat dokumentierten Restore-Tests.

### Cluster-Karten

Jeder Cluster wird als Karte dargestellt mit:

- Clustername mit Gesamtstatus-Badge (OK / Warnung / Kritisch).
- Zeitstempel der letzten erfolgreichen PVE-Synchronisation.
- Fehler-seit-Zeitstempel, falls die letzte Synchronisation fehlschlug.
- Anzahl der Nodes, VMs und eine Aufschluesselung der Backup-Status.
- PBS-Verbindungsstatus (sofern konfiguriert).
- Aktuelle Restore-Test-Ergebnisse als kompakte Icons.

Klicken Sie auf eine Cluster-Karte, um zur Detailseite zu navigieren.

---

## Cluster-Detailseite

Die Cluster-Detailseite (`/clusters/<slug>`) bietet einen detaillierten Einblick in einen einzelnen Proxmox-Cluster.

### Sync-Status

Im oberen Bereich zeigt die Hero-Sektion:

- **Sync-Button** -- Loest eine sofortige Neusynchronisation des Clusters aus.
- **PVE-Sync-Status** -- Ein farbiger Punkt (gruen = Erfolg, rot = Fehler, grau = nie synchronisiert) mit dem letzten Sync-Zeitstempel. Klicken Sie auf den Punkt, um das vollstaendige Sync-Log anzuzeigen.
- **PBS-Sync-Status** -- Eine Zeile pro verbundenem PBS mit demselben Punkt + Zeitstempel-Muster.

### Statistik-Kacheln

Interaktive Statistik-Kacheln zeigen Node-Anzahl, VM-Anzahl, Kritisch/Warnung-Zaehler, Verschluesselungsgrad und Restore-faellig-Anzahl. Klicken Sie auf eine Kachel, um die VM-Tabelle darunter auf passende Zeilen zu filtern.

### Warnung bei unverschluesselten Backups

Falls VMs unverschluesselte Backups haben, listet ein aufklappbarer Warnbereich die betroffenen VMs mit dem Datum des letzten unverschluesselten Backups auf.

### VM-Tabelle

Die Haupttabelle listet jede VM im Cluster mit sortierbaren Spalten:

| Spalte         | Beschreibung                                                |
|----------------|-------------------------------------------------------------|
| Node           | Der Proxmox-Node, auf dem die VM laeuft                     |
| VM             | Gastname                                                    |
| VMID           | Proxmox-VM-ID                                               |
| Letztes Backup | Zeitstempel des juengsten Backups; zeigt eine Fortschrittsanzeige bei laufendem Backup |
| 30 Tage        | Sparkline mit taeglichem Backup-Status der letzten 30 Tage (gruen = OK, rot = fehlgeschlagen, grau = kein Backup) |
| Backup         | Backup-Art (Snapshot/Stop/Suspend), Schweregrad-Badge, Quelle (Lokal/PBS) und Policy-Override-Dropdown |
| Restore-Test   | Letztes Restore-Test-Ergebnis (bestanden/eingeschraenkt/fehlgeschlagen), Recovery-Typ-Icon, Datum und Dauer |

**Node-Filterleiste** -- Buttons oberhalb der Tabelle filtern VMs nach Node.

**Sortierung** -- Klicken Sie auf einen Spaltenkopf, um auf-/absteigend zu sortieren.

**Backup-Policy-Override** -- Verwenden Sie das Dropdown in der Backup-Spalte, um die automatisch erkannte Policy fuer eine VM zu ueberschreiben (taeglich / woechentlich / nie / automatisch).

### Restore-Test-Formular

Unterhalb der VM-Tabelle kann ein neuer Restore-Test dokumentiert werden:

1. VM aus dem Dropdown auswaehlen.
2. Testdatum waehlen.
3. Recovery-Typ auswaehlen: **Vollstaendig** (komplette VM-Wiederherstellung), **Teilweise** (partielle Daten) oder **Datei** (einzelne Datei-Recovery).
4. Ergebnis auswaehlen: **Bestanden**, **Eingeschraenkt** (teilweise erfolgreich) oder **Fehlgeschlagen**.
5. Dauer in Minuten eingeben.
6. Optionale Notizen hinzufuegen.
7. Absenden.

Der Restore-Test wird in der Datenbank gespeichert und sofort in der VM-Tabelle, den KPI-Karten und den Monatsberichten reflektiert.

### Restore-Test-Verlauf

Eine Tabelle unterhalb des Formulars zeigt die juengsten Restore-Tests fuer diesen Cluster, einschliesslich VM-Name, Datum, Typ, Ergebnis, Dauer und durchfuehrender Person.

---

## Berichte

Die Berichtsseite (`/reports`) stellt monatliche Backup-Compliance-Berichte bereit.

### Aktuelle Berichtszusammenfassung

Eine Hero-Karte am oberen Rand zeigt aggregierte Statistiken fuer den aktuellen Berichtszeitraum:

- **Backups gesamt** -- Anzahl der Backup-Events.
- **OK** -- Erfolgreiche Backups.
- **Fehler** -- Fehlgeschlagene Backups.
- **Geloescht** -- Backups, die vom Speicher entfernt wurden.
- **PDF-Snapshot**-Button -- Erzeugt auf Anforderung einen PDF-Bericht fuer den aktuellen Monat.

### Cluster-Aufschluesselung

Unterhalb der Zusammenfassung wird jeder Cluster als aufklappbare Karte dargestellt. Beim Aufklappen werden die VM-Details sichtbar:

- Jede VM ist ein verschachtelter aufklappbarer Bereich.
- VMs mit Fehlern oder Loeschungen sind standardmaessig aufgeklappt.
- Die innere Tabelle zeigt einzelne Backup-Events mit Datum, Groesse, Verschluesselungsstatus, Verifikationsstatus und Ergebnis.

### Berichtsarchiv

Am unteren Rand steht eine Liste zuvor erstellter Berichte (PDF und JSON) zum Download bereit. Berichte werden im `/reports`-Volume gespeichert und bleiben ueber Container-Neustarts hinweg erhalten.

Monatsberichte werden am Ende jedes Monats automatisch erstellt. Sie koennen jederzeit ueber den PDF-Snapshot-Button einen Bericht manuell ausloesen.

---

## Einstellungen

Die Einstellungsseite (`/settings`) ist die zentrale Verwaltung fuer Cluster, PBS-Verbindungen und Benachrichtigungen.

### Neuen Cluster hinzufuegen

1. **Clustername** eingeben (z. B. `pve-prod`).
2. **PVE-API-URL** eingeben (z. B. `https://pve.example.internal:8006`).
3. Auf **Cluster vorbereiten** klicken.
4. Ein Bootstrap-Skript wird angezeigt. Fuehren Sie dieses Skript auf einem beliebigen Node des Proxmox-Clusters aus, um einen API-Token zu erstellen und den Cluster automatisch zu registrieren.
5. Die Seite wartet auf den Abschluss -- sobald das Skript fertig ist, erscheint der Cluster in der Liste.

### Cluster verwalten

Jeder registrierte Cluster wird als Karte angezeigt mit:

- **Sync-Button** -- Loest eine sofortige Synchronisation aus.
- **Clustername-Link** -- Navigiert zur Cluster-Detailseite.
- **Umbenennen-Button** -- Aendert den Anzeigenamen.
- **Loeschen-Button** -- Entfernt den Cluster und alle zugehoerigen Daten.
- **PVE-URL** -- Der konfigurierte API-Endpunkt.
- **Letzte Sync** -- Zeitstempel der letzten erfolgreichen Synchronisation.

### PBS-Verbindungen

Jede Cluster-Karte hat einen "PBS hinzufuegen"-Bereich, um einen Proxmox Backup Server anzubinden:

1. **PBS-Name** eingeben (Anzeige-Label).
2. **PBS-API-URL** eingeben.
3. Ein Bootstrap-Skript wird angezeigt, das auf dem PBS-Host ausgefuehrt werden muss.
4. Nach der Verbindung erscheint der PBS unter dem Cluster mit eigenem Sync-Status.

PBS-Verbindungen koennen einzeln entfernt werden.

### Benachrichtigungs-Einstellungen

Ein eigener Bereich konfiguriert die Benachrichtigungskanaele:

**Gotify (Push-Benachrichtigungen):**
- Gotify-Server-URL.
- API-Token (verschluesselt gespeichert).
- Aktivieren/Deaktivieren-Schalter.
- Test-Button zur Verbindungspruefung.

**E-Mail (SMTP):**
- SMTP-Host, Port, TLS-Modus (SSL/STARTTLS/ohne).
- Benutzername und Passwort (verschluesselt gespeichert).
- Absenderadresse und Empfaengerliste.
- Aktivieren/Deaktivieren-Schalter.
- Test-Button zum Senden einer Test-E-Mail.

**Ruhezeiten (Quiet Hours):**
- Ruhezeiten aktivieren/deaktivieren.
- Start- und Endzeit (z. B. 22:00 -- 07:00).
- Waehrend der Ruhezeiten werden Benachrichtigungen unterdrueckt.

### Governance-Panel

Ein Seitenbereich fasst die NIST-CSF-2.0-Ausrichtung zusammen:

- **Identify** -- Asset-Inventar ueber PVE/PBS-Sync.
- **Protect** -- Verschluesselungs-Monitoring, Backup-Policy-Durchsetzung.
- **Detect** -- Automatische Alarmierung bei Fehlern und Anomalien.
- **Recover** -- Dokumentierte Restore-Tests mit Compliance-Tracking.

### Einstellungs-Auditlog

Aenderungen an Einstellungen werden mit Zeitstempel und dem aendernden Benutzer protokolliert (abgeleitet aus den OAuth2-Proxy-Headern).

---

## Design & Sprache

### Design umschalten

Backup Sentinel unterstuetzt drei Design-Modi, erreichbar ueber die Kopfzeile:

- **Hell** -- Heller Hintergrund mit dunklem Text.
- **Dunkel** -- Dunkler Hintergrund mit hellem Text.
- **Automatisch** -- Folgt der Betriebssystem-Einstellung (`prefers-color-scheme`).

Die Auswahl wird in einem Browser-Cookie gespeichert und bleibt sitzungsuebergreifend erhalten.

### Sprache umschalten

Die Anwendung ist vollstaendig zweisprachig (Deutsch und Englisch). Wechseln Sie die Sprache ueber das Kopfzeilen-Menu. Die Auswahl wird im Cookie `bsentinel-lang` gespeichert.

Alle UI-Beschriftungen, Benachrichtigungen und generierten Berichte beruecksichtigen die gewaehlte Sprache.
