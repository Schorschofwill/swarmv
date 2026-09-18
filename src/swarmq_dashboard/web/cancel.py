"""Stornieren -- eine Anforderung, kein Vollzug (R12, R12a).

Der Rueckgabewert der Bibliothek bedeutet **"Merker neu gesetzt"**, nicht
"storniert": ``SwarmQ.cancel`` setzt ein Flag in Valkey und kehrt zurueck,
ohne auf einen Worker zu warten. ``False`` heisst "es lief schon eine
Anforderung" -- eine Auskunft, kein Fehler. Und ein rechnender Task bemerkt
die Anforderung moeglicherweise nie.

Die Ansicht sagt darum "Stornierung angefordert" und wechselt erst mit dem
Signal auf den Ausgang. Alles andere waere eine Behauptung ueber etwas, das
nur der Worker weiss.
"""

from __future__ import annotations

from typing import Protocol

from swarmq_dashboard.stream import StreamUnavailable


class Canceller(Protocol):
    """Was die Ansicht vom Stornieren braucht."""

    async def cancel(self, task_id: str) -> bool: ...


class SwarmQCanceller:
    """Stornieren ueber die oeffentliche Cancel-Funktion der Bibliothek (R1a).

    Der Merker liegt im **Backend** der Bibliothek (Valkey, Index 0), nicht im
    Ereignisstrom des Dashboards (Index 1) -- deshalb eine eigene Verbindung.
    """

    def __init__(self, broker_url: str, backend_url: str) -> None:
        self._broker_url = broker_url
        self._backend_url = backend_url
        self._app: object | None = None
        self._backend: object | None = None

    async def connect(self) -> None:
        """Verbindung aufbauen. Idempotent. Wirft benannt."""
        if self._app is not None:
            return
        try:
            from swarmq import SwarmQ
            from swarmq.backend.valkey import ValkeyBackend
            from swarmq.broker.rabbitmq import RabbitMQBroker

            backend = ValkeyBackend(self._backend_url)
            await backend.connect()
            # Der Broker wird fuer das Stornieren nicht gebraucht -- der Merker
            # liegt im Backend. Das Objekt muss es trotzdem geben, weil die
            # Bibliothek beides zusammen haelt.
            self._app = SwarmQ(broker=RabbitMQBroker(self._broker_url), backend=backend)
            self._backend = backend
        except Exception as exc:
            await self.close()
            raise StreamUnavailable(f"das Backend ist nicht erreichbar: {exc}") from exc

    async def close(self) -> None:
        if self._backend is not None:
            try:
                await self._backend.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._app = None
        self._backend = None

    async def cancel(self, task_id: str) -> bool:
        """Den Merker setzen. ``False`` heisst: lief schon."""
        if self._app is None:
            raise StreamUnavailable("das Backend ist nicht verbunden")
        return bool(await self._app.cancel(task_id))  # type: ignore[attr-defined]
