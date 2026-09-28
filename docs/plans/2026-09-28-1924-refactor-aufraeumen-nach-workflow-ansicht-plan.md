---
title: Aufräumen nach der Workflow-Ansicht - Plan
type: refactor
date: 2026-09-28
topic: aufraeumen-nach-workflow-ansicht
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Aufräumen nach der Workflow-Ansicht - Plan

## Goal Capsule

- **Objective:** Wer das Dashboard weiterbaut, findet jede Zuständigkeit an einer Stelle und kein Stück Code, das niemand aufruft; offene Detailseiten eines großen Workflows belasten das Backend nicht mehr mit einer Lesung je Kopf-Glied und Live-Meldung.
- **Means:** Fünf Aufräumeinheiten aus dem Code-Review der Workflow-Ansicht (Run `20260928-134253-dcd53248`): eine gemeinsame Basis für die träge verbundenen Valkey-Klienten (KTD2), ein Zwischenspeicher für beendete Ergebnisse (KTD3), `Services` in ein eigenes Modul (KTD4), vier ungenutzte Writer-Methoden weg (KTD5) und eine geschlossene Testlücke.
- **Product authority:** Dieser Plan ändert kein sichtbares Verhalten mit zwei benannten Ausnahmen aus R3: weniger Backend-Lesungen für Ergebnisse, und ein einmal gelesenes fertiges oder storniertes Ergebnis bleibt bis zum Neustart des Dienstes sichtbar, auch wenn es in Valkey inzwischen verfallen ist. Er setzt auf `feat/workflow-ansicht` auf (`docs/plans/2026-09-28-1003-feat-workflow-ansicht-plan.md`); die Bibliothek swarmq bleibt unberührt.
- **Execution profile:** Testgetrieben nach `AGENTS.md`. Reine Umbauten (U2, U5) laufen als Refactoring: vorher und nachher alle Tests grün, kein neues Verhalten. Verhaltensänderungen (U3, U4) beginnen mit einem roten Test.
- **Stop conditions:** Halte an, wenn ein Umbau einen bestehenden Test nur durch Ändern seiner Zusicherung grün bekäme (nicht durch Ändern eines Patch-Ziels oder einer Hilfsfunktion), oder wenn die gemeinsame Basis eine der Eigenheiten aus R2 nicht halten kann. Ausnahme in U2: Zusicherungen auf die Rückgabe der gelöschten Methoden werden durch gleichbedeutende Zusicherungen auf das passende Element des Tupels von `vermerke_fehlend` ersetzt; das ist kein Stoppgrund, solange die geprüfte Sichtbarkeit bzw. der Löschzustand gleich bleibt.
- **Open blockers:** keine.

---

## Product Contract

### Summary

Die drei Valkey-Klienten des Dashboards erben ihr Verbindungsverhalten aus einer Basis, einschließlich Verbindungsriegel. Beendete Ergebnisse liest der Ergebnisleser nur einmal. `Services` liegt in einem eigenen Modul, vier ungenutzte Writer-Methoden fallen weg, und die Dauer-Spalte eines vor dem Start stornierten Schritts ist geprüft.

### Problem Frame

Das Code-Review der Workflow-Ansicht hat fünf Punkte offen gelassen, die kein Fehlverhalten sind, aber Wartung und Last kosten. `web/app.py` ist auf 1020 Zeilen gewachsen, weil `Services` samt Takten dort lebt. Drei Klassen (`SwarmQErgebnisleser`, `SwarmQWorkflowleser`, `SwarmQSetpointStore`) bauen dasselbe Muster aus Verbindung, Ping und Sperrfrist je für sich; nur der Workflowleser hat seit dem Review einen Riegel gegen gleichzeitige Verbindungsaufbauten und schließt einen Klienten, dessen Ping scheitert. Die Detailseite eines Chords liest bei jeder Live-Meldung jedes beendete Kopf-Glied neu, obwohl sich ein beendetes Ergebnis nicht mehr ändert. Vier öffentliche Writer-Methoden benutzt nur die Testreihe, und ein Zweig der Baum-Vorlage ist ungetestet.

### Key Decisions

- KD1. **Eigener Branch und eigener MR auf Basis von `feat/workflow-ansicht`** (session-settled: user-approved — chosen over Nachschieben in MR !1: MR !1 bleibt so, wie er geprüft wurde).
- KD2. **Die gemeinsame Basis umfasst auch den Sollwert-Speicher aus Stufe 1** (session-settled: user-approved — chosen over nur den zwei Lesern: das Muster steht dreimal, und `control.py` hätte sonst weiter keinen Riegel). Governs R1, R2.
- KD3. **Zwischenspeicher für beendete Ergebnisse statt einer Obergrenze** (session-settled: user-approved — chosen over einem Deckel je Render: ein beendetes Ergebnis ändert sich nicht, ein Deckel würde Ergebnisse unterschlagen). Governs R3.
- KD4. **Die vier nur von Tests genutzten Writer-Methoden werden gelöscht** (session-settled: user-approved — chosen over Behalten: ihre Tests laufen dann über die Wege, die der Code wirklich nimmt). Governs R5.

### Requirements

**Verbindungen**

- R1. Ergebnisleser, Workflowleser und Sollwert-Speicher bauen bei gleichzeitigen ersten Aufrufen genau eine Verbindung auf und schließen einen Klienten, dessen Ping scheitert.
- R2. Jede Eigenheit, die ein Test heute festhält, bleibt: der Wortlaut des Sperrgrunds je Klient, der Rückgabewert von `connect()`, das wahlweise synchrone `close()` der App beim Ergebnisleser, `store=` und `_eigen` beim Sollwert-Speicher, dass der Sollwert-Speicher nach einem Fehler mitten im Aufruf nicht sperrt, und jedes Moduls eigenes `SPERRE_S`.

**Ergebnisse**

- R3. Ein fertiges oder storniertes Ergebnis liest der Ergebnisleser je Task-Kennung höchstens einmal, solange der Dienst läuft; `gescheitert`, `fehlt`, `nicht_lesbar` und `nicht_erreichbar` werden bei jeder Anfrage neu gelesen. Der Zwischenspeicher ist nach Einträgen begrenzt, und ein gespeichertes Ergebnis bleibt sichtbar, auch nachdem es in Valkey verfallen ist.

**Struktur**

- R4. `Services` und seine Takte liegen in einem eigenen Modul; die bisherigen Namen aus `swarmq_dashboard.web.app` bleiben importierbar.
- R5. Die Writer-Methoden `vermerke_verfallen`, `vermerke_nie_gesehen`, `bestaetige_kandidat` und `verwerfe_kandidat` gibt es nicht mehr; ihr Verhalten ist über `vermerke_fehlend` und `schreibe_lesung` weiter getestet.

**Tests und Vorlage**

- R6. Ein Test hält fest, dass die Dauer-Spalte eines Schritts, der endete, ohne je zu starten, „--“ zeigt; der tote Zweig `zustand == 'offen'` in `_baum.html` fällt weg.

### Scope Boundaries

- Paralleles Lesen der nicht zwischengespeicherten Ergebnisse. Mit dem Zwischenspeicher bleiben je Render nur die noch nicht gelesenen übrig; das lohnt die Nebenläufigkeit erst, wenn Messungen es zeigen.
- Die zwei nicht freigegebenen Produktkorrekturen aus der Plan-Prüfung der Workflow-Ansicht (Stornierungen von außen lesbar; mitten im Lauf stornierter Schritt als Ursache). Sie ändern Verhalten und brauchen eine eigene Entscheidung.
- Weitere Aufteilung von `web/app.py` über `Services` hinaus.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **Reihenfolge nach Risiko:** zuerst die reinen Test- und Löscharbeiten (U1, U2), dann die Basis (U3), darauf der Zwischenspeicher (U4), zuletzt der Umzug von `Services` (U5), weil er die meisten Importstellen berührt.
- KTD2. **Die Basis folgt `SwarmQWorkflowleser`.** Sie liegt in `src/swarmq_dashboard/zugang.py` und trägt Uhr, Sperrfrist, Riegel, Ping-dann-behalten, `close()` und einen Hook `_ausfall(exc)`. Jede Unterklasse liefert ihren Kern-Aufbau, ihren Sperrtext und ihre `SPERRE_S` als Klassenattribute bzw. Methode. Zwei Hooks halten die Eigenheiten des Sollwert-Speichers: er überschreibt `_ausfall` nicht, weil er mitten im Aufruf nicht sperrt, und ein Hook für das Schließen des Kerns lässt einen eingesetzten `store=` (`_eigen = False`) offen und erhalten, statt ihn zu verwerfen (R2). Das Leck bei gescheitertem Ping gibt es nur beim Ergebnisleser (er setzt `_client` vor dem Ping und schließt ihn im Fehlerzweig nicht); der Sollwert-Speicher schließt schon heute über `close()` im Fehlerzweig von `connect()`. Neu für beide ist der Riegel (R1). Governs R1, R2.
- KTD3. **Der Zwischenspeicher sitzt im Ergebnisleser, nicht in der Route.** So profitieren Workflow-Detail, Glieder-Seite und Task-Ansicht gleichermaßen. Er ist ein begrenztes Mapping (LRU, 4096 Einträge) von Task-Kennung auf `Ergebnisbild`, nur für `zustand == "da"` mit `art` „fertig“ oder „storniert“. `gescheitert` bleibt draußen: ein Requeue aus der Dead-Letter-Queue (DLQ-Seite des Dashboards, `retry_dead_letter` in swarmq) reiht dieselbe Kennung neu ein, und ein erfolgreicher Lauf überschreibt das Fehlerergebnis mit „fertig“. Eine Frist braucht der Speicher nicht, weil fertig und storniert nicht mehr wechseln; verfällt ein solches Ergebnis in Valkey, zeigt der Speicher weiter das gelesene Ergebnis. Ein frisch gestarteter Dienst zeigt dann „abgelaufen“ — das ist die zweite benannte Ausnahme der Product authority. Governs R3.
- KTD4. **`Services` zieht nach `src/swarmq_dashboard/web/services.py` um,** zusammen mit `_teil`, `_NochNichtVerbunden`, `_leise_schliessen` und den Konstanten, die nur `Services` nutzt. `web/app.py` importiert und reexportiert `Services`, `PROBE_TIMEOUT_S` und die übrigen Namen, die Tests von dort holen. Die zwei Tests, die `swarmq_dashboard.web.app.kontrollkanal_grund` patchen, bekommen das neue Patch-Ziel, denn der Name wird dann in `web/services.py` nachgeschlagen; ihre Zusicherungen bleiben. Governs R4.
- KTD5. **Tests der gelöschten Methoden laufen über die genutzten Wege.** Fixtures, die heute `vermerke_verfallen` oder `vermerke_nie_gesehen` aufrufen, nutzen `vermerke_fehlend`. Weil `vermerke_fehlend` zuerst jeden unbestätigten Kandidaten verwirft, brauchen die Fälle „nie gesehen“ und „ohne Lesung, nicht verfallen“ einen bestätigten Workflow (Einreihung mit `workflow_id`), nicht die Fixture `_mit_kandidat`. Aufrufe von `bestaetige_kandidat` auf schon bestätigten Workflows entfallen ersatzlos; `schreibe_lesung` ist der Weg nur für den Test, der die Bestätigung eines Kandidaten prüft. Governs R5.

### Sources

- Review-Artefakte: `/tmp/compound-engineering-1000/ce-code-review/20260928-134253-dcd53248/review.json` (Befunde #1, #2, #4, #7/#8, #9).
- Vorbilder: `src/swarmq_dashboard/workflows.py` (Riegel, Ping-dann-behalten, `_ausfall`), `tests/unit/test_workflows.py` (Tests für gleichzeitige erste Aufrufe und gescheiterten Ping), `_Zaehlleser` in `tests/unit/web/test_workflows_routes.py` zum Zählen von Lesungen.

---

## Implementation Units

### U1. Dauer-Spalte prüfen, toten Vorlagenzweig entfernen

- **Goal:** Die Dauer-Spalte eines vor dem Start stornierten Schritts ist festgehalten, und `_baum.html` enthält keinen Zweig für einen Zustand, den der Leser nie liefert.
- **Requirements:** R6.
- **Dependencies:** keine.
- **Files:** `tests/unit/web/test_workflows_routes.py`, `src/swarmq_dashboard/web/templates/workflows/_baum.html`.
- **Approach:** Den Test `test_nach_dem_abbruch_aller_wartenden_ist_nichts_mehr_aktiv` um eine Zusicherung auf die Zeile `knoten-g0` ergänzen oder einen eigenen Test daneben stellen; danach den Zweig `zustand == 'offen'` aus der Ergebniszeile entfernen.
- **Execution note:** Charakterisierung: der neue Test ist beim ersten Lauf grün, weil er bestehendes Verhalten festhält; eine kurze Gegenprobe (Zweig „--“ vorübergehend ändern) zeigt, dass er greift.
- **Patterns to follow:** `_zeile` und `_text` in `tests/unit/web/test_workflows_routes.py`.
- **Test scenarios:**
  - Ein Glied mit `task_cancelled` ohne `task_started` zeigt in der Dauer-Spalte „--“.
  - Ein Glied mit Start und Ende zeigt weiter die Dauer, eins nur mit Start „läuft seit“.
- **Verification:** Der neue Test ist grün, die Gegenprobe rot, und die Vorlagen-Prüfungen in `tests/unit/web/test_templates.py` bleiben grün.

### U2. Ungenutzte Writer-Methoden löschen

- **Goal:** Der Writer hat nur noch die Workflow-Methoden, die der Code benutzt.
- **Requirements:** R5; KTD5.
- **Dependencies:** keine.
- **Files:** `src/swarmq_dashboard/directory/writer.py`, `tests/unit/directory/test_writer.py`, `tests/unit/directory/test_queries.py`, `tests/unit/web/test_workflows_routes.py`.
- **Approach:**
  1. Die Aufrufer in den Tests auf `vermerke_fehlend` bzw. `schreibe_lesung` umstellen; nach jedem Umstellen alle Tests grün.
  2. Die Tests, die nur die gelöschten Methoden selbst prüfen, auf dieselben Fälle über `vermerke_fehlend` umschreiben (verfallen nur nach „running“ und „gesehen“, nie gesehen nur ohne Lesung, Verwerfen nur unbestätigter Kandidaten).
  3. Die vier Methoden entfernen; die privaten Helfer bleiben für `vermerke_fehlend`.
- **Execution note:** Refactoring: kein neues Verhalten, nach jedem Schritt alle Tests.
- **Patterns to follow:** die bestehenden Tests zu `vermerke_fehlend` in `tests/unit/directory/test_writer.py`.
- **Test scenarios:**
  - `vermerke_fehlend` auf einen zuletzt laufend gesehenen Workflow ergibt verfallen, auf einen bestätigten, nie gelesenen „nie gesehen“, auf einen unbestätigten Kandidaten Verwerfen.
  - Ein Endstatus wird durch `vermerke_fehlend` nicht verfallen.
- **Verification:** `grep` findet die vier Namen nirgends mehr, und die Suite ist grün.

### U3. Gemeinsame Basis für die Valkey-Klienten

- **Goal:** Die drei Klienten teilen Verbindungsaufbau, Riegel, Ping-Prüfung, Sperrfrist und Schließen aus einer Basis.
- **Requirements:** R1, R2; KTD2.
- **Dependencies:** U1, U2 (sauberer Stand).
- **Files:** `src/swarmq_dashboard/zugang.py` (neu), `src/swarmq_dashboard/web/ergebnis.py`, `src/swarmq_dashboard/workflows.py`, `src/swarmq_dashboard/control.py`, `tests/unit/test_zugang.py` (neu), `tests/unit/web/test_ergebnis.py`, `tests/unit/test_control.py`, `tests/unit/test_workflows.py`.
- **Approach:**
  1. Rote Tests zuerst für das neue Verhalten: beim Ergebnisleser und beim Sollwert-Speicher bauen zwei gleichzeitige erste Aufrufe eine Verbindung; beim Ergebnisleser schließt ein gescheiterter Ping den Klienten. Für den Sollwert-Speicher ist der Ping-Test eine Charakterisierung (von Anfang an grün, mit kurzer Gegenprobe).
  2. Die Basis in `zugang.py` aus dem Muster des Workflowlesers bauen und mit eigenen Tests in `tests/unit/test_zugang.py` absichern (Sperre, Wiederversuch nach der Frist, Riegel, Schließen).
  3. Workflowleser, Ergebnisleser und Sollwert-Speicher nacheinander auf die Basis umstellen; nach jedem Umstellen alle Tests grün. Die Eigenheiten aus R2 bleiben als Klassenattribute oder überschriebene Methoden.
- **Execution note:** Die Tests aus Schritt 1 müssen vor dem Umstellen rot sein; die bestehenden Tests der drei Klienten bleiben unverändert grün.
- **Patterns to follow:** `SwarmQWorkflowleser` in `src/swarmq_dashboard/workflows.py`; `_WartendesVerbinden` und `_KlientOhnePing` in `tests/unit/test_workflows.py`.
- **Test scenarios:**
  - Ergebnisleser: zwei gleichzeitige erste `lesen()` gegen ein wartendes `verbinden` ergeben genau einen Verbindungsaufbau.
  - Ergebnisleser: ein gescheiterter Ping schließt den eigenen Klienten genau einmal (rot vor dem Umstellen).
  - Sollwert-Speicher: ein gescheiterter Ping schließt den eigenen Klienten genau einmal (Charakterisierung).
  - Sollwert-Speicher: `close()` lässt einen eingesetzten `store=` offen, und der nächste Aufruf nutzt ihn weiter.
  - Sollwert-Speicher: ein Fehler mitten in `schreiben` sperrt nicht und wird weiter an `Steuerung` gereicht.
  - Jeder Klient nennt nach einem gescheiterten Aufbau seinen bisherigen Sperrtext.
  - Ergebnisleser: `close()` verträgt eine App mit synchronem und asynchronem `close`.
  - Integration: die bestehenden „gibt schnell auf“-Tests in `tests/integration/test_ergebnis_lesen.py`, `test_workflow_zustand.py` und `test_control_roundtrip.py` bleiben grün.
- **Verification:** Die drei Klassen enthalten keinen eigenen Verbindungs- oder Sperrcode mehr außer ihren Eigenheiten, und alle Unit- und Integrationstests sind grün.

### U4. Zwischenspeicher für beendete Ergebnisse

- **Goal:** Ein beendetes Ergebnis kostet höchstens eine Backend-Lesung, egal wie oft eine Seite neu rendert.
- **Requirements:** R3; KTD3.
- **Dependencies:** U3.
- **Files:** `src/swarmq_dashboard/web/ergebnis.py`, `tests/unit/web/test_ergebnis.py`, `tests/unit/web/test_workflows_routes.py`.
- **Approach:** Im Ergebnisleser vor dem Backend-Aufruf im Speicher nachsehen und nach einer Lesung mit `zustand == "da"` ablegen; die Größe als Konstante neben `SPERRE_S`.
- **Execution note:** Test zuerst: zwei Lesungen derselben fertigen Kennung ergeben einen Backend-Aufruf.
- **Patterns to follow:** die App-Attrappe in `tests/unit/web/test_ergebnis.py`. Der Route-Test übergibt `create_app` einen echten `SwarmQErgebnisleser(app=<Attrappe>)`, dessen Attrappe die `get_result`-Aufrufe zählt; `_Zaehlleser` ersetzt den ganzen Leser und sähe den Zwischenspeicher nicht.
- **Test scenarios:**
  - Zweimal dieselbe fertige Kennung lesen ergibt einen Backend-Aufruf und zweimal dasselbe Bild.
  - Storniert wird ebenso gespeichert; gescheitert wird bei jeder Anfrage neu gelesen.
  - Ein fertiges Ergebnis, das nach dem ersten Lesen in der Attrappe verschwindet, wird weiter als fertig gezeigt (die benannte Ausnahme).
  - `fehlt`, `nicht_lesbar` und `nicht_erreichbar` werden bei jeder Anfrage neu gelesen.
  - Über die Obergrenze hinaus fällt der älteste Eintrag heraus und wird bei Bedarf neu gelesen.
  - Die Detailseite eines Chords mit beendeten Kopf-Gliedern liest beim zweiten Abruf nichts mehr aus dem Backend.
- **Verification:** Der Route-Test zählt beim zweiten Abruf keine weiteren `get_result`-Aufrufe für fertige oder stornierte Glieder, und die bestehenden Ergebnis-Tests sind grün.

### U5. `Services` in ein eigenes Modul

- **Goal:** `web/app.py` enthält nur noch App-Bau und Verdrahtung, `Services` lebt in `web/services.py`.
- **Requirements:** R4; KTD4.
- **Dependencies:** U3, U4.
- **Files:** `src/swarmq_dashboard/web/services.py` (neu), `src/swarmq_dashboard/web/app.py`, `tests/unit/web/test_control_routes.py`, `tests/unit/web/test_worker_routes.py`.
- **Approach:**
  1. `Services` samt den nur von ihm genutzten Helfern und Konstanten nach `web/services.py` verschieben; `web/app.py` reexportiert die bisherigen Namen.
  2. Die zwei Patch-Ziele für `kontrollkanal_grund` auf `swarmq_dashboard.web.services` umstellen.
  3. Die Docstrings, die `Services.shutdown()` nennen, stimmen weiter; nur Modulangaben anpassen, falls sie eine nennen.
- **Execution note:** Refactoring: vorher und nachher alle Tests grün; eine rote Zusicherung ist ein Stoppgrund (Goal Capsule), ein rotes Patch-Ziel nicht.
- **Patterns to follow:** die Modulaufteilung von `web/routes/` und `web/ergebnis.py`.
- **Test scenarios:**
  - Test expectation: none -- reiner Umzug; die bestehenden Tests in `tests/unit/web/test_service.py`, `test_live.py`, `test_naht.py` und `tests/integration/test_service_start.py` beweisen, dass Verhalten und Importfläche gleich bleiben.
- **Verification:** `web/app.py` hat unter 450 Zeilen, `from swarmq_dashboard.web.app import Services, PROBE_TIMEOUT_S` funktioniert weiter, und die Suite ist grün.

---

## Verification Contract

| Prüfung | Kommando | Gilt für |
|---|---|---|
| Einheiten | `uv run pytest tests/unit -q` | jede Einheit |
| Integration | `SQD_TEST_RABBITMQ_PORT=5699 SQD_TEST_VALKEY_PORT=6399 uv run pytest tests/integration -q` | U3, U4, U5 |
| Stil | `uv run ruff check src/ tests/` | jede Einheit |
| Typen | `uv run mypy --strict src/` | jede Einheit |

Integrationstests nur gegen die Testcontainer auf 5699 und 6399.

---

## Definition of Done

- U1 bis U5 sind umgesetzt; jede Verhaltensänderung ist durch einen vorher roten Test begründet, jeder Umbau lief mit durchgehend grüner Suite.
- Alle vier Prüfungen oben sind grün.
- Keine Änderung an swarmq, keine sichtbare Verhaltensänderung außer den zwei in R3 benannten.
- Die Arbeit liegt auf einem eigenen Branch, der von `feat/workflow-ansicht` abzweigt, mit eigenem MR (KD1).
- Code aus verworfenen Ansätzen ist entfernt.
