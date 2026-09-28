"""Den Workflow-Zustand lesen und einen Workflow stornieren (U1).

Der Zustand kommt ueber den oeffentlichen Backend-Vertrag
``get_workflow_state``, nicht ueber ``WorkflowHandle`` (KTD1): der Handle
gibt Topologie und Fehler nicht heraus, und ``children()`` laedt alle
Ergebnisse -- die Kopie soll sie nach KD4 gerade nicht enthalten.

Wie beim Ergebnisleser (``web/ergebnis.py``) wird jede Ausnahme zu einem
Zustand (KTD2): Der Abgleich fragt jede Minute, und ein Workflow, dessen
Zustand fehlt oder den diese Fassung nicht versteht, ist eine Auskunft und
kein Absturz.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from swarmq_dashboard.stream import StreamUnavailable
from swarmq_dashboard.workflow_baum import ENDSTATUS as ENDSTATUS
from swarmq_dashboard.workflow_baum import STATUS_RUNNING as STATUS_RUNNING

#: Wie lange nach einem Fehlversuch nicht erneut gewaehlt wird -- derselbe
#: Wert wie beim Ergebnisleser (``web/ergebnis.py``) und beim Kontrollkanal.
SPERRE_S = 30.0

#: Frist fuer den Aufbau und fuer jede einzelne Abfrage, wie beim
#: Ergebnisleser: der Abgleich soll bei einem haengenden Backend nicht
#: minutenlang an einem Workflow stehen.
CONNECT_TIMEOUT_S = 2.0

#: Leere, unveraenderliche Abbildung fuer Bilder ohne Zustand.
_LEER: Mapping[str, str] = MappingProxyType({})

#: Die Schemafassungen, die diese Fassung versteht (``v`` im Zustand). Bewusst
#: hier und nicht aus ``WORKFLOW_STATE_VERSION`` der Bibliothek: hebt eine
#: neue Bibliothek die Fassung, soll das Dashboard "nicht lesbar" sagen,
#: statt ein fremdes Schema nach dem alten zu deuten.
BEKANNTE_VERSIONEN = frozenset({1})

#: Die Status des Schemas ``v=1`` (``workflow_state.py`` der Bibliothek).
#: Das Vokabular steht im reinen Baum-Modul; hier wird es weitergereicht
#: (``ENDSTATUS``, ``STATUS_RUNNING`` -- das ``as`` im Import macht es
#: ausdruecklich).
STATUS = ENDSTATUS | {STATUS_RUNNING}

#: Was der Leser ueber einen Zustand weiss (:attr:`Workflowbild.zustand`).
ZUSTAND_GELESEN = "gelesen"
ZUSTAND_FEHLT = "fehlt"
ZUSTAND_NICHT_LESBAR = "nicht_lesbar"
ZUSTAND_NICHT_ERREICHBAR = "nicht_erreichbar"

#: Wie eine Storno-Anforderung ausging (:attr:`Stornobild.ausgang`).
AUSGANG_NEU_GESETZT = "neu_gesetzt"
AUSGANG_SCHON_GESETZT = "schon_gesetzt"
AUSGANG_NICHT_GEFUNDEN = "nicht_gefunden"
AUSGANG_NICHT_ERREICHBAR = "nicht_erreichbar"

#: Die Wurzelform in der Sprache der Ansicht (R2). ``kind`` ist der
#: Knotentyp aus ``node.to_dict()`` der Bibliothek.
FORMEN: Mapping[str, str] = MappingProxyType({
    "signature": "signatur",
    "chain": "kette",
    "group": "gruppe",
    "chord": "chord",
})


@dataclass(frozen=True)
class Workflowbild:
    """Was der Dienst ueber den Zustand eines Workflows weiss.

    ``zustand`` ist ``gelesen``, ``fehlt``, ``nicht_lesbar`` oder
    ``nicht_erreichbar``; nur ``gelesen`` traegt Status, Form, Topologie,
    ``step_tasks`` und Fehler. ``status`` bleibt der Wert der Bibliothek
    (``running``, ``completed``, ``failed``, ``cancelled``), ``form`` ist der
    Anzeigename aus ``FORMEN``, ``topologie`` das unveraenderte
    ``node.to_dict()``.
    """

    zustand: str
    status: str | None = None
    form: str | None = None
    topologie: Mapping[str, Any] | None = None
    step_tasks: Mapping[str, str] = field(default_factory=lambda: _LEER)
    fehler: Mapping[str, Any] | None = None
    grund: str | None = None


@dataclass(frozen=True)
class Stornobild:
    """Wie eine Storno-Anforderung ausging (KTD9).

    ``neu_gesetzt`` und ``schon_gesetzt`` sagen nur, ob **dieser** Aufruf den
    Merker gesetzt hat. Storniert ist der Workflow erst, wenn der Zustand es
    traegt (R16).
    """

    ausgang: str
    grund: str | None = None


def _nicht_lesbar(grund: str) -> Workflowbild:
    return Workflowbild(zustand=ZUSTAND_NICHT_LESBAR, grund=grund)


def _dekodieren(roh: bytes) -> Workflowbild:
    """Die Bytes des Zustands in ein Bild wandeln -- oder sagen, warum nicht.

    Geprueft wird nur, was die Ansicht braucht: Fassung, Status, die Form der
    Wurzel und die Typen von ``step_tasks`` und ``error``. Die Topologie
    darunter reicht der Leser als Daten weiter; sie zu deuten ist Sache des
    Baum-Moduls (KTD7).
    """
    try:
        daten = json.loads(roh)
    except ValueError as exc:
        # ``JSONDecodeError`` und ``UnicodeDecodeError`` sind beide
        # ``ValueError``: kaputte Bytes wie kaputte Syntax.
        return _nicht_lesbar(f"kein gueltiges JSON: {exc}")
    if not isinstance(daten, dict):
        return _nicht_lesbar(f"kein JSON-Objekt, sondern {type(daten).__name__}")
    if daten.get("v") not in BEKANNTE_VERSIONEN:
        return _nicht_lesbar(f"unbekannte Zustandsfassung v={daten.get('v')!r}")
    status = daten.get("status")
    if status not in STATUS:
        return _nicht_lesbar(f"unbekannter Status {status!r}")
    topologie = daten.get("topology")
    kind = topologie.get("kind") if isinstance(topologie, dict) else None
    if not isinstance(kind, str) or kind not in FORMEN:
        return _nicht_lesbar(f"unbekannte Wurzelform {kind!r}")
    step_tasks = daten.get("step_tasks")
    if not isinstance(step_tasks, dict) or not all(
        isinstance(pfad, str) and isinstance(task_id, str)
        for pfad, task_id in step_tasks.items()
    ):
        return _nicht_lesbar("step_tasks ist keine Abbildung Pfad -> Task-Kennung")
    fehler = daten.get("error")
    if fehler is not None and not isinstance(fehler, dict):
        return _nicht_lesbar(f"error ist kein Objekt, sondern {type(fehler).__name__}")
    return Workflowbild(
        zustand=ZUSTAND_GELESEN,
        status=status,
        form=FORMEN[kind],
        topologie=topologie,
        step_tasks=MappingProxyType(dict(step_tasks)),
        fehler=fehler,
    )


class Workflowleser(Protocol):
    """Was Abgleich und Ansicht vom Workflow-Zustand brauchen."""

    async def lesen(self, workflow_id: str) -> Workflowbild: ...

    async def stornieren(self, workflow_id: str) -> Stornobild: ...


class SwarmQWorkflowleser:
    """Der Leser ueber die oeffentliche API der Bibliothek (KTD1)."""

    def __init__(
        self,
        broker_url: str,
        backend_url: str,
        *,
        app: Any | None = None,
        verbinden: Callable[[], Awaitable[Any]] | None = None,
        uhr: Callable[[], float] | None = None,
    ) -> None:
        self._broker_url = broker_url
        self._backend_url = backend_url
        self._app = app
        self._verbinden = verbinden or self._kern_verbinden
        self._uhr = uhr or time.monotonic
        #: Bis wann nicht erneut gewaehlt wird, und warum.
        self._gesperrt_bis = 0.0
        self._grund = ""
        #: Der eigene Klient. Die Bibliothek schliesst einen eingesetzten
        #: Klienten nicht -- das ist ihr Vertrag, und hier unsere Pflicht.
        self._client: Any | None = None

    async def _kern_verbinden(self) -> Any:
        """Die Bibliothek auf einem **eigenen** Valkey-Klienten.

        Aus demselben Grund wie beim Ergebnisleser: Der Klient, den die
        Bibliothek sich sonst baut, ist fuer den Worker-Pfad eingestellt --
        fuenf Versuche mit Backoff. Der ``ping()`` ist Pflicht, weil mit
        eingesetztem Klienten weder ``from_url`` noch ``connect()`` eine
        Verbindung aufbaut; der Fehlschlag kaeme sonst erst beim Lesen.
        """
        import valkey.asyncio
        from swarmq import SwarmQ
        from swarmq.backend.valkey import ValkeyBackend
        from swarmq.broker.rabbitmq import RabbitMQBroker

        self._client = valkey.asyncio.Valkey.from_url(
            self._backend_url,
            socket_connect_timeout=CONNECT_TIMEOUT_S,
            socket_timeout=CONNECT_TIMEOUT_S,
        )
        await self._client.ping()
        backend = ValkeyBackend(client=self._client)
        # Der Broker wird weder zum Lesen noch zum Stornieren gebraucht --
        # Zustand und Merker liegen im Backend. Das Objekt muss es trotzdem
        # geben, weil die Bibliothek beides zusammen haelt; verbunden wird es
        # nie.
        return SwarmQ(broker=RabbitMQBroker(self._broker_url), backend=backend)

    async def connect(self) -> Any:
        """Verbindung aufbauen und die App liefern. Idempotent. Wirft benannt.

        Traege wie der Ergebnisleser: verbunden wird erst beim ersten Lesen
        oder Stornieren.
        """
        if self._app is not None:
            return self._app
        if self._uhr() < self._gesperrt_bis:
            raise StreamUnavailable(self._grund)
        try:
            self._app = await self._verbinden()
        except Exception as exc:
            self._sperren(f"das Backend ist nicht erreichbar: {exc}")
            raise StreamUnavailable(self._grund) from exc
        return self._app

    async def close(self) -> None:
        """Die Verbindung verwerfen. Wirft nie.

        Gehoert in die Schliessliste von ``Services.shutdown()``, wie der
        Ergebnisleser: ohne das leckt je Lebenszyklus eine Verbindung.
        """
        app, self._app = self._app, None
        schliessen = getattr(app, "close", None)
        if schliessen is not None:
            try:
                await schliessen()
            except Exception:
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    def _sperren(self, grund: str) -> None:
        """Fuer ``SPERRE_S`` nicht mehr waehlen, mit diesem Grund."""
        self._grund = grund
        self._gesperrt_bis = self._uhr() + SPERRE_S

    async def _ausfall(self, exc: Exception) -> str:
        """Die Leitung ist mitten im Aufruf weggebrochen: schliessen, sperren.

        Hier -- und nicht nur beim Verbindungsaufbau -- muss die Sperre
        greifen. Stirbt das Backend nach dem Verbinden, kehrte ``connect()``
        sonst weiter sofort zurueck, und jeder Aufruf liefe in die volle Frist.
        """
        await self.close()
        self._sperren(f"das Backend ist nicht erreichbar: {exc}")
        return self._grund

    async def lesen(self, workflow_id: str) -> Workflowbild:
        """Den Zustand holen und als Bild zurueckgeben."""
        try:
            app = await self.connect()
        except StreamUnavailable as exc:
            return Workflowbild(zustand=ZUSTAND_NICHT_ERREICHBAR, grund=str(exc))
        try:
            roh = await app.backend.get_workflow_state(workflow_id)
        except Exception as exc:
            # Alles hier ist die Leitung, keine Auskunft ueber den Workflow:
            # die Bytes kommen roh, gedeutet wird erst danach.
            return Workflowbild(zustand=ZUSTAND_NICHT_ERREICHBAR, grund=await self._ausfall(exc))
        if roh is None:
            return Workflowbild(zustand=ZUSTAND_FEHLT)
        return _dekodieren(roh)

    async def stornieren(self, workflow_id: str) -> Stornobild:
        """Den Workflow-Merker setzen, ueber ``SwarmQ.workflow(id).cancel()``."""
        from swarmq import WorkflowNotFound

        try:
            app = await self.connect()
        except StreamUnavailable as exc:
            return Stornobild(ausgang=AUSGANG_NICHT_ERREICHBAR, grund=str(exc))
        try:
            # ``workflow()`` liest den Zustand und wirft, wenn er fehlt; erst
            # dann setzt ``cancel()`` den Merker. So entsteht fuer einen
            # verfallenen Workflow kein neuer Merker.
            handle = await app.workflow(workflow_id)
            neu = bool(await handle.cancel())
        except WorkflowNotFound:
            return Stornobild(ausgang=AUSGANG_NICHT_GEFUNDEN)
        except Exception as exc:
            return Stornobild(ausgang=AUSGANG_NICHT_ERREICHBAR, grund=await self._ausfall(exc))
        return Stornobild(ausgang=AUSGANG_NEU_GESETZT if neu else AUSGANG_SCHON_GESETZT)
