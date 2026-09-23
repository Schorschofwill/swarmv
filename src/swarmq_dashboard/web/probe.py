"""Proben einreihen -- die einzige Stelle, an der das Dashboard Arbeit erzeugt.

Gelesen hat es bisher, und an zwei Stellen eingegriffen: Stornieren setzt
einen Merker, die Dead-Letter-Bedienung reiht um. Beides wirkt auf Arbeit,
die schon da war. Einreihen legt neue in eine Queue, und ein Formularfehler
wird damit zu echter Last auf fremden Workern.

Darum sitzt die Pruefung **vor** dem Senden, nicht im Task: Was hier
abgewiesen wird, belegt keinen Slot und taucht in keiner Statistik auf.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from swarmq_dashboard.probe import MAX_DAUER_S, PROBE_NAME, PROBE_QUEUES
from swarmq_dashboard.stream import StreamUnavailable

#: Wie viele Proben ein Formular auf einmal losschicken darf. Genug, um
#: einen Rueckstau zu sehen; zu wenig, um versehentlich eine Flotte
#: lahmzulegen.
MAX_ANZAHL = 200
#: Laengste Verzoegerung. Darueber hinaus ist ein Zeitpunkt (``eta``) das
#: ehrlichere Mittel -- eine Verzoegerung von Tagen liest niemand mehr als
#: Zahl in Sekunden.
MAX_DELAY_S = 86_400.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProbeAuftrag:
    """Was ein Absender je Einreihung bestimmen kann.

    Nur das steht hier drin. Fristen, Wiederholungen, Ratenbegrenzung,
    Sperren, Verfall und Priorität gehoeren zur Registrierung des Tasks
    (``TaskInfo``) und nicht zur einzelnen Einreihung -- die Bibliothek
    nimmt sie bei ``schedule()`` gar nicht entgegen.
    """

    #: Ziel-Queue. Sie entscheidet, welcher Worker die Probe bekommt --
    #: ``io`` fuer wartende, ``cpu`` fuer rechnende Arbeit.
    queue: str = "io"
    anzahl: int = 1
    sleep_s: float = 0.0
    burn_s: float = 0.0
    fail: bool = False
    note: str = ""
    #: Verzoegerung in Sekunden **oder** ein Zeitpunkt -- nie beides.
    delay_s: float | None = None
    eta: datetime | None = None
    #: Entdoppelung (R3.5 im Kern): innerhalb dieser Frist gibt eine zweite
    #: gleiche Einreihung die Kennung der ersten zurueck.
    unique_for: int | None = None
    unique_until: str | None = None


class ProbeFehleingabe(ValueError):
    """Die Eingabe taugt nicht -- abgewiesen, bevor etwas in die Queue geht."""


class SchedulingClient(Protocol):
    """Was der Sender von der Bibliothek braucht."""

    async def schedule(self, task_name: str, **kwargs: Any) -> str: ...


class ProbeSender:
    """Reiht Proben ein. Prueft zuerst, sendet dann."""

    def __init__(self, *, client: SchedulingClient | None = None,
                 broker_url: str = "", backend_url: str = "",
                 stream_url: str = "") -> None:
        self._client = client
        self._broker_url = broker_url
        self._backend_url = backend_url
        #: Der Ereignisstrom des Dashboards. Ohne ihn kennt das Verzeichnis
        #: die Probe erst ab dem Aufgreifen -- ohne Einreihungszeitpunkt und
        #: ohne Argumente, und damit ohne die Notiz, nach der man sie sucht.
        self._stream_url = stream_url
        self._eigen = client is None
        self._app: Any = None
        self._puffer: Any = None

    async def senden(self, auftrag: ProbeAuftrag) -> list[str]:
        """Den Auftrag pruefen und einreihen. Gibt die Kennungen zurueck."""
        pruefen(auftrag)
        client = await self._verbunden()
        argumente: dict[str, Any] = {
            "queue": auftrag.queue,
            "sleep_s": auftrag.sleep_s,
            "burn_s": auftrag.burn_s,
            "fail": auftrag.fail,
            "note": auftrag.note,
        }
        if auftrag.delay_s:
            argumente["delay"] = auftrag.delay_s
        if auftrag.eta is not None:
            argumente["eta"] = auftrag.eta
        if auftrag.unique_for is not None:
            argumente["unique_for"] = auftrag.unique_for
            if auftrag.unique_until:
                argumente["unique_until"] = auftrag.unique_until
        return [await client.schedule(PROBE_NAME, **argumente) for _ in range(auftrag.anzahl)]

    async def _verbunden(self) -> SchedulingClient:
        """Die Verbindung, traege aufgebaut. Wirft benannt.

        Traege wie beim Stornieren und bei der Dead-Letter-Bedienung: Wer
        die Seite nur oeffnet, soll keine Broker-Verbindung bezahlen. Der
        Registry-Zwang der Bibliothek greift hier nicht -- das Dashboard
        kennt den Task-Code, der Worker fuehrt ihn aus.
        """
        if self._client is not None:
            return self._client
        try:
            from swarmq import SwarmQ
            from swarmq.backend.valkey import ValkeyBackend
            from swarmq.broker.rabbitmq import RabbitMQBroker

            broker = RabbitMQBroker(self._broker_url)
            backend = ValkeyBackend(self._backend_url)
            await broker.connect()
            await backend.connect()
            self._app = (broker, backend)
            app = SwarmQ(broker=broker, backend=backend)
            self._hook_anhaengen(app)
            self._client = app
        except Exception as exc:
            await self.close()
            raise StreamUnavailable(f"der Broker ist nicht erreichbar: {exc}") from exc
        return self._client

    def _hook_anhaengen(self, app: Any) -> None:
        """Den Anwendungs-Hook anmelden, damit die Probe ab der Einreihung zaehlt.

        Ohne ihn steht die Probe erst in der Liste, wenn ein Worker sie
        aufgreift -- dann ohne Einreihungszeitpunkt und ohne Argumente. Die
        Notiz, die das Formular als Wiedererkennung verspricht, waere
        nirgends zu finden; im Betrieb genau so aufgefallen.

        Der Puffer wird **vorher** gesetzt, statt ihn den Hook aus der
        Umgebung ableiten zu lassen: der Dienst kennt seine Stromadresse aus
        der eigenen Einstellung, und zwei Wege zu derselben Angabe laufen
        frueher oder spaeter auseinander.
        """
        if not self._stream_url:
            return
        try:
            from swarmq_dashboard.buffer import shared_buffer
            from swarmq_dashboard.hooks.app import DEFAULT_CAPACITY, install

            self._puffer = shared_buffer(self._stream_url, capacity=DEFAULT_CAPACITY)
            app.hook_buffer = self._puffer
            install(app, source="dashboard")
        except Exception:
            # Wie jeder Hook: er darf die Einreihung nicht aufhalten. Ohne
            # ihn fehlt die Vorgeschichte, die Probe laeuft trotzdem.
            logger.warning("probe_hook_nicht_angehaengt", exc_info=True)

    async def close(self) -> None:
        """Die selbst aufgebauten Verbindungen schliessen. Wirft nie."""
        if self._puffer is not None:
            try:
                await self._puffer.flush_once()
            except Exception:
                pass
            self._puffer = None
        if self._app is not None:
            for teil in self._app:
                try:
                    await teil.close()
                except Exception:
                    pass
            self._app = None
        if self._eigen:
            self._client = None


def pruefen(auftrag: ProbeAuftrag) -> None:
    """Alles abweisen, was keine Arbeit werden soll. Wirft ``ProbeFehleingabe``."""
    if auftrag.queue not in PROBE_QUEUES:
        erlaubt = ", ".join(PROBE_QUEUES)
        raise ProbeFehleingabe(f"Queue {auftrag.queue!r} ist keine der angemeldeten ({erlaubt}).")
    if not 1 <= auftrag.anzahl <= MAX_ANZAHL:
        raise ProbeFehleingabe(f"Die Anzahl muss zwischen 1 und {MAX_ANZAHL} liegen.")
    for name, wert in (("sleep_s", auftrag.sleep_s), ("burn_s", auftrag.burn_s)):
        if wert < 0 or wert > MAX_DAUER_S:
            raise ProbeFehleingabe(
                f"{name} muss zwischen 0 und {MAX_DAUER_S:g} Sekunden liegen."
            )
    if auftrag.delay_s is not None and auftrag.eta is not None:
        raise ProbeFehleingabe(
            "Verzögerung und Zeitpunkt zugleich gehen nicht -- die Bibliothek "
            "nimmt entweder das eine oder das andere."
        )
    if auftrag.delay_s is not None and not 0 <= auftrag.delay_s <= MAX_DELAY_S:
        raise ProbeFehleingabe(f"Die Verzögerung muss zwischen 0 und {MAX_DELAY_S:g} s liegen.")
    if auftrag.unique_until and auftrag.unique_until not in ("start", "completion"):
        raise ProbeFehleingabe(
            "Die Entdoppelung endet entweder beim Start oder beim Abschluss."
        )
    if auftrag.unique_until and auftrag.unique_for is None:
        raise ProbeFehleingabe(
            "Ohne Frist gibt es keine Entdoppelung -- unique_for fehlt."
        )
