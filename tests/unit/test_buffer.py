"""Beschraenkter Puffer mit Hintergrund-Flusher (KTD1).

Der Grund fuer diese Einheit steht in swarmq: Task-Signale laufen sequenziell
im Ausfuehrungspfad, ohne Zeitgrenze, waehrend der Nebenlaeufigkeits-Slot
gehalten wird und *vor* dem Broker-Ack. Ein Handler, der auf Netz wartet,
riskiert die Zustellfrist des Brokers. Also wartet hier nichts.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: record() wartet nie ===
1. record() kehrt zurueck, waehrend der Flusher keine Verbindung hat.
2. record() haelt diese Zusage auch ueber viele Aufrufe (Ausfuehrungsnotiz U2).
3. record() wirft nicht, auch nicht an einer fremden Nutzlast.

=== ZYKLUS 2: Obergrenze und Verlustzaehler ===
4. Ein voller Puffer verwirft die aeltesten Eintraege.
5. Der Verlustzaehler steigt um genau die verworfene Zahl.
6. Die Zeitspanne des Verlusts wird mitgefuehrt.

=== ZYKLUS 3: Flusher ===
7. Ein erreichbarer Strom nimmt die gepufferten Ereignisse auf.
8. Ein Fehler beim Schreiben beendet den Hintergrund-Task nicht.
9. Covers AE11. Nach einer Schreibpause erscheint genau ein Verlust-Ereignis
   mit Anzahl und Zeitspanne.
10. Eine Protokollzeile ueber einen Schreibfehler nennt Art, Anzahl und Grund,
    aber keine Argumentwerte (Datenschutz, KTD1).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from swarmq_dashboard.buffer import EventBuffer
from swarmq_dashboard.events import Event, Origin


def _ev(i: int = 0, **extra: Any) -> Event:
    return Event(type="task_started", ts=float(i), source="phx-w1:0",
                 origin=Origin.PICKUP, payload={"task_id": f"t{i}", **extra})


class _ToterStrom:
    """Ein Client, der bei jedem Schreibversuch scheitert."""

    def __init__(self) -> None:
        self.versuche = 0

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        self.versuche += 1
        raise ConnectionError("Valkey nicht erreichbar")


class _Strom:
    """Ein Client, der mitschreibt."""

    def __init__(self) -> None:
        self.geschrieben: list[bytes] = []
        self.maxlen_gesehen: list[int] = []

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        self.geschrieben.extend(events)
        self.maxlen_gesehen.append(maxlen)


# --- Zyklus 1 -----------------------------------------------------------


def test_record_kehrt_ohne_verbindung_zurueck() -> None:
    puffer = EventBuffer(capacity=10, client=_ToterStrom())
    puffer.record(_ev())
    assert puffer.counters.buffered == 1


def test_record_blockiert_auch_ueber_viele_aufrufe_nicht() -> None:
    puffer = EventBuffer(capacity=100, client=_ToterStrom())
    t0 = time.perf_counter()
    for i in range(10_000):
        puffer.record(_ev(i))
    dauer = time.perf_counter() - t0
    assert dauer < 0.5, f"10.000 Aufrufe brauchten {dauer:.3f}s — record() wartet auf etwas"


def test_record_wirft_nicht_an_fremder_nutzlast() -> None:
    class Fremd:
        pass

    puffer = EventBuffer(capacity=10, client=_ToterStrom())
    puffer.record(_ev(0, exception=Fremd()))
    assert puffer.counters.buffered == 1


# --- Zyklus 2 -----------------------------------------------------------


def test_voller_puffer_verwirft_die_aeltesten() -> None:
    puffer = EventBuffer(capacity=3, client=_ToterStrom())
    for i in range(5):
        puffer.record(_ev(i))
    verbleibend = [e.payload["task_id"] for e in puffer.peek()]
    assert verbleibend == ["t2", "t3", "t4"]


def test_verlustzaehler_steigt_um_die_verworfene_zahl() -> None:
    puffer = EventBuffer(capacity=3, client=_ToterStrom())
    for i in range(10):
        puffer.record(_ev(i))
    assert puffer.counters.dropped == 7


def test_zeitspanne_des_verlusts_wird_mitgefuehrt() -> None:
    puffer = EventBuffer(capacity=2, client=_ToterStrom())
    for i in range(5):
        puffer.record(_ev(i))
    assert puffer.drop_span is not None
    erster, letzter = puffer.drop_span
    assert erster <= letzter


# --- Zyklus 3 -----------------------------------------------------------


async def test_erreichbarer_strom_nimmt_auf() -> None:
    strom = _Strom()
    puffer = EventBuffer(capacity=10, client=strom, maxlen=5000)
    for i in range(3):
        puffer.record(_ev(i))
    geschrieben = await puffer.flush_once()
    assert geschrieben == 3
    assert len(strom.geschrieben) == 3
    assert strom.maxlen_gesehen == [5000]
    assert puffer.counters.flushed == 3


async def test_schreibfehler_beendet_den_hintergrund_task_nicht() -> None:
    strom = _ToterStrom()
    puffer = EventBuffer(capacity=10, client=strom, flush_interval=0.01)
    await puffer.start()
    try:
        puffer.record(_ev())
        deadline = time.monotonic() + 2.0
        while strom.versuche < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert strom.versuche >= 2, "der Flusher hat nach dem ersten Fehler aufgegeben"
        assert puffer.counters.failed >= 1
    finally:
        await puffer.stop()


async def test_verlustereignis_nach_rueckkehr_des_stroms() -> None:
    """Covers AE11."""
    strom = _Strom()
    puffer = EventBuffer(capacity=2, client=strom)
    for i in range(6):
        puffer.record(_ev(i))
    assert puffer.counters.dropped == 4
    await puffer.flush_once()

    verlust = [e for e in (__import__("swarmq_dashboard.events", fromlist=["decode"]).decode(r)
                           for r in strom.geschrieben) if e.type == "sqd_events_lost"]
    assert len(verlust) == 1, "genau ein Verlust-Ereignis erwartet"
    assert verlust[0].payload["count"] == 4
    assert verlust[0].payload["worker_id"] == "phx-w1:0"
    assert "from_ts" in verlust[0].payload and "to_ts" in verlust[0].payload

    # Zweiter Durchlauf ohne neue Verluste: kein zweites Verlust-Ereignis.
    strom.geschrieben.clear()
    puffer.record(_ev(99))
    await puffer.flush_once()
    assert not [r for r in strom.geschrieben if b"sqd_events_lost" in r]


async def test_protokollzeile_nennt_keine_argumentwerte(caplog: pytest.LogCaptureFixture) -> None:
    puffer = EventBuffer(capacity=10, client=_ToterStrom())
    puffer.record(_ev(0, kwargs={"kunde": "Maria Musterfrau", "iban": "AT61 1904 3002 3457 3201"}))
    with caplog.at_level("WARNING"):
        await puffer.flush_once()
    text = caplog.text
    assert text, "ein Schreibfehler soll protokolliert werden (R4)"
    assert "Maria Musterfrau" not in text
    assert "AT61" not in text
    assert "ConnectionError" in text or "Valkey" in text
