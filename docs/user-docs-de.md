# Backup Sentinel -- Benutzerhandbuch

![Screenshot](screenshot-overview.png)

## Inhaltsverzeichnis

1. [Dashboard](#dashboard)
2. [Cluster-Detailseite](#cluster-detailseite)
3. [Berichte](#berichte)
4. [Einstellungen](#einstellungen)
5. [Design & Sprache](#design--sprache)

---

## Dashboard

Das Dashboard (`/`) bietet eine Gesamt-Übersicht über alle überwachten Proxmox-Cluster.

### Gesundheitsleiste (Health Bar)

Am oberen Seitenrand zeigt eine farbige Leiste jeden Cluster als Segment an:

| Farbe  | Bedeutung                                         |
|--------|---------------------------------------------------|
| Grün  | Alle Backups OK, keine Warnungen                  |
| Gelb   | Mindestens eine Warnung (z. B. veralteter Sync)   |
| Rot    | Kritische Probleme (fehlgeschlagene Backups, Sync-Fehler) |
| Grau   | Nie synchronisiert / unbekannter Status           |

Ein **stale**-Badge erscheint, wenn Syncs über längere Zeit fehlschlagen. Klicken Sie auf ein Segment, um zur Cluster-Detailseite zu gelangen.

### KPI-Karten

Unterhalb der Gesundheitsleiste werden vier Zusammenfassungskarten angezeigt:

- **Restore-Abdeckung** -- Ein Ringdiagramm mit dem Prozentsatz der VMs, die einen dokumentierten Restore-Test haben (z. B. 85 %). Die Farbe wechselt von Grün (>= 80 %) über Gelb (>= 50 %) zu Rot (< 50 %).
- **Restore überfällig** -- Anzahl der VMs, deren Restore-Test-Intervall abgelaufen ist.
- **Restore kritisch** -- Anzahl der VMs im kritischen Restore-Test-Status.
- **Diesen Monat getestet** -- Anzahl der im laufenden Kalendermonat dokumentierten Restore-Tests.

### Cluster-Karten

Jeder Cluster wird als Karte dargestellt mit:

- Clustername mit Gesamtstatus-Badge (OK / Warnung / Kritisch).
- Zeitstempel der letzten erfolgreichen PVE-Synchronisation.
- Fehler-seit-Zeitstempel, falls die letzte Synchronisation fehlschlug.
- Anzahl der Nodes, VMs und eine Aufschlüsselung der Backup-Status. Hat der Cluster ein Quorum-Device (QDevice), steht in der Nodes-Kachel ein kleines „+Q“ neben der Zahl (Tooltip: Zustand und QNetd-Host); ist es nicht verbunden, wird die Kachel gelb.
- PBS-Verbindungsstatus (sofern konfiguriert).
- Aktuelle Restore-Test-Ergebnisse als kompakte Icons.

Klicken Sie auf eine Cluster-Karte, um zur Detailseite zu navigieren.

---

## Cluster-Detailseite

Die Cluster-Detailseite (`/clusters/<slug>`) bietet einen detaillierten Einblick in einen einzelnen Proxmox-Cluster.

### Sync-Status

Im oberen Bereich zeigt die Hero-Sektion:

- **Sync-Button** -- Löst eine sofortige Neusynchronisation des Clusters aus.
- **PVE-Sync-Status** -- Ein farbiger Punkt (grün = Erfolg, rot = Fehler, grau = nie synchronisiert) mit dem letzten Sync-Zeitstempel. Klicken Sie auf den Punkt, um das vollständige Sync-Log anzuzeigen.
- **PBS-Sync-Status** -- Eine Zeile pro verbundenem PBS mit demselben Punkt + Zeitstempel-Muster.

### Statistik-Kacheln

Interaktive Statistik-Kacheln zeigen Node-Anzahl, VM-Anzahl, Kritisch/Warnung-Zähler, Verschlüsselungsgrad und Restore-fällig-Anzahl. Klicken Sie auf eine Kachel, um die VM-Tabelle darunter auf passende Zeilen zu filtern.

### Warnung bei unverschlüsselten Backups

Falls VMs unverschlüsselte Backups haben, listet ein aufklappbarer Warnbereich die betroffenen VMs mit dem Datum des letzten unverschlüsselten Backups auf.

### VM-Tabelle

Die Haupttabelle listet jede VM im Cluster mit sortierbaren Spalten:

| Spalte         | Beschreibung                                                |
|----------------|-------------------------------------------------------------|
| 🖥 / 📦         | Gast-Symbol: Monitor = VM, Kiste = Container. Die Farbe ist der Status: grün = alles in Ordnung und mindestens 2 Backup-Standorte, blau = in Ordnung, aber nur ein Backup-Standort (Info, kein Problem), gelb = Warnung, rot = kritisch (schlechtester Wert aus Backup-Alter und Verify); ausgegraut = VM gelöscht. Der Tooltip nennt die Gründe |
| HA             | Der Gast wird von PVE-HA verwaltet. Mit Rahmen, wenn er nur auf bestimmten Nodes laufen darf (oder auf bestimmten nie). Der Tooltip nennt HA-Zustand und Nodes, z. B. „Darf nur laufen auf: pve1 (2), pve2“ (Zahl = Priorität); Quelle sind die Node-Affinity-Regeln (PVE 9) bzw. die HA-Gruppe (ältere PVE) |
| TPL            | Der Gast ist eine Vorlage (Template) in PVE: läuft nicht, dient zum Klonen. Dezent ausgegraut |
| VM             | Gastname; ein kleines **w** bzw. **–** zeigt wöchentliche Backups bzw. „kein Backup vorgesehen“ |
| Node           | Der Proxmox-Node, auf dem die VM läuft                     |
| VMID           | Proxmox-VM-ID                                               |
| Letztes Backup | Zeitstempel des jüngsten Backups; laufende Backups werden in der Zeile als Fortschritt angezeigt |
| 30 Tage        | Ein Balken pro Tag (grün = OK, orange = inzwischen gelöscht, rot = fehlgeschlagen, grau = kein Backup). Bei schmalem Fenster zeigt die Leiste weniger Tage statt dünnerer Balken; die Überschrift nennt dann die Anzahl |
| Schutz         | Quelle (PBS/lokal), Anzahl Server mit Backups der letzten 7 Tage (**×2**) und Verify-Status (**✓**/**?**/**✗**). Nur umrandet, wenn alles passt; farbig bei Warnung/Fehler |
| Restore-Test   | „fällig in N T“, **offen** (noch nie getestet), „N T überfällig“ oder „–“ (kein Test vorgesehen). Der Tooltip zeigt den letzten Test |

Die Zeilen sind abwechselnd hinterlegt. **Ein Klick auf eine Zeile** (erkennbar am ▶ vor dem VM-Namen) öffnet darunter die Details: die Einstellungen der VM und den Verlauf der letzten 30 Tage mit einem Balken pro Tag (Größe, Dauer, Durchsatz, wiederverwendete Daten). Zugeklappt wird mit **Einklappen**, einem Klick auf eine freie Stelle der Details oder erneut auf die Zeile.

**Node-Filterleiste** -- Buttons oberhalb der Tabelle filtern VMs nach Node.

**Sortierung** -- Klicken Sie auf einen Spaltenkopf, um auf-/absteigend zu sortieren.

**Backup-Frequenz** -- In den Details einer VM (Klick auf die Zeile) lässt sich die automatisch erkannte Frequenz überschreiben (täglich / wöchentlich / nie / auto).

**Restore-Test-Fälligkeit** -- Die Restore-Test-Spalte warnt rechtzeitig vor Ablauf des Test-Intervalls. Der Vorlauf richtet sich nach dem Intervall: bei **jährlichen** Intervallen erscheint die **Warnung 1 Monat** vorher, bei **quartalsweisen** (oder kürzeren) Intervallen **2 Wochen** vorher. Nach Ablauf des Intervalls wird der Eintrag rot („N T überfällig“). Der Tooltip zeigt den letzten Test und das Intervall.

**Restore-Intervall** -- In den Details einer VM legt ein Auswahlfeld fest, wie oft eine VM getestet werden muss: **90 T**, **180 T** (Standard), **365 T** oder **kein** Test nötig. Bei wöchentlichen Backups gelten mindestens 365 Tage. Eine VM, die noch nie getestet wurde, steht auf **offen**: Sie zählt als fällig, aber nicht als kritisch überfällig. Im Formular *Wiederherstellung dokumentieren* lassen sich mehrere VMs auf einmal auswählen (Strg/⌘), z. B. für eine komplette Restore-Übung.

**Zweitkopie und Verify** -- Die Pill in der Spalte Schutz zeigt, auf wie vielen Servern die Backups der letzten 7 Tage liegen (z. B. **×2**), und den Verify-Status des PBS: **✓** verifiziert, **?** seit über 30 Tagen nicht verifiziert, **✗** Verify fehlgeschlagen. Grün = mindestens 2 Server und verifiziert; blau = ein Server und verifiziert (in Ordnung, nur ohne Zweitkopie); gelb = nicht verifiziert; rot = Verify fehlgeschlagen (dazu kommt eine Benachrichtigung). Ein PBS zählt als ein Server, auch wenn er in PVE unter einem anderen Storage-Namen, per IP oder ohne Fingerabdruck (z. B. mit ACME-Zertifikat) eingetragen ist: Backup Sentinel ordnet den PVE-Storage der registrierten PBS-Verbindung zu (Fingerabdruck, Hostname, Kurzname oder aufgelöste IP) und zeigt im Tooltip die Servernamen.

**Backup-Ziele und Replikation** -- Rechts im Cluster-Kopf zeigt ein Schaubild, wohin die Backups gehen: vom Cluster (PVE) zu den Datastores, in die PVE direkt sichert (laut PBS-Storages in der PVE-Konfiguration), und von dort über die Sync-Jobs der PBS weiter zu ihren Kopien (Pull, Push und lokale Syncs; gestrichelte Linie, blau = letzter Lauf OK, gelb = mit Warnungen, rot = fehlgeschlagen, grau = noch nie gelaufen). Direkte Verbindungen sind breite Leitungen mit gefüllter Pfeilspitze in Datenrichtung, Replikationen bestehen aus Blöcken; Linien, die außen herum laufen, sind dünn. In Ruhe sind die Leitungen gedämpft und stehen still. Läuft ein Backup oder ein Sync, leuchtet seine Leitung auf, helle Datenpakete fließen hindurch, ein Lichtstreifen läuft entlang und die Blöcke einer Replikation bewegen sich (bei Backups die Linie zu den Storages, in die die Backup-Jobs der VM schreiben). „PVE · n VMs“ zählt nur direkte Backups laut den Backup-Jobs von PVE (Auswahl nach VMIDs, alle mit Ausschlüssen, Pool, Node); Kopien, die ein Sync-Job in denselben Datastore holt, zählen nicht. Ein in PVE eingetragener Storage ohne Backup-Job bekommt keine PVE-Linie. Ein laufender Sync zeigt am Ziel-Datastore denselben drehenden Ring wie ein laufendes Backup mit seinem Fortschritt (aus dem Task-Log des PBS, alle 30 Sekunden nachgeladen) und „läuft seit 22:47“. Zwischen zwei Kästen gibt es immer genau eine waagerechte Verbindung, auch wenn mehrere Datastores oder Jobs dazwischen liegen; sie zeigt den auffälligsten Zustand (läuft vor fehlgeschlagen, Warnung, OK). Ein PBS kann dabei für einen Datastore Primärziel und für einen anderen Kopie sein; sichern sich zwei PBS gegenseitig, gibt es Pfeile in beide Richtungen. Kopien werden je Namespace verfolgt, sodass ein Datastore mit den Kopien mehrerer Cluster nur die eigenen zeigt. Der Sync-Status steht als Punkt im Cluster-Kasten (PVE) und in der Kopfzeile jedes PBS-Kastens (grün = OK, rot = Fehler, grau = noch nie); der Tooltip zeigt die Zeit, ein Klick öffnet das Sync-Log. Das Schaubild wird bei jedem Sync vorberechnet, damit die Seite schnell lädt. Unter jeder Kopie stehen Quelle und letzter Lauf, im Tooltip Zeitplan, Namespaces und nächster Lauf. PBS, die nicht in Backup Sentinel registriert sind, erscheinen gestrichelt mit dem Namen aus der Remote-Konfiguration und ohne Belegung.

**PBS-Speicherplatz** -- Jeder Datastore im Schaubild ist ein Ring mit seiner Belegung und, falls er voll läuft, der Prognose des PBS („voll ca. 04/27“). Gelb ab 80 % oder voll in ≤ 60 Tagen, rot ab 90 % oder voll in ≤ 14 Tagen (dann auch als Benachrichtigung). Läuft ein Datastore laut Prognose in den nächsten 30 Tagen voll, steht ein Warndreieck neben dem Namen (gelb, rot ab ≤ 14 Tagen; Tooltip: Tage und Datum). Eine Prognose in der Vergangenheit (sinkende Belegung) wird ignoriert.

**Laufende Backups** -- Über der Tabelle erscheint für jedes gerade laufende Backup eine Kachel mit Fortschrittsring, geschriebener Datenmenge, voraussichtlichem Ende und Durchsatz (↓ Lesen, ↑ Schreiben, ⇅ wenn beides gleich ist); der ganze Ring dreht sich langsam und pulsiert, sodass auch ein Bogen von wenigen Prozent sichtbar wandert. Läuft ein Backup deutlich länger als üblich, steht „langsam, sonst 40 min“ darunter. Ein Klick springt zur VM. In der Tabelle ist die Zeile bis zum aktuellen Prozentwert eingefärbt und links blau markiert; der Hinweis zeigt Start, ETA und Datenmenge. Die Anzeige aktualisiert sich alle 30 Sekunden, ohne die Seite neu zu laden; die Seite selbst wartet nie auf PVE, sondern holt den Fortschritt direkt nach dem Laden nach. Auf der Übersicht zeigt jede Cluster-Karte, wie viele Backups laufen, und „Replikation läuft“, solange ein Sync-Job eines PBS in die Datastores des Clusters kopiert (Tooltip: Quelle → Ziel).

**Gelöschte VMs** -- VMs, die im Proxmox-Cluster nicht mehr existieren, erscheinen ausgegraut mit dem Badge **gelöscht** (Tooltip: seit wann), solange noch Backups von ihnen vorhanden sind. Sie lösen keine Backup- oder Restore-Warnungen aus. Sobald keine Backups mehr existieren, wird die VM beim nächsten Sync samt Restore-Test-Historie aus der Datenbank entfernt -- allerdings nur, wenn in diesem Sync alle PVE-Nodes, -Speicher und PBS-Datastores erreichbar waren.

**Mobile Ansicht** -- Auf schmalen Bildschirmen (Smartphone) wird die Tabelle automatisch in Karten umgewandelt: pro VM eine Karte mit beschrifteten Feldern, sodass nichts horizontal weggescrollt werden muss.

### Restore-Test-Formular

Unterhalb der VM-Tabelle kann ein neuer Restore-Test dokumentiert werden:

1. VM aus dem Dropdown auswählen.
2. Testdatum wählen.
3. Recovery-Typ auswählen: **Vollständig** (komplette VM-Wiederherstellung), **Teilweise** (partielle Daten) oder **Datei** (einzelne Datei-Recovery).
4. Ergebnis auswählen: **Bestanden**, **Eingeschränkt** (teilweise erfolgreich) oder **Fehlgeschlagen**.
5. Dauer in Minuten eingeben.
6. Optionale Notizen hinzufügen.
7. Absenden.

Der Restore-Test wird in der Datenbank gespeichert und sofort in der VM-Tabelle, den KPI-Karten und den Monatsberichten reflektiert.

### Restore-Test-Verlauf

Eine Tabelle unterhalb des Formulars zeigt die jüngsten Restore-Tests für diesen Cluster, einschließlich VM-Name, Datum, Typ, Ergebnis, Dauer und durchführender Person.

---

## Berichte

Die Berichtsseite (`/reports`) stellt monatliche Backup-Compliance-Berichte bereit.

### Aktuelle Berichtszusammenfassung

Eine Hero-Karte am oberen Rand zeigt aggregierte Statistiken für den aktuellen Berichtszeitraum:

- **Backups gesamt** -- Anzahl der Backup-Events.
- **OK** -- Erfolgreiche Backups.
- **Fehler** -- Fehlgeschlagene Backups.
- **Gelöscht** -- Backups, die vom Speicher entfernt wurden.
- **PDF-Snapshot**-Button -- Erzeugt auf Anforderung einen PDF-Bericht für den aktuellen Monat.

### Cluster-Aufschlüsselung

Unterhalb der Zusammenfassung wird jeder Cluster als aufklappbare Karte dargestellt. Beim Aufklappen werden die VM-Details sichtbar:

- Jede VM ist ein verschachtelter aufklappbarer Bereich.
- VMs mit Fehlern oder Löschungen sind standardmäßig aufgeklappt.
- Die innere Tabelle zeigt einzelne Backup-Events mit Datum, Größe, Verschlüsselungsstatus, Verifikationsstatus und Ergebnis.

### Berichtsarchiv

Am unteren Rand steht eine Liste zuvor erstellter Berichte (PDF und JSON) zum Download bereit. Berichte werden im `/reports`-Volume gespeichert und bleiben über Container-Neustarts hinweg erhalten.

Monatsberichte werden am Ende jedes Monats automatisch erstellt. Sie können jederzeit über den PDF-Snapshot-Button einen Bericht manuell auslösen.

---

## Einstellungen

Die Einstellungsseite (`/settings`) ist die zentrale Verwaltung für Cluster, PBS-Verbindungen und Benachrichtigungen. Sie ist in vier Tabs gegliedert: **Cluster** (Cluster, PBS-Verbindungen, neuer Cluster, Governance), **Benachrichtigungen**, **Erscheinungsbild** und **Änderungsprotokoll**. Der aktive Tab steht in der Adresse (z. B. `/settings#benachrichtigungen`), Links darauf öffnen direkt diesen Tab; die Pfeiltasten wechseln zwischen den Tabs.

### Neuen Cluster hinzufügen

1. **Clustername** eingeben (z. B. `pve-prod`).
2. **PVE-API-URL** eingeben (z. B. `https://pve.example.internal:8006`).
3. Auf **Cluster vorbereiten** klicken.
4. Ein Bootstrap-Skript wird angezeigt. Führen Sie dieses Skript auf einem beliebigen Node des Proxmox-Clusters aus, um einen API-Token zu erstellen und den Cluster automatisch zu registrieren.
5. Die Seite wartet auf den Abschluss -- sobald das Skript fertig ist, erscheint der Cluster in der Liste.

### Cluster verwalten

Jeder registrierte Cluster wird als Karte angezeigt mit:

- **Sync-Button** -- Löst eine sofortige Synchronisation aus.
- **Clustername-Link** -- Navigiert zur Cluster-Detailseite.
- **Umbenennen-Button** -- Ändert den Anzeigenamen.
- **Löschen-Button** -- Entfernt den Cluster und alle zugehörigen Daten.
- **PVE-URL** -- Der konfigurierte API-Endpunkt.
- **Letzte Sync** -- Zeitstempel der letzten erfolgreichen Synchronisation.

### PBS-Verbindungen

Jede Cluster-Karte hat einen "PBS hinzufügen"-Bereich, um einen Proxmox Backup Server anzubinden:

1. **PBS-Name** eingeben (Anzeige-Label).
2. **PBS-API-URL** eingeben.
3. Ein Bootstrap-Skript wird angezeigt, das auf dem PBS-Host ausgeführt werden muss.
4. Nach der Verbindung erscheint der PBS unter dem Cluster mit eigenem Sync-Status.

PBS-Verbindungen können einzeln entfernt werden.

### Benachrichtigungs-Einstellungen

Ein eigener Bereich konfiguriert die Benachrichtigungskanäle:

**Gotify (Push-Benachrichtigungen):**
- Gotify-Server-URL.
- API-Token (verschlüsselt gespeichert).
- Aktivieren/Deaktivieren-Schalter.
- Test-Button zur Verbindungsprüfung.

**E-Mail (SMTP):**
- SMTP-Host, Port, TLS-Modus (SSL/STARTTLS/ohne).
- Benutzername und Passwort (verschlüsselt gespeichert).
- Absenderadresse und Empfängerliste.
- Aktivieren/Deaktivieren-Schalter.
- Test-Button zum Senden einer Test-E-Mail.

**Ruhezeiten (Quiet Hours):**
- Ruhezeiten aktivieren/deaktivieren.
- Start- und Endzeit (z. B. 22:00 -- 07:00).
- Während der Ruhezeiten werden Benachrichtigungen unterdrückt.

### Governance-Panel

Ein Seitenbereich fasst die NIST-CSF-2.0-Ausrichtung zusammen:

- **Identify** -- Asset-Inventar über PVE/PBS-Sync.
- **Protect** -- Verschlüsselungs-Monitoring, Backup-Policy-Durchsetzung.
- **Detect** -- Automatische Alarmierung bei Fehlern und Anomalien.
- **Recover** -- Dokumentierte Restore-Tests mit Compliance-Tracking.

**Wochenbericht** -- Ist E-Mail aktiv und der Schalter *Wochenbericht* gesetzt (Standard), kommt jeden Montag ab 7 Uhr eine gestaltete HTML-Mail (mit Textversion für Mail-Programme ohne HTML): im Kopf Logo und Schriftzug wie in der Web-Oberfläche, oben das Urteil der Woche („Alles im grünen Bereich“ oder „N Punkte brauchen Aufmerksamkeit“), die Kennzahlen und je Cluster eine Zeile mit VMs, Nodes, Verschlüsselung und dem, was Aufmerksamkeit braucht (kritisch, Warnungen, fehlgeschlagene Backups, fällige Restore-Tests, Sync-Fehler), darunter die Probleme -- Backups, Verifies, fast volle Datastores (mit Füllstand) -- und als Info die Voll-Läufe der Woche und VMs mit nur einem Backup-Standort, zum Schluss der Stand der Restore-Tests. VM-Namen verlinken auf ihren Cluster (dafür muss `APP_URL` gesetzt sein). Der Button **Wochenbericht-Vorschau** zeigt den Bericht so, wie er jetzt verschickt würde. War der Server montags aus, wird der Bericht im Lauf der Woche nachgeholt.

### Einstellungs-Auditlog

Änderungen an Einstellungen werden mit Zeitstempel und dem ändernden Benutzer protokolliert (abgeleitet aus den OAuth2-Proxy-Headern).

---

## Design & Sprache

### Design umschalten

Backup Sentinel unterstützt drei Design-Modi, erreichbar über die Kopfzeile:

- **Hell** -- Heller Hintergrund mit dunklem Text.
- **Dunkel** -- Dunkler Hintergrund mit hellem Text.
- **Automatisch** -- Folgt der Betriebssystem-Einstellung (`prefers-color-scheme`).

Die Auswahl wird in einem Browser-Cookie gespeichert und bleibt sitzungsübergreifend erhalten.

### Sprache umschalten

Die Anwendung ist vollständig zweisprachig (Deutsch und Englisch). Wechseln Sie die Sprache über das Kopfzeilen-Menu. Die Auswahl wird im Cookie `bsentinel-lang` gespeichert.

Alle UI-Beschriftungen, Benachrichtigungen und generierten Berichte berücksichtigen die gewählte Sprache.
