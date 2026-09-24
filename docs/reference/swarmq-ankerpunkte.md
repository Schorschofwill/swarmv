# Ankerpunkte in swarmq für das Dashboard (Stand 0.6.2 + MR !60, 2026-09-17)

Repo: `/home/georg/Dokumente/Development-Privat/swarmq` (GitLab `schorsch-dev/swarmq`).
Das Dashboard nutzt **nur öffentliche Schnittstellen** (Dashboard-Plan R1a);
fehlt etwas, wird es mit Georg abgestimmt und als eigener MR am Kern geplant.

## Öffentliche API (`swarmq/__init__.py`, `__all__`)

`SwarmQ`, `Task`, `TaskInfo`, `Signal`, `SignalDispatcher`, `Middleware`,
`MessageContext`, `RabbitMQBroker`, `ValkeyBackend`, Fehlerklassen, Workflow-Primitive.

## Signale (Worker-Hook, R3) — `src/swarmq/signals.py`

Task: `task_enqueued`, `task_started`, `task_completed`, `task_failed`,
`task_retrying`, `task_cancelled`, `task_expired`, `task_locked`,
`task_rate_limited`, `task_duplicate_detected`, `task_dlq`, `task_rejected`,
`workflow_step_enqueued`.
Worker: `worker_online`, `worker_shutdown`, `worker_heartbeat`, `worker_recycling`.
Scheduler: `scheduler_leader`, `scheduler_standby`, `scheduler_heartbeat`,
`scheduler_leader_handover`, `scheduler_leader_failover`, `scheduler_fire_skipped`.

Registrierung: `app.on_signal(Signal.X)` (`src/swarmq/app.py`). Der Worker-Hook
wird per `swarmq worker --configure paket.modul:setup` geladen und läuft in
jedem Worker-Prozess nach dem Bau der App (`src/swarmq/cli/worker_cmd.py`).
Achtung: `worker_heartbeat` feuert nur mit konfiguriertem Metrik-Port
(`src/swarmq/worker/lifecycle.py`).

## Anwendungs-Hook (R3a) — `src/swarmq/middleware.py`

`Middleware.post_enqueue(message: MessageContext)` läuft im Client nach
erfolgreichem Publish. `MessageContext` trägt `task_id`, `task_name`, `queue`,
`args`, `kwargs`, `delivery_mode`, optional `retry_count`, `workflow_id`,
`step_path`. Anmelden mit `app.add_middleware(...)`. Offen (Plan): ob Scheduler-
und Workflow-Einreihungen im Worker denselben Haken passieren.

## Aktionen

- Cancel (R12): `await app.cancel(task_id)` (`src/swarmq/app.py`), Flag
  `sq:cancel:{task_id}`.
- DLQ (R28–R30): heutige Wege `swarmq dlq list|inspect|retry|purge` in
  `src/swarmq/cli/dlq_cmd.py`; Broker-Admin `src/swarmq/broker/rabbitmq/admin.py`
  (`queue_depth`, `dead_letter_depth`, Vertrag `src/swarmq/contracts/broker.py`).
  `dlq list` drainiert und reiht neu ein — Reihenfolge-Frage im Plan offen.
- Sollwerte (R15–R17): siehe `kontrollkanal-schnittstelle.md`.

## Backend-Schlüssel (`src/swarmq/backend/valkey/results.py`)

`sq:result:{task_id}`, `sq:cancel:{task_id}`, `sq:progress:{task_id}`,
`sq:unique:{hash}`, `sq:wf:{wf_id}`, Kanäle `sq:notify:{task_id}`,
`sq:progress:channel:{task_id}`. Kontrollkanal: `sq:control:setpoint:*`,
`sq:control:status:*`. Ein eigener Ereignis- und Audit-Strom des Dashboards
sollte einen eigenen Präfix bekommen (Vorschlag: `sqd:`), damit Valkey-ACLs
greifen und die Flush-Fixtures von swarmq nichts Fremdes treffen.

## Worker-Identität (`src/swarmq/config.py`, `worker_identity`)

`<SWARMQ_WORKER_ID oder Hostname>:<slot>` unter dem Supervisor,
`…:p<pid>` eingebettet. Slots sind stabil über Neustarts (Slot-Tabelle
`src/swarmq/process_manager/slots.py`).

## Konventionen aus swarmq, die hier gelten sollen

- TDD nach `AGENTS.md` (kopiert), Tests < 30 s, kein `skip` ohne Ticket.
- Werkzeuge: `uv`, `ruff` (Zeilenlänge 120, `PLR0915`/`C901`), `mypy --strict`,
  `pytest` mit `asyncio_mode = auto` und `timeout = 30`.
- Doku zweisprachig (`*.md` und `*.de.md`), Changelog nach Keep a Changelog.
- Commits: `typ(bereich): Aussage` auf Deutsch, Branch je Vorhaben, MR nach `main`.
- Testdienste lokal: RabbitMQ `5699`, Valkey `6399` (Container `swarmq-test-*`);
  `localhost:6379` gehört einem anderen Projekt.

## Vorbilder

- `swarmq-loadsuite` (eigenes Repo neben der Bibliothek, `worker/relay.py`
  führt Signale per Handler nach außen).
- Flower: https://flower.readthedocs.io/en/latest/features.html
