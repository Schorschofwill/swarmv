"""Der Schreibweg gegen einen echten Valkey.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Aufnahme ===
1. Geschriebene Ereignisse erscheinen im Strom in Aufnahmereihenfolge.
2. Die Nutzlast ueberlebt die Runde durch Valkey unveraendert.

=== ZYKLUS 2: Laengendeckel (R9) ===
3. Der Deckel des Schreibers greift auch ohne laufenden Dienst.
4. Der Deckel liegt auf dem eigenen Praefix, nicht auf dem der Bibliothek.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from swarmq_dashboard.buffer import EventBuffer
from swarmq_dashboard.events import Event, Origin, decode
from swarmq_dashboard.stream import ValkeyStreamClient

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(stream_url: str) -> AsyncIterator[ValkeyStreamClient]:
    c = ValkeyStreamClient(stream_url)
    await c.connect()
    yield c
    await c.close()


@pytest.fixture
def key() -> str:
    return f"sqd:test:{uuid.uuid4().hex[:8]}"


def _ev(i: int) -> Event:
    return Event(type="task_started", ts=float(i), source="phx-w1:0", origin=Origin.PICKUP,
                 payload={"task_id": f"t{i}", "kwargs": {"report_id": 800 + i}})


async def test_ereignisse_erscheinen_in_aufnahmereihenfolge(client: ValkeyStreamClient, key: str) -> None:
    puffer = EventBuffer(capacity=100, client=client, stream_key=key)
    for i in range(5):
        puffer.record(_ev(i))
    assert await puffer.flush_once() == 5

    roh = await client.read_all(key)
    gelesen = [decode(r) for r in roh]
    assert [e.payload["task_id"] for e in gelesen] == ["t0", "t1", "t2", "t3", "t4"]
    await client.delete(key)


async def test_nutzlast_ueberlebt_die_runde(client: ValkeyStreamClient, key: str) -> None:
    puffer = EventBuffer(capacity=10, client=client, stream_key=key)
    puffer.record(_ev(7))
    await puffer.flush_once()

    gelesen = decode((await client.read_all(key))[0])
    assert gelesen.payload["kwargs"] == {"report_id": 807}
    assert gelesen.origin is Origin.PICKUP
    await client.delete(key)


async def test_laengendeckel_greift_ohne_dienst(client: ValkeyStreamClient, key: str) -> None:
    puffer = EventBuffer(capacity=500, client=client, stream_key=key, maxlen=100, batch=500)
    for i in range(400):
        puffer.record(_ev(i))
    await puffer.flush_once()

    laenge = await client.length(key)
    assert laenge <= 200, f"Deckel 100 verlangt, {laenge} Eintraege gefunden"
    assert laenge >= 100, "der Deckel darf nicht mehr wegschneiden als noetig"
    await client.delete(key)


async def test_strom_liegt_unter_eigenem_praefix(key: str) -> None:
    assert key.startswith("sqd:")
    from swarmq_dashboard.events import AUDIT_STREAM, TASK_STREAM

    assert TASK_STREAM.startswith("sqd:") and AUDIT_STREAM.startswith("sqd:")
