"""Strom-Leser: Fortschritt halten, Rueckstand erkennen, Pflege fahren.

Die gefaehrliche Stelle ist der Rueckstand. Der Laengendeckel des Stroms
schneidet am aeltesten Ende ab (R9, U2). Faellt der Leser dahinter zurueck --
bei einem Task-Stoss oder waehrend eines Neustarts des Dienstes --, sind die
ueberholten Eintraege fort. Ein Leser, der einfach am aeltesten Rest aufsetzt,
ueberspringt sie **still**: die betroffenen Tasks fehlen im Verzeichnis, und
ein Teammitglied liest das als "nicht gelaufen". Das ist derselbe Irrtum, vor
dem R18 bei Workern ohne Hook schuetzt, nur aus einer anderen Quelle.

Deshalb vergleicht der Leser vor jedem Durchlauf seinen gemerkten Stand mit
der kleinsten noch vorhandenen Kennung und markiert die Zeitspanne dazwischen
mit demselben Mittel wie ein Verlust-Ereignis der Hooks (R4a).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import TASK_STREAM, decode

#: Schluessel des Lesefortschritts in der Fortschritts-Tabelle.
POSITION_KEY = "stream_pos"
#: Schluessel des Merkers "der Neuaufbau hat den Strom eingeholt" (R8).
CAUGHT_UP_KEY = "rebuild_done"
#: Wie viele Eintraege ein Durchlauf hoechstens holt.
DEFAULT_BATCH = 1000


class StreamSource(Protocol):
    """Was der Leser vom Strom braucht."""

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]: ...

    async def min_id(self, key: str) -> str | None: ...

    async def trim_by_min_id(self, key: str, min_ms: int) -> int: ...


def _as_tuple(stream_id: str) -> tuple[int, int]:
    """Stromkennung als vergleichbares Paar. ``"1789600000000-3"`` -> ``(…, 3)``."""
    ms, _, seq = stream_id.partition("-")
    return (int(ms), int(seq or 0))


class StreamReader:
    """Liest den Strom in das Verzeichnis und haelt beides in der Frist."""

    def __init__(
        self,
        source: StreamSource,
        writer: DirectoryWriter,
        *,
        stream_key: str = TASK_STREAM,
        batch: int = DEFAULT_BATCH,
    ) -> None:
        self.writer = writer
        self._source = source
        self._key = stream_key
        self._batch = batch
        self._behind = False
        self._gap: tuple[str, str] | None = None
        self._dropped = 0
        #: Haelt die SQLite-Arbeit auf einen Thread zur Zeit fest. Sie laeuft
        #: in einem Arbeitsthread (siehe ``schema.connect``), und zwei
        #: gleichzeitige Durchlaeufe haetten dieselbe Verbindung aus zwei
        #: Threads in der Hand.
        self._riegel = asyncio.Lock()
        #: Grenze der letzten eigenen Altersbereinigung, in Millisekunden.
        #: Was davor lag, ist abgelaufen und nicht verloren -- der Unterschied
        #: entscheidet, ob ein Rueckstand gemeldet wird.
        self._expired_before_ms = 0

    # -- Zustand ---------------------------------------------------------

    @property
    def position(self) -> str:
        """Zuletzt verarbeitete Stromkennung, oder leer vor dem ersten Lesen."""
        zeile = self.writer._con.execute(
            "select value from progress where key=?", (POSITION_KEY,)
        ).fetchone()
        return str(zeile["value"]) if zeile is not None else ""

    @property
    def rebuilding(self) -> bool:
        """Baut der Dienst das Verzeichnis gerade aus dem Strom neu auf (R8)?

        Massgeblich ist, ob der Leser den Strom **eingeholt** hat, nicht ob er
        schon einmal gelesen hat. Ein Durchlauf holt hoechstens ``batch``
        Eintraege; bei einem halben Jahrhundert Ereignissen im Strom waeren
        nach dem ersten Durchlauf tausend Zeilen da und fuenfhundertausend
        nicht. Die Kopfzeile meldete "kein Neuaufbau" ueber eine zu 99 %
        leere Liste -- genau die halbe Wahrheit, die R8 ausschliesst.
        """
        zeile = self.writer._con.execute(
            "select value from progress where key=?", (CAUGHT_UP_KEY,)
        ).fetchone()
        return zeile is None

    @property
    def behind(self) -> bool:
        """Hat der Laengendeckel ungelesene Eintraege abgeschnitten?"""
        return self._behind

    def status(self) -> dict[str, Any]:
        """Zustandsbericht fuer die Kopfzeile (R8, R9, R26)."""
        return {
            "position": self.position,
            "rebuilding": self.rebuilding,
            "behind": self._behind,
            "gap": self._gap,
            "dropped": self._dropped,
            # Ereignisse, die die Datenbank nicht annahm, und ein noch nicht
            # eingeloestes Loeschversprechen (R7a). Beides gehoert in die
            # Kopfzeile: still bleiben hiesse, eine Luecke als Vollstaendigkeit
            # auszugeben.
            "rejected": self.writer.rejected,
            "erasure_pending": self.writer.erasure_pending,
            "oldest_entry": self.writer.oldest_entry(),
        }

    def _set_position(self, stream_id: str) -> None:
        self._merken(POSITION_KEY, stream_id)

    def _merken(self, schluessel: str, wert: str) -> None:
        self.writer._con.execute(
            "insert into progress(key, value) values (?,?) "
            "on conflict(key) do update set value=excluded.value",
            (schluessel, wert),
        )
        self.writer._con.commit()

    # -- Lesen -----------------------------------------------------------

    async def poll_once(self) -> int:
        """Einen Durchlauf lesen und einarbeiten. Gibt die Zahl der Eintraege zurueck.

        Der Riegel haelt gleichzeitige Durchlaeufe auseinander: die
        Datenbankarbeit laeuft in einem Arbeitsthread, und zwei Durchlaeufe
        haetten sonst dieselbe SQLite-Verbindung aus zwei Threads in der Hand.
        """
        async with self._riegel:
            return await self._poll_once()

    async def _poll_once(self) -> int:
        vorher = await asyncio.to_thread(lambda: self.position)
        await self._check_behind(vorher)

        eintraege = await self._source.read_after(self._key, vorher, self._batch)
        if not eintraege:
            # Nichts mehr zu holen heisst: eingeholt. Auch wenn es nichts
            # aufzubauen gab.
            await asyncio.to_thread(self._eingeholt_merken, vorher)
            return 0

        ereignisse: list[tuple[str | None, Any]] = []
        ausgefallen: list[float] = []
        for sid, roh in eintraege:
            try:
                ereignisse.append((sid, decode(roh)))
            except Exception:
                # Ein unlesbarer Eintrag darf den Leser nicht anhalten -- aber
                # er darf auch nicht spurlos verschwinden. Ein Hook einer
                # neueren Fassung mit einem unbekannten Herkunftswert liesse
                # sonst *alle* seine Ereignisse lautlos ausfallen, und die
                # betroffenen Tasks laesen sich als "nie gelaufen". Also wird
                # die Zeitspanne markiert, wie bei jedem anderen Verlust (R4a).
                self._dropped += 1
                ausgefallen.append(_as_tuple(sid)[0] / 1000.0)
        await asyncio.to_thread(
            self._einarbeiten, ereignisse, ausgefallen, eintraege[-1][0], len(eintraege)
        )
        return len(eintraege)

    # -- Der blockierende Teil, je in einem Arbeitsthread ------------------

    def _eingeholt_merken(self, vorher: str) -> None:
        self._merken(CAUGHT_UP_KEY, "1")
        if not vorher:
            self._set_position("0-0")

    def _einarbeiten(
        self,
        ereignisse: list[tuple[str | None, Any]],
        ausgefallen: list[float],
        letzte: str,
        zahl: int,
    ) -> None:
        self.writer.apply(ereignisse)
        # Erst **nach** dem Einarbeiten markieren: die Zeilen dieses Stapels
        # gibt es vorher noch gar nicht, und gerade sie sind die, die den
        # Ausfall ueberspannen koennen.
        for augenblick in ausgefallen:
            self.writer.mark_incomplete(augenblick, augenblick)
        self._set_position(letzte)
        if zahl < self._batch:
            self._merken(CAUGHT_UP_KEY, "1")

    async def _check_behind(self, position: str) -> None:
        """Gemerkten Stand gegen das aelteste noch vorhandene Ereignis halten."""
        if not position or position == "0-0":
            self._behind = False
            self._gap = None
            return
        aeltester = await self._source.min_id(self._key)
        if aeltester is None:
            self._behind = False
            self._gap = None
            return
        if _as_tuple(aeltester) <= _as_tuple(position):
            # Kein Rueckstand mehr -- und die alte Luecke mit fortnehmen, sonst
            # meldete der Bericht "kein Rueckstand" und nennte zugleich eine
            # Zeitspanne, was sich selbst widerspricht.
            self._behind = False
            self._gap = None
            return
        if _as_tuple(position)[0] < self._expired_before_ms:
            # Die eigene Altersbereinigung erklaert den Abstand: was vor der
            # Aufbewahrungsgrenze lag, ist abgelaufen, nicht verloren. Ohne
            # diese Unterscheidung meldete jeder Pflegelauf einen Rueckstand
            # und markierte gesunde Zeilen dauerhaft als unvollstaendig --
            # ein lauter Fehlalarm, der die echte Meldung entwertet.
            self._behind = False
            return

        # Der Deckel hat weggeschnitten, was noch nicht gelesen war.
        #
        # Die Zeitspanne kommt aus den Stromkennungen: Valkey vergibt sie als
        # Wanduhr in Millisekunden, und die Zeitstempel der Ereignisse sind
        # dieselbe Wanduhr in Sekunden. Beide koennen um die Flusher-Verzoegerung
        # auseinanderliegen -- Sekunden, nicht Groessenordnungen -- was fuer die
        # Markierung einer Luecke genau genug ist.
        self._behind = True
        self._gap = (position, aeltester)
        von = _as_tuple(position)[0] / 1000.0
        bis = _as_tuple(aeltester)[0] / 1000.0
        await asyncio.to_thread(self.writer.mark_incomplete, von, bis)

    # -- Pflege ----------------------------------------------------------

    async def maintain(self, *, now: float | None = None, retention_s: float) -> None:
        """Strom und Verzeichnis in der Frist halten (R5, R9).

        Beide im selben Takt: der Dienst trimmt den Strom nach Alter -- nicht
        die Hooks, damit die Aufbewahrungsdauer nicht auf jedem Worker-Host
        wohnt -- und laesst das Verzeichnis dieselbe Frist anwenden.
        """
        jetzt = time.time() if now is None else now
        grenze = jetzt - retention_s
        self._expired_before_ms = int(grenze * 1000)
        await self._source.trim_by_min_id(self._key, self._expired_before_ms)
        # Derselbe Riegel wie beim Durchlauf: das Ablaufen dauert auf einem
        # vollen Verzeichnis Sekunden und laeuft deshalb im Arbeitsthread.
        async with self._riegel:
            await asyncio.to_thread(self.writer.expire, older_than=grenze)
