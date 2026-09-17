---
title: swarmq Dashboard - Plan
type: feat
date: 2026-09-16
topic: dashboard
artifact_contract: ce-unified-plan/v1
artifact_readiness: requirements-only
product_contract_source: ce-brainstorm
execution: code
---

# swarmq Dashboard - Plan

## Goal Capsule

- **Objective:** Ein kleines Ops-Team beantwortet die Fragen „Ist Task X gelaufen, mit welchen Daten?“ und „Sind alle Worker da, und wie skaliert der Pool?“ im Browser und greift dort ein, ohne Logs zu lesen oder Worker über die Konsole zu administrieren.
- **Means:** Ein eigenständiges, optionales Paket neben der Bibliothek: Weboberfläche nach Flower-Muster, HTTP-API, lokale Konten, ein Signalstrom in Valkey als Quelle, gespeist von mitgelieferten Hooks in Worker und einreihender Anwendung.
- **Product authority:** Dieser Plan besitzt das Dashboard-Paket. Der Kontrollkanal im swarmq-Kern ist Voraussetzung mit benannter Schnittstelle, aber nicht aktiver Umfang; die Loadsuite-Anbindung ist ausdrücklich nicht Umfang. Beide Systeme werden getrennt entwickelt: jede weitere Änderung am Kern braucht eine eigene Absprache.
- **Open blockers:** keine; offene Punkte sind an die Planung delegiert.

---

## Product Contract

### Summary

Wir bauen `swarmq-dashboard`, ein optionales Zusatzpaket, das Tasks, Worker und Dead-Letter-Queues eines swarmq-Deployments in Echtzeit zeigt und dem Team Cancel, Autoscale- und Prozesszahl-Änderungen sowie die DLQ-Bedienung im Browser erlaubt. Die Daten kommen aus einem Signalstrom in Valkey, den jeder Worker über den `--configure`-Hook des Pakets befüllt und jede einreihende Anwendung über einen zweiten mitgelieferten Hook; Befehle gehen als gemerkte Sollwerte an einen neuen Kontrollkanal im Kern. Die erste Version liefert Tasks, Worker, DLQ und Login; die Steuerung wird mit dem Kontrollkanal des Kerns nutzbar. Erscheint das Dashboard vor diesem MR, liefert es bis dahin Beobachtung, Cancel und DLQ. Queue-Seite, Scheduler, Workflows und Graphen folgen als zweite Stufe.

### Problem Frame

swarmq beantwortet heute keine Betriebsfrage im Browser. Wer wissen will, ob ein bestimmter Task gelaufen ist und mit welchen Argumenten, hat nur die Logs: `swarmq inspect` zeigt Task-Typen, `swarmq dlq list` nur Gescheitertes, Ergebnisse verfallen mit ihrer Ablaufzeit, und Argumente speichert die Bibliothek nirgends. Ob alle Worker verbunden sind, sieht man in Grafana oder gar nicht; Prozesszahl und Autoscale-Grenzen stehen beim Start fest und lassen sich nur per Neustart über die Konsole ändern.

Der Betreiber kommt von Celery mit Flower. Dort waren genau diese beiden Griffe Alltag: einen Task suchen und seine Startdaten sehen, den Worker-Pool im Blick halten und das Autoscaling nachziehen. Mit swarmq in Phoenix soll ein kleines Ops-Team dieselben Griffe ohne ihn erledigen können. Die Bibliothek soll dabei klein bleiben; das Dashboard ist ein Angebot, das jeder Nutzer selbst hinzunimmt.

### Key Decisions

- **KD1. Eigenständiges, optionales Paket statt Einbau in die Bibliothek** (session-settled: user-directed — chosen over Einbau in `swarmq` selbst: der Kern bleibt klein, jeder Nutzer entscheidet selbst). Governs R1, R2.
- **KD2. Ereignis-Spiegel als Datenquelle** (session-settled: user-approved — chosen over einer Verwaltungsschnittstelle je Worker und über das reine Lesen vorhandenen Zustands: nur der Strom liefert die Task-Historie mit Argumenten ohne Änderung am Kern und begrenzt die Kernänderung auf den Kontrollkanal). Governs R3, R4, R5, R6.
- **KD3. Kontrollkanal im Kern ist Voraussetzung, wird separat geplant** (session-settled: user-approved — chosen over einem gemeinsamen Plan und über „Kern zuerst“: das Dashboard wird vollständig beschrieben, die Kernänderung bekommt eine klare Schnittstelle und einen eigenen kleinen MR). Governs R14, R17.
- **KD4. Steuerumfang: Autoscale-Grenzen und Prozesszahl zur Laufzeit** (session-settled: user-directed — chosen over „nur beobachten und canceln“ und über voller Flower-Fernsteuerung: deckt den Flower-Alltag des Betreibers ab, ohne Queue-Zuordnung, Neustart und Limits je Task in den Kern zu ziehen). Governs R14, R15.
- **KD5. Sollwerte überleben Neustarts** (session-settled: user-approved — chosen over flüchtigen Änderungen wie bei Flower: sonst kehrt die Konsole beim nächsten Deploy zurück). Governs R16.
- **KD6. Lokale Konten mit Passwort** (session-settled: user-approved — chosen over OIDC-Anbieter, Proxy-Login und gemeinsamem Token: keine Abhängigkeit nach außen, funktioniert im geschlossenen Netz, jede Aktion hat ein Konto). Governs R20, R21, R22, R23.
- **KD7. Sieben Tage Aufbewahrung** (session-settled: user-directed — chosen over 24 Stunden und über einer festen Task-Zahl: deckt Wochenendvorfälle; braucht ein eigenes Speicherbudget in Valkey). Governs R5, R9.
- **KD8. Grundform „Seiten wie Flower“** (session-settled: user-directed — chosen over einem Live-Board auf einer Seite und über „Suche zuerst mit Statusleiste“, entschieden an einer Skizze: vertraut, ruhig, Stufe 2 hängt sich als weitere Seiten an). Governs R24, R25, R26.
- **KD9. Stufenfolge** (session-settled: user-directed — chosen over allem in einer Version: Tasks, Worker mit Steuerung und DLQ zuerst; Queue-Tiefen, Scheduler und Workflows, Graphen danach). Governs Scope Boundaries.
- **KD10. Nutzeraktionen sind Ereignisse im selben Strom** (session-settled: user-approved — chosen over keinem Protokoll: mit persönlichen Konten ist ein Audit fast kostenlos und beantwortet „wer hat das storniert“). Governs R27.
- **KD11. Argumente werden im Klartext vorgehalten** (session-settled: user-approved — chosen over Maskierungsregeln: der Zweck ist, die Startdaten zu sehen; Kundendaten im Verzeichnis sind eine bewusste Annahme des Betreibers). Governs R7.
- **KD12. Das Paket liefert auch einen Anwendungs-Hook** (session-settled: user-directed — chosen over einem reinen Worker-Hook: nur so kennt das Verzeichnis einen Task ab der Einreihung, mit seinen Argumenten und als wartend stornierbar; Preis ist eine Codezeile in jeder einreihenden Anwendung). Governs R2, R3a, R6, R12.
- **KD13. Getrennte Repositories, Kernänderungen nur nach Absprache** (session-settled: user-directed — chosen over einem gemeinsamen Repo und über freiem Zugriff auf die Bibliothek: das Dashboard entsteht als eigenes Projekt neben `swarmq-loadsuite` und veröffentlicht eigene Versionen; fehlt ihm eine Funktion im Kern, wird sie nicht nebenbei dort eingebaut, sondern erst abgestimmt und als eigener MR an der Bibliothek geplant). Governs R1, R1a.

<!-- ce-section: work-relationships -->
### How This Work Fits Together

Dieser Plan besitzt das Dashboard-Paket. Die Aufteilung darunter ist der heutige Stand des Verständnisses, kein festgelegter Fahrplan.

- **Kontrollkanal im swarmq-Kern** — Depends on: dieser Plan setzt ihn für R14 bis R17 voraus. Der Supervisor nimmt Sollwerte für Autoscale-Grenzen und Prozesszahl je Worker-Gruppe entgegen und wendet sie an; die Schnittstelle (Ort der Sollwerte, Gruppenschlüssel, Rückmeldung) ist in Outstanding Questions an die Planung delegiert. Eigener kleiner MR an der Bibliothek, vor oder parallel zur Steuerungs-Seite des Dashboards. Er ist die einzige geplante Änderung am Kern; weiterer Bedarf des Dashboards wird abgestimmt, bevor etwas in der Bibliothek entsteht (KD13).
- **Zweite Stufe des Dashboards** — Enables: Queue-Tiefen je Queue, Scheduler- und Workflow-Ansicht, Echtzeit-Graphen hängen sich als weitere Seiten an die Grundform aus KD8; sie lesen dieselben Quellen und brauchen keine neue Kernänderung.
- **Loadsuite-Anbindung** — Can proceed independently of: die Lasttest-Suite hat einen eigenen Ereignis-Relay; ob sie später den Strom des Dashboards mitnutzt, ist eine spätere Frage. Still to decide.

### Actors

- A1. **Ops-Teammitglied** — meldet sich mit eigenem Konto an, sucht Tasks, prüft Worker, storniert, ändert Sollwerte, bedient die DLQ. Alle Konten haben dieselben Rechte.
- A2. **Betreiber** — installiert das Paket, legt Konten an, konfiguriert Aufbewahrung und Worker-Hook; im Alltag selbst A1.
- A3. **Worker** — swarmq-Worker mit geladenem Hook; schreibt Signale in den Strom, empfängt Sollwerte über den Kontrollkanal des Kerns.
- A4. **Dashboard-Dienst** — eigener Prozess; liest den Strom, hält das Verzeichnis, bedient Oberfläche und API, merkt sich Sollwerte.
- A5. **Einreihende Anwendung** — hält den swarmq-Client; mit geladenem Anwendungs-Hook meldet sie jede Einreihung mit Argumenten in den Strom.

### Requirements

**Paket und Betrieb**

- R1. Das Dashboard ist ein eigenes Python-Paket mit eigenem Prozess und eigener Abhängigkeitsliste; die Bibliothek `swarmq` erhält dadurch keine neue Pflichtabhängigkeit.
- R1a. Das Dashboard entsteht in einem eigenen Repository, veröffentlicht eigene Versionen und hängt von `swarmq` in einer gepflegten Versionsspanne ab; es nutzt ausschließlich öffentliche Schnittstellen der Bibliothek. Fehlt eine Funktion im Kern, wird sie nicht als Teil dieser Arbeit dort ergänzt, sondern abgestimmt und getrennt geplant.
- R2. Das Paket liefert zwei Hooks mit: den Worker-Hook, den ein Betreiber per `swarmq worker --configure` lädt, und den Anwendungs-Hook, den eine einreihende Anwendung mit einer Codezeile auf ihre swarmq-Instanz anwendet. Ohne den jeweiligen Hook laufen Worker und Anwendung unverändert.

**Ereignisstrom und Verzeichnis**

- R3. Der Hook schreibt jedes Signal des Workers mit Zeitstempel, `worker_id` und vollständiger Nutzlast in einen Strom in Valkey.
- R3a. Der Anwendungs-Hook schreibt jede Einreihung mit Kennung, Name, Queue, Argumenten und Zeitpunkt in denselben Strom, bevor ein Worker den Task aufgreift.
- R4. Kein Hook darf den Task-Ablauf beeinflussen: Fehler oder Verzögerungen beim Schreiben werden protokolliert, der Task läuft weiter, und das Einreihen gelingt auch dann, wenn der Strom nicht erreichbar ist.
- R4a. Jeder Hook zählt Ereignisse, die er nicht schreiben konnte, und schreibt nach Rückkehr des Stroms ein Verlust-Ereignis mit `worker_id`, Anzahl und Zeitspanne; Task-Liste und Worker-Ansicht weisen betroffene Zeitspannen als „unvollständig“ aus.
- R5. Der Dienst hält aus dem Strom ein Verzeichnis aller Tasks der letzten sieben Tage vor; ältere Einträge fallen ohne manuelles Eingreifen weg.
- R6. Das Verzeichnis kennt je Task Kennung, Name, Queue, Status (wartend, laufend, fertig, gescheitert, storniert), Zeitpunkte von Einreihung, Start und Ende, Laufzeit, Worker, Wiederholungszähler, Fehlertext und Ergebnisverweis, soweit die Signale sie tragen.
- R7. Das Verzeichnis hält die Argumente jedes Tasks im Klartext, wie sie beim Einreihen übergeben wurden; sie stammen aus dem Anwendungs-Hook.
- R8. Die Oberfläche zeigt, wie weit die Suche gerade zurückreicht, sobald der Strom kürzer ist als sieben Tage.
- R9. Der Speicherbedarf des Stroms ist begrenzt und für den Betreiber sichtbar; die Grenze folgt aus Aufbewahrungsdauer und Aufkommen, ihre Form ist an die Planung delegiert. Das Budget des Task-Stroms begrenzt den Audit-Strom aus R27 nicht.

**Tasks**

- R10. Ein Teammitglied findet einen Task über Kennung, Namen oder Argument-Text und filtert nach Status, Name, Worker und Zeitraum.
- R11. Die Task-Ansicht zeigt alle Felder aus R6 und R7 sowie die Folge der Signale des Tasks in Zeitreihenfolge.
- R12. Ein Teammitglied storniert einen wartenden oder laufenden Task aus der Task-Ansicht; das Dashboard nutzt dafür die bestehende Cancel-Funktion der Bibliothek.
- R13. Neue Ereignisse erscheinen in offenen Listen und Ansichten ohne manuelles Neuladen innerhalb weniger Sekunden.

**Worker und Steuerung**

- R14. Die Worker-Ansicht zeigt je Worker Slot, `worker_id`, Verbundenheit, belegte Slots, verarbeitete Tasks, Uptime und den Zeitpunkt des letzten Lebenszeichens, gruppiert nach Worker-Gruppe. Je Gruppe zeigt sie zusätzlich die aktuelle Zahl wartender Nachrichten ihrer Queues (Summe, wie sie der Supervisor für die Autoskalierung misst) und ob die wirksamen Werte aus einem Sollwert oder aus den Startargumenten stammen.
- R15. Ein Teammitglied ändert für eine Worker-Gruppe den Sollwert aus Autoscale-Grenzen und Prozesszahl; die Änderung wirkt zur Laufzeit über den Kontrollkanal des Kerns. Ein Sollwert beschreibt je Gruppe genau einen Zustand aus Unter- und Obergrenze; eine feste Prozesszahl N ist der Sollwert N..N und schaltet die Autoskalierung ab, Gruppen mit Rechen-Pool nehmen nur diese feste Form an. Ein neuer Sollwert ersetzt den vorigen vollständig. Ein Teammitglied hebt den Sollwert einer Gruppe auf; danach gelten wieder die Startargumente des Deployments.
- R16. Der zuletzt gesetzte Sollwert je Worker-Gruppe liegt im Kontrollkanal; ein Supervisor liest ihn beim Start vor dem ersten Spawn und wendet ihn an. Die Startargumente des Deployments gelten nur, wenn kein Sollwert gesetzt ist. Der Dienst hält Anzeige und Verlauf der Sollwerte; für die Anwendung nach einem Deploy ist er nicht nötig.
- R16a. Der Betreiber setzt je Worker-Gruppe beim Start eine Obergrenze für Prozesszahl und Autoscale-Maximum; der Supervisor lehnt Sollwerte oberhalb dieser Grenze ab und meldet die Ablehnung wie einen unbestätigten Sollwert (R17); ein abgelehnter Sollwert wird nicht gemerkt.
- R17. Die Worker-Ansicht zeigt zu jedem Sollwert, ob der Supervisor ihn bestätigt hat, und meldet einen Sollwert, der nach einer Frist unbestätigt bleibt. Ein Teammitglied kann jederzeit erneut senden; der neue Sollwert ersetzt den hängenden vollständig (R15), und die Ansicht nennt den Zeitpunkt des letzten Sendens.
- R18. Der Dienst vergleicht je Queue die Zahl der Konsumenten am Broker (passive Queue-Deklaration) mit der Zahl der verbundenen Worker, die der Strom für diese Queue kennt; einen Überschuss weist er als „Konsumenten ohne Hook“ aus, damit fehlende Einträge im Verzeichnis nicht als „nicht gelaufen“ gelesen werden.
- R19. Ein Worker ohne Lebenszeichen über die konfigurierte Frist wird als getrennt markiert und bleibt sichtbar, bis er zurückkehrt oder der Betreiber ihn entfernt.

**Anmeldung und Konten**

- R20. Jedes Teammitglied meldet sich mit eigenem Konto und Passwort an; der Betreiber legt Konten über die Kommandozeile des Pakets an, ändert Passwörter und sperrt Konten.
- R21. Passwörter werden nur als Hash mit anerkanntem Verfahren gespeichert; Sitzungen laufen nach Inaktivität ab und lassen sich durch Abmelden beenden.
- R22. Wiederholte Fehlanmeldungen verlangsamen weitere Versuche für dieses Konto; der Betreiber sieht Fehlanmeldungen im Audit.
- R23. Alle Konten haben dieselben Rechte; Oberfläche, API und der Kanal für Live-Aktualisierung verweigern jeden Zugriff — Lesen wie Aktionen — ohne gültige Sitzung; nur die Anmeldeseite ist ohne Sitzung erreichbar. Zustandsändernde Anfragen (Cancel, Sollwert, DLQ-Aktionen, Kontoverwaltung) sind gegen Aufrufe von fremden Seiten geschützt: Sitzungscookies gelten streng seitengebunden und die API nimmt Schreibanfragen nur mit einem sitzungsgebundenen Zusatzmerkmal an.

**Oberfläche**

- R24. Die Oberfläche folgt der Grundform „Seiten“: eine linke Navigation mit den Bereichen Worker, Tasks und DLQ, je Bereich eine Tabellenseite, Details als eigene Seite; die Startseite ist die Worker-Übersicht.
- R25. Die Oberfläche bleibt auf das Nötige beschränkt: keine Bereiche, Kacheln oder Kennzahlen, die nicht zu R10 bis R19 oder R28 bis R30 gehören; Stufe-2-Bereiche erscheinen erst, wenn sie da sind.
- R26. Die Oberfläche ist auf Notebook- und Desktop-Breite ausgelegt und zeigt Broker-, Backend- und Strom-Zustand in einer Kopfzeile: Broker und Backend als erreichbar oder nicht erreichbar, den Strom als aktuell oder verzögert mit dem Zeitpunkt des zuletzt empfangenen Signals.
- R27. Jede Aktion eines Teammitglieds (Cancel, Sollwert setzen und aufheben, DLQ-Aktion, An- und Abmeldung) wird mit Konto, Zeitpunkt und Ziel als Ereignis in einem eigenen Audit-Strom in Valkey festgehalten, der unabhängig vom Task-Aufkommen mindestens die Aufbewahrungsdauer aus R5 nach Alter hält, und ist im Dashboard einsehbar.

**Dead-Letter-Queues**

- R28. Die DLQ-Ansicht listet je Queue die Dead-Letter-Nachrichten mit Task-Kennung, Name, Wiederholungszähler, Fehlervorschau und Alter.
- R29. Ein Teammitglied reiht eine Nachricht oder alle Nachrichten einer Queue erneut ein und leert eine Queue; das Dashboard nutzt dafür dieselben Wege wie `swarmq dlq retry` und `swarmq dlq purge`. Eine Sammelaktion meldet die Zahl der erfolgreich und der nicht verarbeiteten Nachrichten; was scheitert, bleibt für einen späteren Versuch in der Queue.
- R30. Leeren verlangt eine Bestätigung mit Nennung der Queue und der Nachrichtenzahl.

### Key Flows

- F1. Task-Rückfrage
  - **Trigger:** Ein Kollege fragt, ob Task X für Kunde Y heute gelaufen ist.
  - **Actors:** A1, A4
  - **Steps:** Teammitglied meldet sich an; öffnet Tasks; sucht nach Task-Name oder einem Argument-Fragment; öffnet den Treffer; liest Status, Argumente, Zeitpunkte und Fehlertext; storniert bei Bedarf.
  - **Covers R10, R11, R12, R20**

- F2. Rückstau und Autoscale
  - **Trigger:** Die Worker-Übersicht zeigt alle Slots belegt und wachsende Task-Zahlen.
  - **Actors:** A1, A4, A3
  - **Steps:** Teammitglied öffnet die Worker-Gruppe; setzt die Autoscale-Obergrenze höher; Dienst speichert den Sollwert und schreibt ihn in den Kontrollkanal; Supervisor bestätigt; Ansicht zeigt den bestätigten Sollwert und die neuen Slots.
  - **Covers R14, R15, R17, R27**

- F3. Deploy mit gemerktem Sollwert
  - **Trigger:** Ein Deploy startet die Worker-Gruppe mit den alten Startargumenten neu.
  - **Actors:** A3, A4
  - **Steps:** Supervisor startet; liest den gemerkten Sollwert aus dem Kontrollkanal; wendet ihn vor dem ersten Spawn an und bestätigt; die Ansicht zeigt den bestätigten Sollwert; ohne gemerkten Sollwert bleiben die Startargumente.
  - **Covers R16, R17**

- F4. DLQ-Aufräumen
  - **Trigger:** Die DLQ-Ansicht zeigt Nachrichten in einer Queue nach einem Fehlerdeploy.
  - **Actors:** A1, A4
  - **Steps:** Teammitglied sichtet Fehlervorschauen; reiht alle erneut ein, nachdem der Fix live ist; leert Rest nach Bestätigung; Aktionen erscheinen im Audit.
  - **Covers R28, R29, R30, R27**

- F5. Worker ohne Hook
  - **Trigger:** Ein neu ausgerollter Worker startet ohne `--configure`.
  - **Actors:** A3, A4, A1
  - **Steps:** Der Broker meldet für die Queue mehr Konsumenten, als der Strom Worker kennt; Dienst weist die Differenz als „Konsumenten ohne Hook“ aus; Teammitglied sieht die Markierung statt leerer Task-Listen.
  - **Covers R18**

### Acceptance Examples

- AE1. **Covers R10, R11.** Given: Task `render_pdf` wurde vor drei Stunden mit `{"report_id": 812}` eingereiht und ist fertig. When: ein Teammitglied sucht nach `812`. Then: der Treffer zeigt Name, Status „fertig“, die Argumente im Klartext und die Signalfolge mit Zeitpunkten.
- AE2. **Covers R5, R8.** Given: der Strom enthält Einträge von acht Tagen. When: ein Teammitglied filtert auf die letzten zehn Tage. Then: die Ansicht zeigt Treffer der letzten sieben Tage und den Hinweis, wie weit die Suche zurückreicht.
- AE3. **Covers R12.** Given: ein Task ist bereits fertig. When: ein Teammitglied klickt Cancel. Then: das Dashboard meldet, dass der Task nicht mehr stornierbar ist, und protokolliert keinen Cancel.
- AE4. **Covers R15, R17.** Given: eine Worker-Gruppe läuft mit Autoscale 2 bis 4. When: ein Teammitglied setzt 2 bis 8. Then: der Sollwert erscheint als „gesendet“, nach der Bestätigung des Supervisors als „aktiv“, und die Slots 4 bis 7 erscheinen bei Last.
- AE5. **Covers R16.** Given: für die Gruppe ist der Sollwert 4..4 gemerkt, das Deployment startet mit `--processes 2`. When: der Supervisor startet. Then: er wendet 4..4 vor dem ersten Spawn an, die Ansicht zeigt vier Slots und nennt den Sollwert als Herkunft des wirksamen Werts.
- AE6. **Covers R17.** Given: ein Sollwert wurde gesendet. When: nach der konfigurierten Frist keine Bestätigung eintrifft. Then: die Worker-Ansicht markiert den Sollwert als unbestätigt und nennt den Zeitpunkt des Sendens.
- AE7. **Covers R18.** Given: eine Queue hat drei Konsumenten am Broker, der Strom kennt dafür zwei verbundene Worker. When: die Worker-Übersicht geladen wird. Then: die Queue trägt den Hinweis „1 Konsument ohne Hook“.
- AE8. **Covers R19.** Given: ein Worker sendet seit mehr als der konfigurierten Frist kein Lebenszeichen. When: die Worker-Übersicht geladen wird. Then: der Worker steht als getrennt mit Zeitpunkt des letzten Lebenszeichens in der Liste.
- AE9. **Covers R22.** Given: fünf Fehlanmeldungen für ein Konto in kurzer Folge. When: der sechste Versuch kommt. Then: die Antwort verzögert sich spürbar, das Konto bleibt bestehen, und das Audit zeigt die Versuche.
- AE10. **Covers R23.** Given: keine gültige Sitzung. When: die API für Cancel aufgerufen wird. Then: die Aktion wird verweigert, ohne dass etwas storniert wird. When: die Task-Liste ohne Sitzung abgerufen wird. Then: es werden keine Task-Daten ausgeliefert.
- AE11. **Covers R4.** Given: Valkey ist für den Strom kurz nicht erreichbar. When: der Worker Tasks verarbeitet. Then: die Tasks laufen unverändert durch, der Hook protokolliert den Schreibfehler, und das Verzeichnis zeigt nach der Rückkehr eine Lücke statt falscher Zustände, die Task-Liste weist die betroffene Zeitspanne als unvollständig aus.
- AE12. **Covers R30.** Given: die DLQ `reports.XQ` enthält 37 Nachrichten. When: ein Teammitglied klickt „Leeren“. Then: eine Bestätigung nennt `reports.XQ` und 37 Nachrichten; erst die Bestätigung löscht.
- AE13. **Covers R6, R12.** Given: ein Task wartet seit zwei Minuten in seiner Queue, kein Worker hat ihn aufgegriffen. When: ein Teammitglied öffnet ihn und klickt Cancel. Then: die Ansicht zeigt ihn als wartend mit Einreihungszeitpunkt und Argumenten, und der Task läuft nicht an.

### Success Criteria

- Drei Monate nach der Einführung braucht das Team für Task-Rückfragen und für Änderungen an Prozesszahl und Autoscaling keine Logs und keine Konsole mehr; die Anzahl solcher Konsolengriffe geht gegen null.
- Ein Teammitglied beantwortet „ist Task X gelaufen, mit welchen Daten?“ in unter einer Minute, ohne Rückfrage an den Betreiber.
- Worker-Ausfälle und fehlende Hooks fallen im Dashboard auf, bevor jemand in Grafana oder Logs schaut.

### Scope Boundaries

**Deferred for later (zweite Stufe)**

- Queue-Seite je Queue mit wartenden, unbestätigten und Dead-Letter-Nachrichten und deren Verlauf; die Summe wartender Nachrichten je Worker-Gruppe ist bereits Teil von R14.
- Scheduler-Ansicht mit Cron-Einträgen, nächster Ausführung und Leader-Worker; Workflow-Ansicht mit Status und Schritten.
- Echtzeit-Graphen für Durchsatz, Fehler und Laufzeiten; bis dahin bleibt Grafana der Ort dafür.
- Nur-Lese-Rollen und Mandantentrennung.

**Outside this product's identity**

- Queues je Worker hinzufügen oder entfernen, Worker neu starten oder herunterfahren, Rate- und Zeitlimits je Task-Typ ändern: Flower kann das, swarmq hält Task-Regeln bewusst im Code.
- Tasks aus dem Dashboard einreihen oder ausführen.
- Ersatz für Prometheus und Grafana als Langzeit-Monitoring.
- Eingriff in Deployment-Dateien (Compose, systemd) durch das Dashboard.
- Funktionen in der Bibliothek `swarmq` ergänzen, weil das Dashboard sie braucht: außer dem abgestimmten Kontrollkanal bleibt der Kern unberührt, weiterer Bedarf wird abgestimmt und getrennt geplant.

### Dependencies / Assumptions

- **Kontrollkanal im swarmq-Kern** (KD3): R15 bis R17 sind erst erfüllbar, wenn der Supervisor Sollwerte für Autoscale-Grenzen und Prozesszahl entgegennimmt, anwendet und bestätigt. Die Steuerungs-Seite des Dashboards wird zusammen mit diesem MR nutzbar. Erscheint das Dashboard vorher, liefert es bis dahin Beobachtung, Cancel und DLQ. Ob beides zusammen ausgeliefert wird oder die Beobachtung zuerst, entscheidet die Planung.
- **Worker laden den Hook**: Der Betreiber kontrolliert die Startkommandos aller Worker in Phoenix (verifiziert: `--configure` läuft je Worker-Prozess vor dem Start, `src/swarmq/cli/worker_cmd.py`).
- **Lebenszeichen setzen den Metrik-Sampler voraus**: `WORKER_HEARTBEAT` feuert nur mit konfiguriertem Metrik-Port (`src/swarmq/worker/lifecycle.py`); R14, R18 und R19 nehmen an, dass Worker mit Metriken laufen, oder die Planung wählt eine andere Lebenszeichenquelle.
- **Argumente sind heute nirgends gespeichert** (verifiziert: nur Ergebnisse unter `sq:result:{task_id}` mit Ablaufzeit); der Strom ist die einzige Quelle für R7. Sie liegen im Client-Prozess aber vollständig vor: der Nachrichtenkontext der Einreihung trägt Kennung, Name, Queue, Argumente und Schlüsselwortargumente, und die Bibliothek nimmt eigene Middleware über eine öffentliche Schnittstelle an (verifiziert: `src/swarmq/middleware.py`, `src/swarmq/app.py`). Der Anwendungs-Hook braucht dafür keine Kernänderung.
- **Anwendungen ohne Hook**: Eine einreihende Anwendung ohne den Hook erzeugt Tasks, die das Verzeichnis erst ab ihrem Start kennt; das ist die Anwendungs-Entsprechung zum Worker ohne Hook aus R18.
- **Kundendaten im Verzeichnis** (KD11): sieben Tage Klartext für alle Kontoinhaber sind vom Betreiber bewusst akzeptiert; Datenschutzfolgen liegen beim Betreiber.
- **Valkey ist Vertrauensgrenze**: Wer auf den Valkey des Backends schreiben darf, kann Sollwerte setzen und Ereignisse in den Strom schreiben — also jeder Worker mit Hook und jede Anwendung mit swarmq-Client. Konten und Audit schützen nur den Weg über das Dashboard. Die Planung des Kontrollkanals legt Sollwerte und Audit-Ereignisse in einen eigenen Schlüsselraum, den der Betreiber per Valkey-ACL auf Dienst und Supervisoren beschränken kann.
- **Zwei getrennte Veröffentlichungswege**: `swarmq` und `swarmq-dashboard` liegen in eigenen Repositories und erscheinen getrennt auf PyPI; Vorbild ist `swarmq-loadsuite`, das schon heute als eigenes Projekt neben der Bibliothek liegt. Das Dashboard nennt eine Versionsspanne der Bibliothek statt eines festen Pins, weil beide Pakete über die Signal-Nutzlasten gekoppelt sind.
- **Ein Dashboard je swarmq-Deployment**: ein Dienst bedient genau ein Paar aus Broker und Backend.

### Outstanding Questions

**Resolve Before Planning**

Keine.

**Deferred to Planning**

- Anbindung des Anwendungs-Hooks: Er hängt sich an den Haken, den die Bibliothek nach erfolgreichem Publish im Client aufruft, und bekommt dort den vollständigen Nachrichtenkontext. Zu klären, ob derselbe Haken auch die Einreihungen abdeckt, die in einem Worker entstehen (Scheduler-Auslösungen und Workflow-Schritte), ob ein Task ohne Anwendungs-Hook im Verzeichnis als solcher erkennbar sein soll, und welche Form die Codezeile in der Anwendung bekommt.
- Schnittstelle des Kontrollkanals: Ort der Sollwerte in Valkey, Schlüssel der Worker-Gruppe (Hostname, Queue-Menge, explizites Label), Form der Bestätigung und der Ablehnung durch den Supervisor, Frist für R17, sowie die Semantik von Prozesszahl gegenüber Grenzen im Supervisor (heute ist `--processes` unter `--autoscale` nur der Startwert).
- Form des Speicherbudgets (R9): Stream-Trimmung nach Alter, Zahl oder Bytes, und wie das Dashboard den Füllstand zeigt.
- Wie der Dienst den Strom in ein durchsuchbares Verzeichnis überführt und ob Volltext auf Argumente in Valkey oder in einem eigenen Index liegt.
- Technik der Live-Aktualisierung im Browser (R13) und Wahl von Web-Framework und Frontend-Stack; Maßstab ist R25.
- Wie das Dashboard DLQ-Inhalte liest, ohne die Reihenfolge zu verändern: heute drainiert `swarmq dlq list` und reiht neu ein.
- Konfiguration der Fristen für „getrennt“ (R19) und „unbestätigt“ (R17) und ihre Voreinstellungen.
- Form der Versionsspanne zwischen Dashboard und Bibliothek und wie eine Änderung an den Signal-Nutzlasten zwischen beiden Projekten abgestimmt wird.

### Sources / Research

- Flower-Funktionsumfang als Vorlage: https://flower.readthedocs.io/en/latest/features.html, HTTP-API https://flower.readthedocs.io/en/latest/api.html, Oberflächenbereiche im README https://github.com/mher/flower.
- Bewusste Ablehnung eines eingebauten Dashboards: `website/explanation/design-decisions.md`.
- Vorhandene Datenquellen: Signale `src/swarmq/signals.py`; Metriken `src/swarmq/metrics.py`; Slot-Tabelle `src/swarmq/process_manager/slots.py`; Ergebnis- und Zustandsschlüssel `src/swarmq/backend/valkey/results.py`; Worker-Identität `src/swarmq/config.py`; Konfigurations-Hook `src/swarmq/cli/worker_cmd.py`; DLQ-Zugriff `src/swarmq/cli/dlq_cmd.py`, `src/swarmq/broker/rabbitmq/admin.py`.
- Vorbild für den Ereignis-Relay im Worker: die Lasttest-Suite (`swarmq-loadsuite`, `worker/relay.py`), die Signale per Handler nach außen führt.
- Kein bestehendes Web-Framework und keine Authentifizierung im Kern: `src/swarmq/_http.py`, `pyproject.toml`, `src/swarmq/health.py`.
