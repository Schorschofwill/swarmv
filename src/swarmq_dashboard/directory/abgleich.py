"""Der Abgleich: die Kopie des Workflow-Zustands nachfuehren (U3).

Nur der Zustand in Valkey kennt Topologie, Fehler und das Ende eines
Workflows (R6); der Strom meldet Schritte, nicht Workflows. Der Abgleich
liest ihn darum selbst und schreibt jede Lesung in die Kopie des
Verzeichnisses (KTD4) -- im Takt fuer alle offenen Workflows und nach jedem
Ergebnis eines Schritts gleich zweimal (KTD3): die Ereignisse eines Schritts
kommen vor dem Schreiben des Zustands an, eine Lesung nur dann bliebe auf
"laeuft" stehen.

Jede Aenderung der Kopie geht an die offenen Ansichten (KTD10): sie hat kein
Strom-Ereignis, das sie sonst wecken wuerde.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any

from swarmq_dashboard.directory.writer import (
    SICHT_NIE_GESEHEN,
    SICHT_VERFALLEN,
    DirectoryWriter,
)
from swarmq_dashboard.workflow_baum import Workflowkopie, baue_baum, taskzeile_aus
from swarmq_dashboard.workflows import (
    ZUSTAND_FEHLT,
    ZUSTAND_NICHT_ERREICHBAR,
    ZUSTAND_NICHT_LESBAR,
    Workflowbild,
    Workflowleser,
)

logger = logging.getLogger("swarmq_dashboard.directory.abgleich")

#: Abstand der zweiten Lesung nach einem Ereignis (KTD3).
NACHLESEN_S = 2.0

#: Ereignisse, nach denen der Zustand neu gelesen wird (KTD3): die Ergebnisse
#: eines Schritts und das Einreihen eines Folgeschritts durch einen Worker.
ANSTOSS_ARTEN = frozenset({
    "task_completed", "task_failed", "task_dlq", "task_cancelled", "workflow_step_enqueued",
})

#: Sichtbarkeiten, bei denen der Takt nicht mehr fragt.
_ZUSTAND_FORT = frozenset({SICHT_VERFALLEN, SICHT_NIE_GESEHEN})

#: Meldung einer geaenderten Kopie an die Live-Schicht.
Melder = Callable[[str], Awaitable[None]]


class Abgleich:
    """Liest Workflow-Zustaende und schreibt sie in die Kopie."""

    def __init__(
        self,
        writer: DirectoryWriter,
        leser: Workflowleser,
        *,
        melden: Melder | None = None,
        uhr: Callable[[], float] = time.time,
        schlafen: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._writer = writer
        self._leser = leser
        self._melden = melden
        self._uhr = uhr
        self._schlafen = schlafen
        #: Warum zuletzt nicht gelesen werden konnte, fuer ``status()``;
        #: ``None`` nach einer Lesung, die das Backend erreicht hat.
        self.grund: str | None = None
        #: Die laufenden Lesungen aus Anstoessen. Gehalten, damit keine
        #: Aufgabe unbemerkt verschwindet und das Anhalten sie findet.
        self._aufgaben: set[asyncio.Task[None]] = set()
        #: Gesetzt beim Herunterfahren: danach schreibt keine Lesung mehr.
        self._halt = False
        #: Workflows mit ausstehender sofortiger und verzoegerter Lesung.
        self._sofort: set[str] = set()
        #: Je Workflow mit wartender verzoegerter Lesung: ob danach noch eine
        #: Runde noetig ist.
        self._verzoegert: dict[str, bool] = {}

    async def eingearbeitet(self, ereignisse: Sequence[tuple[str | None, Any]]) -> None:
        """Die Naht zum Strom-Leser: nach dem Einarbeiten gerufen (KTD3).

        Ein Ergebnis eines Workflow-Schritts oder ein vom Worker eingereihter
        Folgeschritt stoesst eine Lesung an. Ob ein Task ein Schritt ist,
        steht in seiner Zeile: der Eingang traegt ``workflow_id`` aus der
        Nutzlast oder aus dem Muster ``<uuid>#<pfad>`` ein (KTD5).
        """
        kennungen = [
            str(ev.payload.get("task_id") or "")
            for _sid, ev in ereignisse
            if ev.type in ANSTOSS_ARTEN
        ]
        if not kennungen:
            return
        for workflow_id in await asyncio.to_thread(self._writer.workflows_der_tasks, kennungen):
            self.anstossen(workflow_id)

    def anstossen(self, workflow_id: str) -> None:
        """Einen Workflow jetzt und in ``NACHLESEN_S`` Sekunden lesen (KTD3).

        Die zweite Lesung sieht, was die Bibliothek nach dem Ergebnis des
        Schritts in den Zustand schreibt: das Ereignis kommt vorher an.

        Zusammengefasst je Workflow: steht eine sofortige oder eine
        verzoegerte Lesung noch aus, kommt keine zweite dazu. Sonst loesten
        die 500 Glieder einer Gruppe tausend Lesungen desselben Zustands aus.
        """
        if self._halt:
            return
        if workflow_id not in self._sofort:
            self._sofort.add(workflow_id)
            self._starten(workflow_id, self._jetzt(workflow_id))
        if workflow_id in self._verzoegert:
            self._verzoegert[workflow_id] = True
        else:
            self._verzoegert[workflow_id] = False
            self._starten(workflow_id, self._spaeter(workflow_id))

    def _starten(self, workflow_id: str, lauf: Coroutine[Any, Any, None]) -> None:
        aufgabe = asyncio.ensure_future(self._behuetet(workflow_id, lauf))
        self._aufgaben.add(aufgabe)
        aufgabe.add_done_callback(self._aufgaben.discard)

    async def _behuetet(self, workflow_id: str, lauf: Coroutine[Any, Any, None]) -> None:
        """Eine Hintergrundlesung, deren Fehler nicht verschwindet.

        Eine Aufgabe, die an einer Ausnahme endet, schweigt sonst bis zur
        Muellabfuhr; der Takt faengt seine Fehler selbst, diese Aufgaben
        haben niemanden.
        """
        try:
            await lauf
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.grund = f"Workflow {workflow_id} nicht abgeglichen: {exc}"
            logger.warning("%s", self.grund)

    async def _jetzt(self, workflow_id: str) -> None:
        # Ausstehend ist sie nur bis zum Beginn: ein Anstoss waehrend der
        # Lesung kann Neues bedeuten, das sie nicht mehr sieht.
        self._sofort.discard(workflow_id)
        await self.lesen(workflow_id)

    async def _spaeter(self, workflow_id: str) -> None:
        """Die verzoegerte Lesung, so oft wie noetig, aber nie zwei zugleich.

        Ein Anstoss, der waehrend der Wartezeit oder der Lesung kommt, haette
        sonst womoeglich nur Sekundenbruchteile Vorlauf -- er bekommt eine
        weitere Runde. Anstoesse vor Beginn der Wartezeit deckt diese selbst.
        """
        try:
            while True:
                self._verzoegert[workflow_id] = False
                await self._schlafen(NACHLESEN_S)
                await self.lesen(workflow_id)
                if not self._verzoegert.get(workflow_id):
                    return
        finally:
            self._verzoegert.pop(workflow_id, None)

    def anhalten(self) -> None:
        """Keine Lesung mehr beginnen. Ausstehende kehren ohne Lesung zurueck.

        Das Herunterfahren schliesst danach das Verzeichnis; eine Lesung, die
        dann noch schreibt, traefe eine geschlossene Verbindung.
        """
        self._halt = True

    async def abwarten(self) -> None:
        """Bis alle angestossenen Lesungen durch sind."""
        while self._aufgaben:
            await asyncio.wait(set(self._aufgaben))

    async def alle_lesen(self) -> None:
        """Ein Takt: jeden offenen Workflow einmal lesen (KTD3).

        Nacheinander, nicht gleichzeitig: der Leser sperrt sich nach einem
        Fehlschlag selbst, und hundert gleichzeitige Lesungen gegen ein
        totes Backend liefen alle in die volle Frist.
        """
        offen = await asyncio.to_thread(self._writer.offene_workflows)
        for wf in offen:
            # Verfallen und nie gesehen heisst: der Zustand ist fort. Ihn
            # jede Minute erneut zu erfragen kostete bei vielen Workflows
            # ohne Auskunft; ein neues Ereignis stoesst eine Lesung an.
            if wf.sichtbarkeit in _ZUSTAND_FORT:
                continue
            await self.lesen(wf.workflow_id)

    async def lesen(self, workflow_id: str) -> None:
        """Den Zustand eines Workflows lesen und die Kopie nachfuehren."""
        if self._halt:
            return
        bild = await self._leser.lesen(workflow_id)
        if bild.zustand == ZUSTAND_NICHT_ERREICHBAR:
            # Keine Auskunft ueber den Workflow, sondern ueber die Leitung:
            # die Kopie bleibt, wie sie ist, und die Kopfzeile nennt es.
            self.grund = bild.grund or "das Backend ist nicht erreichbar"
            return
        self.grund = None
        if bild.zustand == ZUSTAND_NICHT_LESBAR:
            # Die Bytes sind da, aber diese Fassung versteht sie nicht. Die
            # Kopie behaelt, was sie aus einer verstandenen Lesung weiss.
            logger.warning("Workflow-Zustand %s nicht lesbar: %s", workflow_id, bild.grund)
            return
        geaendert = await asyncio.to_thread(self._einarbeiten, workflow_id, bild)
        if geaendert and self._melden is not None:
            await self._melden(workflow_id)

    def _einarbeiten(self, workflow_id: str, bild: Workflowbild) -> bool:
        """Die Lesung schreiben. Im Arbeitsfaden. ``True``, wenn sich die
        Kopie geaendert hat."""
        vorher = _stand(self._writer.workflow(workflow_id))
        if bild.zustand == ZUSTAND_FEHLT:
            # Ein Kandidat ohne Zustand war nie ein Workflow (KTD5), ein
            # bestaetigter wird verfallen oder nie gesehen -- in einem Zug.
            # Gemeldet wird unten ueber den Vergleich der Kopie.
            self._writer.vermerke_fehlend(workflow_id)
        else:
            if self._writer.schreibe_lesung(
                workflow_id, status=bild.status or "", wurzel=bild.form,
                topologie=bild.topologie, step_tasks=bild.step_tasks, fehler=bild.fehler,
                gelesen_am=self._uhr(),
            ):
                self._anzeige_vermerken(workflow_id)
        return _stand(self._writer.workflow(workflow_id)) != vorher

    def _anzeige_vermerken(self, workflow_id: str) -> None:
        """Den Anzeige-Code nach dieser Lesung festhalten (KTD6).

        Aus demselben reinen Modul wie die Ansicht, damit "zuletzt: ..."
        nach dem Verfall genau das nennt, was vorher dastand -- auch
        "Stornierung angefordert, nichts mehr aktiv", das von den Schritten
        abhaengt.
        """
        zeile = self._writer.workflow(workflow_id)
        if zeile is None:
            return
        kopie = Workflowkopie(
            workflow_id=workflow_id,
            topologie=json.loads(zeile["topologie"]) if zeile["topologie"] else None,
            step_tasks=json.loads(zeile["step_tasks"]),
            status=zeile["status"],
            sichtbarkeit=zeile["sichtbarkeit"],
            storno_angefordert_at=zeile["cancel_requested_at"],
            gelesen_at=zeile["gelesen_am"],
        )
        schritte = [taskzeile_aus(z) for z in self._writer.workflow_schritte(workflow_id)]
        try:
            code = baue_baum(kopie, schritte).status.code
        except Exception as exc:
            # Der Leser prueft nur die Wurzelform; was darunter steht, deutet
            # erst der Baum. Die Kopie steht dann trotzdem -- nur ohne Stand
            # fuer "zuletzt: ...".
            logger.warning("Workflow %s: Anzeige nicht ableitbar: %r", workflow_id, exc)
            return
        self._writer.vermerke_anzeige(workflow_id, code)


def _stand(zeile: sqlite3.Row | None) -> tuple[object, ...] | None:
    """Was die Ansicht aus der Kopie zeigt -- ohne den Zeitpunkt der Lesung.

    Der Zeitpunkt aendert sich mit jeder Lesung; zaehlte er, meldete jede
    Lesung eine Aenderung.
    """
    if zeile is None:
        return None
    # ``sqlite3.Row`` liefert beim Iterieren Werte, nicht Schluessel.
    return tuple(zeile[k] for k in zeile.keys() if k != "gelesen_am")  # noqa: SIM118
