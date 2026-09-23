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

Die Detailseite eines Tasks zeigt heute Argumente und Fehlertext, aber kein Ergebnis. Dabei liegt es vor: Die Bibliothek legt es unter `sq:result:<task_id>` im Backend ab, mit der Verfallsfrist aus `TaskInfo.result_ttl`. Die Seite holt es künftig beim Aufruf und zeigt es unter den Argumenten — mit derselben Sorgfalt, die für die Argumente schon gilt.

### Problem Frame

Ein Teammitglied sieht auf der Detailseite „fertig" und die Laufzeit. Was der Task zurückgab, steht nirgends. Bei einer Probe sind das Worker, PID und gemessene Laufzeit; bei einem echten Task die Antwort, wegen der er lief. Wer sie braucht, greift heute zur Kommandozeile und liest den Backend-Schlüssel selbst — genau der Griff, den das Dashboard abschaffen soll (F1).

R6 sieht das vor und nennt einen **Ergebnisverweis**; die Spalte `result_ref` steht im Schema. Gefüllt wird sie von niemandem. Das ist keine vergessene Zeile, sondern eine offene Naht: Der Ereignisstrom trägt das Ergebnis nicht, `task_completed` nennt nur Kennung, Name, Queue und Dauer.

### Key Decisions

- **KD1. Das Ergebnis wird gelesen, nicht gespeichert.** Die Seite holt es bei jedem Aufruf aus dem Backend. Alternative wäre, es beim Einlesen ins Verzeichnis zu übernehmen — dagegen sprechen drei Dinge: Es hat eine eigene Verfallsfrist, die das Verzeichnis nicht kennt und nicht einhalten könnte; es wäre eine zweite Kopie personenbezogener Daten an einem zweiten Ort, mit einer zweiten Löschpflicht (R7a); und der Ereignisstrom trägt es gar nicht, die Übernahme bräuchte also ohnehin einen Backend-Zugriff. Governs R6.
- **KD2. Nur über die öffentliche API.** `SwarmQ.get_result(task_id, timeout=0)` ist ein einzelner GET ohne Warten. Die Schlüsselnamen und das Packformat der Bibliothek bleiben ihre Sache (R1a).
- **KD3. Vier Zustände, keiner davon stillschweigend.** „da", „abgelaufen oder nie gespeichert", „zu groß zum Anzeigen", „Backend nicht erreichbar". Ein fehlendes Ergebnis als leeres Feld zu zeigen wäre die stille Falschaussage, gegen die R33 steht.
- **KD4. Dieselbe Schutzklasse wie die Argumente.** Das Ergebnis steht im Klartext da, wie die Argumente seit KD11 — mit dem Unterschied, dass es nicht im Dashboard liegt. Es geht nicht in den Audit-Strom und nicht ins Verzeichnis.

### Actors

- **Teammitglied im Vorfall** — will wissen, was ein Task zurückgab, ohne eine Konsole zu öffnen.
- **Betreiber** — muss wissen, wo diese Daten liegen und wann sie verfallen (R7a).

### Requirements

- R1. Die Detailseite zeigt das Ergebnis eines Tasks, sobald es im Backend vorliegt.
- R2. Liegt keines vor, unterscheidet die Seite „abgelaufen oder nie gespeichert" von „Backend nicht erreichbar" und nennt bei letzterem den Grund.
- R3. Ein gescheiterter Task zeigt seinen Fehler; ein stornierter zeigt, dass er storniert wurde. Beides kommt aus dem Ergebnis, nicht nur aus dem Signal.
- R4. Ein Ergebnis über der Anzeigegrenze wird abgeschnitten und **als abgeschnitten gekennzeichnet**, mit seiner wahren Größe.
- R5. Das Nachschlagen hält die Seite nie auf: eine eigene Frist, und ein Fehlschlag ist eine Auskunft, kein Fehler.
- R6a. Das Ergebnis wird nicht ins Verzeichnis übernommen und nicht in den Audit geschrieben.
- R7. Die Betriebsdoku nennt, wo Ergebnisse liegen, wann sie verfallen und dass das Dashboard sie nur liest.

### Key Flows

- **F1. Nachsehen, was herauskam.** Teammitglied öffnet einen fertigen Task → die Seite zeigt unter den Argumenten das Ergebnis im Klartext, mit dem Hinweis, dass es nach `result_ttl` verfällt.
- **F2. Der Task ist gescheitert.** Die Seite zeigt den Fehlertext aus dem Signal **und** den aus dem Ergebnis, falls beide vorliegen — sie können auseinandergehen, wenn eine Wiederholung dazwischenlag.
- **F3. Das Ergebnis ist weg.** Der Task ist alt, die Frist abgelaufen: Die Seite sagt „abgelaufen oder nie gespeichert" statt eines leeren Feldes, und nennt `result_ttl` als Ursache.

### Acceptance Examples

- **AE1. Covers R1.** Given: ein fertiger Task mit Ergebnis im Backend. When: die Detailseite geöffnet wird. Then: das Ergebnis steht im Klartext da.
- **AE2. Covers R2.** Given: ein fertiger Task, dessen Ergebnis verfallen ist. When: die Seite geöffnet wird. Then: dort steht „abgelaufen oder nie gespeichert", nicht ein leeres Feld.
- **AE3. Covers R2, R5.** Given: das Backend ist nicht erreichbar. When: die Seite geöffnet wird. Then: sie lädt vollständig, und beim Ergebnis steht der Grund.
- **AE4. Covers R3.** Given: ein gescheiterter Task. When: die Seite geöffnet wird. Then: der Fehler aus dem Ergebnis steht da, als Fehler gekennzeichnet.
- **AE5. Covers R4.** Given: ein Ergebnis von 2 MB. When: die Seite geöffnet wird. Then: ein Anfang davon steht da, daneben „abgeschnitten, 2,0 MB".

### Success Criteria

1. Die Frage „was kam heraus?" ist auf der Detailseite beantwortet, ohne Konsole.
2. Kein Zustand des Ergebnisses ist stillschweigend: jeder trägt ein Wort.
3. Die Seite lädt ohne Backend genauso schnell wie mit.
4. Kein Ergebnis landet im Verzeichnis, im Audit oder in einem Protokoll.

### Scope Boundaries

**In scope:** Detailseite, Ergebnis-Leser, Doku.
**Out of scope:** Ergebnisse in der Task-**Liste** (eine Spalte, die je Zeile einen Backend-Zugriff kostet); Suche über Ergebnisse; Fortschritt (`get_progress`) — eigene Frage, eigener Plan; jede Änderung an der Bibliothek.

### Dependencies / Assumptions

- swarmq ≥ 0.7 mit `SwarmQ.get_result`; im Paket bereits Abhängigkeit.
- Das Backend ist dasselbe, das der Canceller schon benutzt (`SWARMQ_BACKEND_URL`, Index 0).
- `result_ttl` ist Sache der Anwendung; das Dashboard liest sie nicht, es nennt sie nur als Ursache.

### Outstanding Questions

Keine offen. Die Frage, ob ein abgeschnittenes Ergebnis nachladbar sein soll, ist bewusst verneint: Wer das ganze Ergebnis braucht, hat es im Backend, und eine zweite Ansicht dafür wäre eine Kennzahlenseite, die R25 ausschließt.

---

## Planning Contract

### Key Technical Decisions

- **KTD1. Ein eigener kleiner Dienst, nach dem Muster von `Canceller` und `ProbeSender`.** Protokoll plus swarmq-Implementierung, träge verbunden: Wer die Liste ansieht, bezahlt keine Backend-Verbindung. Nach einem Fehlversuch gilt eine Sperrfrist wie beim Kontrollkanal (`SPERRE_S`), damit eine Seite mit totem Backend nicht bei jedem Aufruf in dieselbe Frist läuft.
- **KTD2. `get_result` wirft bei Fehler und Storno — das ist hier kein Fehlerfall, sondern die Auskunft.** `_unpack` der Bibliothek wirft `TaskCancelledError` bei storniert und die Task-Ausnahme bei gescheitert, mit Vorrang für Storno. Der Leser fängt beides und gibt es als Zustand zurück (`fertig`, `gescheitert`, `storniert`) statt es durchzureichen. Ohne diese Wandlung führte ein gescheiterter Task zu einer Fehlerseite statt zu seiner Auskunft.
- **KTD3. Anzeigegrenze 8 KB, gemessen an der kodierten Länge.** Darüber wird abgeschnitten und die wahre Größe genannt. Die Zahl ist nicht gemessen, sondern gewählt: eine Karte fasst ungefähr so viel, bevor Scrollen die Seite unbrauchbar macht.
- **KTD4. Eigene Frist von 2 s für den Nachschlag,** wie beim Kontrollkanal. Die Detailseite ist kein Ort zum Warten; ein langsames Backend kostet höchstens diese zwei Sekunden und wird danach benannt.

### High-Level Technical Design

Die Detailroute ruft `services.ergebnisse.lesen(task_id)` und bekommt ein `Ergebnisbild` zurück:

```
Ergebnisbild(
    zustand: "da" | "fehlt" | "zu_gross" | "nicht_erreichbar",
    art:     "fertig" | "gescheitert" | "storniert" | None,
    text:    str,          # dargestellter Klartext, ggf. abgeschnitten
    groesse: int | None,   # wahre Größe in Bytes, wenn bekannt
    grund:   str | None,   # bei nicht_erreichbar
)
```

Die Vorlage bildet die vier Zustände auf die vorhandenen Marken ab: `is-ok` für fertig, `is-fail` für gescheitert, `is-wait` für storniert und zu groß, neutral für fehlt.

### Risks & Dependencies

| Risiko | Wirkung | Gegenmaßnahme |
|---|---|---|
| Das Ergebnis enthält personenbezogene Daten | Sie stehen im Browser, wie die Argumente | KD4: dieselbe Klasse, dokumentiert in `betrieb.md`; keine Kopie im Dashboard |
| Ein Task gibt etwas zurück, das sich nicht darstellen lässt | Fehlerseite statt Auskunft | Darstellung über `repr` mit Ausnahmefang; misslingt sie, steht der Typ da |
| Backend langsam | Detailseite hängt | KTD4: eigene Frist, danach benannte Ursache |
| Zwei Wahrheiten beim Fehlertext | Signal und Ergebnis widersprechen sich nach einer Wiederholung | F2: beide zeigen, beide beschriftet — nicht eines stillschweigend bevorzugen |

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
  1. `Ergebnisbild` als eingefrorene Datenklasse mit den fünf Feldern aus dem Entwurf.
  2. Protokoll `Ergebnisleser` mit `lesen(task_id) -> Ergebnisbild`; Implementierung `SwarmQErgebnisleser` auf `SwarmQ.get_result(task_id, timeout=0)`, träge verbunden, mit Sperrfrist nach einem Fehlversuch (KTD1).
  3. `ResultNotReadyError` wird zu `zustand="fehlt"` — nicht zu einem Fehler.
  4. `TaskCancelledError` zu `art="storniert"`, jede andere Ausnahme aus dem Entpacken zu `art="gescheitert"` mit ihrem Text (KTD2).
  5. Darstellung über `repr`, abgeschnitten bei 8 KB, mit wahrer Größe (KTD3).
  6. Verbindungsfehler zu `zustand="nicht_erreichbar"` mit Grund.
- **Patterns to follow:** `src/swarmq_dashboard/web/cancel.py` für den Zuschnitt, `src/swarmq_dashboard/control.py` für Sperrfrist und eigene Frist.
- **Test scenarios:**
  - Ein vorliegendes Ergebnis kommt als `zustand="da"`, `art="fertig"` mit seinem Klartext.
  - Covers AE2. `ResultNotReadyError` ergibt `zustand="fehlt"`, nicht eine Ausnahme.
  - Covers AE4. Eine Task-Ausnahme ergibt `art="gescheitert"` mit ihrem Text.
  - Ein storniertes Ergebnis ergibt `art="storniert"` — auch wenn zugleich ein Fehler vorliegt (Vorrang wie in der Bibliothek).
  - Covers AE5. Ein Ergebnis über der Grenze wird abgeschnitten, `groesse` nennt die wahre Länge.
  - Ein Objekt, dessen `repr` wirft, ergibt trotzdem eine Auskunft (Typname), keine Ausnahme.
  - Covers AE3. Ein toter Broker/Backend ergibt `zustand="nicht_erreichbar"` mit Grund.
  - Nach einem Fehlversuch wird innerhalb der Sperrfrist nicht erneut verbunden.
  - Integration: ein echtes Ergebnis aus dem Backend wird gelesen und dargestellt.
- **Verification:** Einheitentests ohne Backend, Integration gegen den Testcontainer.

### U2. Die Detailseite zeigt es

- **Goal:** Unter den Argumenten steht das Ergebnis, mit einem Wort für jeden Zustand.
- **Requirements:** R1, R2, R3, R4, R6a
- **Dependencies:** U1
- **Files:** `src/swarmq_dashboard/web/routes/tasks.py`, `src/swarmq_dashboard/web/templates/tasks/detail.html`, `src/swarmq_dashboard/web/app.py`, `tests/unit/web/test_tasks_routes.py`
- **Approach:**
  1. `Services` hält den Leser; `create_app` nimmt ihn als Naht entgegen, wie `canceller` und `probe_sender`.
  2. Die Detailroute ruft ihn nur für beendete Tasks — ein wartender hat kein Ergebnis, und der Aufruf wäre ein Backend-Zugriff für nichts.
  3. Eine eigene Karte unter den Argumenten, Aufbau wie dort: Klartext in `.args`, darüber die Zustandsmarke.
  4. Bei `gescheitert` steht der Fehler aus dem Ergebnis **neben** dem aus dem Signal, beide beschriftet (F2).
  5. Bei `fehlt` nennt die Seite `result_ttl` als Ursache, ohne sie zu kennen: „nach der Aufbewahrungsfrist der Anwendung".
- **Test scenarios:**
  - Covers AE1. Ein fertiger Task zeigt sein Ergebnis im Klartext.
  - Covers AE2. Ein fehlendes Ergebnis zeigt das Wort, nicht ein leeres Feld.
  - Covers AE3. Ohne Backend lädt die Seite vollständig und nennt den Grund.
  - Covers AE4, F2. Ein gescheiterter Task zeigt beide Fehlertexte, jeden beschriftet.
  - Covers AE5. Ein zu großes Ergebnis ist gekennzeichnet und nennt seine Größe.
  - Ein wartender Task löst keinen Backend-Zugriff aus.
  - Das Ergebnis steht in keinem Audit-Eintrag (R6a).
- **Verification:** Am laufenden Dienst gegen eine Probe mit Rückgabewert und eine, die absichtlich scheitert.

### U3. Doku

- **Goal:** Der Betreiber weiß, wo Ergebnisse liegen und wann sie verschwinden.
- **Requirements:** R7
- **Dependencies:** keine
- **Files:** `docs/reference/betrieb.md`, `README.md`
- **Approach:** Ein Abschnitt in `betrieb.md`: Ergebnisse liegen im Backend der Bibliothek unter `sq:result:<task_id>`, ihre Frist ist `TaskInfo.result_ttl`, das Dashboard liest sie nur und speichert sie nirgends. Ein Satz im README bei der Beschreibung der Detailseite.

---

## Verification Contract

| Tor | Womit | Erwartung |
|---|---|---|
| Stil | `ruff` über `src/` und `tests/` | keine Meldung |
| Typen | `mypy --strict` | keine Meldung |
| Einheiten | `pytest tests/unit` | grün, Suite bleibt unter 30 s |
| Integration | `pytest tests/integration` | grün gegen die Testdienste |

Darüber hinaus:

- Die Detailseite eines wartenden Tasks macht **keinen** Backend-Zugriff — nachgewiesen über eine Attrappe, die jeden Aufruf mitzählt.
- Ohne erreichbares Backend lädt die Seite in derselben Zeit wie mit; die Sperrfrist verhindert, dass jeder Aufruf erneut in die Frist läuft.
- Kein Ergebnis steht im Audit-Strom, im Verzeichnis oder in einer Protokollzeile.

## Definition of Done

- AE1 bis AE5 laufen nach.
- Jeder der vier Zustände trägt ein Wort auf der Seite, keiner ein leeres Feld.
- `betrieb.md` nennt Ort und Frist der Ergebnisse.
- Ein Durchlauf am laufenden Dienst: eine Probe mit Rückgabewert, eine gescheiterte, eine mit abgelaufenem Ergebnis.
