"""Dead-Letter-Bedienung (R28, R28a, R29, R30).

**Sichten ist nicht harmlos.** Die Bibliothek nennt ``list_dead_letters``
"non-destructive", und im Wortsinn stimmt das: jede gelesene Nachricht wird
wieder eingereiht. Aber sie wird **neu** eingereiht -- die Reihenfolge aendert
sich, der Zustellzaehler beginnt von vorn, und stuerzt der Dienst mitten im
Vorgang ab, sind die schon gelesenen Nachrichten fort. R28a verlangt darum
zweierlei: die Oberflaeche sagt das **vorher**, und zwei Teammitglieder
koennen es nicht gleichzeitig auf derselben Queue ausloesen.

Der Riegel je Queue ist der einzige Schutz gegen den zweiten Fall. Er traegt,
weil der Dienst in einem Prozess laeuft (KTD5) -- diese Einschraenkung ist
hier keine Sparsamkeit, sondern die Voraussetzung. Mit mehreren Arbeitern
braeuchte es eine Sperre in Valkey, und die Seite muesste sagen, dass sie
nicht mehr greift.

Die **Tiefe** dagegen (R28) kommt aus der passiven Beobachtung in
``broker.py``: beliebig oft abfragbar, ohne die Queue zu beruehren. Sie ist
der Dauerzustand der Seite; das Sichten ist die Ausnahme.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

#: Wie viele Nachrichten eine Sichtung hoechstens liest. Bewusst klein: jede
#: gelesene Nachricht wird umgereiht, und wer zehntausend sichtet, reiht
#: zehntausend um.
DEFAULT_PEEK_LIMIT = 50


class DlqBusy(RuntimeError):
    """Auf dieser Queue laeuft bereits eine Sichtung."""


class DlqAdmin(Protocol):
    """Was die Dead-Letter-Bedienung von der Bibliothek braucht."""

    async def list_dead_letters(self, queue: str, limit: int = 100) -> tuple[list[dict[str, Any]], int]: ...

    async def retry_dead_letter(self, queue: str, task_id: str,
                                scan_limit: int = 1000) -> tuple[bool, int]: ...

    async def retry_all_dead_letters(self, queue: str, limit: int = 10_000) -> tuple[int, int]: ...

    async def purge_dead_letters(self, queue: str) -> int: ...

    #: Die passive Tiefe. Steht hier, weil das Leeren ohne sie nicht
    #: stattfindet -- nicht als Kuer, sondern als Bedingung.
    async def dead_letter_depth(self, queue: str) -> int: ...


@dataclass(frozen=True, slots=True)
class PeekResult:
    """Was eine Sichtung ergeben hat."""

    queue: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    #: Wie viele Nachrichten wirklich gelesen -- und damit umgereiht -- wurden.
    gelesen: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MoveResult:
    """Was eine Umreihung oder ein Leeren ergeben hat."""

    queue: str
    moved: int = 0
    scanned: int = 0
    error: str | None = None

    @property
    def unverarbeitet(self) -> int:
        """Gelesen, aber nicht weitergereicht (R29).

        Getrennt ausgewiesen, weil "zwei von drei" etwas anderes heisst als
        "zwei": nur die erfolgreiche Zahl zu melden liesse "alles erledigt"
        lesen, wo eine Nachricht liegen blieb.
        """
        return max(0, self.scanned - self.moved)


class DlqService:
    """Sichten, Wiedereinreihen und Leeren -- je Queue serialisiert."""

    def __init__(self, admin: DlqAdmin) -> None:
        self._admin = admin
        self._riegel: dict[str, asyncio.Lock] = {}

    def _lock(self, queue: str) -> asyncio.Lock:
        """Ein Riegel **je Queue**, nicht einer fuer alles: zwei Teams duerfen
        sich nicht gegenseitig blockieren."""
        return self._riegel.setdefault(queue, asyncio.Lock())

    def busy(self, queue: str) -> bool:
        """Laeuft auf dieser Queue gerade eine Sichtung?"""
        return self._lock(queue).locked()

    async def peek(self, queue: str, limit: int = DEFAULT_PEEK_LIMIT) -> PeekResult:
        """Den Inhalt sichten. **Reiht jede gelesene Nachricht neu ein.**

        Wirft ``DlqBusy``, wenn schon jemand sichtet -- und zwar sofort, statt
        zu warten: ein Teammitglied, das vor einer haengenden Seite sitzt,
        loest sie irgendwann neu und hat dann zwei Durchlaeufe.
        """
        riegel = self._lock(queue)
        if riegel.locked():
            raise DlqBusy(
                f"Die Queue {queue} wird gerade gesichtet. Jede Sichtung reiht die "
                "Nachrichten neu ein -- zwei gleichzeitig wuerden sie doppelt umreihen."
            )
        async with riegel:
            try:
                eintraege, gelesen = await self._admin.list_dead_letters(queue, limit=limit)
            except Exception as exc:
                # Benannt zurueckgeben, nicht werfen -- und der Riegel faellt
                # durch das ``async with`` trotzdem. Bliebe er haengen, waere
                # die Queue nach der ersten Stoerung dauerhaft gesperrt.
                return PeekResult(queue=queue, error=str(exc))
            return PeekResult(queue=queue, entries=list(eintraege), gelesen=gelesen)

    async def requeue(self, queue: str, task_id: str) -> MoveResult:
        """Eine Nachricht zurueck in die Arbeits-Queue (R29)."""
        try:
            gefunden, gelesen = await self._admin.retry_dead_letter(queue, task_id)
        except Exception as exc:
            return MoveResult(queue=queue, error=str(exc))
        return MoveResult(queue=queue, moved=int(gefunden), scanned=gelesen)

    async def requeue_all(self, queue: str) -> MoveResult:
        """Alles zurueck in die Arbeits-Queue (R29)."""
        try:
            bewegt, gelesen = await self._admin.retry_all_dead_letters(queue)
        except Exception as exc:
            return MoveResult(queue=queue, error=str(exc))
        return MoveResult(queue=queue, moved=bewegt, scanned=gelesen)

    async def purge(self, queue: str, *, bestaetigung: str, erwartet: int) -> MoveResult:
        """Die Dead-Letter-Queue leeren (R30).

        Die einzige Handlung im ganzen Dashboard, die Daten endgueltig
        vernichtet. Die Bestaetigung nennt darum **beides**: was vernichtet
        wird und wie viel. Stimmt die Zahl nicht mehr, ist inzwischen etwas
        dazugekommen -- und dann ist die Bestaetigung nicht mehr die, die das
        Teammitglied gegeben hat.
        """
        if bestaetigung != queue:
            return MoveResult(
                queue=queue,
                error=f"Zur Bestaetigung den Queue-Namen {queue!r} eingeben.",
            )
        try:
            jetzt = await self._aktuelle_tiefe(queue)
        except Exception as exc:
            return MoveResult(queue=queue, error=str(exc))
        if jetzt != erwartet:
            return MoveResult(
                queue=queue,
                error=f"Die Queue enthaelt jetzt {jetzt} Nachrichten, nicht {erwartet} -- "
                "bitte noch einmal ansehen und neu bestaetigen.",
            )
        try:
            entfernt = await self._admin.purge_dead_letters(queue)
        except Exception as exc:
            return MoveResult(queue=queue, error=str(exc))
        return MoveResult(queue=queue, moved=entfernt, scanned=entfernt)

    async def _aktuelle_tiefe(self, queue: str) -> int:
        """Die Tiefe zum Zeitpunkt der Bestaetigung.

        **Pflicht**, nicht Kuer: eine Bestaetigung, deren Zahl niemand
        nachprueft, ist Theater. Laesst sich die Tiefe nicht ermitteln, wird
        nicht geloescht -- lieber eine Handlung, die nicht stattfindet, als
        eine, die mehr vernichtet als bestaetigt wurde.
        """
        tiefe = getattr(self._admin, "dead_letter_depth", None)
        if tiefe is None:
            raise RuntimeError(
                "Die Zahl der Nachrichten laesst sich nicht pruefen -- es wird nichts geloescht."
            )
        return int(await tiefe(queue))


class SwarmQDlqAdmin:
    """Die wirkende Seite ueber ``RabbitMQBroker`` -- **traege** verbunden.

    Absichtlich nicht beim Hochfahren: das Oeffnen der Seite kommt ohne sie
    aus. Die Tiefe (R28) liefert die passive Beobachtung in ``broker.py``,
    und die Dead-Letter-Tasks liefert das Verzeichnis. Eine Verbindung, die
    nur fuer Aktionen gebraucht wird, erst bei der ersten Aktion aufzubauen
    heisst: wer nur hinsieht, bezahlt nichts -- auch keinen Kanal.
    """

    def __init__(self, broker_url: str) -> None:
        self._url = broker_url
        self._broker: Any | None = None

    async def _verbunden(self) -> Any:
        if self._broker is None:
            from swarmq.broker.rabbitmq import RabbitMQBroker

            broker = RabbitMQBroker(self._url)
            await broker.connect()
            self._broker = broker
        return self._broker

    async def close(self) -> None:
        if self._broker is not None:
            try:
                await self._broker.close()
            except Exception:
                pass
            self._broker = None

    async def list_dead_letters(self, queue: str, limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        return await (await self._verbunden()).list_dead_letters(queue, limit=limit)  # type: ignore[no-any-return]

    async def retry_dead_letter(self, queue: str, task_id: str,
                                scan_limit: int = 1000) -> tuple[bool, int]:
        broker = await self._verbunden()
        return await broker.retry_dead_letter(queue, task_id, scan_limit=scan_limit)  # type: ignore[no-any-return]

    async def retry_all_dead_letters(self, queue: str, limit: int = 10_000) -> tuple[int, int]:
        return await (await self._verbunden()).retry_all_dead_letters(queue, limit=limit)  # type: ignore[no-any-return]

    async def purge_dead_letters(self, queue: str) -> int:
        return int(await (await self._verbunden()).purge_dead_letters(queue))

    async def dead_letter_depth(self, queue: str) -> int:
        return int(await (await self._verbunden()).dead_letter_depth(queue))
