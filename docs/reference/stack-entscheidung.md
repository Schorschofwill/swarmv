# Stack des Dashboards (Stand 2026-09-17)

Festgelegt vor dem ersten Code. Schließt sechs Punkte, die der Dashboard-Plan
unter „Deferred to Planning" offen gelassen hat: Web-Framework, Frontend-Stack,
Technik der Live-Aktualisierung, Form des durchsuchbaren Verzeichnisses,
Volltext über Argumente, und den Ort der Sitzungen.

Maßstab war: schlank, modern, gut zu bedienen — und R25 („nur das Nötige").
Alle Zahlen unten sind gemessen, nicht geschätzt.

## Die Wahl

| Schicht | Wahl | Deckt ab |
|---|---|---|
| HTTP-Framework | **Litestar 2.24** | R23, R24 |
| Serialisierung | **msgspec** (bringt swarmq mit) | — |
| Server | **Granian 2.8**, ASGI, ein Arbeiter | — |
| Templates | **Jinja2** (`JinjaTemplateEngine`) | R24 |
| Interaktion | **HTMX 2 + Idiomorph**, mitgeliefert | R24, R26 |
| Live-Aktualisierung | **SSE** über `ServerSentEvent` + `litestar.channels` | **R13** |
| CSS | handgeschrieben, ~400 Zeilen, modernes CSS | R24, R26 |
| Task-Verzeichnis | **SQLite + FTS5**, Stdlib `sqlite3`, WAL | R5–R11 |
| Sitzungen | **`litestar.stores.valkey.ValkeyStore`** | R21 |
| Anmeldung | `litestar.security.session_auth.SessionAuth` | R20, R23 |
| CSRF | `CSRFConfig(cookie_samesite="strict")` | R23 |
| Anmeldebremse | `litestar.middleware.rate_limit` | R22 |
| Passwörter | **argon2-cffi** (argon2id) | R21 |
| Konto-CLI | **argparse** (wie der swarmq-Kern) | R20 |
| Broker/DLQ | `RabbitMQBroker` + `BrokerAdmin` aus swarmq | R18, R28–R30 |

Python ≥ 3.14 wie der Kern. Alle Binärpakete (`granian`, `msgspec`,
`argon2-cffi-bindings`, `cffi`, `markupsafe`) haben cp314-Wheels geprüft —
kein Compiler beim Install.

## Warum Litestar und nicht Starlette

Die Paketzahl spricht klar gegen Litestar: **22 Pakete gegen 4**, und
`faker`, `polyfactory`, `rich`, `rich-click`, `pygments` und `httpx` stehen in
seiner Abhängigkeitsliste, ohne dass sie zur Laufzeit importiert werden. Sie
lassen sich nicht wegdeklarieren; wer `swarmq-dashboard` installiert, bekommt
sie. (Von Hand entfernt bootet die App weiter — nur `pyyaml` ist ein harter
Import über `app.py` → `_openapi`.)

Dagegen steht, was Litestar an Eigencode abnimmt, und das ist genau die
Fläche, auf der Sicherheitsfehler wohnen:

- `stores/valkey.py` — Sitzungen in **Valkey**, nicht Redis. Die TTL *ist* der
  Inaktivitätsablauf aus R21, Abmelden *ist* ein `DEL`.
- `channels` mit `MemoryChannelsBackend` — der Fanout eines Valkey-Stroms auf
  N offene Browser (R13). Warteschlange je Verbindung, Backpressure,
  Abo-Lebenszyklus, Verhalten bei langsamen Lesern: das ist der größte Posten
  und der, den man am ungernsten selbst debuggt, während ein Dashboard hängt.
- `security.session_auth` — inklusive Absicherung der SSE-Route, die R23
  ausdrücklich verlangt.
- `CSRFConfig` — die zweite Hälfte von R23.
- `litestar-htmx` — HTMX-Integration, schon als Abhängigkeit dabei.

Erste Schätzung für den Eigenbau auf Starlette waren 150 Zeilen. Das war zu
optimistisch: Sitzung und CSRF treffen ungefähr zu, der Channels-Fanout allein
ist mehr als das. Die Entscheidung fiel gegen die Paketzahl und für weniger
sicherheitskritischen Eigencode.

FastAPI wäre 10 Pakete, brächte aber Pydantic als zweite Serialisierungswelt
neben dem msgspec, das swarmq ohnehin lädt.

## Warum Granian und nicht uvicorn

Gemessen gegen dieselbe Litestar-App, 32 Keep-Alive-Verbindungen, 5 s:

| Variante | Pakete | Größe | Anfragen/s |
|---|---|---|---|
| uvicorn pur | 3 | 1,2 MB | 10.400 |
| uvicorn + uvloop + httptools | 5 | 19 MB | 26.100 |
| **Granian** | 2 | 21 MB | 51.300 |

Funktional sind sie gleichwertig: beide streamen SSE sauber, beide hängen beim
Herunterfahren mit offenem Stream unendlich, beide beenden nach exakt 3 s mit
gesetzter Frist.

Die Größe ist der Preis (21 MB gegen 1,2 MB), und die Leistung kauft für ein
Ops-Dashboard mit einer Handvoll Tabs nichts — 10.400 Anfragen/s wären drei
Größenordnungen über dem Bedarf. Entschieden wurde trotzdem für Granian:
Betreiberwunsch, und die Alternative „uvicorn mit Beschleunigern" ist mit
19 MB bei halber Leistung ohnehin das Schlechteste aus beidem. Beide sprechen
ASGI — der Anwendungscode ist identisch, der Wechsel wäre ein Wort.

Dreingabe: Granian bringt Prozessaufsicht mit (`--respawn-failed-workers`,
`--workers-max-rss`), die sonst systemd erledigen müsste.

## Startkommando

```bash
granian --interface asgi --host 0.0.0.0 --port 8080 \
        --workers 1 \
        --workers-kill-timeout 10 \
        --respawn-failed-workers \
        --workers-max-rss 512 \
        --no-access-log \
        swarmq_dashboard.asgi:app
```

| Flag | Warum |
|---|---|
| `--interface asgi` | **nicht** `asginl` — Litestar braucht Lifespan für Valkey-Verbindung, SQLite und den Channels-Plugin. |
| `--workers 1` | Pflicht, nicht Sparsamkeit: `MemoryChannelsBackend` lebt im Prozess. Mehrere Arbeiter bräuchten das Redis-Channels-Backend gegen Valkey. Deckt sich mit „ein Dashboard je Deployment". |
| `--workers-kill-timeout 10` | Ohne Frist hängt das Herunterfahren an jeder offenen SSE-Verbindung unendlich lange (gemessen). |
| `--respawn-failed-workers` | Neustart nach Absturz. Offene SSE-Verbindungen brechen dabei ab; `EventSource` im Browser verbindet von selbst neu. |
| `--workers-max-rss 512` | Langläufer-Absicherung. Gleiche Folge wie oben: Browser verbinden neu. |

Entwicklung: `--reload` (braucht `granian[reload]`), `--log-level debug`,
`--access-log`.

`--http` bleibt auf `auto`. HTTP/2 würde das Browserlimit von sechs
Verbindungen je Domain aufheben, das eine SSE-Verbindung mitbelegt — praktisch
braucht das TLS, und hinter einem TLS-terminierenden Reverse-Proxy spricht der
Proxy ohnehin HTTP/1.1 mit Granian.

## Verzeichnis: SQLite statt Valkey

Der Dienst schreibt den Valkey-Strom in eine lokale SQLite-Datei. Filter nach
Status, Name, Worker und Zeitraum (R10) sowie Volltext über die Argumente
(R7, R10) sind je eine Abfrage; Aufbewahrung (R5) ist ein `DELETE`. FTS5 steckt
in Pythons `sqlite3` — **null zusätzliche Pakete**. WAL-Modus, ein
Schreiber-Task mit eigener Verbindung, Lesen über `asyncio.to_thread`.

Geht die Datei verloren, baut der Dienst sie aus dem Strom neu auf.

Verworfen: **nur Valkey** klingt schlanker, ist es nicht — Filter bräuchten
selbstgepflegte Index-Mengen, und Volltext über Argumente gibt es in Valkey
OSS nicht, das würde ein Scan über sieben Tage Tasks. **PostgreSQL** wäre eine
dritte Infrastruktur allein fürs Dashboard.

## Frontend: kein Build

Server rendert HTML mit Jinja2, HTMX tauscht Fragmente, Idiomorph morpht sie
ein, damit Scrollposition, Textmarkierung und offene Dialoge in Tabellen nicht
verspringen. SSE schickt die Fragmente. Beide Bibliotheken liegen als Dateien
im Paket — **kein CDN** (geschlossenes Netz), kein Node, kein Bundler, keine
gebauten Assets im Wheel.

CSS von Hand, mit `@layer`, Nesting, `light-dark()`, `color-mix()` und
Container Queries. Dark Mode fällt dabei ab.

Verworfen: **React/Vue-SPA** (Node-Toolchain im Repo, zwei Test-Suiten, steht
gegen R25). **Alpine.js zusätzlich** (Tabellenzustand läge doppelt vor; die
paar Dialoge sind zweistellig viele Zeilen Vanilla-JS). **Datastar** war die
Versuchung — eine 15-kB-Bibliothek, die Reaktivität und SSE-Patching zugleich
abdeckt und für ein Live-Dashboard konzeptionell besser sitzt als HTMX plus
SSE-Erweiterung; gelassen wegen dünnem Ökosystem. Ein späterer Tausch beträfe
nur die Templates.

## Was offen bleibt

Diese Entscheidung berührt sie nicht; sie gehören in den Implementierungsplan:

- Form des Speicherbudgets für den Strom (R9) — Trimmung nach Alter, Zahl oder
  Bytes, und wie der Füllstand angezeigt wird.
- DLQ-Lesen ohne Reihenfolgeänderung (R28) — heute drainiert `dlq list` und
  reiht neu ein.
- Anbindung des Anwendungs-Hooks bei Scheduler- und Workflow-Einreihungen.
- Fristen für „getrennt" (R19) und „unbestätigt" (R17) samt Voreinstellungen.
- Versionsspanne zu `swarmq` und Abstimmung bei Änderungen an den Nutzlasten.

## Testpflicht, die aus dem Stack folgt

Der SSE-Generator **muss** auf Disconnect und Shutdown hören. Gemessen: ohne
das hängt jedes Herunterfahren an jeder offenen Verbindung, bei Granian wie bei
uvicorn — die Server-Frist ist das Netz darunter, nicht die Lösung. Das gehört
nach `AGENTS.md` als eigener fehlschlagender Test vor die Implementierung.
