"""Beschraenkter Puffer mit Hintergrund-Flusher (KTD1).

swarmq verschickt Task-Signale sequenziell im Ausfuehrungspfad, ohne
Zeitgrenze, waehrend der Nebenlaeufigkeits-Slot gehalten wird und **vor** dem
Broker-Ack. Ein Handler, der auf Netz wartet, haelt den Slot und riskiert die
Zustellfrist des Brokers -- aus einem Dashboard-Problem wuerde ein
Task-Problem. Deshalb ist ``record`` synchron, in O(1), und wartet auf nichts.

Vorbild ist der Ereignis-Relay der Lasttest-Suite
(``swarmq-loadsuite``, ``src/loadsuite/worker/relay.py``). Dort puffert eine
Datei, weil ein verlorenes Ereignis dort einen Beweis zerstoert. Hier genuegt
Arbeitsspeicher: ein verlorenes Beobachtungsereignis kostet eine Luecke, die
R4a ausweist. Der Preis ist benannt -- stirbt der Prozess waehrend einer
Stoerung, gehen gepufferte Ereignisse **und** der Verlustzaehler zusammen
verloren, und die Zeitspanne wird nie als unvollstaendig gemeldet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from swarmq_dashboard.events import TASK_STREAM, Event, Origin, encode

logger = logging.getLogger("swarmq_dashboard.buffer")

#: Ereignisart, mit der ein Hook eine eigene Luecke meldet (R4a).
LOST_EVENT = "sqd_events_lost"

DEFAULT_FLUSH_INTERVAL_S = 1.0
DEFAULT_BATCH = 500
DEFAULT_RETRY_MAX_S = 10.0
#: Laengendeckel des Stroms. Der Schreiber setzt nur ihn; die Trimmung nach
#: Alter macht der Dienst (U2 Punkt 5), damit die Aufbewahrungsdauer nicht auf
#: jedem Worker-Host wohnt.
DEFAULT_MAXLEN = 1_000_000


class StreamClient(Protocol):
    """Was der Puffer vom Strom braucht -- mehr nicht."""

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None: ...


@dataclass
class Counters:
    """Zaehlerstand. Geht als Ereignis in den Strom und in die Kopfzeile."""

    buffered: int = 0
    flushed: int = 0
    failed: int = 0
    dropped: int = 0


class EventBuffer:
    """Nimmt Ereignisse synchron an und bringt sie asynchron in den Strom."""

    def __init__(
        self,
        *,
        capacity: int,
        client: StreamClient,
        stream_key: str = TASK_STREAM,
        maxlen: int = DEFAULT_MAXLEN,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL_S,
        batch: int = DEFAULT_BATCH,
    ) -> None:
        self._queue: deque[Event] = deque(maxlen=capacity)
        self._client = client
        self._key = stream_key
        self._maxlen = maxlen
        self._interval = flush_interval
        self._batch = batch
        self._counters = Counters()
        self._drop_from: float | None = None
        self._drop_to: float | None = None
        self._source = ""
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- Aufnahme --------------------------------------------------------

    @property
    def counters(self) -> Counters:
        return Counters(**vars(self._counters))

    @property
    def drop_span(self) -> tuple[float, float] | None:
        """Zeitspanne der verworfenen Ereignisse, oder ``None``."""
        if self._drop_from is None or self._drop_to is None:
            return None
        return (self._drop_from, self._drop_to)

    def peek(self) -> list[Event]:
        """Momentaufnahme des Puffers. Nur fuer Tests und die Zustandsanzeige."""
        return list(self._queue)

    def record(self, event: Event) -> None:
        """Ereignis annehmen. Synchron, in O(1), wartet auf nichts, wirft nie.

        Laeuft der Puffer ueber, faellt das aelteste Ereignis weg und der
        Verlustzaehler steigt -- der Prozess bleibt gesund, und R4a macht die
        Luecke spaeter sichtbar.
        """
        try:
            if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
                verworfen = self._queue[0]
                self._counters.dropped += 1
                ts = verworfen.ts
                self._drop_from = ts if self._drop_from is None else min(self._drop_from, ts)
                self._drop_to = ts if self._drop_to is None else max(self._drop_to, ts)
            self._queue.append(event)
            self._counters.buffered += 1
            if not self._source:
                self._source = event.source
        except Exception:
            # Ein Hook darf den Task nie stoeren -- auch nicht durch sich selbst.
            logger.warning("record_failed", exc_info=True)

    # -- Flusher ---------------------------------------------------------

    def _lost_event(self) -> Event | None:
        """Verlust-Ereignis aus dem aktuellen Zaehlerstand bauen (R4a)."""
        if self._drop_from is None or self._drop_to is None:
            return None
        return Event(
            type=LOST_EVENT,
            ts=time.time(),
            source=self._source,
            origin=Origin.PICKUP,
            payload={
                "worker_id": self._source,
                "count": self._counters.dropped,
                "from_ts": self._drop_from,
                "to_ts": self._drop_to,
            },
        )

    async def flush_once(self) -> int:
        """Einen Durchlauf schreiben. Gibt die Zahl geschriebener Ereignisse zurueck."""
        if not self._queue and self._drop_from is None:
            return 0
        stapel: list[Event] = []
        while self._queue and len(stapel) < self._batch:
            stapel.append(self._queue.popleft())
        verlust = self._lost_event()
        if verlust is not None:
            stapel.append(verlust)
        if not stapel:
            return 0
        try:
            await self._client.xadd_batch(self._key, [encode(e) for e in stapel], self._maxlen)
        except Exception as exc:
            self._counters.failed += 1
            # Ohne Nutzlast: die Ereignisse tragen Argumente im Klartext (R7),
            # und Logs erreichen weder die Rechtezusage aus R7a noch die
            # Trimmung aus R9.
            logger.warning(
                "stream_write_failed kind=%s count=%d reason=%r",
                self._key, len(stapel), exc,
            )
            self._queue.extendleft(reversed([e for e in stapel if e.type != LOST_EVENT]))
            return 0
        if verlust is not None:
            self._drop_from = self._drop_to = None
        self._counters.flushed += len(stapel)
        return len(stapel)

    async def _loop(self) -> None:
        delay = self._interval
        while not self._stop.is_set():
            try:
                geschrieben = await self.flush_once()
                delay = self._interval if geschrieben or not self._queue else min(delay * 2, DEFAULT_RETRY_MAX_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("flush_loop_error", exc_info=True)
                delay = min(delay * 2, DEFAULT_RETRY_MAX_S)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=max(self._interval, delay))

    async def start(self) -> None:
        """Hintergrund-Flusher starten."""
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Flusher beenden und ein letztes Mal leeren."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        with contextlib.suppress(Exception):
            await self.flush_once()
