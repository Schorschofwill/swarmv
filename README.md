# swarmv — Dashboard für swarmq

Flower-artiges Web-Dashboard für [swarmq](https://github.com/Schorschofwill/swarmq):
Worker-Übersicht, Task-Verzeichnis, Workflow-Ansicht, Dead-Letter-Bedienung
und Steuerung der Worker-Gruppen über den Kontrollkanal. Eigenes Paket, eigener Prozess, eigene
Veröffentlichung; die Bibliothek bekommt dadurch keine neue Pflichtabhängigkeit.

## Stand (2026-09-24)

- Plan: `docs/plans/2026-09-16-1558-feat-dashboard-plan.md` — Anforderungen
  (R1–R33), Entscheidungen (KD1–KD16, KTD1–KTD14) und 19 Implementierungs-
  einheiten in fünf Stufen, umsetzungsreif.
- Umgesetzt: **Stufe A bis E**, also alle Einheiten. Stufe A (Ereignisvertrag,
  Puffer, Strom, beide Hooks), Stufe B (Verzeichnis, Strom-Leser, Suche und
  Filter, Broker-Beobachtung), Stufe C (Konten, Sitzung, Audit), Stufe D
  (Oberfläche: Worker, Tasks, Dead-Letter, Live-Schicht), U18 (`serve`) und
  **U16** (Sollwert-Steuerung über den Kontrollkanal).
- Seit 0.1.0 dazugekommen: ein **Container-Abbild** (`Dockerfile`), das
  **Absenden einer Probe** über die Oberfläche — mit allen Parametern, die
  das System für Tasks trägt, für `io` wie für `cpu` — und die
  **Ergebnisanzeige** auf der Detailseite
  (`docs/plans/2026-09-23-1050-feat-ergebnisanzeige-plan.md`).
- **Workflows** (`docs/plans/2026-09-28-1003-feat-workflow-ansicht-plan.md`):
  ein eigener Bereich listet Ketten, Gruppen und Chords mit Status und
  Fortschritt; die Detailseite zeigt die Topologie als Baum — worauf ein Chord
  wartet, welches Glied warum scheiterte, was in den Body ging — und
  storniert den ganzen Workflow nach Bestätigung. Den Zustand liest der
  Dienst selbst aus dem Backend und hält eine Kopie, damit Baum und Endstatus
  die sieben Tage überdauern; ein Endstatus steht nur da, wenn der Zustand
  ihn trägt. Takt, Anzeigezustände und der Bezug zur `result_ttl`:
  `docs/reference/betrieb.md`.
- Stack: `docs/reference/stack-entscheidung.md` — Litestar, msgspec, Granian,
  Jinja2 + HTMX, SQLite mit FTS5-Trigrammen, argon2id.
- Oberfläche: `docs/reference/oberflaeche.md` und `docs/mockup/` (freigegeben).
- **Betrieb: `docs/reference/betrieb.md`** — Datenverzeichnis, Rechte, was
  gelöscht werden darf, Konten anlegen, wo die Ergebnisse liegen, und wie
  die Workflow-Ansicht ihren Zustand liest.
- Die Task-Detailseite zeigt neben Argumenten und Signalfolge auch das
  **Ergebnis**. Es wird bei jedem Aufruf im Backend der Bibliothek
  nachgeschlagen und nirgends gespeichert: Es hat eine eigene Verfallsfrist
  (per Vorgabe 24 Stunden, siehe `betrieb.md`), die das Verzeichnis mit seinen
  sieben Tagen nicht einhalten könnte.
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

## Installieren

```bash
pip install swarmq-dashboard        # oder: uv add swarmq-dashboard
```

Python 3.14 oder neuer. Die Bibliothek kommt als `swarmq>=0.7,<0.10` mit: Die
Untergrenze ist der Kontrollkanal, ohne den es keine Sollwert-Steuerung gibt;
die Obergrenze hält eine Änderung der Signal-Nutzlasten auf, bevor sie den
Ereignisspiegel still beschädigt. Geprüft gegen 0.7.0, 0.8.0 und 0.9.0
(2026-10-08): Signale, Nutzlasten, Middleware-Haken, Backend-Schlüssel und
Kontrollkanal sind in 0.8 und 0.9 unverändert; was sich dort geändert hat,
betrifft das Logging und die Zustellung im Worker (`docs/reference/swarmq-ankerpunkte.md`).

### Die zwei Hooks

Der Dienst liest keine Datenbank der Bibliothek, sondern einen eigenen
Ereignisstrom. Gefüllt wird der von zwei Hooks, und beide sind eine Zeile:

```bash
# Im Worker -- Ausführung, Ergebnisse, Lebenszeichen, Stammdaten:
export SQD_WORKER_GROUP=orders-eu          # muss zu --group passen
swarmq worker --module app.tasks \
  --configure swarmq_dashboard.hooks.worker:setup \
  --queues orders --max-processes 16 --group orders-eu
```

```python
# In der einreihenden Anwendung -- Tasks ab der Einreihung, mit Argumenten,
# Einreihungszeitpunkt und als wartend stornierbar:
from swarmq_dashboard.hooks.app import install
install(app)
```

Ohne den Anwendungs-Hook kennt das Dashboard einen Task erst, wenn ein Worker
ihn aufgreift — dann ohne Einreihungszeitpunkt. Scheduler-Auslösungen und
verzögerte Einreihungen gehen auch am Anwendungs-Hook vorbei; sie
veröffentlichen direkt am Broker.

### Im Container

```bash
docker build -t swarmq-dashboard:0.1.0 .

# Konto anlegen (das Volume behält es)
docker run --rm -it -v sqd-data:/var/lib/sqd swarmq-dashboard:0.1.0 account create ops

# Fahren -- hinter einem vorgelagerten TLS-Abschluss
docker run -d --name sqd -p 8080:8080 \
  -e SWARMQ_BROKER_URL="amqp://user:pass@rabbit:5672/" \
  -e SWARMQ_BACKEND_URL="valkey://valkey:6379/0" \
  -v sqd-data:/var/lib/sqd \
  swarmq-dashboard:0.1.0
```

Das Abbild lauscht auf `0.0.0.0:8080` **ohne eigenes TLS** und setzt dafür
`--behind-tls-proxy`: Im Container ist der vorgelagerte Abschluss der
Normalfall, und auf `127.0.0.1` zu lauschen erreichte kein Portmapping. Die
Sitzungscookies behalten dabei ihr `Secure` — der Browser spricht https mit
dem Proxy, nur die Strecke dahinter ist Klartext, und die abzusichern ist
Sache des Betreibers.

**Direkt per `http://` im Browser kommt man deshalb nicht hinein**, und das
ist Absicht: Der Browser schickt ein `Secure`-Cookie über Klartext nicht
mit. Wer ohne Proxy ausprobieren will, nimmt eigene Zertifikate:

```bash
docker run -d -p 8443:8443 -v /etc/ssl/sqd:/certs:ro ... swarmq-dashboard:0.1.0 \
  serve --host 0.0.0.0 --port 8443 --tls-cert /certs/cert.pem --tls-key /certs/key.pem
```

Das Datenverzeichnis liegt auf `/var/lib/sqd` und gehört einem eigenen
Benutzer. Es gehört in ein Volume: die Kontendatei steht in keinem Strom und
käme nach einem `docker rm` nicht wieder.

### Den Dienst starten

```bash
export SWARMQ_BROKER_URL="amqp://user:pass@rabbit:5672/"
export SWARMQ_BACKEND_URL="valkey://valkey:6379/0"
swarmq-dashboard --data-dir /var/lib/sqd account create ops
swarmq-dashboard --data-dir /var/lib/sqd serve --tls-cert cert.pem --tls-key key.pem
```

`--data-dir` gilt für alle Unterbefehle; ohne das Argument greift
`SQD_DATA_DIR`, sonst `./sqd-data`. Ohne TLS startet der Dienst nicht — die
Sitzungscookies tragen `Secure`, und niemand käme sonst hinein; für die
Entwicklung auf 127.0.0.1 gibt es `--insecure-http`.

**Der Ereignisstrom liegt auf Valkey-Datenbankindex 1**, nicht 0. Index 0
gehört der Bibliothek, und ihre Integrationstests führen dort ein `FLUSHDB`
aus. Ein eigener Index lässt sich mit `SQD_STREAM_URL` setzen; ohne die
Variable wird er aus `SWARMQ_BACKEND_URL` abgeleitet.

**Workflows liest der Dienst im Takt von `SQD_WORKFLOW_POLL_S`** (Vorgabe
60 Sekunden) aus dem Backend, dazu gleich nach jedem Ergebnis eines Schritts.
Der Takt muss weit unter der kleinsten `result_ttl` der Worker liegen: ein
Zustand, der zwischen zwei Lesungen verfällt, erscheint als „Zustand
verfallen" statt mit seinem Endstatus.

Alle Einstellungen des Dienstes auf einen Blick:

| Variable | Vorgabe | Wirkung |
|---|---|---|
| `SWARMQ_BROKER_URL` | Pflicht | RabbitMQ der Bibliothek |
| `SWARMQ_BACKEND_URL` | Pflicht | Valkey der Bibliothek (Index 0) |
| `SQD_STREAM_URL` | aus `SWARMQ_BACKEND_URL`, Index 1 | Ereignisstrom des Dashboards; auch die Hooks lesen ihn |
| `SQD_DATA_DIR` | `./sqd-data` | Verzeichnis- und Kontendatei; `--data-dir` geht vor |
| `SQD_RETENTION_DAYS` | `7` | Aufbewahrung von Verzeichnis, Strom und Workflow-Kopie |
| `SQD_SESSION_IDLE_S` | `1800` | Inaktivitätsfrist einer Sitzung |
| `SQD_DISCONNECTED_AFTER_S` | `40` | ab wann ein Worker als getrennt gilt |
| `SQD_UNCONFIRMED_AFTER_S` | `15` | ab wann ein gesendeter Sollwert als unbestätigt gilt |
| `SQD_STREAM_STALE_AFTER_S` | `120` | ab wann der Ereignisstrom als verzögert gilt |
| `SQD_BACKLOG_CAP` | `300` | Rückstau je Live-Verbindung |
| `SQD_WORKFLOW_POLL_S` | `60` | Takt des Workflow-Abgleichs in Sekunden |
| `SQD_WORKER_GROUP` | Queue-Menge | nur im Worker: Gruppe des Hooks, muss zu `--group` passen |

Was wohin gehört, welche Datei gelöscht werden darf und welche nicht:
`docs/reference/betrieb.md`.

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
