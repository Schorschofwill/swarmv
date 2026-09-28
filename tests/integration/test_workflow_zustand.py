"""Der Workflow-Zustandsleser gegen einen echten Valkey (U1).

Was eine Attrappe nicht zeigen kann: dass der Leser das Schema liest, das
``Client.apply`` der Bibliothek tatsaechlich schreibt, und dass das
Stornieren den Merker unter dem Schluessel setzt, den die Worker pruefen.
Angelegt wird ein kleiner Chord ueber swarmq selbst, ohne Worker -- er
bleibt also "running", und genau das ist der Fall, den der Abgleich sieht.

Test-Liste (AGENTS.md):

1. [x] Ein echter Chord wird als "gelesen", laufend, Form chord, mit beiden
       Kopf-Pfaden gelesen; Stornieren setzt den Merker, ein zweites Mal
       meldet es die bestehende Anforderung.
2. [x] Ein Workflow ohne Zustand ist beim Lesen "fehlt" und beim Stornieren
       "nicht_gefunden".
3. [x] Ein totes Backend gibt schnell auf, nicht nach fuenf Versuchen mit
       Backoff.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from swarmq_dashboard.workflows import SwarmQWorkflowleser

pytestmark = pytest.mark.timeout(60)

#: Wie beim Ergebnisleser: laenger als ein Aufbau je dauern darf, kuerzer
#: als die Versuche mit Backoff, die der Kern fuer den Worker-Pfad einstellt.
FRIST_S = 1.0


def _tasks(queue: str) -> list[type[Any]]:
    """Zwei Tasks fuer den Chord, **ohne** globale Registrierung.

    ``Task.__init_subclass__`` traegt jede Klasse mit ``task_info`` in eine
    prozessweite Liste ein, die jede spaeter gebaute App uebernimmt. Das
    ``task_info`` wird darum erst nach der Klasse gesetzt und nur in der
    Registry dieser einen App eingetragen.
    """
    from swarmq.task import Task, TaskInfo

    class Lade(Task):
        async def run(self, n: int) -> int:   # pragma: no cover -- kein Worker
            return n

    class Fasse(Task):
        async def run(self, teile: list[int]) -> int:   # pragma: no cover
            return sum(teile)

    Lade.task_info = TaskInfo(name=f"{queue}_lade", queues=[queue])
    Fasse.task_info = TaskInfo(name=f"{queue}_fasse", queues=[queue])
    return [Lade, Fasse]


@pytest.fixture
async def chord_id(broker_url: str, backend_url: str) -> AsyncIterator[str]:
    """Ein laufender Chord mit zwei Kopf-Gliedern und einem Body.

    Die Queue wird nie deklariert: die Nachrichten der Anfangsfront gehen im
    Standard-Exchange ins Leere, damit kein Worker eines anderen Tests sie
    aufgreift. Der Zustand in Valkey entsteht davor und unabhaengig davon.
    """
    import valkey.asyncio
    from swarmq import RabbitMQBroker, SwarmQ, ValkeyBackend, chord, group, sig

    queue = f"sqd_itest_wf_{uuid.uuid4().hex[:8]}"
    app = SwarmQ(broker=RabbitMQBroker(broker_url), backend=ValkeyBackend(backend_url))
    lade, fasse = _tasks(queue)
    app.registry.register(lade)
    app.registry.register(fasse)
    async with app:
        handle = await app.apply(
            chord(group(sig(f"{queue}_lade", 1), sig(f"{queue}_lade", 2)), sig(f"{queue}_fasse")),
        )
        workflow_id = handle.workflow_id

    yield workflow_id

    client = valkey.asyncio.Valkey.from_url(backend_url)
    try:
        await client.delete(f"sq:wf:{workflow_id}", f"sq:wf:cancel:{workflow_id}")
    finally:
        await client.aclose()


async def test_ein_echter_chord_wird_gelesen_und_storniert(
    broker_url: str, backend_url: str, chord_id: str,
) -> None:
    """R5, R16, AE8 gegen die Schluessel der Bibliothek, nicht gegen eine
    Attrappe."""
    from swarmq.backend.valkey import ValkeyBackend

    leser = SwarmQWorkflowleser(broker_url, backend_url)
    try:
        bild = await leser.lesen(chord_id)
        erstes = await leser.stornieren(chord_id)
        zweites = await leser.stornieren(chord_id)
    finally:
        await leser.close()

    assert bild.zustand == "gelesen", bild.grund
    assert bild.status == "running"
    assert bild.form == "chord"
    # Das Pfadschema der Bibliothek (``workflow_topology.py``): ``h`` ist der
    # Chord-Kopf, ``gN`` das N-te Glied einer Gruppe.
    assert dict(bild.step_tasks) == {
        "/h/g0": f"{chord_id}#/h/g0", "/h/g1": f"{chord_id}#/h/g1",
    }
    assert bild.topologie is not None and bild.topologie["kind"] == "chord"
    assert erstes.ausgang == "neu_gesetzt", erstes.grund
    assert zweites.ausgang == "schon_gesetzt"

    backend = ValkeyBackend(backend_url)
    await backend.connect()
    try:
        assert await backend.is_workflow_cancelled(chord_id) is True
    finally:
        await backend.close()


async def test_ein_workflow_ohne_zustand_fehlt_und_ist_nicht_stornierbar(
    broker_url: str, backend_url: str,
) -> None:
    """R7, R15: ``SwarmQ.workflow`` wirft ``WorkflowNotFound`` -- und das
    Stornieren setzt dabei **keinen** Merker fuer einen Workflow, den es
    nicht mehr gibt."""
    from swarmq.backend.valkey import ValkeyBackend

    workflow_id = str(uuid.uuid4())
    leser = SwarmQWorkflowleser(broker_url, backend_url)
    try:
        bild = await leser.lesen(workflow_id)
        storno = await leser.stornieren(workflow_id)
    finally:
        await leser.close()

    assert bild.zustand == "fehlt", bild.grund
    assert storno.ausgang == "nicht_gefunden", storno.grund

    backend = ValkeyBackend(backend_url)
    await backend.connect()
    try:
        assert await backend.is_workflow_cancelled(workflow_id) is False
    finally:
        await backend.close()


async def test_ein_totes_backend_gibt_schnell_auf(broker_url: str) -> None:
    """KTD2: Der Port ist zu, nichts horcht. Die Antwort muss sofort kommen,
    nicht nach drei Sekunden Backoff -- sonst steht der Abgleich an jedem
    Workflow."""
    leser = SwarmQWorkflowleser(broker_url, "valkey://localhost:6300/0")

    beginn = time.monotonic()
    bild = await leser.lesen(str(uuid.uuid4()))
    gebraucht = time.monotonic() - beginn
    await leser.close()

    assert bild.zustand == "nicht_erreichbar"
    assert bild.grund
    assert gebraucht < FRIST_S, f"der Aufbau dauerte {gebraucht:.1f}s"
