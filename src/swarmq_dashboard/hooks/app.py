"""Anwendungs-Hook: eine Zeile in der einreihenden Anwendung.

::

    from swarmq_dashboard.hooks.app import install
    install(app)

Nur dieser Hook kennt einen Task **ab der Einreihung** -- mit seinen
Argumenten, mit Einreihungszeitpunkt und als wartend stornierbar (KD12, R3a).
Das ist der Grund, warum das Paket ueberhaupt zwei Hooks mitliefert.

Er faengt nicht alles: Scheduler-Ausloesungen und jede verzoegerte Einreihung
publizieren direkt am Broker und passieren diesen Haken nie. Die holt der
Worker-Hook beim Aufgreifen nach, dann aber ohne Einreihungszeitpunkt (R3b,
R31).

Wie der Worker-Hook wirft und wartet er nicht (R4). Die Bibliothek verschluckt
Fehler dieses Hakens ohnehin -- darauf verlaesst er sich nicht.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from swarmq import Middleware

from swarmq_dashboard.buffer import EventBuffer
from swarmq_dashboard.config import HookConfig
from swarmq_dashboard.events import Event, Origin
from swarmq_dashboard.stream import ValkeyStreamClient

logger = logging.getLogger("swarmq_dashboard.hooks.app")

#: Ereignisart einer Einreihung durch die Anwendung.
ENQUEUE_EVENT = "task_enqueue"

DEFAULT_CAPACITY = 10_000


class _EnqueueMiddleware(Middleware):
    """Meldet jede erfolgreiche Einreihung mit ihren Argumenten."""

    def __init__(self, buffer: EventBuffer, source: str) -> None:
        self._buffer = buffer
        self._source = source

    async def post_enqueue(self, message: Any) -> None:
        # Laeuft im Client nach erfolgreichem Publish. Nur anhaengen.
        self._buffer.record(
            Event(
                type=ENQUEUE_EVENT,
                ts=time.time(),
                source=self._source,
                origin=Origin.ENQUEUE,
                payload=dict(message),
            )
        )
        # Eine einreihende Anwendung hat kein Lebenszyklus-Signal, an dem der
        # Flusher haengen koennte -- anders als der Worker, der WORKER_ONLINE
        # bekommt. Er startet deshalb hier, beim ersten Einreihen, und zwar
        # idempotent. Ohne das schrieb der Hook nie etwas nach Valkey: die
        # Ereignisse liefen in den Puffer und rollten dort still durch.
        await self._buffer.ensure_started()


def install(app: Any, *, source: str | None = None) -> None:
    """Den Hook auf eine swarmq-Instanz anwenden. Wirft unter keinen Umstaenden.

    Laeuft der Worker-Hook im selben Prozess, wird dessen Puffer geteilt statt
    ein zweiter angelegt -- sonst haette der Prozess zwei Warteschlangen, zwei
    Flusher und zwei Verlustzaehler fuer denselben Strom.

    Mehrfacher Aufruf ist wirkungslos: eine zweite Registrierung wuerde jede
    Einreihung doppelt melden, und das Verzeichnis kann Dubletten nicht von
    echten Wiederholungen unterscheiden.
    """
    try:
        _install(app, source)
    except Exception:
        logger.warning("dashboard_app_hook_setup_failed; Anwendung reiht ohne Hook ein", exc_info=True)


def _install(app: Any, source: str | None) -> None:
    if getattr(app, "_sqd_app_hook", False):
        return

    buffer: EventBuffer | None = getattr(app, "hook_buffer", None)
    if buffer is None:
        hook_cfg = HookConfig.from_env()
        if not hook_cfg.usable:
            logger.warning("dashboard_app_hook_inaktiv reason=%s", hook_cfg.reason)
            app.hook_buffer = None
            app._sqd_app_hook = True
            return
        buffer = EventBuffer(
            capacity=DEFAULT_CAPACITY,
            client=ValkeyStreamClient(hook_cfg.stream_url or ""),
        )
        app.hook_buffer = buffer

    app.add_middleware(_EnqueueMiddleware(buffer, source or str(app.config.worker_identity)))
    app._sqd_app_hook = True
    logger.info("dashboard_app_hook_aktiv")


async def shutdown(app: Any) -> None:
    """Gepufferte Ereignisse wegschreiben und den Flusher beenden.

    Fuer kurzlebige Anwendungen -- ein Skript, ein Cron-Job, eine Anfrage in
    einem kurzlebigen Prozess. Ein Dienst, der ohnehin laeuft, braucht das
    nicht: der Flusher schreibt im Takt weiter.
    """
    buffer = getattr(app, "hook_buffer", None)
    if buffer is not None:
        await buffer.stop()
