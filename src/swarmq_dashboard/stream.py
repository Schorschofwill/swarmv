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

from typing import Any

from valkey.asyncio import Valkey

#: Feldname der Nutzlast im Stromeintrag. Ein Feld genuegt -- die Huelle ist
#: bereits kodiert, und Valkey-Streams sind Feld-Wert-Paare.
FIELD = "e"


class ValkeyStreamClient:
    """Duenne Schreib- und Leseschicht ueber einem Valkey-Strom."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._client: Valkey | None = None

    async def connect(self) -> None:
        """Verbindung aufbauen. Idempotent."""
        if self._client is None:
            self._client = Valkey.from_url(self._url)

    async def close(self) -> None:
        """Verbindung schliessen. Idempotent."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> Valkey:
        """Roher Client. Wirft, solange nicht verbunden."""
        if self._client is None:
            raise RuntimeError("ValkeyStreamClient ist nicht verbunden")
        return self._client

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
        await pipe.execute()

    # -- Lesen -----------------------------------------------------------

    async def read_all(self, key: str) -> list[bytes]:
        """Alle Eintraege in Stromreihenfolge. Fuer Tests und den Neuaufbau."""
        eintraege: list[tuple[bytes, dict[bytes, bytes]]] = await self.client.xrange(key)
        return [felder[FIELD.encode()] for _, felder in eintraege]

    async def length(self, key: str) -> int:
        """Zahl der Eintraege."""
        result: int = await self.client.xlen(key)
        return result

    async def memory_usage(self, key: str) -> int | None:
        """Speicherbedarf des Stroms in Bytes -- der Fuellstand aus R9."""
        result: Any = await self.client.memory_usage(key)
        return int(result) if result is not None else None

    async def delete(self, key: str) -> None:
        """Strom entfernen. Nur fuer Tests und das Aufraeumen."""
        await self.client.delete(key)
