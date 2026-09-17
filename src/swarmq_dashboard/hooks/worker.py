"""Worker-Hook: ``swarmq worker --configure swarmq_dashboard.hooks.worker:setup``.

Drei Aufnahmestellen in einem Ziel (KTD2):

* jedes Signal des Workers (R3),
* die Argumente beim Aufgreifen eines Tasks (R3b) -- nur so kennt das
  Verzeichnis die Argumente von Scheduler- und verzoegerten Einreihungen, die
  den Anwendungs-Hook nie passieren,
* die Stammdaten des Workers beim Hochfahren -- Queue-Menge und Gruppe stehen
  in keinem Signal, und ohne sie hat die Worker-Seite keine Quelle fuer R14 und
  R18.

**Dieses Ziel wirft nie.** swarmq huellt jede Ausnahme des ``--configure``-Ziels
in einen Konfigurationsfehler und beendet den Worker mit einem
Konfigurations-Exitcode, statt ihn neu zu starten. Ein vergessener
Umgebungswert wuerde die ganze Worker-Flotte am Hochfahren hindern -- aus einem
Problem des optionalen Beobachtungspakets ein Produktionsausfall. Ist etwas
unklar, bleibt der Hook untaetig und der Worker laeuft wie ohne ihn (R2).

**Dieses Ziel wartet nie.** Alles landet im Puffer aus ``buffer.py``; der
Netz-Zugriff passiert im Hintergrund (KTD1, R4).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from swarmq import Middleware, Signal

from swarmq_dashboard.buffer import EventBuffer
from swarmq_dashboard.config import HookConfig
from swarmq_dashboard.events import Event, Origin
from swarmq_dashboard.stream import ValkeyStreamClient

logger = logging.getLogger("swarmq_dashboard.hooks.worker")

#: Ereignisart der Worker-Stammdaten.
META_EVENT = "sqd_worker_meta"
#: Prioritaets-Endungen der Bibliothek. Sie legt ``{queue}.high`` und
#: ``{queue}.low`` als eigene AMQP-Queues an. ``resolve_priority_queue`` steht
#: nicht in ``swarmq.__all__``, deshalb hier abgeleitet statt importiert (R1a).
PRIORITY_SUFFIXES = ("high", "low")

DEFAULT_CAPACITY = 10_000


class _PickupMiddleware(Middleware):
    """Meldet Argumente in dem Moment, in dem ein Worker den Task aufgreift."""

    def __init__(self, buffer: EventBuffer, source: str) -> None:
        self._buffer = buffer
        self._source = source

    async def pre_execute(self, message: Any) -> None:
        # Laeuft IM Ausfuehrungspfad. Nur anhaengen, nie warten.
        self._buffer.record(
            Event(
                type="task_pickup",
                ts=time.time(),
                source=self._source,
                origin=Origin.PICKUP,
                payload=dict(message),
            )
        )


def _all_queues(queues: list[str]) -> list[str]:
    """Basis-Queues samt Prioritaetsvarianten.

    Die Broker-Beobachtung (U7) muss alle drei abfragen; eine Abfrage nur auf
    der Basis-Queue untertreibt die wartenden Nachrichten aus R14 und die
    Dead-Letter-Tiefe aus R28.
    """
    out: list[str] = []
    for q in queues:
        out.append(q)
        out.extend(f"{q}.{suffix}" for suffix in PRIORITY_SUFFIXES)
    return out


def _group_key(queues: list[str], env_group: str | None) -> str:
    """Gruppenschluessel wie ihn der Supervisor bildet.

    Mit Label das Label, sonst die sortierte, kommagetrennte Queue-Menge --
    dieselbe Regel wie im Kontrollkanal, damit beide Seiten denselben
    Schluessel meinen.
    """
    if env_group:
        return env_group
    return ",".join(sorted(set(queues)))


def setup(app: Any) -> None:
    """``--configure``-Ziel. Wirft unter keinen Umstaenden.

    Laeuft in jedem Worker-Kindprozess nach dem Bau der App und **vor** dem
    Verbinden von Broker und Backend -- hier darf deshalb keine Verbindung
    entstehen. Der Verbindungsaufbau haengt am Signal fuer "Worker ist oben".
    """
    try:
        _install(app)
    except Exception:
        # Letzte Bastion. Kommt es hierher, ist etwas grundsaetzlich anders als
        # erwartet -- der Worker laeuft trotzdem weiter, nur ohne Dashboard.
        logger.warning("dashboard_hook_setup_failed; Worker laeuft ohne Hook", exc_info=True)


def _install(app: Any) -> None:
    if getattr(app, "_sqd_worker_hook", False):
        # Zweimal registrieren hiesse jedes Signal doppelt melden, und das
        # Verzeichnis kann Dubletten nicht von echten Wiederholungen trennen.
        return

    buffer: EventBuffer | None = getattr(app, "hook_buffer", None)
    if buffer is None:
        hook_cfg = HookConfig.from_env()
        if not hook_cfg.usable:
            logger.warning("dashboard_hook_inaktiv reason=%s", hook_cfg.reason)
            app.hook_buffer = None
            app._sqd_worker_hook = True
            return
        buffer = EventBuffer(
            capacity=DEFAULT_CAPACITY,
            client=ValkeyStreamClient(hook_cfg.stream_url or ""),
        )
        app.hook_buffer = buffer

    source = str(app.config.worker_identity)
    app._sqd_worker_hook = True

    _register_signals(app, buffer, source)
    app.add_middleware(_PickupMiddleware(buffer, source))
    logger.info("dashboard_hook_aktiv source=%s", source)


def _register_signals(app: Any, buffer: EventBuffer, source: str) -> None:
    """Jedes Signal der Bibliothek spiegeln.

    Die Handler nehmen freie Schluesselwortargumente: die tatsaechlich
    verschickten Nutzlasten weichen an mehreren Stellen von der Dokumentation
    ab, und ein Signaturfehler wuerde vom Dispatcher als Warnung verschluckt --
    ein stilles Loch im Ereignisspiegel.
    """
    for signal in Signal:
        app.on_signal(signal)(_mirror(buffer, source, signal.value))

    app.on_signal(Signal.WORKER_ONLINE)(_on_online(app, buffer, source))
    app.on_signal(Signal.WORKER_SHUTDOWN)(_on_shutdown(buffer))


def _mirror(buffer: EventBuffer, source: str, event_type: str) -> Any:
    async def handler(**kwargs: Any) -> None:
        buffer.record(
            Event(
                type=event_type,
                ts=time.time(),
                source=str(kwargs.get("worker_id") or source),
                origin=Origin.PICKUP,
                payload=kwargs,
            )
        )

    handler.__qualname__ = f"dashboard_mirror[{event_type}]"
    return handler


def _on_online(app: Any, buffer: EventBuffer, source: str) -> Any:
    """Verbindung aufbauen, Flusher starten, Stammdaten melden."""
    import os

    async def handler(**kwargs: Any) -> None:
        worker_id = str(kwargs.get("worker_id") or source)
        queues = list(app.config.queues)
        buffer.record(
            Event(
                type=META_EVENT,
                ts=time.time(),
                source=worker_id,
                origin=Origin.PICKUP,
                payload={
                    "worker_id": worker_id,
                    "slot": app.config.worker_slot,
                    "group": _group_key(queues, os.environ.get("SQD_WORKER_GROUP")),
                    "queues": queues,
                    "all_queues": _all_queues(queues),
                    "concurrency": app.config.concurrency,
                    "pool": app.config.pool,
                    "started_at": time.time(),
                },
            )
        )
        await buffer.ensure_started()

    handler.__qualname__ = "dashboard_worker_online"
    return handler


def _on_shutdown(buffer: EventBuffer) -> Any:
    """Leeren anstossen -- aber den Flusher **nicht** beenden.

    swarmq verschickt das Herunterfahr-Signal **vor** dem Auslaufen der
    laufenden Tasks. Deren Ausgaenge fallen also erst danach an. Wer hier den
    Flusher beendet, verliert bei jedem Rollout die Ausgaenge aller gerade
    laufenden Tasks, und die Zeilen stehen sieben Tage lang auf "laufend".
    """

    async def handler(**kwargs: Any) -> None:
        # Nur wecken, nicht warten: swarmq verschickt dieses Signal mit einer
        # Zeitgrenze von zehn Sekunden, und die Gnadenfrist des Supervisors
        # laeuft dabei bereits. Ein Netz-Roundtrip hier wuerde bei
        # unerreichbarem Valkey jedes Herunterfahren um zehn Sekunden
        # verlaengern und den Handler danach mitten im Schreiben abbrechen.
        buffer.nudge()

    handler.__qualname__ = "dashboard_worker_shutdown"
    return handler
