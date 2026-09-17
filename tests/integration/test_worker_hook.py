"""Der Worker-Hook gegen echte Dienste.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Der Hook laesst den Worker unveraendert ===
1. Ein Worker mit Hook verarbeitet Tasks und fuellt den Strom.
2. Derselbe Worker ohne Hook laeuft unveraendert und schreibt nichts.
3. Ein Task, der nach dem Herunterfahr-Signal fertig wird, erscheint im Strom.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from swarmq import RabbitMQBroker, Signal, SwarmQ, ValkeyBackend

from swarmq_dashboard.events import decode
from swarmq_dashboard.hooks.worker import META_EVENT, setup
from swarmq_dashboard.stream import ValkeyStreamClient

pytestmark = pytest.mark.integration


@pytest.fixture
async def strom(stream_url: str) -> AsyncIterator[tuple[ValkeyStreamClient, str]]:
    key = f"sqd:test:hook:{uuid.uuid4().hex[:8]}"
    c = ValkeyStreamClient(stream_url)
    await c.connect()
    yield c, key
    await c.delete(key)
    await c.close()


def _app(broker_url: str, backend_url: str, **cfg: object) -> SwarmQ:
    return SwarmQ(broker=RabbitMQBroker(broker_url), backend=ValkeyBackend(backend_url), **cfg)


@pytest.mark.usefixtures("stream_env")
async def test_hook_fuellt_den_strom(
    broker_url: str, backend_url: str, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, key = strom
    app = _app(broker_url, backend_url, queues=["reports"], concurrency=2)
    setup(app)
    puffer = app.hook_buffer  # type: ignore[attr-defined]
    assert puffer is not None
    puffer._key = key  # in den Testschluessel umlenken
    puffer._client = client

    await app._signal_dispatcher.dispatch(Signal.WORKER_ONLINE, worker_id="phx-test:0")
    await app._signal_dispatcher.dispatch(
        Signal.TASK_STARTED, task_id="a41f", task_name="render_pdf", queue="reports"
    )
    await puffer.flush_once()
    await puffer.stop()

    gelesen = [decode(r) for r in await client.read_all(key)]
    arten = [e.type for e in gelesen]
    assert META_EVENT in arten, "Stammdaten fehlen — die Worker-Seite haette keine Queue-Menge"
    assert "task_started" in arten
    meta = next(e for e in gelesen if e.type == META_EVENT)
    assert meta.payload["group"] == "reports"
    assert "reports.high" in meta.payload["all_queues"]


@pytest.mark.usefixtures("stream_env")
async def test_ohne_hook_bleibt_der_strom_leer(
    broker_url: str, backend_url: str, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, key = strom
    app = _app(broker_url, backend_url, queues=["reports"])
    # kein setup() — der Worker laeuft wie vorher
    await app._signal_dispatcher.dispatch(
        Signal.TASK_STARTED, task_id="a41f", task_name="render_pdf", queue="reports"
    )
    assert await client.length(key) == 0


@pytest.mark.usefixtures("stream_env")
async def test_ausgang_nach_dem_herunterfahr_signal_erscheint(
    broker_url: str, backend_url: str, strom: tuple[ValkeyStreamClient, str]
) -> None:
    """swarmq verschickt WORKER_SHUTDOWN vor dem Auslaufen der Tasks."""
    client, key = strom
    app = _app(broker_url, backend_url, queues=["reports"])
    setup(app)
    puffer = app.hook_buffer  # type: ignore[attr-defined]
    puffer._key = key
    puffer._client = client
    await puffer.start()

    await app._signal_dispatcher.dispatch(Signal.WORKER_SHUTDOWN, worker_id="phx-test:0")
    # Erst danach laeuft der Task aus — genau die Reihenfolge der Bibliothek.
    await app._signal_dispatcher.dispatch(
        Signal.TASK_COMPLETED, task_id="spaet", task_name="render_pdf", queue="reports", duration_ms=12.0
    )
    deadline = asyncio.get_running_loop().time() + 3.0
    while asyncio.get_running_loop().time() < deadline:
        if any(b"spaet" in r for r in await client.read_all(key)):
            break
        await asyncio.sleep(0.05)
    await puffer.stop()

    gelesen = [decode(r) for r in await client.read_all(key)]
    assert any(e.payload.get("task_id") == "spaet" for e in gelesen), (
        "der Ausgang des auslaufenden Tasks fehlt — der Flusher wurde zu frueh beendet"
    )
