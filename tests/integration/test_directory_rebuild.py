"""Verzeichnis und Strom-Leser gegen echte Dienste.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Der Weg vom Hook bis in die Zeile ===
1. Ereignisse aus dem echten Strom landen als Task-Zeilen im Verzeichnis.
2. Der Leser setzt nach einem Neustart am gemerkten Stand auf.

=== ZYKLUS 2: Neuaufbau (R8) ===
3. Nach Loeschen der Verzeichnisdatei baut der Leser sie aus dem Strom neu auf.
4. Der Neuaufbau ist von aussen sichtbar, solange er laeuft.

=== ZYKLUS 3: Aufbewahrung (R5, R9) ===
5. Die Pflege trimmt den echten Strom nach Alter.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from swarmq_dashboard.directory.reader import StreamReader
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin, encode
from swarmq_dashboard.stream import ValkeyStreamClient

pytestmark = pytest.mark.integration


@pytest.fixture
async def strom(stream_url: str) -> AsyncIterator[tuple[ValkeyStreamClient, str]]:
    key = f"sqd:test:dir:{uuid.uuid4().hex[:8]}"
    c = ValkeyStreamClient(stream_url)
    await c.connect()
    yield c, key
    await c.delete(key)
    await c.close()


def _ev(tid: str, ts: float) -> Event:
    return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
                 payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                          "args": [], "kwargs": {"report_id": 812}})


async def test_ereignisse_werden_zu_zeilen(
    tmp_path: Path, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, key = strom
    jetzt = time.time()
    await client.xadd_batch(key, [encode(_ev(f"t{i}", jetzt + i)) for i in range(3)], 10_000)

    r = StreamReader(client, DirectoryWriter(connect(tmp_path / "v.sqlite3")), stream_key=key)
    assert await r.poll_once() == 3
    assert r.writer.row("t0") is not None
    assert r.writer.row("t2")["task_name"] == "render_pdf"
    r.writer.close()


async def test_neustart_setzt_am_gemerkten_stand_auf(
    tmp_path: Path, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, key = strom
    jetzt = time.time()
    pfad = tmp_path / "v.sqlite3"
    await client.xadd_batch(key, [encode(_ev("a", jetzt))], 10_000)

    r1 = StreamReader(client, DirectoryWriter(connect(pfad)), stream_key=key)
    await r1.poll_once()
    stand = r1.position
    r1.writer.close()

    await client.xadd_batch(key, [encode(_ev("b", jetzt + 1))], 10_000)
    r2 = StreamReader(client, DirectoryWriter(connect(pfad)), stream_key=key)
    assert r2.position == stand
    assert r2.rebuilding is False
    assert await r2.poll_once() == 1, "der Leser hat den Strom erneut von vorn gelesen"
    r2.writer.close()


async def test_neuaufbau_nach_verlust_der_datei(
    tmp_path: Path, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, key = strom
    jetzt = time.time()
    pfad = tmp_path / "v.sqlite3"
    await client.xadd_batch(key, [encode(_ev(f"t{i}", jetzt + i)) for i in range(4)], 10_000)

    r1 = StreamReader(client, DirectoryWriter(connect(pfad)), stream_key=key)
    await r1.poll_once()
    r1.writer.close()
    for endung in ("", "-wal", "-shm"):
        pfad.with_name(pfad.name + endung).unlink(missing_ok=True)

    r2 = StreamReader(client, DirectoryWriter(connect(pfad)), stream_key=key)
    assert r2.rebuilding is True, "der Neuaufbau ist nicht sichtbar (R8)"
    assert await r2.poll_once() == 4, "der Strom wurde nicht vollstaendig neu eingelesen"
    assert r2.rebuilding is False
    assert r2.writer.row("t3") is not None
    r2.writer.close()


async def test_pflege_trimmt_den_echten_strom(
    tmp_path: Path, strom: tuple[ValkeyStreamClient, str]
) -> None:
    client, key = strom
    jetzt = time.time()
    # Ein alter und ein junger Eintrag, ueber die Stromkennung datiert.
    await client.client.xadd(key, {"e": encode(_ev("alt", jetzt - 10_000))},
                             id=f"{int((jetzt - 10_000) * 1000)}-0")
    await client.client.xadd(key, {"e": encode(_ev("neu", jetzt))},
                             id=f"{int(jetzt * 1000)}-0")

    r = StreamReader(client, DirectoryWriter(connect(tmp_path / "v.sqlite3")), stream_key=key)
    await r.poll_once()
    await r.maintain(now=jetzt, retention_s=1_000.0)

    assert await client.length(key) == 1, "der alte Stromeintrag blieb stehen"
    assert r.writer.row("alt") is None, "die alte Zeile blieb im Verzeichnis"
    assert r.writer.row("neu") is not None
    r.writer.close()
