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
#: Wie lange ``stop`` hoechstens versucht zu leeren. Ohne Frist haengt ein
#: Herunterfahren an einem unerreichbaren Strom fest.
DEFAULT_STOP_DEADLINE_S = 2.0


class StreamClient(Protocol):
    """Was der Puffer vom Strom braucht -- mehr nicht."""

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None: ...

    async def connect(self) -> None: ...


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
        self._span_dropped = 0
        self._write_lock = asyncio.Lock()
        self._wake = asyncio.Event()
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

    @property
    def running(self) -> bool:
        """Laeuft der Hintergrund-Flusher noch?"""
        return self._task is not None and not self._task.done()

    @property
    def client(self) -> StreamClient:
        """Der Strom-Client, damit der Hook ihn verbinden kann."""
        return self._client

    def peek(self) -> list[Event]:
        """Momentaufnahme des Puffers. Nur fuer Tests und die Zustandsanzeige."""
        return list(self._queue)

    def _account_loss(self, events: list[Event]) -> None:
        """Verlorene Ereignisse buchen. Einzige Stelle, die ``dropped`` erhoeht.

        Verluste entstehen an drei Stellen: beim Ueberlauf in ``record``, beim
        Zuruecklegen nach einem Schreibfehler, und beim Abbruch eines
        Schreibvorgangs. Frueher zaehlte nur die erste -- die anderen beiden
        verschwanden lautlos, und die Lueckenmeldung fiel zu klein aus (R4a).
        """
        if not events:
            return
        self._counters.dropped += len(events)
        self._span_dropped += len(events)
        for ev in events:
            self._drop_from = ev.ts if self._drop_from is None else min(self._drop_from, ev.ts)
            self._drop_to = ev.ts if self._drop_to is None else max(self._drop_to, ev.ts)

    def _push_back(self, events: list[Event]) -> None:
        """Ereignisse an den Kopf zuruecklegen und Verdraengtes buchen.

        ``extendleft`` auf einem gedeckelten ``deque`` verdraengt am anderen
        Ende -- also die **neuesten** Ereignisse, und lautlos. Beides ist
        falsch: verloren werden soll das Aelteste, und gezaehlt werden muss
        jeder Verlust (R4a).
        """
        if not events:
            return
        frei = (self._queue.maxlen or (len(self._queue) + len(events))) - len(self._queue)
        if frei >= len(events):
            self._queue.extendleft(reversed(events))
            return
        passt = events[len(events) - frei :] if frei > 0 else []
        zuviel = events[: len(events) - frei] if frei > 0 else events
        if passt:
            self._queue.extendleft(reversed(passt))
        self._account_loss(zuviel)

    def record(self, event: Event) -> None:
        """Ereignis annehmen. Synchron, in O(1), wartet auf nichts, wirft nie.

        Laeuft der Puffer ueber, faellt das aelteste Ereignis weg und der
        Verlustzaehler steigt -- der Prozess bleibt gesund, und R4a macht die
        Luecke spaeter sichtbar.
        """
        try:
            if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
                self._account_loss([self._queue[0]])
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
                "count": self._span_dropped,
                "from_ts": self._drop_from,
                "to_ts": self._drop_to,
            },
        )

    async def flush_once(self) -> int:
        """Einen Durchlauf schreiben. Gibt die Zahl geschriebener Ereignisse zurueck.

        Serialisiert: zwei gleichzeitige Durchlaeufe wuerden disjunkte Stapel
        entnehmen und in beliebiger Reihenfolge landen -- die Stromreihenfolge
        traegt aber die Statusableitung des Verzeichnisses.
        """
        async with self._write_lock:
            return await self._flush_locked()

    async def _flush_locked(self) -> int:
        if not self._queue and self._drop_from is None:
            return 0
        stapel: list[Event] = []
        while self._queue and len(stapel) < self._batch:
            stapel.append(self._queue.popleft())
        verlust = self._lost_event()
        nutzlast = [*stapel, verlust] if verlust is not None else stapel
        if not nutzlast:
            return 0
        try:
            await self._client.xadd_batch(self._key, [encode(e) for e in nutzlast], self._maxlen)
        except asyncio.CancelledError:
            # Abbruch mitten im Schreiben: der Stapel darf nicht verschwinden.
            # swarmq bricht Lebenszyklus-Handler nach ihrer Zeitgrenze ab.
            self._push_back(stapel)
            raise
        except Exception as exc:
            self._counters.failed += 1
            # Ohne Nutzlast: die Ereignisse tragen Argumente im Klartext (R7),
            # und Logs erreichen weder die Rechtezusage aus R7a noch die
            # Trimmung aus R9.
            logger.warning(
                "stream_write_failed kind=%s count=%d reason=%r",
                self._key, len(nutzlast), exc,
            )
            self._push_back(stapel)
            return 0
        if verlust is not None:
            self._drop_from = self._drop_to = None
            self._span_dropped = 0
        self._counters.flushed += len(nutzlast)
        return len(nutzlast)

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
            self._wake.clear()
            warten = [asyncio.create_task(self._stop.wait()), asyncio.create_task(self._wake.wait())]
            _, offen = await asyncio.wait(
                warten, timeout=max(self._interval, delay), return_when=asyncio.FIRST_COMPLETED
            )
            for aufgabe in offen:
                aufgabe.cancel()

    async def start(self) -> None:
        """Hintergrund-Flusher starten. Idempotent."""
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def ensure_started(self) -> None:
        """Verbinden und den Flusher starten, falls noch nicht geschehen.

        Idempotent und fehlertolerant: scheitert das Verbinden, laeuft der
        Flusher trotzdem und versucht es bei jedem Durchlauf erneut. Aufrufbar
        aus jedem asynchronen Pfad -- die einreihende Anwendung hat kein
        Lebenszyklus-Signal, an dem sie haengen koennte.
        """
        if self.running:
            return
        try:
            await self._client.connect()
        except Exception:
            logger.warning("dashboard_stream_connect_failed", exc_info=True)
        await self.start()

    def nudge(self) -> None:
        """Den Flusher wecken, ohne auf ihn zu warten.

        Fuer Aufrufer im Signalpfad: ein ``await flush_once()`` dort waere ein
        Netz-Roundtrip in einem Handler mit Zeitgrenze (R4).
        """
        self._wake.set()

    async def stop(self, *, deadline_s: float = DEFAULT_STOP_DEADLINE_S) -> None:
        """Flusher beenden und leeren, aber nur bis zur Frist.

        Leert in Stapeln statt in einem -- ein einziger liesse bei vollem
        Puffer tausende Ereignisse liegen. Die Frist ist trotzdem Pflicht: an
        einem unerreichbaren Strom wuerde die Schleife sonst nie enden, und ein
        Herunterfahren haengt daran fest. Was bis dahin nicht hinausgeht, wird
        **gezaehlt** statt verschluckt, damit R4a die Luecke spaeter ausweist.
        """
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        ende = time.monotonic() + deadline_s
        while self._queue or self._drop_from is not None:
            rest = ende - time.monotonic()
            if rest <= 0:
                break
            try:
                if await asyncio.wait_for(self.flush_once(), timeout=rest) == 0:
                    break
            except (TimeoutError, asyncio.CancelledError):
                break
            except Exception:
                break

        if self._queue:
            # Nicht mehr losgeworden. Buchen, nicht verschweigen.
            rest_events = list(self._queue)
            self._queue.clear()
            self._account_loss(rest_events)
