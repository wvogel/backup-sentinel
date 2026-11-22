# Backup Sentinel -- Benutzerhandbuch

![Übersicht](screenshot-overview-de.png)

## Inhaltsverzeichnis

1. [Dashboard](#dashboard)
2. [Cluster-Detailseite](#cluster-detailseite)
3. [Berichte](#berichte)
4. [Benachrichtigungen](#benachrichtigungen)
5. [Einstellungen](#einstellungen)
6. [Design & Sprache](#design--sprache)

---

## Dashboard

Das Dashboard (`/`) bietet eine Gesamt-Übersicht über alle überwachten Proxmox-Cluster.

### Restore-Tests

Oben steht ein Kasten *Restore-Tests* (der ganze Kasten führt zu `/restore`). Rechts sechs Kacheln, so breit wie die der Cluster-Zeilen:

- **Queue** -- Restore-Tests, die in der Warteschlange stehen.
- **Abdeckung** -- Anteil der VMs mit dokumentiertem Restore-Test. Grün (>= 80 %), Gelb (>= 50 %), Rot (< 50 %).
- **Getestet** -- VMs mit Restore-Test von denen, die einen brauchen (z. B. „12/83“).
- **Fällig** -- VMs, deren Restore-Test-Intervall abgelaufen ist oder bald abläuft.
- **Kritisch** -- VMs, deren Restore-Test deutlich überfällig ist.
- **Diesen Monat** -- Im laufenden Kalendermonat dokumentierte Restore-Tests.

Darunter links der laufende Restore mit demselben Ring und denselben Details wie auf `/restore` (VM, Phase, gelesene Daten, Durchsatz, voraussichtliches Ende, Disk-Leiste mit Fortschritt), live aktualisiert; beim Hochfahren und Prüfen der Erreichbarkeit zählt ein Countdown mit Balken das Prüffenster herunter (erste Wartezeit plus Timeout), bis der Test aufgibt; läuft nichts, der Zustand der Queue. Rechts die Ringe für RAM und Storages des Restore-Servers.

Ganz unten listet *Wiederherstellungs-Nachweise* die letzten Restore-Tests als Tabelle: VM, Start, Ende, Dauer, Status und die Checks des automatischen Tests (ICMP, ARP, jeder TCP-Port; grün = auf mindestens einer IP des Gasts bestanden). Manuell dokumentierte Tests zeigen ihr Datum und „manuell dokumentiert“. Der Tooltip der Dauer zeigt, wo die Zeit blieb (Vorbereitung, Backup prüfen, Helfer-Container, Wiederherstellen mit Durchsatz, Netzwerk anpassen, Hochfahren, Erreichbarkeit prüfen). *Hochfahren* läuft vom Start-Befehl bis zum ersten grünen Check (ICMP, ARP oder ein Port), danach zählt *Erreichbarkeit prüfen*. Wird kein Check grün, steht bei Hochfahren „–“ – wie schnell der Gast hochfuhr, ist dann unbekannt, nicht dass er nicht hochfuhr; die ganze Zeit ab dem Start zählt dann zu *Erreichbarkeit prüfen*. Die Aufteilung gibt es auch für Läufe, die nach einem Update der Anwendung fortgesetzt wurden: Jeder Phasenwechsel wird mit Zeitpunkt gespeichert, die Zeit während des Neustarts zählt zur laufenden Phase.

### Cluster-Zeilen

Darunter folgt je Cluster eine Zeile, alle gleich hoch:

- **Name** des Clusters, darunter die **Sync-Symbole**: ein Server-Symbol für PVE und ein Datenbank-Symbol je PBS. Ihre Farbe zeigt, wie aktuell die Daten sind: grün = letzter Sync erfolgreich und höchstens zwei Sync-Intervalle her (ein ausgelassener Lauf ist in Ordnung), gelb = länger her oder der letzte Versuch schlug fehl, rot = seit über 24 Stunden kein erfolgreicher Sync bzw. letzter PBS-Sync fehlgeschlagen, grau = PBS noch nie synchronisiert. Ein gemeinsamer Tooltip nennt für PVE den Zustand, den letzten erfolgreichen Sync und ggf. „Fehlerzustand seit“, für jeden PBS den letzten Sync und sein Ergebnis.
- Neben dem Namen, was **gerade läuft**: „1 Backup“, „2 Backups“, „1 Replikation“ mit drehendem Symbol (Tooltip: die VMs bzw. Quelle → Ziel). Ist wenig Platz, rutschen sie neben die Symbole; die Zeile wird dadurch nicht höher.
- Rechts die **Kacheln**: Nodes, VMs, kritisch, Warnungen, verschlüsselt, Restore fällig. „Restore fällig“ zählt VMs, deren Restore-Test überfällig ist oder noch nie stattfand – nicht die Vorwarnung „fällig in N T“. Kacheln mit Handlungsbedarf sind farbig umrandet. Hat der Cluster ein Quorum-Device (QDevice), steht in der Nodes-Kachel ein kleines „+Q“ neben der Zahl (Tooltip: Zustand und QNetd-Host); ist es nicht verbunden, wird die Kachel gelb.

Klicken Sie auf eine Cluster-Zeile, um zur Detailseite zu navigieren.

---

## Cluster-Detailseite

Die Cluster-Detailseite (`/clusters/<slug>`) bietet einen detaillierten Einblick in einen einzelnen Proxmox-Cluster.

![Cluster-Detailseite](screenshot-cluster-de.png)

### Sync-Status

Im oberen Bereich zeigt die Hero-Sektion:

- **Sync-Button** -- Löst eine sofortige Neusynchronisation des Clusters aus.
- **Sync-Status** -- Im Schaubild (siehe *Backup-Ziele und Replikation*) ist das Server-Symbol des Clusters (PVE) und das Datenbank-Symbol jedes PBS farbig wie auf der Übersicht; der Tooltip nennt Zustand und letzten Sync. Ohne Schaubild stehen die Symbole in der Kopfzeile.
- **Sync-Log** -- Ein Klick auf ein Sync-Symbol öffnet das Log in einem Fenster über der Seite, ans Ende gescrollt. Das Kopier-Symbol oben rechts kopiert das ganze Log (ein Haken bestätigt es); ×, Esc oder ein Klick daneben schließen es. Läuft gerade ein Sync, erscheint dort das Live-Log. Dasselbe Fenster zeigt auch die Notiz eines Restore-Tests.

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
| Schutz         | Quelle (PBS/lokal), Anzahl Server, die ein Backup halten (**×2**; aktuell = 7 Tage, 9 bei wöchentlichem Sync-Job) und Verify-Status (**✓**/**?**/**✗**). Nur umrandet, wenn alles passt; farbig bei Warnung/Fehler |
| Restore-Test   | „fällig in N T“, **offen** (noch nie getestet), „N T überfällig“ oder „–“ (kein Test vorgesehen). Der Tooltip zeigt den letzten Test |

Die Zeilen sind abwechselnd hinterlegt. **Ein Klick auf eine Zeile** (erkennbar am ▶ vor dem VM-Namen) öffnet darunter die Details: die Einstellungen der VM, den Verlauf der letzten 30 Tage mit einem Balken pro Tag (Größe, Dauer, Durchsatz, wiederverwendete Daten) und ihre letzten 10 Restore-Tests – automatische Läufe mit jedem Ergebnis (auch abgebrochen oder Fehler) samt Auslöser, Dauer (Phasen im Tooltip), Checks und Log sowie manuell dokumentierte Tests. Zugeklappt wird mit **Einklappen**, einem Klick auf eine freie Stelle der Details oder erneut auf die Zeile.

**Node-Filterleiste** -- Buttons oberhalb der Tabelle filtern VMs nach Node. **Probleme (n)** am Ende zeigt nur VMs mit einem Problem: Backup kritisch oder Warnung, Restore-Test kritisch überfällig, oder Verify fehlgeschlagen bzw. seit über 30 Tagen nicht verifiziert. Eine nie getestete VM („offen“) zählt allein nicht; n ist die Zahl dieser VMs.

**Sortierung** -- Klicken Sie auf einen Spaltenkopf, um auf-/absteigend zu sortieren.

**Backup-Frequenz** -- In den Details einer VM (Klick auf die Zeile) lässt sich die automatisch erkannte Frequenz überschreiben (täglich / wöchentlich / nie / auto).

**Restore-Test-Fälligkeit** -- Die Restore-Test-Spalte warnt rechtzeitig vor Ablauf des Test-Intervalls. Der Vorlauf richtet sich nach dem Intervall: bei **jährlichen** Intervallen erscheint die **Warnung 1 Monat** vorher, bei **quartalsweisen** (oder kürzeren) Intervallen **2 Wochen** vorher. Nach Ablauf des Intervalls wird der Eintrag rot („N T überfällig“). Der Tooltip zeigt das Ergebnis des letzten Laufs mit Datum und getrennt davon den letzten gültigen Test (bestanden, eingeschränkt oder manuell dokumentiert) – nur dieser setzt die Fälligkeit zurück, ein abgebrochener oder fehlgeschlagener Lauf nicht. Die Symbole von links: **Uhr** = automatischer Restore-Test an (blau) oder aus (grau), dann der Turnus, der Punkt für das jüngste Ergebnis (letzter Lauf oder letzter gültiger Test, je nachdem, was neuer ist; ein abgebrochener Lauf zählt dafür nicht) und rechts der **Kreispfeil** für den aktuellen Stand — steht er still, wartet die VM in der Warteschlange; dreht er sich, läuft ihr Restore gerade. Ganz rechts zeigt eine **Stoppuhr mit +**, dass für die VM ein eigener Timeout eingestellt ist; der Tooltip nennt die Minuten.

**Restore-Intervall** -- In den Details einer VM legt ein Auswahlfeld fest, wie oft eine VM getestet werden muss: **90 T**, **180 T** (Standard), **365 T** oder **kein** Test nötig. Bei wöchentlichen Backups gelten mindestens 365 Tage. Eine VM, die noch nie getestet wurde, steht auf **offen**: Sie zählt als fällig, aber nicht als kritisch überfällig. Im Formular *Restore-Test dokumentieren* lassen sich mehrere VMs auf einmal auswählen (Strg/⌘), z. B. für eine komplette Restore-Übung.

**Zweitkopie und Verify** -- Die Pill in der Spalte Schutz zeigt, auf wie vielen Servern Backups des Gasts liegen (z. B. **×2**), und den Verify-Status des PBS: **✓** verifiziert, **?** seit über 30 Tagen nicht verifiziert, **✗** Verify fehlgeschlagen. Grün = mindestens 2 Server mit aktueller Kopie und verifiziert; gelb = eine Zweitkopie ist veraltet (ihr Sync läuft nicht mehr); blau = ein Server und verifiziert (in Ordnung, nur ohne Zweitkopie); gelb = nicht verifiziert; rot = Verify fehlgeschlagen (dazu kommt eine Benachrichtigung). Ein fehlgeschlagener Verify zählt, bis ein späteres Backup erfolgreich verifiziert ist; ein neueres, noch nicht verifiziertes Backup reicht nicht, weil es die meisten Chunks mit dem fehlerhaften teilt. Ein PBS zählt als ein Server, auch wenn er in PVE unter einem anderen Storage-Namen, per IP oder ohne Fingerabdruck (z. B. mit ACME-Zertifikat) eingetragen ist: Backup Sentinel ordnet den PVE-Storage der registrierten PBS-Verbindung zu (Fingerabdruck, Hostname, Kurzname oder aufgelöste IP) und zeigt im Tooltip die Servernamen. Als aktuell zählt eine Kopie, wenn ihr letztes Backup höchstens 7 Tage alt ist; wird der Server von einem wöchentlichen Sync-Job beliefert, 9 Tage (eine Woche zwischen den Kopien plus Backup-Tag und Puffer). Eine ältere Kopie zählt bei **×N** mit, die Pill wird dann aber gelb, bis wieder 2 Server aktuell sind. Der Tooltip nennt je Server die letzte Kopie und markiert zu alte als „veraltet“. Ein Backup, das gerade noch synchronisiert oder geschrieben wird, zählt erst als Kopie, wenn es fertig ist (PBS kennt seine Größe erst am Ende); bis dahin wird es auch nicht für einen Restore-Test angeboten. Ziel-Namespaces von Sync-Jobs, die Backups dieses Clusters kopieren, liest Backup Sentinel automatisch mit.

**Backup-Ziele und Replikation** -- Rechts im Cluster-Kopf zeigt ein Schaubild, wohin die Backups gehen: vom Cluster (PVE) zu den Datastores, in die PVE direkt sichert (laut PBS-Storages in der PVE-Konfiguration), und von dort über die Sync-Jobs der PBS weiter zu ihren Kopien (Pull, Push und lokale Syncs; gestrichelte Linie, blau = letzter Lauf OK, gelb = mit Warnungen, rot = fehlgeschlagen, grau = noch nie gelaufen). Direkte Verbindungen sind breite Leitungen mit gefüllter Pfeilspitze in Datenrichtung, Replikationen bestehen aus Blöcken; Linien, die außen herum laufen, sind dünn. In Ruhe sind die Leitungen gedämpft und stehen still. Läuft ein Backup oder ein Sync, leuchtet seine Leitung auf, bei einem Backup wandern Chevrons (›) durch die Leitung bis in die Pfeilspitze (die Linie zu den Storages, in die die Backup-Jobs der VM schreiben), bei einer Replikation bewegen sich die Blöcke und ein weicher Lichtstreifen läuft hindurch. Alle Animationen laufen im Takt der Uhr: Das Nachladen alle 30 Sekunden oder ein Neuladen der Seite setzt sie nicht zurück, sie laufen nahtlos weiter. „PVE · n VMs“ zählt nur direkte Backups laut den Backup-Jobs von PVE (Auswahl nach VMIDs, alle mit Ausschlüssen, Pool, Node); Kopien, die ein Sync-Job in denselben Datastore holt, zählen nicht. Ein in PVE eingetragener Storage ohne Backup-Job bekommt keine PVE-Linie. Daneben zeigt „Sync · n VMs“ bei einem Datastore, in den Sync-Jobs kopieren, wie viele Gäste dieses Clusters dort eine aktuelle Kopie liegen haben, ohne die direkt dorthin gesicherten (Stand des letzten Syncs). Aktuell heißt wie in der Schutz-Spalte: höchstens 7 Tage alt, bei wöchentlichen Sync-Jobs 9. Gäste, von denen dort nur ältere Kopien liegen – der Sync bringt sie nicht mehr –, stehen gelb als „n veraltet“ dahinter; der Tooltip nennt sie mit dem Datum ihrer letzten Kopie. Ein laufender Sync zeigt in seiner eigenen Zeile unter dem Ziel-Datastore einen kleinen drehenden Ring mit seinem Fortschritt (aus dem Task-Log des PBS, alle 30 Sekunden nachgeladen); laufen mehrere Syncs in denselben Datastore, steht jeder mit seinem Ring untereinander. „Läuft seit“ steht im Tooltip des Rings. Ist der Sync fertig, zeigt die Zeile wieder Haken und „gerade fertig“. Zwischen zwei Kästen gibt es immer genau eine waagerechte Verbindung, auch wenn mehrere Datastores oder Jobs dazwischen liegen; sie zeigt den auffälligsten Zustand (läuft vor fehlgeschlagen, Warnung, OK). Ein PBS kann dabei für einen Datastore Primärziel und für einen anderen Kopie sein; sichern sich zwei PBS gegenseitig, gibt es Pfeile in beide Richtungen. Kopien werden je Namespace verfolgt, sodass ein Datastore mit den Kopien mehrerer Cluster nur die eigenen zeigt. Der Sync-Status steckt im Symbol vor dem Cluster-Namen (PVE) und vor dem Namen jedes PBS (Farben wie auf der Übersicht); der Tooltip zeigt Zustand und Zeit, ein Klick öffnet das Sync-Log. Das Schaubild wird bei jedem Sync vorberechnet, damit die Seite schnell lädt. Unter jeder Kopie stehen Quelle (mit Namespace, falls gesetzt) und letzter Lauf, im Tooltip Zeitplan, Namespaces und nächster Lauf. PBS, die nicht in Backup Sentinel registriert sind, erscheinen gestrichelt mit dem Namen aus der Remote-Konfiguration und ohne Belegung.

**PBS-Speicherplatz** -- Jeder Datastore im Schaubild ist ein Ring mit seiner Belegung und, falls er voll läuft, der Prognose des PBS („voll ca. 04/27“). Gelb ab 80 % oder voll in ≤ 60 Tagen, rot ab 90 % oder voll in ≤ 14 Tagen (dann auch als Benachrichtigung). Läuft ein Datastore laut Prognose in den nächsten 30 Tagen voll, steht ein Warndreieck neben dem Namen (gelb, rot ab ≤ 14 Tagen; Tooltip: Tage und Datum). Eine Prognose in der Vergangenheit (sinkende Belegung) wird ignoriert.

**Laufende Backups** -- Über der Tabelle erscheint für jedes gerade laufende Backup eine Kachel mit Fortschrittsring, geschriebener Datenmenge, voraussichtlichem Ende und Durchsatz (↓ Lesen, ↑ Schreiben, ⇅ wenn beides gleich ist); der ganze Ring dreht sich langsam und pulsiert, sodass auch ein Bogen von wenigen Prozent sichtbar wandert. Das voraussichtliche Ende trennt bei einem vollständigen Lesedurchlauf (Dirty-Bitmap ungültig oder neu) echte Daten von leerem Platz: Leere Bereiche laufen mit mehreren GiB/s durch, Daten mit der Geschwindigkeit des Storage. Gerechnet wird die restliche Datenmenge mit der aktuellen Datenrate (Median der letzten 30 Minuten), nicht mit dem Schnitt des ganzen Laufs – nach einem großen leeren Bereich wäre der viel zu optimistisch. Wie viele Daten noch kommen, liefert bei dateibasierten Storages (qcow2/raw auf Verzeichnis, NFS, CIFS) die belegte Größe der Disks; ohne sie wird angenommen, dass der Rest dasselbe Verhältnis aus Daten und leerem Platz hat wie das bisher Gelesene. Darunter zeigt eine Disk-Leiste (wie bei laufenden Restores) jede Disk mit ihrer Größe und Storage-Farbe, gefüllt bis zu dem Teil, der schon gesichert ist; die Disk in Arbeit pulsiert an ihrer Kante. PVE sichert die Disks nacheinander, in der Reihenfolge des Logs („include disk …“). Bei einem inkrementellen Lauf zählt je Disk nur ihr geänderter Anteil (Dirty-Bitmap), eine unveränderte Disk ist sofort voll. Läuft ein Backup deutlich länger als üblich, steht „langsam, sonst 40 min“ darunter. Ein Klick springt zur VM. In der Tabelle ist die Zeile bis zum aktuellen Prozentwert eingefärbt und links blau markiert; der Hinweis zeigt Start, ETA und Datenmenge. Die Anzeige aktualisiert sich alle 30 Sekunden, ohne die Seite neu zu laden; die Seite selbst wartet nie auf PVE, sondern holt den Fortschritt direkt nach dem Laden nach. Auf der Übersicht zeigt jede Cluster-Zeile, wie viele Backups laufen (liegt nichts Aktuelles vor, wartet die Seite dafür bis zu 4 Sekunden auf PVE), und „1 Replikation“, solange ein Sync-Job eines PBS in die Datastores des Clusters kopiert (Tooltip: Quelle → Ziel).

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

Die Berichtsseite (`/reports`) erstellt den **NIS2-Nachweis** zur Datensicherung (NIS2 Art. 21 (2) c, BSIG § 30 (1) Nr. 3): Er belegt lückenlose Backups und protokollierte Restore-Tests. Der Bericht richtet sich an IT-Leitung, Geschäftsführung und Auditoren: vorne eine Management-Zusammenfassung, dahinter die Belege.

### Die beiden Kennzahlen

- **Backup-Lückenlosigkeit** -- Anteil der Zeit, in der jede VM ein erfolgreiches Backup hatte, das nicht älter als ihr RPO war (Richtlinie der VM, Standard 24 h, wöchentlich gesicherte VMs mindestens 180 h). Gemessen wird jede VM mit Backup-Pflicht, auch VMs ohne eingerichteten Backup-Job (wie täglich), ab dem Zeitpunkt, an dem Backup Sentinel sie zuerst gesehen hat. Durch Aufbewahrung später gelöschte Backups zählen mit. Eine **Lücke** liegt erst vor, wenn zwischen zwei erfolgreichen Backups mehr als **RPO + 12 h** liegen; verschobene Startzeiten zählen also nicht. Liegt eine Lücke vor, zählt die gesamte Zeit über dem RPO. Ziel ≥ 99 %, Hinweis ab 98 %.
- **Restore-Nachweis** -- Anteil der testpflichtigen VMs mit bestandenem (oder eingeschränkt bestandenem) Restore-Test innerhalb ihres Prüfintervalls (Turnus der VM), automatisch oder dokumentiert, am letzten Tag des Zeitraums. Ziel ≥ 90 %, Hinweis ab 80 %. Ein nicht voll bestandener Versuch entfällt im ganzen Bericht, wenn dieselbe VM danach im selben Monat einen Test bestanden hat.

Prozentwerte sind ganze Zahlen und werden **abgeschnitten, nie aufgerundet**: 100 % steht nur, wenn nichts gefehlt hat. Die Farbe richtet sich nach der gedruckten Zahl. Die Gesamtbewertung („Erfüllt“, „Erfüllt mit Hinweisen“, „Handlungsbedarf“) ist die schlechtere der beiden Kennzahlen. Ist eine VM am Ende des Zeitraums außerhalb ihres RPO, lautet sie mindestens „Hinweis“.

### Die Seite

Oben stehen die Werte des laufenden Monats bis heute: Lückenlosigkeit, Restore-Nachweis, Lücken, VMs ohne gültigen Test und die Gesamtbewertung. **Vorschau** öffnet den Bericht im Browser. **Zwischenbericht erstellen** archiviert den aktuellen Stand als PDF. Darunter steht je Cluster eine Zeile mit denselben Kacheln und der Bewertung; die Kachel **Vorschau** zeigt den Bericht nur dieses Clusters.

### Der Bericht (PDF)

1. **Gesamtübersicht** -- Bewertung in Klartext, die beiden Kennzahlen als Ringe mit Ziel, vier Zahlen (erfolgreiche Backups, Lücken, bestandene Restore-Tests mit Ø Dauer, VMs ohne gültigen Test), der Verlauf beider Kennzahlen über 12 Monate (Monate vor Beginn der Aufzeichnung bleiben leer, nie 0 %), eine Tabelle der Cluster mit Ampel und Felder zur Kenntnisnahme durch IT-Leitung und Geschäftsführung.
2. **Je Cluster eine Seite** -- Bewertung, Kennzahlen, ein Streifen mit der Lückenlosigkeit je Tag, die Lücken über RPO (letztes Backup davor, nächstes Backup, Stunden über RPO), der Teststatus der VMs (gültig, überfällig, nie getestet), die fälligen Tests und die Restore-Tests des Zeitraums.
3. **Anhang** -- alle Lücken, das vollständige **Restore-Testprotokoll** (jeder abgeschlossene Test mit dem Datum des Snapshots, Ergebnis, Dauer und jeder einzelnen Prüfung samt Ergebnis: ICMP, ARP und jeder TCP-Port), der **Backup-Kalender** je VM und Tag sowie eine Seite zur **Methode**.

Zu jedem Bericht entsteht außerdem je Cluster ein eigenes PDF (Clusterseite mit Kenntnisnahme und Anhängen dieses Clusters) und eine JSON-Datei mit allen berechneten Werten.

### Berichtsarchiv

Am unteren Rand stehen alle erstellten Berichte mit Monat, Erstellungszeit und Art (automatisch oder Zwischenbericht). **Gesamt** lädt den Gesamtbericht, die Cluster-Namen laden das jeweilige Cluster-PDF, **JSON** die Daten. Berichte liegen im `/reports`-Volume. Ein archivierter Bericht ist der eingefrorene Nachweis seines Monats und ändert sich nicht mehr, auch wenn später VMs gelöscht werden. **Löschen** entfernt einen Bericht samt aller Dateien nach einer Rückfrage. Ältere Berichte aus der Zeit vor dem NIS2-Bericht bleiben als PDF/JSON abrufbar.

Der Bericht des Vormonats entsteht beim ersten Sync nach dem Monatswechsel automatisch und geht, wenn E-Mail aktiv ist, als PDF an die Empfänger. Ein Zwischenbericht verhindert den automatischen Monatsbericht nicht.

---

## Benachrichtigungen

Die Benachrichtigungsseite (`/notifications`, Glocken-Symbol in der Kopfzeile) listet die Benachrichtigungen der letzten 30 Tage, neueste zuerst, 50 pro Seite.

- **Filter** -- Typ, Cluster und Kanal (Gotify / E-Mail); **Filter zurücksetzen** hebt sie auf.
- **Spalten** -- Zeit, Typ, Cluster, Nachricht und Kanal. Ein Klick auf eine Nachricht zeigt Titel und vollständigen Text.
- **CSV exportieren** -- Lädt die gefilterte Liste als `notifications.csv` herunter (Semikolon als Trennzeichen).

Aufgeführt sind nur tatsächlich zugestellte Benachrichtigungen. Einträge werden 90 Tage aufbewahrt.

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
- **PVE-URL** -- Der konfigurierte API-Endpunkt, davor das PVE-Sync-Symbol (Farben wie auf der Übersicht, Tooltip mit dem letzten Sync, Klick öffnet das Log).

### PBS-Verbindungen

Jede Cluster-Karte hat einen Button **PBS hinzufügen**, um einen Proxmox Backup Server anzubinden:

1. Auf **PBS hinzufügen** klicken. Ein Bootstrap-Befehl wird angezeigt; einzugeben ist nichts, PBS-Name und API-URL ermittelt das Skript selbst.
2. Den Befehl kopieren und auf dem PBS-Host als root ausführen.
3. Die Seite wartet auf den Abschluss. Nach der Verbindung erscheint der PBS unter dem Cluster mit eigenem Sync-Symbol (Klick öffnet sein Log).

Jeder angebundene PBS hat zwei Felder:

- **Namespace** -- Beschränkt die Verbindung auf diesen Namespace samt Unter-Namespaces, z. B. das Ziel eines Pull-Syncs mit den Kopien dieses Clusters. Leer = nur Root-Namespace.
- **Aliase** -- Weitere Hostnamen oder IPs, unter denen PVE oder ein Sync-Remote diesen PBS kennt (durch Komma getrennt). Nur nötig, wenn Backup Sentinel den PBS nicht selbst erkennt, z. B. in einem getrennten Backup-Netz.

PBS-Verbindungen können einzeln entfernt werden.

### Benachrichtigungs-Einstellungen

Der Tab **Benachrichtigungen** konfiguriert die Benachrichtigungskanäle:

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
- Während der Ruhezeiten werden Gotify-Meldungen zurückgehalten und nach dem Ende der Ruhezeit gesammelt verschickt. E-Mails sind nicht betroffen.

**Benachrichtigungstypen** -- Gotify meldet:

| Typ | Wann | Priorität |
|-----|------|-----------|
| Backup kritisch | Das Backup einer VM ist kritisch alt oder fehlt | 8 |
| Größenanomalie | Ein Backup ist deutlich größer oder kleiner als der Durchschnitt der VM | 5 |
| Sync fehlgeschlagen | Ein Cluster-Sync ist fehlgeschlagen | 7 |
| Sync überfällig | Seit 24 Stunden kein erfolgreicher Sync (einmal pro Ausfall; **auch per E-Mail**) | 9 |
| Restore überfällig | Ein Restore-Test ist überfällig (nur wenn eingeschaltet, siehe unten) | 6 |
| Backup langsam | Ein laufendes Backup dauert deutlich länger als üblich (einmal pro Lauf) | 5 |
| Datastore fast voll | Ein PBS-Datastore läuft voll | 8 |
| Verify fehlgeschlagen | Ein PBS-Verify eines Backups ist fehlgeschlagen (einmal pro Snapshot) | 8 |
| Unverschlüsselt | Ein Cluster hat unverschlüsselte Backups | 6 |

Dieselbe Meldung geht höchstens einmal in 24 Stunden hinaus. Gotify-Meldungen werden 5 Minuten gesammelt; kommt nichts Neues dazu, gehen sie als eine Nachricht („Backup Sentinel: N Meldungen“) mit der höchsten enthaltenen Priorität hinaus. E-Mail wird nur für Sync-Ausfälle über 24 Stunden, den monatlichen PDF-Bericht und den Wochenbericht genutzt.

**Überfällige Restore-Tests** -- Im Gotify-Block lässt sich *Warnung bei überfälligen Restore-Tests senden* einschalten (Standard: aus). Dann meldet jeder Sync VMs, deren letzter Restore-Test älter als ihr Intervall ist, einmal pro VM und Tag mit der Zahl der überfälligen Tage. Nie getestete VMs („offen“) lösen keine Warnung aus.

**Wochenbericht** -- Ist E-Mail aktiv und der Schalter *Wochenbericht* gesetzt (Standard), kommt jeden Montag ab 7 Uhr eine gestaltete HTML-Mail (mit Textversion für Mail-Programme ohne HTML): im Kopf Logo und Schriftzug wie in der Web-Oberfläche, oben das Urteil der Woche („Alles im grünen Bereich“ oder „N Punkte brauchen Aufmerksamkeit“), die Kennzahlen und je Cluster eine Zeile mit VMs, Nodes, Verschlüsselung und dem, was Aufmerksamkeit braucht (kritisch, Warnungen, fehlgeschlagene Backups, fällige Restore-Tests, Sync-Fehler), darunter die Probleme -- Backups, Verifies, fast volle Datastores (mit Füllstand) -- und als Info die Voll-Läufe der Woche und VMs mit nur einem Backup-Standort, zum Schluss der Stand der Restore-Tests. VM-Namen verlinken auf ihren Cluster (dafür muss `APP_URL` gesetzt sein). Der Button **Wochenbericht-Vorschau** zeigt den Bericht so, wie er jetzt verschickt würde. War der Server montags aus, wird der Bericht im Lauf der Woche nachgeholt.

### Erscheinungsbild

Der Tab **Erscheinungsbild** legt die Farben der Oberfläche fest:

- **Vorlage:** *branded* (Standard) oder *Neutral*.
- **Vier Grundfarben:** Primärfarbe, Akzent (Hauptaktion), Hintergrund und laufende Backups. Alle übrigen Töne, auch der Dark Mode, werden daraus berechnet. Eine Vorschau zeigt die Wirkung vor dem Speichern.
- **Kontrastprüfung:** Beim Speichern wird nach WCAG geprüft (Text ≥ 4,5:1, laufende Backups ≥ 3:1). Bei schlechter Lesbarkeit erscheint eine Warnung mit dem gemessenen Wert; gespeichert wird trotzdem.
- **Auf Vorlage zurücksetzen** entfernt die eigenen Farben.

Änderungen landen im Einstellungs-Audit. Der PDF-Report übernimmt Primär-, Akzent- und Laufend-Farbe.

### Governance-Panel

Ein Seitenbereich fasst die NIST-CSF-2.0-Ausrichtung zusammen:

- **Identify** -- Asset-Inventar über PVE/PBS-Sync.
- **Protect** -- Verschlüsselungs-Monitoring, Backup-Policy-Durchsetzung.
- **Detect** -- Automatische Alarmierung bei Fehlern und Anomalien.
- **Recover** -- Dokumentierte Restore-Tests mit Compliance-Tracking.

### Einstellungs-Auditlog

Änderungen an Einstellungen werden mit Zeitstempel und dem ändernden Benutzer protokolliert (abgeleitet aus den OAuth2-Proxy-Headern).

---

## Design & Sprache

### Design umschalten

Backup Sentinel unterstützt drei Design-Modi. Der Umschalter in der Kopfzeile wechselt reihum:

- **Hell** -- Heller Hintergrund mit dunklem Text.
- **Dunkel** -- Dunkler Hintergrund mit hellem Text.
- **Automatisch** -- Folgt der Betriebssystem-Einstellung (`prefers-color-scheme`).

Die Auswahl wird im lokalen Speicher des Browsers (`bsentinel-theme`) abgelegt und bleibt sitzungsübergreifend erhalten. Sie gilt nur für diesen Browser.

### Sprache umschalten

Die Anwendung ist vollständig zweisprachig (Deutsch und Englisch). Der Sprach-Button in der Kopfzeile (DE / EN) wechselt sie. Die Auswahl wird je Browser im Cookie `bsentinel-lang` gespeichert und gilt für alle Beschriftungen der Oberfläche.

Alles, was auf dem Server entsteht -- PDF-Berichte, Gotify- und E-Mail-Benachrichtigungen, der Wochenbericht und Sync-Logs --, verwendet die vom Administrator eingestellte Serversprache (`BSENTINEL_REPORT_LANGUAGE`, Standard Deutsch), unabhängig von der Sprache der Oberfläche.


## Automatisierte Restore-Tests

Unter **Einstellungen → Restore-Server** wird der dedizierte PVE-Restore-Host einmalig hinterlegt. Danach aktivierst du den Test **pro VM** (im VM-Einstellungs-Panel der Cluster-Seite): Turnus wählen (Monat/Quartal/Halbjahr/Jahr, Default Halbjahr), optional TCP-Ports (z. B. 3389, 445) und ob bei Fehlschlag angehalten werden soll. **Jetzt testen** reiht sofort einen Lauf ein.

Die Seite **Restore-Tests** (`/restore`) zeigt laufende Läufe mit Prozent und voraussichtlichem Ende („bis 16:20“, wie bei den Backup-Kacheln; an einem anderen Tag mit Datum). Die Disk-Leiste darunter füllt sich Disk für Disk mit dem kopierten Anteil; die Disk, die gerade zurückgesichert wird, pulsiert an ihrer Kante. Außerdem zeigt sie die Host-Kapazität und — nach einem Fehlschlag mit „Anhalten" — die gehaltenen Umgebungen. Dort kannst du nach einer Anpassung **erneut testen**, bei Erfolg **als erfolgreich dokumentieren** (räumt ab) oder **abräumen**. Die Queue lässt sich manuell anhalten/fortsetzen.

Das funktioniert für **VMs und LXC-Container** gleich — für einen Container trägst du einfach dessen Dienst-Port als TCP-Port ein (z. B. `5403` für ein Corosync-Qdevice), und der Test prüft nach dem Restore, dass der Container bootet und auf dem Port antwortet.
