"""Die gemeinsame Basis der traege verbundenen Valkey-Klienten (KTD2).

Ergebnisleser, Workflowleser und Sollwert-Speicher bauen ihre Verbindung
gleich auf: erst beim ersten Aufruf -- wer die Startseite nur ansieht,
bezahlt keine Backend-Verbindung --, hinter einem Riegel, auf einem
**eigenen** Klienten, der erst nach bestandener Pruefung behalten wird, und
mit einer Sperrfrist nach jedem Fehlversuch.

Die Unterklassen liefern, was sie unterscheidet: den Aufbau ihres Kerns auf
dem Klienten (:meth:`Zugang._kern_auf`), ihren Sperrtext, ihr ``SPERRE_S``
und ihre ``CONNECT_TIMEOUT_S``. Wer den Kern beim Schliessen behalten muss,
ueberschreibt :meth:`Zugang._kern_schliessen`.
"""

from __future__ import annotations

import abc
import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from swarmq_dashboard.stream import StreamUnavailable


class Zugang(abc.ABC):
    """Traege Verbindung zum Backend, mit Riegel und Sperrfrist."""

    #: Wie lange nach einem Fehlversuch nicht erneut gewaehlt wird. Jedes
    #: Modul setzt sein eigenes.
    SPERRE_S: ClassVar[float] = 30.0
    #: Der Grund, den die Seite waehrend der Sperre nennt; ``{exc}`` ist der
    #: Fehlschlag, der sie ausloeste.
    SPERRTEXT: ClassVar[str] = "das Backend ist nicht erreichbar: {exc}"
    #: Frist fuer den Aufbau und fuer jede einzelne Abfrage des Klienten.
    CONNECT_TIMEOUT_S: ClassVar[float] = 2.0

    def __init__(
        self,
        backend_url: str,
        *,
        kern: Any | None = None,
        verbinden: Callable[[], Awaitable[Any]] | None = None,
        uhr: Callable[[], float] | None = None,
    ) -> None:
        self._backend_url = backend_url
        #: Was die Unterklasse benutzt: die App der Bibliothek oder ihr
        #: Sollwert-Speicher.
        self._kern: Any | None = kern
        self._verbinden = verbinden or self._kern_verbinden
        self._uhr = uhr or time.monotonic
        #: Bis wann nicht erneut gewaehlt wird, und warum.
        self._gesperrt_bis = 0.0
        self._grund = ""
        #: Der eigene Klient. Die Bibliothek schliesst einen eingesetzten
        #: Klienten nicht -- das ist ihr Vertrag, und hier unsere Pflicht.
        self._client: Any | None = None
        #: Aufrufe kommen nebenlaeufig: ohne Riegel baute jeder erste Aufruf
        #: einen eigenen Klienten, und ``close()`` schloesse nur den letzten.
        self._verbindungsriegel = asyncio.Lock()

    @abc.abstractmethod
    async def _kern_auf(self, client: Any) -> Any:
        """Den Kern auf dem eigenen Klienten bauen und pruefen.

        Die Pruefung (``ping()``) ist Pflicht: mit eingesetztem Klienten baut
        weder ``from_url`` noch ``connect()`` der Bibliothek eine Verbindung
        auf. Ohne sie meldete der Aufbau Erfolg gegen einen toten Port, und
        der Fehlschlag kaeme erst beim ersten Aufruf -- nach der Sperre statt
        vor ihr.
        """

    async def _kern_verbinden(self) -> Any:
        """Der Kern auf einem **eigenen** Valkey-Klienten.

        Der Klient, den die Bibliothek sich sonst baut, ist fuer den
        Worker-Pfad eingestellt: fuenf Versuche mit Backoff, gemessen 3,2 s
        bis zur Aufgabe. Fuer einen Worker richtig, fuer eine Seite falsch.
        """
        import valkey.asyncio

        client = valkey.asyncio.Valkey.from_url(
            self._backend_url,
            socket_connect_timeout=self.CONNECT_TIMEOUT_S,
            socket_timeout=self.CONNECT_TIMEOUT_S,
        )
        # Erst lokal: behalten wird der Klient nur, wenn der Aufbau auf ihm
        # durchkommt. Sonst schloesse ihn niemand.
        try:
            kern = await self._kern_auf(client)
        except BaseException:
            try:
                await client.aclose()
            except Exception:
                pass
            raise
        self._client = client
        return kern

    async def connect(self) -> Any:
        """Verbindung aufbauen und den Kern liefern. Idempotent. Wirft benannt."""
        if self._kern is not None:
            return self._kern
        async with self._verbindungsriegel:
            # Nach dem Warten erneut pruefen: wer vorher dran war, hat
            # vielleicht schon verbunden -- oder gesperrt.
            if self._kern is not None:
                return self._kern
            if self._uhr() < self._gesperrt_bis:
                raise StreamUnavailable(self._grund)
            try:
                self._kern = await self._verbinden()
            except Exception as exc:
                self._sperren(self.SPERRTEXT.format(exc=exc))
                raise StreamUnavailable(self._grund) from exc
            return self._kern

    async def close(self) -> None:
        """Kern und eigenen Klienten schliessen. Wirft nie.

        Gehoert in die Schliessliste von ``Services.shutdown()``: ohne das
        leckt je Lebenszyklus eine Verbindung.
        """
        await self._kern_schliessen()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def _kern_schliessen(self) -> None:
        """Den Kern verwerfen und schliessen -- synchron oder asynchron."""
        kern, self._kern = self._kern, None
        schliessen = getattr(kern, "close", None)
        if schliessen is None:
            return
        try:
            ergebnis = schliessen()
            if hasattr(ergebnis, "__await__"):
                await ergebnis
        except Exception:
            pass

    async def _ausfall(self, exc: Exception) -> str:
        """Die Leitung ist mitten im Aufruf weggebrochen: schliessen, sperren.

        Hier -- und nicht nur beim Verbindungsaufbau -- muss die Sperre
        greifen. Stirbt das Backend nach dem Verbinden, kehrte ``connect()``
        sonst weiter sofort zurueck, und jeder Aufruf liefe in die volle Frist.
        """
        await self.close()
        self._sperren(self.SPERRTEXT.format(exc=exc))
        return self._grund

    def _sperren(self, grund: str) -> None:
        """Fuer ``SPERRE_S`` nicht mehr waehlen, mit diesem Grund."""
        self._grund = grund
        self._gesperrt_bis = self._uhr() + self.SPERRE_S
