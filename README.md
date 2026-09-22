# swarmv — Dashboard für swarmq

Flower-artiges Web-Dashboard für [swarmq](https://gitlab.com/Schorsch1984/swarmq):
Worker-Übersicht, Task-Verzeichnis, Dead-Letter-Bedienung und Steuerung der
Worker-Gruppen über den Kontrollkanal. Eigenes Paket, eigener Prozess, eigene
Veröffentlichung; die Bibliothek bekommt dadurch keine neue Pflichtabhängigkeit.

## Stand (2026-09-22)

- Plan: `docs/plans/2026-09-16-1558-feat-dashboard-plan.md` — Anforderungen
  (R1–R33), Entscheidungen (KD1–KD16, KTD1–KTD14) und 19 Implementierungs-
  einheiten in fünf Stufen, umsetzungsreif.
- Umgesetzt: **Stufe A bis E**, also alle Einheiten. Stufe A (Ereignisvertrag,
  Puffer, Strom, beide Hooks), Stufe B (Verzeichnis, Strom-Leser, Suche und
  Filter, Broker-Beobachtung), Stufe C (Konten, Sitzung, Audit), Stufe D
  (Oberfläche: Worker, Tasks, Dead-Letter, Live-Schicht), U18 (`serve`) und
  **U16** (Sollwert-Steuerung über den Kontrollkanal).
- Stack: `docs/reference/stack-entscheidung.md` — Litestar, msgspec, Granian,
  Jinja2 + HTMX, SQLite mit FTS5-Trigrammen, argon2id.
- Oberfläche: `docs/reference/oberflaeche.md` und `docs/mockup/` (freigegeben).
- **Betrieb: `docs/reference/betrieb.md`** — Datenverzeichnis, Rechte, was
  gelöscht werden darf, Konten anlegen.
- Voraussetzung im Kern: der Kontrollkanal, erschienen in **swarmq 0.7.0**
  (MR !60, `docs/plans/2026-09-16-1753-feat-kontrollkanal-plan.md`);
  Schnittstelle in `docs/reference/kontrollkanal-schnittstelle.md`. Die
  Bibliothek liegt auf PyPI; die Abhängigkeit löst sich als gewöhnliche
  Versionsspanne auf. Fehlt der Kanal, bleibt die Steuerung abgeschaltet und
  begründet — jede andere Seite trägt weiter.
- Ankerpunkte in swarmq (Signale, Middleware-Haken, Cancel, DLQ, Schlüssel,
  Konventionen): `docs/reference/swarmq-ankerpunkte.md`.
- Testmethodik gegen abtastende Prozesse: `docs/reference/scheduler-cron-sync-clobber-test-flake-2026-06-01.md`.
- Arbeitsregeln (TDD, Gates): `AGENTS.md`, aus swarmq übernommen.

## Getroffene Grundentscheidungen (aus dem Plan)

Ereignis-Spiegel in Valkey als Datenquelle, gespeist von zwei mitgelieferten
Hooks (Worker-Hook per `--configure`, Anwendungs-Hook als Middleware);
lokale Konten mit Passwort; sieben Tage Aufbewahrung; Grundform „Seiten wie
Flower“ (Worker, Tasks, DLQ); Steuerumfang nur Autoscale-Grenzen und
Prozesszahl; Abhängigkeit von `swarmq` als Versionsspanne.

## Lokale Dienste

RabbitMQ `amqp://guest:guest@localhost:5699/`, Valkey `valkey://localhost:6399/0`
(Container `swarmq-test-rabbit`, `swarmq-test-valkey`). Ein Worker mit
Kontrollkanal zum Ausprobieren steht in `docs/reference/kontrollkanal-schnittstelle.md`.

## Entwickeln

```bash
uv sync                                  # Abhaengigkeiten, Python 3.14
uv run ruff check src/ tests/            # Stil
uv run mypy src/swarmq_dashboard         # Typen
uv run pytest tests/unit -q              # Einheiten
uv run pytest tests/integration -q       # braucht die Testdienste unten
```

Die Integrationsstufe ueberspringt sich selbst, wenn Broker oder Valkey
fehlen. Der Ereignisstrom liegt auf Valkey-Datenbankindex **1**, nicht 0 —
die Integrationsvorrichtung von swarmq fuehrt auf Index 0 bei jedem Testlauf
ein `FLUSHDB` aus.

## Stack (festgelegt 2026-09-17)

Litestar + msgspec, Granian als Server (ein Arbeiter), Jinja2 mit HTMX 2 und
Idiomorph ohne Build-Schritt, SSE über `litestar.channels` für die
Live-Aktualisierung, SQLite mit FTS5 als durchsuchbares Task-Verzeichnis,
Sitzungen im Valkey-Store, argon2id für Passwörter. Begründung, Messwerte und
verworfene Alternativen: `docs/reference/stack-entscheidung.md`.

## Oberfläche (freigegeben 2026-09-17)

Mockup zum Öffnen: `docs/mockup/index.html` — sechs Seiten, verlinkte
Navigation, kein Server nötig. Verbindlich für den Bau; die Regeln dahinter
stehen in `docs/reference/oberflaeche.md`, das fertige Stylesheet in
`docs/reference/oberflaeche.css` (wandert beim Bau nach
`src/swarmq_dashboard/static/app.css`).

Leitsatz: **Farbe trägt Bedeutung, nie Marke.** Gehäuse grafit und
papierfarben, Farbe nur als Zustand. IBM Plex Sans und Mono, selbst gehostet.

## Worker anschließen

```bash
export SQD_WORKER_GROUP=orders-eu          # muss zu --group passen
swarmq worker --module app.tasks \
  --configure swarmq_dashboard.hooks.worker:setup \
  --queues orders --max-processes 16 --group orders-eu
```

`--max-processes` schaltet den Kontrollkanal ein; ohne das Argument meldet der
Supervisor nichts und ein Sollwert erreicht ihn nicht. `SQD_WORKER_GROUP` und
`--group` müssen dasselbe sagen — der Hook läuft im Kindprozess und sieht das
Argument nicht. Stimmen sie nicht überein, führt das Dashboard dieselbe Gruppe
unter zwei Namen und findet nie einen Supervisor. Einzelheiten:
`docs/reference/betrieb.md`.

## Offen

Die Ansicht des Audit-Stroms im Browser ist nach R27 ausdrücklich Stufe 2 und
bleibt vorerst bei der Kommandozeile.
