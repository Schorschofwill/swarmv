"""Valkey-Anbindung des Ereignisstroms.

Traegt die Schreibseite (der Puffer aus ``buffer.py`` nutzt sie) und die
Leseseite, die der Dienst spaeter braucht. Der Schreiber setzt **nur** den
Laengendeckel: er schuetzt Valkey auch bei stehendem Dienst. Die Trimmung nach
Alter macht der Dienst (U2 Punkt 5, R9), damit die Aufbewahrungsdauer nicht auf
jedem Worker-Host wohnt und die angezeigte Grenze die wirksame ist.

Nicht ``swarmq.backend.valkey`` wiederverwendet: das Dashboard schreibt auf
einen eigenen Datenbankindex (KTD9) und mit eigenem Praefix, und R1a haelt es
auf oeffentliche Schnittstellen der Bibliothek fest -- eine eigene, duenne
Anbindung ist ehrlicher als ein fremder Verbindungspool.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

#: Feldname der Nutzlast im Stromeintrag. Ein Feld genuegt -- die Huelle ist
#: bereits kodiert, und Valkey-Streams sind Feld-Wert-Paare.
FIELD = "e"
#: Zeitgrenze fuer den Verbindungsaufbau. Ohne sie haengt der Aufbau gegen ein
#: Gegenueber, das TCP annimmt und dann schweigt -- eine Firewall, die
#: verwirft statt abzulehnen --, und mit ihm der ganze Takt.
#: Frist fuer den Aufbau **und** fuer jede spaetere Erreichbarkeitspruefung.
#: Sie muss kuerzer sein als die umschliessende Frist des Dienstes
#: (``web/services.py``, ``PROBE_TIMEOUT_S``) -- sonst bricht die aeussere die
#: innere ab, bevor diese aufraeumen kann, und jeder Takt laesst einen
#: Socket zurueck. Gemessen: 25 Takte, 25 offene Dateizeiger.
DEFAULT_CONNECT_TIMEOUT_S = 3.0
#: Zeitgrenze einzelner Befehle. valkey-py hat hier eine Vorgabe von 5 s; sie
#: wird hier ausdruecklich gesetzt, damit sie nicht still wandern kann.
DEFAULT_SOCKET_TIMEOUT_S = 5.0

T = TypeVar("T")


class StreamUnavailable(RuntimeError):
    """Der Ereignisstrom war nicht erreichbar.

    Das Gegenstueck zu ``BrokerUnavailable``. Es gibt ihn, weil eine
    Taktschleife sonst Valkey-Ausnahmetypen importieren muesste, um
    Erreichbarkeit von einem echten Fehler zu unterscheiden -- und dabei
    unvermeidlich zu weit oder zu eng fasst.
    """


async def _leise(client: Any) -> None:
    """Schliessen, ohne dass ein Fehler dabei den eigentlichen verdeckt."""
    with contextlib.suppress(Exception):
        await client.aclose()


class ValkeyStreamClient:
    """Duenne Schreib- und Leseschicht ueber einem Valkey-Strom."""

    def __init__(
        self,
        url: str,
        *,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        socket_timeout_s: float = DEFAULT_SOCKET_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._connect_timeout_s = connect_timeout_s
        self._socket_timeout_s = socket_timeout_s
        self._client: Valkey | None = None

    async def connect(self) -> None:
        """Verbindung aufbauen **und pruefen**. Idempotent.

        Das ``ping`` ist der Kern dieser Methode, nicht Zierrat:
        ``Valkey.from_url`` erzeugt nur ein Objekt und spricht mit niemandem.
        Gemessen kam ein Aufbau gegen einen toten Port so in 0,1 ms ohne
        Fehler zurueck -- und der Dienst haette beim Hochfahren gemeldet, der
        Strom sei erreichbar (R26, R33). Der Fehler erschien erst beim ersten
        echten Aufruf, an einer Stelle, an der ihn niemand mehr erwartete.
        """
        # **Auch** wenn schon ein Klient steht. Frueher kehrte die Methode
        # hier still zurueck: einmal erreichbar hiess fuer immer erreichbar.
        # Der Dienst reicht seinen vorhandenen Klienten in jedem Takt hierher
        # -- faellt Valkey danach weg, meldete ``/api/status`` weiter "ok",
        # waehrend jeder Zugriff scheiterte. Das ist die Stelle, an der im
        # Vorfall zuerst nachgesehen wird, und sie war die letzte, die es
        # bemerkte.
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.ping(),
                                       timeout=self._connect_timeout_s)
            except BaseException as exc:
                # **BaseException**, nicht ``Exception``: ein Abbruch von
                # aussen ist kein Fehler, hinterlaesst aber denselben
                # Schmutz. Genau daran leckte der Auffrischungstakt.
                alt = self._client
                self._client = None
                with contextlib.suppress(Exception):
                    await alt.aclose()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise StreamUnavailable(str(exc)) from exc
            return
        try:
            client = self._bauen()
        except Exception as exc:
            raise StreamUnavailable(f"unbrauchbare Adresse: {exc}") from exc
        try:
            await asyncio.wait_for(client.ping(), timeout=self._connect_timeout_s)
        except TimeoutError as exc:
            await _leise(client)
            raise StreamUnavailable(
                f"keine Antwort binnen {self._connect_timeout_s:.0f} s"
            ) from exc
        except BaseException as exc:
            # Siehe oben: auch der Abbruch muss den halb aufgebauten Klienten
            # schliessen, sonst bleibt sein Socket bis zum Prozessende offen.
            await _leise(client)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise StreamUnavailable(str(exc)) from exc
        self._client = client

    def _bauen(self) -> Valkey:
        """Den Client erzeugen. Eigene Methode, damit die Testreihe eine
        Haengestelle einsetzen kann, die keine Socket-Grenze abfaengt."""
        client: Valkey = Valkey.from_url(
            self._url,
            socket_connect_timeout=self._connect_timeout_s,
            socket_timeout=self._socket_timeout_s,
        )
        return client

    async def close(self) -> None:
        """Verbindung schliessen. Idempotent."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> Valkey:
        """Roher Client. Wirft, solange nicht verbunden."""
        if self._client is None:
            raise StreamUnavailable("der Ereignisstrom ist nicht verbunden")
        return self._client

    async def _call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Einen Valkey-Aufruf fahren und seine Fehler benennen."""
        try:
            return await fn()
        except StreamUnavailable:
            raise
        except (ValkeyError, OSError, TimeoutError) as exc:
            raise StreamUnavailable(str(exc)) from exc

    # -- Schreiben -------------------------------------------------------

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        """Einen Stapel anhaengen, mit ungefaehrem Laengendeckel.

        Der Deckel ist bewusst ungefaehr (``approximate``): Valkey schneidet
        dann an einer Knotengrenze statt Eintrag fuer Eintrag, was deutlich
        billiger ist. Die Zahl liegt danach knapp ueber dem Deckel, nie darunter.
        """
        pipe = self.client.pipeline(transaction=False)
        for raw in events:
            pipe.xadd(key, {FIELD: raw}, maxlen=maxlen, approximate=True)
        await self._call(pipe.execute)

    # -- Lesen -----------------------------------------------------------

    async def read_all(self, key: str) -> list[bytes]:
        """Alle Eintraege in Stromreihenfolge. Fuer Tests und den Neuaufbau."""
        eintraege: list[tuple[bytes, dict[bytes, bytes]]] = await self._call(
            lambda: self.client.xrange(key)
        )
        return [felder[FIELD.encode()] for _, felder in eintraege]

    async def length(self, key: str) -> int:
        """Zahl der Eintraege."""
        result: int = await self._call(lambda: self.client.xlen(key))
        return result

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        """Eintraege nach ``last_id`` in Stromreihenfolge.

        Ein leeres ``last_id`` heisst "von vorn" -- der Fall des Neuaufbaus.
        ``XRANGE`` mit exklusiver Untergrenze, nicht ``XREAD``: der Leser will
        nachholen, nicht auf Neues warten.
        """
        von = f"({last_id}" if last_id else "-"
        eintraege: list[tuple[bytes, dict[bytes, bytes]]] = await self._call(
            lambda: self.client.xrange(key, min=von, count=count)
        )
        return [(sid.decode(), felder[FIELD.encode()]) for sid, felder in eintraege]

    async def min_id(self, key: str) -> str | None:
        """Kennung des aeltesten noch vorhandenen Eintrags, oder ``None``."""
        eintraege: list[tuple[bytes, dict[bytes, bytes]]] = await self._call(
            lambda: self.client.xrange(key, count=1)
        )
        return eintraege[0][0].decode() if eintraege else None

    async def trim_by_min_id(self, key: str, min_ms: int) -> int:
        """Alles vor ``min_ms`` entfernen -- die Trimmung nach Alter aus R9.

        Genau, nicht ungefaehr: die Aufbewahrungsdauer ist eine Zusage an den
        Betreiber (KD7), und ein ungefaehrer Schnitt liesse je nach Knotenlage
        mal mehr, mal weniger stehen.
        """
        entfernt: int = await self._call(
            lambda: self.client.xtrim(key, minid=f"{min_ms}-0", approximate=False)
        )
        return entfernt

    async def memory_usage(self, key: str) -> int | None:
        """Speicherbedarf des Stroms in Bytes -- der Fuellstand aus R9."""
        result: Any = await self._call(lambda: self.client.memory_usage(key))
        return int(result) if result is not None else None

    async def delete(self, key: str) -> None:
        """Strom entfernen. Nur fuer Tests und das Aufraeumen."""
        await self._call(lambda: self.client.delete(key))
