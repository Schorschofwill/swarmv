"""Der Anwendungs-Hook gegen echte Dienste.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Einreihen bleibt unveraendert ===
1. Eine echte Einreihung gelingt mit Hook genauso wie ohne.
2. Der Task steht im Strom, bevor ein Worker ihn aufgreift (KD12).
3. Eine Stapel-Einreihung erzeugt je erfolgreichem Element genau ein Ereignis.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from swarmq import RabbitMQBroker, SwarmQ, Task, TaskInfo, ValkeyBackend

from swarmq_dashboard.events import TASK_STREAM as STREAM
from swarmq_dashboard.events import Origin, decode
from swarmq_dashboard.hooks.app import ENQUEUE_EVENT, install, shutdown
from swarmq_dashboard.stream import ValkeyStreamClient

pytestmark = pytest.mark.integration


@pytest.fixture
async def strom(stream_url: str) -> AsyncIterator[tuple[ValkeyStreamClient, str]]:
    c = ValkeyStreamClient(stream_url)
    await c.connect()
    await c.delete(STREAM)     # Rueckstand eines Vorgaengers wegraeumen
    yield c, STREAM
    await c.delete(STREAM)
    await c.close()


#: Die Task-Klasse muss stehen, BEVOR die App gebaut wird -- die Registry
#: entsteht bei der Konstruktion aus den bis dahin bekannten Tasks.
QUEUE = f"sqd_test_{uuid.uuid4().hex[:8]}"


class RenderPdf(Task):
    task_info = TaskInfo(name="sqd_render_pdf", queues=[QUEUE])

    async def run(self, report_id: int) -> int:
        return report_id


@pytest.fixture
async def app(broker_url: str, backend_url: str) -> AsyncIterator[SwarmQ]:
    instanz = SwarmQ(
        broker=RabbitMQBroker(broker_url),
        backend=ValkeyBackend(backend_url),
        queues=[QUEUE],
    )
    async with instanz:
        yield instanz


@pytest.mark.usefixtures("stream_env")
async def test_einreihen_gelingt_mit_hook(
    app: SwarmQ, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, _ = strom
    install(app)
    task_id = await app.schedule("sqd_render_pdf", 812)
    assert task_id, "die Einreihung selbst muss unveraendert gelingen"

    await shutdown(app)
    gelesen = [decode(r) for r in await client.read_all(STREAM)]
    einreihungen = [e for e in gelesen if e.type == ENQUEUE_EVENT]
    assert len(einreihungen) == 1
    p = einreihungen[0].payload
    assert p["task_id"] == task_id
    assert p["task_name"] == "sqd_render_pdf"
    assert p["args"] == [812]
    assert einreihungen[0].origin is Origin.ENQUEUE


@pytest.mark.usefixtures("stream_env")
async def test_task_steht_im_strom_vor_dem_aufgreifen(
    app: SwarmQ, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, _ = strom
    install(app)
    await app.schedule("sqd_render_pdf", 813)
    await shutdown(app)

    gelesen = [decode(r) for r in await client.read_all(STREAM)]
    # Kein Worker lief: es gibt ausschliesslich die Einreihung, kein Aufgreifen.
    assert [e.type for e in gelesen] == [ENQUEUE_EVENT]
    assert all(e.origin is Origin.ENQUEUE for e in gelesen)


@pytest.mark.usefixtures("stream_env")
async def test_stapel_erzeugt_je_element_ein_ereignis(
    app: SwarmQ, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, _ = strom
    install(app)
    await app.schedule_many([("sqd_render_pdf", (i,), {}) for i in range(5)])
    await shutdown(app)

    gelesen = [decode(r) for r in await client.read_all(STREAM)]
    einreihungen = [e for e in gelesen if e.type == ENQUEUE_EVENT]
    assert len(einreihungen) == 5
    assert sorted(e.payload["args"][0] for e in einreihungen) == [0, 1, 2, 3, 4]


# === Befund 1 aus der Codepruefung Stufe A ==============================
#
# 4. Eine reine Anwendung schreibt OHNE Handverdrahtung in den Strom.
#    Die drei Tests oben setzen _key und _client selbst und rufen flush_once()
#    von Hand -- genau der fehlende Lebenszyklus war damit verdeckt.


@pytest.mark.usefixtures("stream_env")
async def test_reine_anwendung_schreibt_ohne_handverdrahtung(
    app: SwarmQ, stream_url: str
) -> None:
    """Die dokumentierte eine Zeile muss genuegen (R3a, Abnahme U4)."""
    import asyncio

    install(app)                       # nichts sonst
    for i in range(3):
        await app.schedule("sqd_render_pdf", 900 + i)

    c = ValkeyStreamClient(stream_url)
    await c.connect()
    try:
        # **Nach Art zaehlen, nicht nach Stromlaenge.** Die Anwendung
        # schreibt nicht nur Einreihungen: der Scheduler von swarmq legt in
        # unregelmaessigen Abstaenden ein ``scheduler_heartbeat`` dazu. Die
        # erste Fassung mass die Laenge des Stroms und verlangte dann von
        # **jedem** Eintrag die Herkunft "einreihung" -- der Test fiel damit
        # ungefaehr jeden zehnten Lauf um, je nachdem, ob der Herzschlag in
        # die fuenf Sekunden fiel. Ein Test, der manchmal besteht, prueft
        # nichts (AGENTS.md).
        deadline = asyncio.get_running_loop().time() + 5.0
        einreihungen: list[object] = []
        while asyncio.get_running_loop().time() < deadline:
            gelesen = [decode(r) for r in await c.read_all(STREAM)]
            einreihungen = [e for e in gelesen if e.type == ENQUEUE_EVENT]
            if len(einreihungen) >= 3:
                break
            await asyncio.sleep(0.1)
        assert len(einreihungen) == 3, (
            f"nur {len(einreihungen)} von 3 Einreihungen erreichten den Strom"
        )
        assert all(e.origin is Origin.ENQUEUE for e in einreihungen)
    finally:
        # Erst den Puffer beenden, dann loeschen -- sonst schreibt stop() noch
        # in einen Strom, den der Test fuer geleert haelt.
        await shutdown(app)
        await c.delete(STREAM)
        await c.close()
