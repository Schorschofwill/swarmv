# swarmv — Dashboard für swarmq

Flower-artiges Web-Dashboard für [swarmq](https://gitlab.com/Schorsch1984/swarmq):
Worker-Übersicht, Task-Verzeichnis, Dead-Letter-Bedienung und Steuerung der
Worker-Gruppen über den Kontrollkanal. Eigenes Paket, eigener Prozess, eigene
Veröffentlichung; die Bibliothek bekommt dadurch keine neue Pflichtabhängigkeit.

## Stand (2026-09-17)

- Anforderungen: `docs/plans/2026-09-16-1558-feat-dashboard-plan.md`
  (ce-brainstorm, von sieben Reviewern geprüft; R1–R30, KD1–KD13).
  Ein Implementierungsplan fehlt noch: nächster Schritt ist `ce-plan` auf dieser Datei.
- Voraussetzung im Kern: der Kontrollkanal ist umgesetzt, MR !60 in swarmq
  (`docs/plans/2026-09-16-1753-feat-kontrollkanal-plan.md`); Schnittstelle in
  `docs/reference/kontrollkanal-schnittstelle.md`.
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

## Stack (festgelegt 2026-09-17)

Litestar + msgspec, Granian als Server (ein Arbeiter), Jinja2 mit HTMX 2 und
Idiomorph ohne Build-Schritt, SSE über `litestar.channels` für die
Live-Aktualisierung, SQLite mit FTS5 als durchsuchbares Task-Verzeichnis,
Sitzungen im Valkey-Store, argon2id für Passwörter. Begründung, Messwerte und
verworfene Alternativen: `docs/reference/stack-entscheidung.md`.

## Offen vor dem ersten Code

Speicherbudget des Stroms, DLQ-Lesen ohne Drain, Anbindung des Anwendungs-Hooks
bei Scheduler- und Workflow-Einreihungen, Fristen für „getrennt“ und
„unbestätigt“, Versionsspanne zu swarmq — im Plan unter „Deferred to Planning“.
