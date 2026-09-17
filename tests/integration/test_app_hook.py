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

from swarmq_dashboard.events import Origin, decode
from swarmq_dashboard.hooks.app import ENQUEUE_EVENT, install
from swarmq_dashboard.stream import ValkeyStreamClient

pytestmark = pytest.mark.integration


@pytest.fixture
async def strom(stream_url: str) -> AsyncIterator[tuple[ValkeyStreamClient, str]]:
    key = f"sqd:test:app:{uuid.uuid4().hex[:8]}"
    c = ValkeyStreamClient(stream_url)
    await c.connect()
    yield c, key
    await c.delete(key)
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
    client, key = strom
    install(app)
    puffer = app.hook_buffer  # type: ignore[attr-defined]
    puffer._key, puffer._client = key, client

    task_id = await app.schedule("sqd_render_pdf", 812)
    assert task_id, "die Einreihung selbst muss unveraendert gelingen"

    await puffer.flush_once()
    gelesen = [decode(r) for r in await client.read_all(key)]
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
    client, key = strom
    install(app)
    puffer = app.hook_buffer  # type: ignore[attr-defined]
    puffer._key, puffer._client = key, client

    await app.schedule("sqd_render_pdf", 813)
    await puffer.flush_once()

    gelesen = [decode(r) for r in await client.read_all(key)]
    # Kein Worker lief: es gibt ausschliesslich die Einreihung, kein Aufgreifen.
    assert [e.type for e in gelesen] == [ENQUEUE_EVENT]
    assert all(e.origin is Origin.ENQUEUE for e in gelesen)


@pytest.mark.usefixtures("stream_env")
async def test_stapel_erzeugt_je_element_ein_ereignis(
    app: SwarmQ, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, key = strom
    install(app)
    puffer = app.hook_buffer  # type: ignore[attr-defined]
    puffer._key, puffer._client = key, client

    await app.schedule_many([("sqd_render_pdf", (i,), {}) for i in range(5)])
    await puffer.flush_once()

    gelesen = [decode(r) for r in await client.read_all(key)]
    einreihungen = [e for e in gelesen if e.type == ENQUEUE_EVENT]
    assert len(einreihungen) == 5
    assert sorted(e.payload["args"][0] for e in einreihungen) == [0, 1, 2, 3, 4]
