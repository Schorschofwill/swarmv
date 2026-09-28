"""Der Abgleich: die Kopie des Workflow-Zustands laufend nachfuehren (U3).

Der Abgleich liest den Zustand im Takt und nach Ereignissen (KTD3), schreibt
jede Lesung in die Kopie des Verzeichnisses und meldet jede Aenderung der
Kopie an die offenen Ansichten (KTD10). Der Leser ist gefaelscht, das
Verzeichnis echt (eine Datei in ``tmp_path``). Zeit vergeht nicht: das
Schlafen ist ein Tor, das der Test oeffnet.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Eine Lesung schreibt die Kopie ===
1. Ein laufender Workflow, dessen Lesung "completed" ergibt, steht danach
   als fertig in der Kopie, und genau eine Meldung geht hinaus.
2. Eine Lesung, die nichts aendert, meldet nichts -- sonst laedt jede
   offene Seite jede Minute je offenem Workflow neu.

=== ZYKLUS 2: Der Zustand fehlt (R7, R17, KTD5) ===
3. Ein zuletzt laufend gesehener Workflow, dessen Lesung "fehlt" ergibt,
   wird verfallen, mit dem Zeitpunkt der letzten Lesung.
4. Covers AE6. Ein bestaetigter Workflow, dessen erste Lesung "fehlt"
   ergibt, bleibt "nie gesehen" und bekommt keinen Endstatus.
5. Ein unbestaetigter Kandidat, dessen Lesung "fehlt" ergibt, wird
   verworfen und erscheint nie in der Liste.

=== ZYKLUS 3: Stoerungen aendern nichts ===
6. "Backend nicht erreichbar" aendert die Kopie nicht und steht als Grund
   da; die naechste gelungene Lesung raeumt ihn weg.
7. "Nicht lesbar" ueberschreibt nichts und wird protokolliert.

=== ZYKLUS 4: Der Takt ===
8. Der Takt liest alle offenen Workflows; einer mit Endstatus wird nicht
   mehr gelesen.
9. Verfallene und nie gesehene liest der Takt nicht mehr -- ihr Zustand
   ist fort, und jede Minute danach zu fragen kostet bei vielen
   Workflows ohne Auskunft.

=== ZYKLUS 5: Lesen nach Ereignissen (KTD3) ===
10. Ein Anstoss liest sofort und ein zweites Mal nach zwei Sekunden; die
    zweite Lesung sieht den inzwischen geschriebenen Endstatus.
11. Viele Anstoesse in kurzer Folge -- die Glieder einer grossen Gruppe --
    fuehren zu hoechstens einer sofortigen und einer verzoegerten Lesung.
12. Ein Anstoss waehrend der Wartezeit fuehrt zu einer weiteren
    verzoegerten Lesung danach, damit auch sein Schritt gesehen wird.
13. Ein Ergebnis-Ereignis eines Schritts mit ``workflow_id`` loest einen
    Anstoss aus.
14. Ein Ergebnis-Ereignis eines Schritts, der nur ueber das Muster
    erkennbar ist, loest ebenfalls einen Anstoss aus.
15. Ein ``workflow_step_enqueued`` loest einen Anstoss aus.
16. Ein Ereignis ohne Ergebnis (``task_started``) und ein Task ohne
    Workflow loesen keinen aus.

12a. Ein Fehler in einer angestossenen Lesung -- etwa ein geschlossenes
     Verzeichnis -- verschwindet nicht in einer Hintergrundaufgabe, sondern
     steht als Grund da.

=== ZYKLUS 6: Der zuletzt angezeigte Zustand (KTD6) ===
17. Nach einer Lesung "running" mit eigener Stornierung und ohne aktiven
    Schritt steht "angefordert_nichts_aktiv" als zuletzt angezeigt; nach
    dem Verfall bleibt er stehen.
17a. Eine Topologie, aus der der Baum nichts ableiten kann, haelt die
     Lesung nicht auf: die Kopie steht, die Meldung geht hinaus.

=== ZYKLUS 7: Anhalten ===
18. Nach dem Anhalten liest keine ausstehende verzoegerte Lesung mehr,
    kein neuer Anstoss und kein Takt -- das Herunterfahren schliesst
    danach das Verzeichnis.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from swarmq_dashboard.directory.abgleich import Abgleich
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin
from swarmq_dashboard.workflows import Stornobild, Workflowbild

pytestmark = pytest.mark.timeout(5)

WF = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
WF2 = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c"
JETZT = 1_700_000_000.0

_TOPO = {"kind": "chain", "steps": [
    {"kind": "signature", "task": "download", "args": [], "kwargs": {}, "immutable": False},
    {"kind": "signature", "task": "merge", "args": [], "kwargs": {}, "immutable": False}]}


def _bild(status: str, **kw: Any) -> Workflowbild:
    werte: dict[str, Any] = {"zustand": "gelesen", "status": status, "form": "kette",
                             "topologie": _TOPO, "step_tasks": {}, "fehler": None}
    werte.update(kw)
    return Workflowbild(**werte)


class FakeLeser:
    """Liefert je Workflow das eingestellte Bild und zaehlt die Lesungen."""

    def __init__(self) -> None:
        self.bilder: dict[str, Workflowbild] = {}
        self.gelesen: list[str] = []
        self._neu = asyncio.Condition()

    async def lesen(self, workflow_id: str) -> Workflowbild:
        self.gelesen.append(workflow_id)
        async with self._neu:
            self._neu.notify_all()
        return self.bilder.get(workflow_id, Workflowbild(zustand="fehlt"))

    async def stornieren(self, workflow_id: str) -> Stornobild:
        raise AssertionError("der Abgleich storniert nie")

    async def warte_auf(self, anzahl: int) -> None:
        """Bis mindestens *anzahl* Lesungen geschehen sind -- ohne zu schlafen."""
        async with self._neu:
            await self._neu.wait_for(lambda: len(self.gelesen) >= anzahl)


class Tor:
    """Das Schlafen des Abgleichs: kehrt erst zurueck, wenn der Test oeffnet."""

    def __init__(self) -> None:
        self.offen = asyncio.Event()
        self.gewartet: list[float] = []
        self._neu = asyncio.Condition()

    async def __call__(self, sekunden: float) -> None:
        self.gewartet.append(sekunden)
        async with self._neu:
            self._neu.notify_all()
        await self.offen.wait()

    async def warte_auf(self, anzahl: int) -> None:
        async with self._neu:
            await self._neu.wait_for(lambda: len(self.gewartet) >= anzahl)


@pytest.fixture
def writer(tmp_path: Path) -> Iterator[DirectoryWriter]:
    w = DirectoryWriter(connect(tmp_path / "v.sqlite3"))
    yield w
    w.close()


def _sig(tid: str, typ: str, ts: float = JETZT, **kw: object) -> Event:
    return Event(type=typ, ts=ts, source="phx-w1:0", origin=Origin.PICKUP,
                 payload={"task_id": tid, "task_name": "download", "queue": "q", **kw})


def _bestaetigt(w: DirectoryWriter, wf: str = WF) -> None:
    """Ein Workflow, den ein Ereignis mit ``workflow_id`` angelegt hat."""
    w.apply([(None, _sig(f"{wf}#/0", "task_started", workflow_id=wf, step_path="/0"))])


def _kandidat(w: DirectoryWriter, wf: str = WF) -> None:
    """Ein Workflow, den nur das Muster der Kennung kennt."""
    w.apply([(None, _sig(f"{wf}#/0", "task_started"))])


class _Melder:
    def __init__(self) -> None:
        self.gemeldet: list[str] = []

    async def __call__(self, workflow_id: str) -> None:
        self.gemeldet.append(workflow_id)


def _abgleich(w: DirectoryWriter, leser: FakeLeser, *, tor: Tor | None = None,
              melder: _Melder | None = None) -> Abgleich:
    return Abgleich(w, leser, melden=melder, uhr=lambda: JETZT + 50,
                    schlafen=tor or Tor())


# --- Zyklus 1: Eine Lesung schreibt die Kopie ------------------------------


async def test_lesung_completed_steht_als_fertig_in_der_kopie(writer: DirectoryWriter) -> None:
    """Covers R5, R6, R14."""
    _bestaetigt(writer)
    leser, melder = FakeLeser(), _Melder()
    abgleich = _abgleich(writer, leser, melder=melder)
    leser.bilder[WF] = _bild("running")
    await abgleich.lesen(WF)
    melder.gemeldet.clear()

    leser.bilder[WF] = _bild("completed")
    await abgleich.lesen(WF)

    wf = writer.workflow(WF)
    assert (wf["status"], wf["sichtbarkeit"], wf["gelesen_am"]) == ("completed", "gesehen", JETZT + 50)
    assert melder.gemeldet == [WF], "genau eine Live-Meldung je Aenderung"


async def test_lesung_ohne_aenderung_meldet_nichts(writer: DirectoryWriter) -> None:
    """Covers R14. Der Takt liest jede Minute jeden offenen Workflow; eine
    Meldung je Lesung liesse jede offene Seite ebenso oft neu laden."""
    _bestaetigt(writer)
    leser, melder = FakeLeser(), _Melder()
    abgleich = _abgleich(writer, leser, melder=melder)
    leser.bilder[WF] = _bild("running", step_tasks={"/0": f"{WF}#/0"})
    await abgleich.lesen(WF)
    await abgleich.lesen(WF)
    assert melder.gemeldet == [WF]


# --- Zyklus 2: Der Zustand fehlt --------------------------------------------


async def test_zuletzt_laufend_gesehen_und_fehlt_wird_verfallen(writer: DirectoryWriter) -> None:
    """Covers R17. Die Ansicht nennt "zuletzt: laeuft" mit dem Zeitpunkt
    der letzten Lesung -- nicht mit dem der Lesung, die nichts fand."""
    _bestaetigt(writer)
    leser, melder = FakeLeser(), _Melder()
    leser.bilder[WF] = _bild("running")
    await Abgleich(writer, leser, uhr=lambda: 500.0, schlafen=Tor()).lesen(WF)

    del leser.bilder[WF]
    await _abgleich(writer, leser, melder=melder).lesen(WF)

    wf = writer.workflow(WF)
    assert (wf["sichtbarkeit"], wf["status"], wf["gelesen_am"]) == ("verfallen", "running", 500.0)
    assert melder.gemeldet == [WF]


async def test_bestaetigt_und_nie_gelesen_bleibt_nie_gesehen(writer: DirectoryWriter) -> None:
    """Covers AE6, R7. Kein Endstatus, den keine Lesung geliefert hat (R6)."""
    _bestaetigt(writer)
    await _abgleich(writer, FakeLeser()).lesen(WF)
    wf = writer.workflow(WF)
    assert (wf["sichtbarkeit"], wf["status"], wf["gelesen_am"]) == ("nie_gesehen", None, None)


async def test_unbestaetigter_kandidat_der_fehlt_wird_verworfen(writer: DirectoryWriter) -> None:
    """Covers R1, KTD5. Eine eigene Kennung mit Raute, zu der es nie einen
    Zustand gab, erscheint nie in der Liste."""
    _kandidat(writer)
    _bestaetigt(writer, WF2)
    abgleich = _abgleich(writer, FakeLeser())
    await abgleich.lesen(WF)
    await abgleich.lesen(WF2)
    assert writer.workflow(WF) is None
    assert writer.workflow(WF2) is not None, "ein bestaetigter Workflow wird nie verworfen"


# --- Zyklus 3: Stoerungen aendern nichts ------------------------------------


async def test_nicht_erreichbar_aendert_nichts_und_steht_als_grund_da(
    writer: DirectoryWriter,
) -> None:
    """Covers R5, R33. Ein weggebrochenes Backend ist keine Auskunft ueber
    den Workflow: weder "verfallen" noch ein Verwerfen des Kandidaten."""
    _bestaetigt(writer)
    _kandidat(writer, WF2)
    leser, melder = FakeLeser(), _Melder()
    abgleich = _abgleich(writer, leser, melder=melder)
    leser.bilder[WF] = _bild("running")
    await abgleich.lesen(WF)
    vorher = dict(writer.workflow(WF))
    melder.gemeldet.clear()

    weg = Workflowbild(zustand="nicht_erreichbar", grund="das Backend ist nicht erreichbar: weg")
    leser.bilder[WF] = leser.bilder[WF2] = weg
    await abgleich.lesen(WF)
    await abgleich.lesen(WF2)

    assert dict(writer.workflow(WF)) == vorher
    assert writer.workflow(WF2) is not None, "der Kandidat wurde ohne Auskunft verworfen"
    assert melder.gemeldet == []
    assert abgleich.grund == "das Backend ist nicht erreichbar: weg"

    leser.bilder[WF] = _bild("running")
    await abgleich.lesen(WF)
    assert abgleich.grund is None, "der Grund blieb nach der Rueckkehr stehen"


async def test_nicht_lesbar_ueberschreibt_nichts_und_wird_protokolliert(
    writer: DirectoryWriter, caplog: pytest.LogCaptureFixture,
) -> None:
    """Covers R6, KTD2. Eine neue Zustandsfassung der Bibliothek darf eine
    gute Kopie nicht mit Leerem ueberschreiben -- und still bleiben darf sie
    auch nicht."""
    _bestaetigt(writer)
    leser, melder = FakeLeser(), _Melder()
    abgleich = _abgleich(writer, leser, melder=melder)
    leser.bilder[WF] = _bild("running", step_tasks={"/0": f"{WF}#/0"})
    await abgleich.lesen(WF)
    vorher = dict(writer.workflow(WF))
    melder.gemeldet.clear()

    leser.bilder[WF] = Workflowbild(zustand="nicht_lesbar", grund="unbekannte Zustandsfassung v=2")
    with caplog.at_level(logging.WARNING, logger="swarmq_dashboard.directory.abgleich"):
        await abgleich.lesen(WF)

    assert dict(writer.workflow(WF)) == vorher
    assert melder.gemeldet == []
    assert any(WF in r.getMessage() and "v=2" in r.getMessage() for r in caplog.records)


# --- Zyklus 4: Der Takt -----------------------------------------------------


async def test_takt_liest_offene_und_nicht_beendete(writer: DirectoryWriter) -> None:
    """Covers R5, R6, KTD3. Ein Endstatus ist die Auskunft fuer die ganze
    Aufbewahrung; ihn weiter zu lesen verlaengerte nur die Frist in Valkey."""
    _bestaetigt(writer)
    _kandidat(writer, WF2)
    leser = FakeLeser()
    abgleich = _abgleich(writer, leser)
    leser.bilder[WF] = _bild("completed")
    leser.bilder[WF2] = _bild("running")

    await abgleich.alle_lesen()
    assert sorted(leser.gelesen) == sorted([WF, WF2])

    leser.gelesen.clear()
    await abgleich.alle_lesen()
    assert leser.gelesen == [WF2]


async def test_takt_liest_verfallene_und_nie_gesehene_nicht_mehr(
    writer: DirectoryWriter,
) -> None:
    """Covers R7, R17. Ihr Zustand ist fort; ein neues Ereignis eines ihrer
    Schritte stoesst eine Lesung trotzdem an (Zyklus 5)."""
    _bestaetigt(writer)
    _bestaetigt(writer, WF2)
    leser = FakeLeser()
    abgleich = _abgleich(writer, leser)
    leser.bilder[WF] = _bild("running")
    await abgleich.lesen(WF)
    del leser.bilder[WF]
    await abgleich.lesen(WF)       # verfallen
    await abgleich.lesen(WF2)      # nie gesehen
    leser.gelesen.clear()

    await abgleich.alle_lesen()
    assert leser.gelesen == []


# --- Zyklus 5: Lesen nach Ereignissen ---------------------------------------


async def test_anstoss_liest_sofort_und_nach_zwei_sekunden(writer: DirectoryWriter) -> None:
    """Covers R6, R14, KTD3. Das Ergebnis eines Schritts kommt vor dem
    Schreiben des Zustands an; die erste Lesung sieht noch "running"."""
    _bestaetigt(writer)
    leser, tor = FakeLeser(), Tor()
    abgleich = _abgleich(writer, leser, tor=tor)
    leser.bilder[WF] = _bild("running")

    abgleich.anstossen(WF)
    await leser.warte_auf(1)
    await tor.warte_auf(1)
    assert writer.workflow(WF)["status"] == "running"
    assert tor.gewartet == [2.0]

    leser.bilder[WF] = _bild("completed")
    tor.offen.set()
    await abgleich.abwarten()
    assert leser.gelesen == [WF, WF]
    assert writer.workflow(WF)["status"] == "completed"


async def test_viele_anstoesse_ergeben_hoechstens_zwei_lesungen(writer: DirectoryWriter) -> None:
    """Covers R14, KTD3. Eine Gruppe mit 500 Gliedern meldet 500 Ergebnisse
    in kurzer Folge; zwei Lesungen je Glied waeren tausend Lesungen desselben
    Zustands."""
    _bestaetigt(writer)
    leser, tor = FakeLeser(), Tor()
    abgleich = _abgleich(writer, leser, tor=tor)
    leser.bilder[WF] = _bild("running")

    for _ in range(50):
        abgleich.anstossen(WF)
    tor.offen.set()
    await abgleich.abwarten()
    assert leser.gelesen == [WF, WF]


async def test_anstoss_waehrend_der_wartezeit_liest_danach_noch_einmal(
    writer: DirectoryWriter,
) -> None:
    """Covers R6, KTD3. Zusammengefasst in die schon wartende Lesung, saehe
    der spaete Schritt womoeglich nur Sekundenbruchteile Vorlauf -- zu wenig,
    damit die Bibliothek den Zustand geschrieben hat."""
    _bestaetigt(writer)
    leser, tor = FakeLeser(), Tor()
    abgleich = _abgleich(writer, leser, tor=tor)
    leser.bilder[WF] = _bild("running")

    abgleich.anstossen(WF)
    await tor.warte_auf(1)
    await leser.warte_auf(1)
    abgleich.anstossen(WF)
    await leser.warte_auf(2)
    tor.offen.set()
    await abgleich.abwarten()
    assert tor.gewartet == [2.0, 2.0], "nach dem spaeten Anstoss wurde nicht erneut gewartet"
    assert len(leser.gelesen) == 4


async def _eingearbeitet(w: DirectoryWriter, abgleich: Abgleich, ev: Event) -> None:
    """Wie der Strom-Leser: erst einarbeiten, dann melden."""
    w.apply([(None, ev)])
    await abgleich.eingearbeitet([(None, ev)])


async def test_ergebnis_eines_schritts_mit_workflow_id_stoesst_an(
    writer: DirectoryWriter,
) -> None:
    """Covers R14, KTD3."""
    leser, tor = FakeLeser(), Tor()
    abgleich = _abgleich(writer, leser, tor=tor)
    await _eingearbeitet(writer, abgleich, _sig("eigene-kennung", "task_failed",
                                                workflow_id=WF, step_path="/0"))
    await leser.warte_auf(1)
    assert leser.gelesen == [WF]
    tor.offen.set()
    await abgleich.abwarten()


@pytest.mark.parametrize(("task_id", "typ", "extra"), [
    # Ohne Anwendungs-Hook ist die erste Front nur ueber das Muster erkennbar
    # -- der Hauptweg, kein Randfall (KTD5).
    (f"{WF}#/h/g3", "task_completed", {}),
    (f"{WF}#/h/g4", "task_dlq", {}),
    (f"{WF}#/h/g5", "task_cancelled", {}),
    (f"{WF}#/b", "workflow_step_enqueued", {"workflow_id": WF, "step_path": "/b"}),
], ids=["muster-fertig", "muster-dead-letter", "muster-storniert", "folgeschritt"])
async def test_weitere_anstoesse(writer: DirectoryWriter, task_id: str, typ: str,
                                 extra: dict[str, object]) -> None:
    """Covers R14, KTD3, KTD5."""
    leser, tor = FakeLeser(), Tor()
    abgleich = _abgleich(writer, leser, tor=tor)
    await _eingearbeitet(writer, abgleich, _sig(task_id, typ, **extra))
    await leser.warte_auf(1)
    assert leser.gelesen == [WF]
    tor.offen.set()
    await abgleich.abwarten()


@pytest.mark.parametrize(("task_id", "typ"), [
    (f"{WF}#/0", "task_started"),
    ("rechnung-7", "task_completed"),
], ids=["kein-ergebnis", "kein-workflow"])
async def test_kein_anstoss(writer: DirectoryWriter, task_id: str, typ: str) -> None:
    """Ein Start aendert den Zustand nicht; ein Task ohne Workflow hat keinen."""
    leser = FakeLeser()
    abgleich = _abgleich(writer, leser)
    await _eingearbeitet(writer, abgleich, _sig(task_id, typ))
    await abgleich.abwarten()
    assert leser.gelesen == []


# --- Zyklus 6: Der zuletzt angezeigte Zustand -------------------------------


async def test_zuletzt_angezeigt_ueberdauert_den_verfall(writer: DirectoryWriter) -> None:
    """Covers R16, R17, KTD6. "Zustand verfallen, zuletzt: Stornierung
    angefordert, nichts mehr aktiv" -- die eigene Stornierung bleibt
    sichtbar, auch wenn der Zustand danach verfaellt."""
    _bestaetigt(writer)
    writer.apply([(None, _sig(f"{WF}#/0", "task_cancelled", ts=JETZT + 1,
                              workflow_id=WF, step_path="/0"))])
    writer.vermerke_workflow_storno(WF, jetzt=JETZT + 2)
    leser = FakeLeser()
    abgleich = _abgleich(writer, leser)
    leser.bilder[WF] = _bild("running", step_tasks={"/0": f"{WF}#/0"})
    await abgleich.lesen(WF)
    assert writer.workflow(WF)["zuletzt_angezeigt"] == "angefordert_nichts_aktiv"

    del leser.bilder[WF]
    await abgleich.lesen(WF)
    wf = writer.workflow(WF)
    assert (wf["sichtbarkeit"], wf["zuletzt_angezeigt"]) == ("verfallen", "angefordert_nichts_aktiv")


async def test_unverstandene_topologie_haelt_die_lesung_nicht_auf(
    writer: DirectoryWriter, caplog: pytest.LogCaptureFixture,
) -> None:
    """Covers R5. Der Leser prueft nur die Wurzelform; was darunter steht,
    deutet erst der Baum. Ein Fehler dort darf die Kopie nicht verhindern."""
    _bestaetigt(writer)
    leser, melder = FakeLeser(), _Melder()
    abgleich = _abgleich(writer, leser, melder=melder)
    leser.bilder[WF] = _bild("completed", topologie={"kind": "chain"})
    with caplog.at_level(logging.WARNING, logger="swarmq_dashboard.directory.abgleich"):
        await abgleich.lesen(WF)
    assert writer.workflow(WF)["status"] == "completed"
    assert melder.gemeldet == [WF]
    assert any(WF in r.getMessage() for r in caplog.records)


async def test_fehler_einer_angestossenen_lesung_steht_als_grund_da(tmp_path: Path) -> None:
    """Covers R33. Eine Hintergrundaufgabe, die an einer Ausnahme endet,
    schweigt sonst -- und die Kopie bliebe ohne Hinweis stehen."""
    w = DirectoryWriter(connect(tmp_path / "v.sqlite3"))
    _bestaetigt(w)
    leser, tor = FakeLeser(), Tor()
    tor.offen.set()
    abgleich = _abgleich(w, leser, tor=tor)
    leser.bilder[WF] = _bild("running")
    w.close()
    abgleich.anstossen(WF)
    await abgleich.abwarten()
    assert abgleich.grund is not None and "Workflow" in abgleich.grund


# --- Zyklus 7: Anhalten -----------------------------------------------------


async def test_nach_dem_anhalten_wird_nichts_mehr_gelesen(writer: DirectoryWriter) -> None:
    """Das Herunterfahren schliesst danach das Verzeichnis; eine Lesung, die
    dann noch schreibt, trifft eine geschlossene Verbindung (``app.py``,
    ``stop_tasks``)."""
    _bestaetigt(writer)
    leser, tor = FakeLeser(), Tor()
    abgleich = _abgleich(writer, leser, tor=tor)
    leser.bilder[WF] = _bild("running")
    abgleich.anstossen(WF)
    await tor.warte_auf(1)
    await leser.warte_auf(1)

    abgleich.anhalten()
    tor.offen.set()
    await abgleich.abwarten()
    abgleich.anstossen(WF)
    await abgleich.alle_lesen()
    await abgleich.abwarten()
    assert leser.gelesen == [WF]
