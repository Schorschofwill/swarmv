"""U1 -- der Zustandsleser und der Storno-Adapter fuer Workflows.

Der Leser holt den Workflow-Zustand ueber den oeffentlichen Backend-Vertrag
``get_workflow_state`` (KTD1) und gibt ihn als **Zustand** zurueck, nie als
Ausnahme: gelesen, fehlt, nicht lesbar, Backend nicht erreichbar (KTD2).
Gestorniert wird ueber ``SwarmQ.workflow(id).cancel()``; dessen ``bool``
heisst "Merker neu gesetzt", nicht "storniert" (KTD9).

Testliste (AGENTS.md). Erledigtes bleibt stehen, damit die Reihenfolge der
Zyklen nachvollziehbar ist.

Lesen (U1 Punkt 2; R5, R6):
  1. [x] ein laufender Chord mit zwei eingereihten Gliedern -> "gelesen",
         Status running, Form chord, beide Pfade, Topologie als Daten
  2. [x] die Wurzelform wird fuer Signatur, Kette und Gruppe benannt
  3. [x] ein gescheiterter Zustand reicht seinen Fehler weiter
  4. [x] kein Zustand vorhanden -> "fehlt" (R7, R17)

Nicht lesbar, ohne Ausnahme (KTD2):
  5. [x] unbekannte Version ``v`` -> "nicht_lesbar" mit Grund
  6. [x] kaputtes JSON -> "nicht_lesbar"
  7. [x] ein JSON-Wert, der kein Zustand ist (kein Objekt, unbekannter
         Status, unbekannte Form, falsche Feldtypen) -> "nicht_lesbar"

Sperrfrist (KTD2, wie beim Ergebnisleser):
  8. [x] gescheitertes Verbinden -> "nicht_erreichbar"; innerhalb der Frist
         wird nicht erneut gewaehlt
  9. [x] gescheitertes **Lesen** schliesst und sperrt; der zweite Aufruf
         greift nicht zu
 10. [x] nach Ablauf der Frist wird wieder versucht

Stornieren (U1 Punkt 3; R15, R16):
 11. [x] ein laufender Workflow -> "neu_gesetzt", der zweite Aufruf
         "schon_gesetzt" (AE8)
 12. [x] ein verfallener Workflow -> "nicht_gefunden"
 13. [x] ein Backend-Fehler beim Stornieren -> "nicht_erreichbar" und Sperre
 14. [x] waehrend der Sperre wird fuer das Stornieren nicht gewaehlt

Aufraeumen:
 15. [x] ``close()`` schliesst App und eigenen Klienten und wirft nie

Nebenlaeufigkeit (Review-Befund #10):
 16. [x] zwei gleichzeitige erste Lesungen bauen genau eine Verbindung
 17. [x] eine gleichzeitige Lesung und ein Storno teilen die eine Verbindung
 18. [x] scheitert der ``ping()`` des eigenen Klienten, wird er geschlossen
         und nicht behalten
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from swarmq_dashboard.workflows import SwarmQWorkflowleser

WF = "0199aaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"


def _sig(name: str) -> dict[str, Any]:
    return {"kind": "signature", "task": name, "args": [], "kwargs": {}, "immutable": False}


def _chord() -> dict[str, Any]:
    return {
        "kind": "chord",
        "header": {"kind": "group", "members": [_sig("lade"), _sig("lade")]},
        "body": _sig("fasse"),
    }


def _zustand(**aenderungen: Any) -> bytes:
    """Ein Zustand im Schema ``v=1`` der Bibliothek (``workflow_state.py``)."""
    zustand: dict[str, Any] = {
        "v": 1,
        "workflow_id": WF,
        "status": "running",
        "topology": _chord(),
        "final_result_task_id": None,
        "error": None,
        "step_tasks": {"/h/g0": f"{WF}#/h/g0", "/h/g1": f"{WF}#/h/g1"},
    }
    zustand.update(aenderungen)
    return json.dumps(zustand).encode()


class _Backend:
    """Der Backend-Vertrag, soweit der Leser ihn braucht."""

    def __init__(self, roh: bytes | None = None, *, wirft: BaseException | None = None) -> None:
        self._roh = roh
        self._wirft = wirft
        self.rufe: list[str] = []

    async def get_workflow_state(self, wf_id: str, op_timeout: float | None = None) -> bytes | None:
        self.rufe.append(wf_id)
        if self._wirft is not None:
            raise self._wirft
        return self._roh


class _Handle:
    """``WorkflowHandle.cancel()``: ``True`` nur beim ersten Setzen (SET NX)."""

    def __init__(self, merker: set[str], workflow_id: str, *, wirft: BaseException | None) -> None:
        self._merker = merker
        self._id = workflow_id
        self._wirft = wirft

    async def cancel(self) -> bool:
        if self._wirft is not None:
            raise self._wirft
        neu = self._id not in self._merker
        self._merker.add(self._id)
        return neu


class _App:
    """Was der Leser von ``SwarmQ`` braucht: ``backend`` und ``workflow``."""

    def __init__(
        self,
        backend: _Backend | None = None,
        *,
        bekannt: frozenset[str] = frozenset({WF}),
        wirft: BaseException | None = None,
    ) -> None:
        self.backend = backend or _Backend()
        self._bekannt = bekannt
        self._wirft = wirft
        self.merker: set[str] = set()

    async def workflow(self, workflow_id: str) -> _Handle:
        if workflow_id not in self._bekannt:
            from swarmq import WorkflowNotFound

            raise WorkflowNotFound(workflow_id)
        return _Handle(self.merker, workflow_id, wirft=self._wirft)


@pytest.mark.timeout(5)
async def test_ein_laufender_chord_wird_gelesen() -> None:
    """Der einfachste Fall: der Zustand liegt vor und steht als Bild da."""
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", app=_App(_Backend(_zustand())))

    bild = await leser.lesen(WF)

    assert bild.zustand == "gelesen"
    assert bild.status == "running"
    assert bild.form == "chord"
    assert dict(bild.step_tasks) == {"/h/g0": f"{WF}#/h/g0", "/h/g1": f"{WF}#/h/g1"}
    assert bild.topologie == _chord()


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    ("topologie", "form"),
    [
        (_sig("a"), "signatur"),
        ({"kind": "chain", "steps": [_sig("a"), _sig("b")]}, "kette"),
        ({"kind": "group", "members": [_sig("a"), _sig("b")]}, "gruppe"),
    ],
)
async def test_die_wurzelform_wird_benannt(topologie: dict[str, Any], form: str) -> None:
    """R2: Die Liste zeigt die Form der Wurzel, in der Sprache der Ansicht.

    Die Bibliothek schreibt ``kind`` englisch; die Seite spricht von Kette
    und Gruppe. Uebersetzt wird einmal hier, nicht in jeder Vorlage.
    """
    leser = SwarmQWorkflowleser(
        "amqp://x", "valkey://x", app=_App(_Backend(_zustand(topology=topologie))),
    )

    bild = await leser.lesen(WF)

    assert bild.form == form


@pytest.mark.timeout(5)
async def test_ein_gescheiterter_zustand_reicht_seinen_fehler_weiter() -> None:
    """R12: Der Fehlertext steht oben in der Ansicht -- er kommt von hier.

    Der Zustand nennt keinen Schritt, nur ``{"type", "message"}``; welcher
    Schritt scheiterte, weiss das Verzeichnis (U4).
    """
    fehler = {"type": "ValueError", "message": "Datei 17 fehlt"}
    leser = SwarmQWorkflowleser(
        "amqp://x", "valkey://x",
        app=_App(_Backend(_zustand(status="failed", error=fehler))),
    )

    bild = await leser.lesen(WF)

    assert bild.status == "failed"
    assert bild.fehler == fehler


@pytest.mark.timeout(5)
async def test_ein_fehlender_zustand_ist_kein_fehler() -> None:
    """R7, R17: Nie gestartet oder verfallen -- beides heisst hier "fehlt".

    Was daraus wird, "Zustand nie gesehen" oder "Zustand verfallen",
    entscheidet der Abgleich mit seiner Kopie (U3), nicht der Leser.
    """
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", app=_App(_Backend(None)))

    bild = await leser.lesen(WF)

    assert bild.zustand == "fehlt"
    assert bild.status is None
    assert bild.topologie is None


@pytest.mark.timeout(5)
async def test_eine_unbekannte_version_ist_nicht_lesbar() -> None:
    """KTD2: Ein Schema, das diese Fassung nicht kennt, wird nicht geraten.

    Eine kuenftige Bibliothek mag ``status`` anders belegen; die Ansicht
    zeigte dann einen Endstatus, den keine Lesung geliefert hat (R6).
    """
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", app=_App(_Backend(_zustand(v=2))))

    bild = await leser.lesen(WF)

    assert bild.zustand == "nicht_lesbar"
    assert bild.status is None
    assert bild.grund and "2" in bild.grund


@pytest.mark.timeout(5)
@pytest.mark.parametrize("roh", [b"{kaputt", b"\xff\xfe"])
async def test_kaputtes_json_ist_nicht_lesbar(roh: bytes) -> None:
    """KTD2: Kaputte Bytes sind eine Auskunft ueber die Ablage, kein Absturz."""
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", app=_App(_Backend(roh)))

    bild = await leser.lesen(WF)

    assert bild.zustand == "nicht_lesbar"
    assert bild.grund


def _roh(wert: Any) -> bytes:
    return json.dumps(wert).encode()


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    "roh",
    [
        _roh([1, 2]),
        _roh("running"),
        _zustand(status="pausiert"),
        _zustand(topology={"kind": "baum"}),
        _zustand(topology=None),
        _zustand(step_tasks=["/h/g0"]),
        _zustand(step_tasks={"/h/g0": 7}),
        _zustand(error="kaputt"),
    ],
    ids=[
        "liste", "text", "status_unbekannt", "form_unbekannt", "ohne_topologie",
        "step_tasks_liste", "step_tasks_zahl", "fehler_text",
    ],
)
async def test_ein_wert_der_kein_zustand_ist_ist_nicht_lesbar(roh: bytes) -> None:
    """KTD2: Gueltiges JSON ist noch kein Zustand.

    Ein unbekannter Status ist der wichtigste Fall: die Ansicht darf nach
    R6 nur Endstatus zeigen, die eine Lesung geliefert hat, und nicht einen
    Wert weiterreichen, den keine Vorlage kennt.
    """
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", app=_App(_Backend(roh)))

    bild = await leser.lesen(WF)

    assert bild.zustand == "nicht_lesbar"
    assert bild.grund


class _Uhr:
    """Eine Uhr, die nur weitergeht, wenn der Test es sagt."""

    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


class _Verbinden:
    """Ein Verbindungsaufbau, der zaehlt und nach Wunsch scheitert."""

    def __init__(self, app: Any | None = None) -> None:
        self._app = app
        self.versuche = 0

    async def __call__(self) -> Any:
        self.versuche += 1
        if self._app is None:
            raise ConnectionError("Port 6399 zu")
        return self._app


@pytest.mark.timeout(5)
async def test_nach_gescheitertem_verbinden_wird_innerhalb_der_frist_nicht_gewaehlt() -> None:
    """KTD2: Der Abgleich fragt jede Minute je Workflow -- bei totem Backend
    darf nicht jede Frage erneut in die volle Verbindungsfrist laufen."""
    verbinden = _Verbinden()
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())

    erstes = await leser.lesen(WF)
    zweites = await leser.lesen(WF)

    assert erstes.zustand == "nicht_erreichbar"
    assert erstes.grund and "6399" in erstes.grund
    assert zweites.zustand == "nicht_erreichbar"
    assert zweites.grund == erstes.grund
    assert verbinden.versuche == 1


@pytest.mark.timeout(5)
async def test_nach_gescheitertem_lesen_wird_nicht_mehr_zugegriffen() -> None:
    """KTD2: Stirbt das Backend **nach** dem Verbinden, muss die Sperre auch
    greifen. ``connect()`` kehrte sonst weiter sofort zurueck, und jede
    Lesung liefe erneut in die volle Frist."""
    backend = _Backend(wirft=TimeoutError("keine Antwort"))
    verbinden = _Verbinden(_App(backend))
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())

    erstes = await leser.lesen(WF)
    zweites = await leser.lesen(WF)

    assert erstes.zustand == "nicht_erreichbar"
    assert erstes.grund and "keine Antwort" in erstes.grund
    assert zweites.zustand == "nicht_erreichbar"
    assert backend.rufe == [WF]
    assert verbinden.versuche == 1


@pytest.mark.timeout(5)
async def test_nach_ablauf_der_frist_wird_wieder_versucht() -> None:
    """Die Sperre ist eine Pause, kein Urteil: kommt das Backend zurueck,
    liest der Abgleich nach ``SPERRE_S`` wieder."""
    from swarmq_dashboard.workflows import SPERRE_S

    uhr = _Uhr()
    verbinden = _Verbinden()
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=uhr)
    await leser.lesen(WF)

    uhr.jetzt += SPERRE_S - 0.1
    await leser.lesen(WF)
    assert verbinden.versuche == 1

    uhr.jetzt += 0.2
    verbinden._app = _App(_Backend(_zustand()))
    bild = await leser.lesen(WF)

    assert verbinden.versuche == 2
    assert bild.zustand == "gelesen"


@pytest.mark.timeout(5)
async def test_stornieren_setzt_den_merker_und_meldet_eine_bestehende_anforderung() -> None:
    """R16, AE8: Das ``bool`` der Bibliothek heisst "Merker neu gesetzt".

    Der zweite Aufruf ist kein Fehler, sondern die Auskunft, dass schon eine
    Anforderung lief -- die Ansicht zeigt sie, das Audit sagt "lief bereits".
    """
    app = _App()
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", app=app)

    erstes = await leser.stornieren(WF)
    zweites = await leser.stornieren(WF)

    assert erstes.ausgang == "neu_gesetzt"
    assert zweites.ausgang == "schon_gesetzt"
    assert app.merker == {WF}


@pytest.mark.timeout(5)
async def test_stornieren_eines_verfallenen_workflows_ist_nicht_gefunden() -> None:
    """R15: Ist der Zustand weg, gibt es nichts mehr zu stornieren.

    ``SwarmQ.workflow`` wirft dann ``WorkflowNotFound``; die Ansicht meldet
    "nicht mehr stornierbar" und schreibt kein Audit (KTD9, U7).
    """
    app = _App(bekannt=frozenset())
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", app=app)

    bild = await leser.stornieren(WF)

    assert bild.ausgang == "nicht_gefunden"
    assert app.merker == set()


@pytest.mark.timeout(5)
async def test_ein_backend_fehler_beim_stornieren_ist_nicht_erreichbar_und_sperrt() -> None:
    """KTD9: Jeder Ausgang ist benannt, auch der Ausfall mitten im Aufruf.

    Danach gilt dieselbe Sperre wie fuers Lesen: der Abgleich, der dieselbe
    Verbindung nutzt, soll nicht in die volle Frist laufen.
    """
    verbinden = _Verbinden(_App(wirft=ConnectionError("Verbindung verloren")))
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())

    bild = await leser.stornieren(WF)
    danach = await leser.lesen(WF)

    assert bild.ausgang == "nicht_erreichbar"
    assert bild.grund and "Verbindung verloren" in bild.grund
    assert danach.zustand == "nicht_erreichbar"
    assert verbinden.versuche == 1


@pytest.mark.timeout(5)
async def test_waehrend_der_sperre_wird_fuers_stornieren_nicht_gewaehlt() -> None:
    """Die Sperre gilt fuer beide Wege: ein Klick auf "stornieren" bei totem
    Backend antwortet sofort mit "nicht erreichbar"."""
    verbinden = _Verbinden()
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())
    await leser.lesen(WF)

    bild = await leser.stornieren(WF)

    assert bild.ausgang == "nicht_erreichbar"
    assert bild.grund and "6399" in bild.grund
    assert verbinden.versuche == 1


class _SchliessbareApp(_App):
    def __init__(self, *, wirft: bool = False) -> None:
        super().__init__(_Backend(_zustand()))
        self._close_wirft = wirft
        self.geschlossen = 0

    async def close(self) -> None:
        self.geschlossen += 1
        if self._close_wirft:
            raise RuntimeError("schon zu")


@pytest.mark.timeout(5)
@pytest.mark.parametrize("wirft", [False, True], ids=["sauber", "close_wirft"])
async def test_close_schliesst_die_app_und_wirft_nie(wirft: bool) -> None:
    """Gehoert in die Schliessliste von ``Services.shutdown()``: der Leser
    haelt eine eigene Valkey-Verbindung. Ein Fehler beim Schliessen darf das
    Herunterfahren nicht abbrechen."""
    app = _SchliessbareApp(wirft=wirft)
    verbinden = _Verbinden(app)
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())
    await leser.lesen(WF)

    await leser.close()
    await leser.close()

    assert app.geschlossen == 1


class _WartendesVerbinden:
    """Ein Verbindungsaufbau, der haengt, bis der Test ihn freigibt."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.versuche = 0
        self.begonnen = asyncio.Event()
        self.frei = asyncio.Event()

    async def __call__(self) -> Any:
        self.versuche += 1
        self.begonnen.set()
        await self.frei.wait()
        return self._app


@pytest.mark.timeout(5)
async def test_zwei_gleichzeitige_erste_lesungen_bauen_eine_verbindung() -> None:
    """Befund #10: Der Abgleich liest nebenlaeufig. Ohne Riegel baute jede
    erste Lesung einen eigenen Klienten, und ``close()`` schloesse nur den
    letzten -- die anderen leckten."""
    verbinden = _WartendesVerbinden(_App(_Backend(_zustand())))
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())

    erste = asyncio.create_task(leser.lesen(WF))
    zweite = asyncio.create_task(leser.lesen(WF))
    await verbinden.begonnen.wait()
    verbinden.frei.set()
    bilder = await asyncio.gather(erste, zweite)

    assert verbinden.versuche == 1
    assert [b.zustand for b in bilder] == ["gelesen", "gelesen"]


@pytest.mark.timeout(5)
async def test_eine_gleichzeitige_lesung_und_ein_storno_teilen_eine_verbindung() -> None:
    """Befund #10: ``stornieren()`` geht denselben Verbindungsweg und ist
    vom selben Riegel geschuetzt."""
    app = _App(_Backend(_zustand()))
    verbinden = _WartendesVerbinden(app)
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())

    lesung = asyncio.create_task(leser.lesen(WF))
    storno = asyncio.create_task(leser.stornieren(WF))
    await verbinden.begonnen.wait()
    verbinden.frei.set()
    bild, storno_bild = await asyncio.gather(lesung, storno)

    assert verbinden.versuche == 1
    assert bild.zustand == "gelesen"
    assert storno_bild.ausgang == "neu_gesetzt"
    assert app.merker == {WF}


class _KlientOhnePing:
    """Ein Valkey-Klient, dessen ``ping()`` scheitert."""

    def __init__(self) -> None:
        self.geschlossen = 0

    async def ping(self) -> None:
        raise ConnectionError("Port 6399 zu")

    async def aclose(self) -> None:
        self.geschlossen += 1


@pytest.mark.timeout(5)
async def test_ein_gescheiterter_ping_schliesst_den_eigenen_klienten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Befund #10: Der Klient wird erst nach dem ``ping()`` behalten. Scheitert
    er, wird er sofort geschlossen -- und ``close()`` schliesst ihn nicht ein
    zweites Mal."""
    import valkey.asyncio

    klient = _KlientOhnePing()
    monkeypatch.setattr(valkey.asyncio.Valkey, "from_url", lambda *a, **k: klient)
    leser = SwarmQWorkflowleser("amqp://x", "valkey://x", uhr=_Uhr())

    bild = await leser.lesen(WF)
    assert bild.zustand == "nicht_erreichbar"
    assert klient.geschlossen == 1

    await leser.close()
    assert klient.geschlossen == 1
