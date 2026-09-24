---
title: Ergebnis eines Tasks in der Detailansicht - Plan
type: feat
date: 2026-09-23
topic: ergebnisanzeige
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: Betriebsfrage aus dem Demo-Lauf
execution: code
---

# Ergebnis eines Tasks in der Detailansicht - Plan

## Goal Capsule

- **Objective:** Wer die Detailseite eines Tasks öffnet, sieht nicht nur, **dass** er fertig wurde, sondern **was** herauskam — und erkennt auf einen Blick, ob das Ergebnis noch da ist, abgelaufen ist oder nie gespeichert wurde.
- **Means:** Die Seite schlägt das Ergebnis beim Aufruf über die öffentliche API der Bibliothek im Backend nach. Es wird nicht ins Verzeichnis übernommen: es hat eine eigene Verfallsfrist, und eine zweite Kopie derselben Daten wäre ein zweiter Ort, an dem sie zu löschen wären.
- **Product authority:** Dieser Plan besitzt die Detailansicht. Änderungen an der Bibliothek sind nicht Umfang; was `get_result` nicht hergibt, wird nicht erfunden.
- **Execution profile:** testgetrieben nach `AGENTS.md` — ein fehlschlagender Test je Schritt, kein Produktionscode ohne ihn.
- **Stop conditions:** Halte an, wenn eine Einheit verlangt, das Ergebnis im Verzeichnis zu speichern, wenn sie an der Bibliothek vorbei auf Backend-Schlüssel zugreift, oder wenn die Seite ohne erreichbares Backend nicht mehr lädt.
- **Open blockers:** keine.

---

## Product Contract

### Summary

Die Detailseite eines Tasks zeigt heute Argumente und Fehlertext, aber kein Ergebnis. Dabei liegt es vor: Die Bibliothek legt es unter `sq:result:<task_id>` im Backend ab, mit einer Frist, die der Worker als Kaskade aufloest: `TaskInfo.result_ttl`, sonst `SWARMQ_RESULT_TTL` bzw. `config.result_ttl` (Vorgabe 86400 s), sonst 24 h -- und bei `0`, wie es Fire-and-Forget ohne eigene Frist ergibt, wird gar nichts abgelegt. Der Dead-Letter-Pfad nimmt immer den globalen Wert. Die Seite holt es künftig beim Aufruf und zeigt es unter den Argumenten — mit derselben Sorgfalt, die für die Argumente schon gilt.

### Problem Frame

Ein Teammitglied sieht auf der Detailseite „fertig" und die Laufzeit. Was der Task zurückgab, steht nirgends. Bei einer Probe sind das Worker, PID und gemessene Laufzeit; bei einem echten Task die Antwort, wegen der er lief. Wer sie braucht, greift heute zur Kommandozeile und liest den Backend-Schlüssel selbst — genau der Griff, den das Dashboard abschaffen soll (F1).

R6 sieht das vor und nennt einen **Ergebnisverweis**; die Spalte `result_ref` steht im Schema. Gefüllt wird sie von niemandem. Das ist keine vergessene Zeile, sondern eine offene Naht: Der Ereignisstrom trägt das Ergebnis nicht, `task_completed` nennt nur Kennung, Name, Queue und Dauer.

Getragen wird die neue Karte darum von **R11** (die Task-Ansicht zeigt alle Felder aus R6 und R7) -- sie liegt im Band, das R25 erlaubt, und ist keine neue Kachel. KD1 ersetzt den **Ergebnisverweis** aus R6 durch das Nachschlagen beim Aufruf; die Spalte `result_ref` bleibt bewusst unbefüllt und wird von diesem Plan nicht wiederbelebt.

### Key Decisions

- **KD1. Das Ergebnis wird gelesen, nicht gespeichert.** Die Seite holt es bei jedem Aufruf aus dem Backend. Alternative wäre, es beim Einlesen ins Verzeichnis zu übernehmen — dagegen sprechen drei Dinge: Es hat eine eigene Verfallsfrist, die das Verzeichnis nicht kennt und nicht einhalten könnte; es wäre eine zweite Kopie personenbezogener Daten an einem zweiten Ort, mit einer zweiten Löschpflicht (R7a); und der Ereignisstrom trägt es gar nicht, die Übernahme bräuchte also ohnehin einen Backend-Zugriff. Governs R6.
- **KD2. Nur über die öffentliche API.** `SwarmQ.get_result(task_id, timeout=0)` ist ein einzelner GET ohne Warten. Die Schlüsselnamen und das Packformat der Bibliothek bleiben ihre Sache (R1a).
- **KD3. Kein Zustand bleibt stillschweigend.** „da", „abgelaufen, oder diese Anwendung speichert keine Ergebnisse", „Backend nicht erreichbar", „mit dieser swarmq-Fassung nicht lesbar", „der Task ist noch nicht beendet". „Zu groß zum Anzeigen" ist keiner davon, sondern eine Eigenschaft des dargestellten Textes (siehe Entwurf). Ein fehlendes Ergebnis als leeres Feld zu zeigen wäre die stille Falschaussage, gegen die R33 steht.
- **KD4. Dieselbe Schutzklasse wie die Argumente.** Das Ergebnis steht im Klartext da, wie die Argumente seit KD11 — mit dem Unterschied, dass es nicht im Dashboard liegt. Es geht nicht in den Audit-Strom und nicht ins Verzeichnis.

### Actors

- **Teammitglied im Vorfall** — will wissen, was ein Task zurückgab, ohne eine Konsole zu öffnen.
- **Betreiber** — muss wissen, wo diese Daten liegen und wann sie verfallen (R7a).

### Requirements

- R1. Die Detailseite zeigt das Ergebnis eines Tasks, sobald es im Backend vorliegt.
- R2. Liegt keines vor, unterscheidet die Seite „abgelaufen, oder diese Anwendung speichert keine Ergebnisse" von „Backend nicht erreichbar" und nennt bei letzterem den Grund. Der erste Wortlaut nennt beide Ursachen, ohne eine zu behaupten: das Dashboard kann sie nicht auseinanderhalten.
- R2a. Ein Task, der noch nicht beendet ist, trägt das Wort „noch nicht beendet" — ohne Backend-Zugriff.
- R2b. Ein Ergebnis, das vorliegt, sich mit der Fassung des Dashboards aber nicht entpacken lässt, wird als solches benannt — nicht als Fehlschlag des Tasks.
- R3. Ein gescheiterter Task zeigt seinen Fehler; ein stornierter zeigt, dass er storniert wurde. Beides kommt aus dem Ergebnis, nicht nur aus dem Signal.
- R4. Ein Ergebnis über der Anzeigegrenze wird abgeschnitten und **als abgeschnitten gekennzeichnet**, mit seiner wahren Größe.
- R5. Das Nachschlagen hält die Seite nie auf: eine eigene Frist, und ein Fehlschlag ist eine Auskunft, kein Fehler.
- R6a. Das Ergebnis wird nicht ins Verzeichnis übernommen und nicht in den Audit geschrieben.
- R7. Die Betriebsdoku nennt, wo Ergebnisse liegen, wann sie verfallen und dass das Dashboard sie nur liest.

### Key Flows

- **F1. Nachsehen, was herauskam.** Teammitglied öffnet einen fertigen Task → die Seite zeigt unter den Argumenten das Ergebnis im Klartext, mit dem Hinweis, dass es nach `result_ttl` verfällt.
- **F2. Der Task ist gescheitert.** Die Seite zeigt den Fehlertext aus dem Signal **und** den aus dem Ergebnis, falls beide vorliegen — sie können auseinandergehen, wenn eine Wiederholung dazwischenlag.
- **F3. Das Ergebnis ist weg -- oder war nie da.** Der Task ist alt und die Frist abgelaufen, oder die Anwendung legt gar keine Ergebnisse ab (Fire-and-Forget, oder `SWARMQ_RESULT_TTL=0`). Die Seite nennt beide Möglichkeiten statt eines leeren Feldes und verweist auf die Frist der Anwendung, ohne sie zu kennen.

### Acceptance Examples

- **AE1. Covers R1.** Given: ein fertiger Task mit Ergebnis im Backend. When: die Detailseite geöffnet wird. Then: das Ergebnis steht im Klartext da.
- **AE2. Covers R2.** Given: ein fertiger Task, dessen Ergebnis verfallen ist. When: die Seite geöffnet wird. Then: dort steht „abgelaufen, oder diese Anwendung speichert keine Ergebnisse", nicht ein leeres Feld.
- **AE3. Covers R2, R5.** Given: das Backend ist nicht erreichbar. When: die Seite geöffnet wird. Then: sie lädt vollständig, und beim Ergebnis steht der Grund.
- **AE4. Covers R3.** Given: ein gescheiterter Task. When: die Seite geöffnet wird. Then: der Fehler aus dem Ergebnis steht da, als Fehler gekennzeichnet.
- **AE5. Covers R4.** Given: ein Ergebnis von 2 MB. When: die Seite geöffnet wird. Then: ein Anfang davon steht da, daneben „abgeschnitten -- die Darstellung wäre 2,0 MB lang". Die Zahl ist die Länge der Darstellung, nicht die des im Backend abgelegten Pakets (KTD3).

### Success Criteria

1. Die Frage „was kam heraus?" ist auf der Detailseite beantwortet, ohne Konsole — solange das Ergebnis im Backend liegt.
2. Kein Zustand des Ergebnisses ist stillschweigend: jeder trägt ein Wort — auch „noch nicht beendet" und „nicht lesbar".
3. Ohne erreichbares Backend kostet höchstens **ein** Aufruf je Sperrfrist die 2-s-Frist; jeder weitere Aufruf in diesem Fenster lädt so schnell wie mit gesundem Backend.
4. Kein Ergebnis landet im Verzeichnis, im Audit oder in einem Protokoll.

### Scope Boundaries

**In scope:** Detailseite, Ergebnis-Leser, Doku.
**Out of scope:** Ergebnisse in der Task-**Liste** (eine Spalte, die je Zeile einen Backend-Zugriff kostet); Suche über Ergebnisse; Fortschritt (`get_progress`) — eigene Frage, eigener Plan; jede Änderung an der Bibliothek.

### Dependencies / Assumptions

- swarmq ≥ 0.7 mit `SwarmQ.get_result`; im Paket bereits Abhängigkeit.
- Das Backend ist dasselbe, das der Canceller schon benutzt (`SWARMQ_BACKEND_URL`, Index 0).
- `result_ttl` ist Sache der Anwendung; das Dashboard liest sie nicht, es nennt sie nur als Ursache. Wirksam ist die Kaskade `TaskInfo.result_ttl` → `SWARMQ_RESULT_TTL`/`config.result_ttl` (Vorgabe 86400 s) → 24 h; der Dead-Letter-Pfad nimmt immer den globalen Wert, und `0` heißt: es wird nichts abgelegt.

### Outstanding Questions

Die Frage, ob ein abgeschnittenes Ergebnis nachladbar sein soll, ist bewusst verneint: Wer das ganze Ergebnis braucht, hat es im Backend, und eine zweite Ansicht dafür wäre eine Kennzahlenseite, die R25 ausschließt.

Zwei Fragen bleiben offen und gehören dem Betreiber, nicht diesem Plan:

1. **Gilt die Klartext-Zustimmung aus KD11 auch für Rückgabewerte?** KD11 des Eltern-Plans nahm Kundendaten im Verzeichnis bewusst an — für **Startdaten**, die der Aufrufer selbst mitgibt. KD4 überträgt diese Klasse auf **Rückgabewerte**, die ein Task berechnet oder lädt: Ein Task, der einen Kundendatensatz holt, zeigt ihn damit vollständig auf der Detailseite. Das ist eine andere Datenklasse, und die Ausweitung ist bislang angenommen, nicht zugestimmt. U3 dokumentiert sie; ob sie so bleibt, ist zu entscheiden.
2. **Wird `SWARMQ_RESULT_TTL` auf das Sieben-Tage-Fenster gehoben?** Das Verzeichnis hält Tasks sieben Tage (KD7, ausdrücklich für Wochenendvorfälle gewählt), Ergebnisse verfallen per Vorgabe nach 24 Stunden. Der Montagsblick auf den Samstagstask findet die Karte also leer. Der Hebel liegt allein in der Anwendung — das Dashboard hat keinen, und ein eigener wäre eine zweite Kopie, die KD1 ausschließt.

---

## Planning Contract

### Key Technical Decisions

- **KTD1. Ein eigener kleiner Dienst, nach dem Muster von `Canceller` und `ProbeSender`.** Protokoll plus swarmq-Implementierung, träge verbunden: Wer die Liste ansieht, bezahlt keine Backend-Verbindung. Nach einem Fehlversuch gilt eine Sperrfrist wie beim Kontrollkanal (`SPERRE_S`), damit eine Seite mit totem Backend nicht bei jedem Aufruf in dieselbe Frist läuft. **Sie wird auch von einem gescheiterten Lesen gesetzt, nicht nur von einem gescheiterten Verbindungsaufbau** — sonst greift sie genau im häufigsten Vorfall nicht: Stirbt das Backend, *nachdem* der Leser verbunden war, kehrt `connect()` nach dem Vorbild in `control.py` sofort zurück, die Sperre wird nie gesetzt, und jeder Aufruf läuft erneut in die volle Frist. Ein gescheitertes Lesen verwirft darum die Verbindung, setzt `SPERRE_S` und merkt sich den Grund.
- **KTD2. `get_result` wirft bei Fehler und Storno — das ist hier kein Fehlerfall, sondern die Auskunft.** `_unpack` der Bibliothek wirft `TaskCancelledError` bei storniert und `TaskError` bei gescheitert — **eine feste Klasse mit dem gespeicherten Fehlertext, nicht die ursprüngliche Ausnahme des Tasks**. Wer die Testdaten um den eigenen Ausnahmetyp herum baut, prüft gegen eine Form, die die Bibliothek nie erzeugt. Und `TaskCancelledError` ist eine **Unterklasse** von `TaskError`: Sie muss zuerst gefangen werden, sonst verschwindet der Storno-Zustand still im Fehlerzweig — das ist der „Vorrang für Storno". Der Leser fängt beides und gibt es als Zustand zurück (`fertig`, `gescheitert`, `storniert`) statt es durchzureichen. Ohne diese Wandlung führte ein gescheiterter Task zu einer Fehlerseite statt zu seiner Auskunft.
- **KTD3. Anzeigegrenze 8 KB, gemessen an der UTF-8-Länge der Darstellung.** Darüber wird abgeschnitten und diese Länge genannt. Sie ist ausdrücklich **nicht** die Größe des im Backend abgelegten Pakets: `get_result` gibt das entpackte Objekt zurück, an die Rohbytes kommt das Dashboard nach KD2 nicht heran. Seite und Doku sagen das, damit die Zahl nicht als Speicherbelegung gelesen wird. Die Zahl ist nicht gemessen, sondern gewählt: eine Karte fasst ungefähr so viel, bevor Scrollen die Seite unbrauchbar macht.
- **KTD4. Eigene Frist von 2 s für den Nachschlag,** wie beim Kontrollkanal. Die Detailseite ist kein Ort zum Warten; ein langsames Backend kostet höchstens diese zwei Sekunden und wird danach benannt. **Verworfene Alternative:** die Karte als Fragment nach dem Seitenaufbau nachladen, wie KTD7 des Eltern-Plans es ohnehin vorsieht. Das machte R5 baulich wahr und erübrigte Frist und Sperre. Dagegen spricht, dass die Seite dann in zwei Zuständen existiert — einmal ohne, einmal mit Ergebnis — und eine zweite Route samt eigenem Fehlerweg trägt, während der synchrone Weg mit korrekt gesetzter Sperre (KTD1) die Kosten bereits auf einen Aufruf je Sperrfrist begrenzt. Erweist sich das im Betrieb als zu teuer, ist der Fragment-Weg die vorgesehene Nachbesserung und ändert nur U2.

### High-Level Technical Design

Die Detailroute ruft `services.ergebnisse.lesen(task_id)` und bekommt ein `Ergebnisbild` zurück:

```
Ergebnisbild(
    zustand: "da" | "fehlt" | "offen" | "nicht_lesbar" | "nicht_erreichbar",
    art:     "fertig" | "gescheitert" | "storniert" | None,
    text:    str,           # dargestellter Klartext, ggf. abgeschnitten
    abgeschnitten: bool,    # ob `text` gekürzt wurde
    groesse: int | None,    # UTF-8-Länge der vollen Darstellung
    grund:   str | None,    # bei nicht_erreichbar und nicht_lesbar
)
```

`zu_gross` ist **kein** Zustand: Abschneiden ist eine Eigenschaft des dargestellten Textes und steht als eigenes Feld daneben. Sonst trügen ein gescheiterter Task und ein langer Fehlertext zwei Zustände zugleich, und die Marke wäre nicht entscheidbar.

Die Marke kommt bei `zustand="da"` allein aus `art`: `is-ok` für fertig, `is-fail` für gescheitert, **`is-idle` (neutral) für storniert** — so, wie `_ZUSTANDSART` in `templates/__init__.py` den Status-Marker desselben Tasks schon färbt und wie `oberflaeche.md` „storniert" in der neutralen Zeile führt; `is-wait` meint dort die *angeforderte* Stornierung, nicht den Ausgang. Die übrigen Zustände: neutral für `fehlt` und `offen`, `is-fail` für `nicht_erreichbar` und `nicht_lesbar` — dieselbe Marke, die die Seite im Storno-Fluss für „Das Backend ist nicht erreichbar" schon trägt. Das Abschneiden wird als Beschriftung neben dem Text gezeigt, nicht über die Farbe.

### Risks & Dependencies

| Risiko | Wirkung | Gegenmaßnahme |
|---|---|---|
| Das Ergebnis enthält personenbezogene Daten | Sie stehen im Browser, wie die Argumente | KD4: dieselbe Klasse, dokumentiert in `betrieb.md`; keine Kopie im Dashboard |
| Ein Task gibt etwas zurück, das sich nicht lesbar darstellen lässt | Unleserliche Karte statt Auskunft | Darstellung als JSON wie bei den Argumenten (`ensure_ascii=False`, `sort_keys=True`); nur was JSON nicht kodieren kann, fällt auf `repr` zurück, dieses mit Ausnahmefang |
| Ein sehr großes Ergebnis wird vollständig entpackt, bevor die Anzeigegrenze greift | Ein Seitenaufruf belastet den einen Dienstprozess (KTD5) | **Bewusst hingenommen.** Die Bibliothek deckelt eingehende Task-Nachrichten bei 16 MiB, das Ablegen eines Ergebnisses aber nicht, und `get_result` gibt nur das fertig entpackte Objekt heraus. Vorher zu messen hieße, die Rohbytes am Backend-Schlüssel zu lesen — genau der Griff, den die Stop-Conditions dieses Plans untersagen und den KD2/R1a dem Betreiber vorbehalten. Wer Ergebnisse dieser Größenordnung erzeugt, begrenzt sie in der Anwendung; ändert sich das, ist es eine Änderung an KD2 und gehört entschieden, nicht umgangen |
| Backend langsam | Detailseite hängt | KTD4: eigene Frist, danach benannte Ursache |
| Zwei Wahrheiten beim Fehlertext | Signal und Ergebnis widersprechen sich, wenn Signale verloren gingen (nach einer Wiederholung laufen beide auf den letzten Versuch zusammen) | F2: beide zeigen, beide beschriftet — nicht eines stillschweigend bevorzugen |

### Sequencing

U1 trägt U2; U3 ist Doku und kann parallel laufen.

---

## Implementation Units

### U1. Ergebnis-Leser

- **Goal:** Ein kleiner Dienst liest das Ergebnis eines Tasks und gibt es als Zustand zurück, statt zu werfen.
- **Requirements:** R1, R2, R3, R4, R5
- **Dependencies:** keine
- **Files:** `src/swarmq_dashboard/web/ergebnis.py` (neu), `tests/unit/web/test_ergebnis.py` (neu), `tests/integration/test_ergebnis_lesen.py` (neu)
- **Approach:**
  1. `Ergebnisbild` als eingefrorene Datenklasse mit den sechs Feldern aus dem Entwurf.
  2. Protokoll `Ergebnisleser` mit `lesen(task_id) -> Ergebnisbild`; Implementierung `SwarmQErgebnisleser` auf `SwarmQ.get_result(task_id, timeout=0)`, träge verbunden, mit Sperrfrist nach einem Fehlversuch (KTD1) — **auch nach einem gescheiterten Lesen**, nicht nur nach einem gescheiterten Verbindungsaufbau: Verbindung verwerfen, `SPERRE_S` setzen, Grund merken.
  3. `ResultNotReadyError` wird zu `zustand="fehlt"` — nicht zu einem Fehler.
  4. `TaskCancelledError` **zuerst** fangen — sie ist Unterklasse von `TaskError` — und zu `art="storniert"` machen; `TaskError` danach zu `art="gescheitert"` mit dem gespeicherten Text (KTD2).
  4a. `ResultSerializationError` eigens fangen und zu `zustand="nicht_lesbar"` mit dem Ausnahmetext machen. Sie ist weder `TaskError` noch `BackendConnectionError`; fiele sie in einen der beiden Sammelfänge, behauptete die Seite „der Task ist gescheitert" oder „Backend nicht erreichbar", obwohl in Wahrheit die Ablage nicht lesbar ist — erreichbar über Versionsversatz zwischen Anwendung und Dashboard.
  5. Darstellung als JSON wie in der Argumente-Karte (`json.dumps(..., ensure_ascii=False, sort_keys=True)`, wie `directory/writer.py` sie baut); nur was JSON nicht kodieren kann, fällt auf `repr` zurück, dieses mit Ausnahmefang. Abgeschnitten bei 8 KB, `abgeschnitten=True`, `groesse` = UTF-8-Länge der vollen Darstellung (KTD3).
  6. Verbindungsfehler zu `zustand="nicht_erreichbar"` mit Grund.
  7. `zustand="offen"` wird vom Leser nie erzeugt — er setzt ihn nicht, weil er für einen unbeendeten Task gar nicht erst gerufen wird (U2).
- **Patterns to follow:** `src/swarmq_dashboard/web/cancel.py` für den Zuschnitt, `src/swarmq_dashboard/control.py` für Sperrfrist und eigene Frist.
- **Test scenarios:**
  - Ein vorliegendes Ergebnis kommt als `zustand="da"`, `art="fertig"` mit seinem Klartext.
  - Covers AE2. `ResultNotReadyError` ergibt `zustand="fehlt"`, nicht eine Ausnahme.
  - Covers AE4. Eine Task-Ausnahme ergibt `art="gescheitert"` mit ihrem Text.
  - Ein storniertes Ergebnis ergibt `art="storniert"` — auch wenn zugleich ein Fehler vorliegt (Vorrang wie in der Bibliothek).
  - Covers AE5. Ein Ergebnis über der Grenze wird abgeschnitten, `abgeschnitten=True`, `groesse` nennt die UTF-8-Länge der vollen Darstellung.
  - Ein gescheiterter Task mit übergroßem Fehlertext behält `art="gescheitert"` — Abschneiden verdrängt die Art nicht.
  - Eine Zeichenkette mit Zeilenumbrüchen und ein Wörterbuch erscheinen als lesbares JSON, nicht als Python-`repr`.
  - Ein Objekt, das JSON nicht kodieren kann und dessen `repr` wirft, ergibt trotzdem eine Auskunft (Typname), keine Ausnahme.
  - Covers AE3. Ein toter Broker/Backend ergibt `zustand="nicht_erreichbar"` mit Grund.
  - Nach einem gescheiterten **Lesen** wird innerhalb der Sperrfrist gar nicht mehr auf das Backend zugegriffen — nicht nur nicht erneut verbunden.
  - Ein nicht entpackbares Ergebnis ergibt `zustand="nicht_lesbar"` mit Grund, nicht `art="gescheitert"`.
  - Ein storniertes Ergebnis wird als solches erkannt, obwohl seine Ausnahme Unterklasse der Fehler-Ausnahme ist.
  - Integration: ein echtes Ergebnis aus dem Backend wird gelesen und dargestellt.
- **Verification:** Einheitentests ohne Backend, Integration gegen den Testcontainer.

### U2. Die Detailseite zeigt es

- **Goal:** Unter den Argumenten steht das Ergebnis, mit einem Wort für jeden Zustand.
- **Requirements:** R1, R2, R3, R4, R6a
- **Dependencies:** U1
- **Files:** `src/swarmq_dashboard/web/routes/tasks.py`, `src/swarmq_dashboard/web/templates/tasks/detail.html`, `src/swarmq_dashboard/web/app.py`, `tests/unit/web/test_tasks_routes.py`
- **Approach:**
  1. `Services` hält den Leser; `create_app` nimmt ihn als Naht entgegen, wie `canceller` und `probe_sender`. Er gehört ebenso in die Schließliste von `Services.shutdown()` — dieselbe Aufzählung, in der Canceller, DLQ-Admin, Kontrollkanal und Probe-Sender stehen; er hält eine eigene Valkey-Verbindung, und ohne das leckt je Lebenszyklus eine.
  2. Die Detailroute ruft ihn nur für beendete Tasks — ein wartender hat kein Ergebnis, und der Aufruf wäre ein Backend-Zugriff für nichts. Ein unbeendeter Task bekommt `zustand="offen"` ohne jeden Zugriff, und die Karte trägt „der Task ist noch nicht beendet" (R2a) statt leer zu bleiben.
  2a. **Ebenso nachgeschlagen wird für Zeilen, die das Verzeichnis als unvollständig führt** — dieselbe Lücken-Markierung, die die Vorlage schon als Marke zeigt. Ging das Abschlusssignal verloren, steht die Zeile auf einem nicht beendeten Status, obwohl das Ergebnis im Backend liegt; ohne diese Erweiterung verweigerte die Seite die Auskunft ausgerechnet in dem Vorfall, in dem das Backend die letzte verbliebene Quelle ist. Ausgespart bleibt nur, was unbeendet **und** ohne Lücken-Markierung ist.
  3. Eine eigene Karte unter den Argumenten, Aufbau wie dort: Klartext in `.args`, darüber die Zustandsmarke.
  4. Bei `gescheitert` steht der Fehler aus dem Ergebnis **neben** dem aus dem Signal, beide beschriftet (F2).
  5. Bei `fehlt` nennt die Seite beide möglichen Ursachen, ohne eine zu behaupten: „abgelaufen nach der Aufbewahrungsfrist der Anwendung, oder diese Anwendung speichert keine Ergebnisse" — Letzteres trifft auf Fire-and-Forget ohne eigene Frist und auf `SWARMQ_RESULT_TTL=0` zu, wo gar nichts abgelegt wird.
- **Test scenarios:**
  - Covers AE1. Ein fertiger Task zeigt sein Ergebnis im Klartext.
  - Covers AE2. Ein fehlendes Ergebnis zeigt das Wort, nicht ein leeres Feld.
  - Covers AE3. Ohne Backend lädt die Seite vollständig und nennt den Grund.
  - Covers AE4, F2. Ein gescheiterter Task zeigt beide Fehlertexte, jeden beschriftet.
  - Covers AE5. Ein zu großes Ergebnis ist gekennzeichnet und nennt seine Größe.
  - Ein wartender Task löst keinen Backend-Zugriff aus und trägt trotzdem ein Wort auf der Karte.
  - Ein wartender Task, dessen Zeile als unvollständig markiert ist, löst den Nachschlag **doch** aus und zeigt das gefundene Ergebnis.
  - Ein stornierter Task zeigt die Ergebniskarte in derselben neutralen Marke wie seinen Status-Marker.
  - Das Ergebnis steht in keinem Audit-Eintrag (R6a).
- **Verification:** Am laufenden Dienst gegen eine Probe mit Rückgabewert und eine, die absichtlich scheitert.

### U3. Doku

- **Goal:** Der Betreiber weiß, wo Ergebnisse liegen und wann sie verschwinden.
- **Requirements:** R7
- **Dependencies:** keine
- **Files:** `docs/reference/betrieb.md`, `README.md`
- **Approach:** Ein Abschnitt in `betrieb.md`, im Aufbau des schon vorhandenen „Was in der Verzeichnisdatei steht": Ergebnisse liegen im Backend der Bibliothek unter `sq:result:<task_id>`; das Dashboard liest sie nur und speichert sie nirgends. Er nennt drei Dinge ausdrücklich:
  1. **Die Frist ist eine Kaskade**, nicht ein Feld: `TaskInfo.result_ttl`, sonst `SWARMQ_RESULT_TTL`/`config.result_ttl` (Vorgabe 86400 s), sonst 24 h; der Dead-Letter-Pfad nimmt immer den globalen Wert, und `0` — etwa bei Fire-and-Forget — heißt: es wird gar nichts abgelegt.
  2. **Das Verhältnis zur Aufbewahrung des Verzeichnisses:** Tasks bleiben sieben Tage sichtbar (`SQD_RETENTION_DAYS`, KD7), Ergebnisse per Vorgabe einen Tag. Wer die Ergebnisse über das ganze Fenster tragen will — etwa für den Montagsblick auf einen Samstagsvorfall —, muss `SWARMQ_RESULT_TTL` entsprechend setzen; das Dashboard hat dafür keinen Schalter.
  3. **Die neue Datenklasse:** Jedes angemeldete Konto sieht den vollständigen Rückgabewert jedes beendeten Tasks — auch Token, signierte URLs oder Datensätze, die in keinem Argument standen. Der einzige Hebel liegt in der Anwendung: solche Werte nicht zurückgeben, oder die Frist kürzen. Ein Satz im README bei der Beschreibung der Detailseite.

---

## Verification Contract

| Tor | Womit | Erwartung |
|---|---|---|
| Stil | `ruff` über `src/` und `tests/` | keine Meldung |
| Typen | `mypy --strict` | keine Meldung |
| Einheiten | `pytest tests/unit` | grün, Suite bleibt unter 30 s |
| Integration | `pytest tests/integration` | grün gegen die Testdienste |

Darüber hinaus:

- Die Detailseite eines wartenden Tasks macht **keinen** Backend-Zugriff — nachgewiesen über eine Attrappe, die jeden Aufruf mitzählt — und trägt dort trotzdem ein Wort.
- Ohne erreichbares Backend zahlt höchstens ein Aufruf je Sperrfrist die 2-s-Frist; jeder weitere in diesem Fenster lädt so schnell wie mit gesundem Backend. Nachgewiesen über eine Uhr-Attrappe, mit **beiden** Fällen: Backend von Anfang an tot, und Backend stirbt nach dem ersten erfolgreichen Lesen.
- Kein Ergebnis steht im Audit-Strom, im Verzeichnis oder in einer Protokollzeile.

## Definition of Done

- AE1 bis AE5 laufen nach.
- Jeder Zustand trägt ein Wort auf der Seite, keiner ein leeres Feld — einschließlich „noch nicht beendet" und „nicht lesbar".
- `betrieb.md` nennt Ort und Frist der Ergebnisse, das Verhältnis zur Aufbewahrung des Verzeichnisses und die neue Datenklasse.
- Ein Durchlauf am laufenden Dienst: eine Probe mit Rückgabewert, eine gescheiterte, eine mit abgelaufenem Ergebnis.
