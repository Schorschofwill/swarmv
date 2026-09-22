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
import contextlib
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

    async def connect(self) -> None: ...

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        self.versuche += 1
        raise ConnectionError("Valkey nicht erreichbar")


class _Strom:
    """Ein Client, der mitschreibt."""

    def __init__(self) -> None:
        self.geschrieben: list[bytes] = []
        self.maxlen_gesehen: list[int] = []

    async def connect(self) -> None: ...

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


# === Befunde aus der Codepruefung Stufe A ================================
#
# 11. Die Ruecklage nach einem Schreibfehler zaehlt verdraengte Ereignisse.
# 12. Die Lueckenmeldung nennt die Zahl ihrer Zeitspanne, nicht die Gesamtzahl.
# 13. Zwei gleichzeitige Durchlaeufe ueberlappen nicht.
# 14. stop() leert mehr als einen Stapel.
# 15. Ein Abbruch waehrend des Schreibens verliert den Stapel nicht ungezaehlt.


async def test_ruecklage_zaehlt_verdraengte_ereignisse() -> None:
    """Befund 2: extendleft verdraengt am anderen Ende, ohne zu zaehlen."""

    class Langsam:
        async def connect(self) -> None: ...
        async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
            await asyncio.sleep(0)
            raise ConnectionError("aus")

    puffer = EventBuffer(capacity=10, client=Langsam(), batch=5)
    for i in range(10):
        puffer.record(_ev(i))
    aufgabe = asyncio.create_task(puffer.flush_once())
    await asyncio.sleep(0)
    for i in range(100, 105):
        puffer.record(_ev(i))
    await aufgabe

    drin = {e.payload["task_id"] for e in puffer.peek()}
    erwartet = {f"t{i}" for i in range(10)} | {f"t{i}" for i in range(100, 105)}
    verloren = len(erwartet - drin)
    assert puffer.counters.dropped >= verloren, (
        f"{verloren} Ereignisse verloren, nur {puffer.counters.dropped} gezaehlt"
    )


async def test_lueckenmeldung_nennt_die_zahl_ihrer_zeitspanne() -> None:
    """Befund 3: der Zaehler ist kumulativ, die Zeitspanne wird zurueckgesetzt."""
    from swarmq_dashboard.events import decode

    strom = _Strom()
    puffer = EventBuffer(capacity=2, client=strom)
    for i in range(6):
        puffer.record(_ev(i))
    await puffer.flush_once()
    erste = next(decode(r) for r in strom.geschrieben if b"sqd_events_lost" in r)

    strom.geschrieben.clear()
    for i in range(10, 15):
        puffer.record(_ev(i))
    await puffer.flush_once()
    zweite = next(decode(r) for r in strom.geschrieben if b"sqd_events_lost" in r)

    assert erste.payload["count"] + zweite.payload["count"] == puffer.counters.dropped, (
        f"Summe der Meldungen {erste.payload['count']}+{zweite.payload['count']} "
        f"weicht vom Gesamtverlust {puffer.counters.dropped} ab"
    )


async def test_zwei_durchlaeufe_ueberlappen_nicht() -> None:
    """Befund 5: ohne Ausschluss schreiben zwei Durchlaeufe durcheinander."""
    gleichzeitig = 0
    hoechststand = 0

    class Zaehlend:
        async def connect(self) -> None: ...
        async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
            nonlocal gleichzeitig, hoechststand
            gleichzeitig += 1
            hoechststand = max(hoechststand, gleichzeitig)
            await asyncio.sleep(0.01)
            gleichzeitig -= 1

    puffer = EventBuffer(capacity=100, client=Zaehlend(), batch=5)
    for i in range(50):
        puffer.record(_ev(i))
    await asyncio.gather(puffer.flush_once(), puffer.flush_once(), puffer.flush_once())
    assert hoechststand == 1, f"{hoechststand} Durchlaeufe gleichzeitig im Schreibvorgang"


async def test_stop_leert_mehr_als_einen_stapel() -> None:
    """Befund 8: stop() schrieb hoechstens einen Stapel."""
    strom = _Strom()
    puffer = EventBuffer(capacity=2000, client=strom, batch=100)
    for i in range(1000):
        puffer.record(_ev(i))
    await puffer.stop()
    assert len(strom.geschrieben) == 1000, (
        f"nur {len(strom.geschrieben)} von 1000 geschrieben — der Rest ist weg"
    )


async def test_abbruch_waehrend_des_schreibens_verliert_nicht_ungezaehlt() -> None:
    """Befund 6/8: ein abgebrochener Schreibvorgang darf den Stapel nicht verschlucken."""

    class Haengend:
        async def connect(self) -> None: ...
        async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
            await asyncio.sleep(30)

    puffer = EventBuffer(capacity=100, client=Haengend(), batch=10)
    for i in range(10):
        puffer.record(_ev(i))
    aufgabe = asyncio.create_task(puffer.flush_once())
    await asyncio.sleep(0.02)
    aufgabe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await aufgabe

    erhalten = len(puffer.peek()) + puffer.counters.dropped
    assert erhalten == 10, f"nur {erhalten} von 10 Ereignissen erhalten oder gezaehlt"


# 16. stop() haengt nicht an einem unerreichbaren Strom (Regression aus Befund 8).
# 17. Was stop() nicht mehr wegschreiben kann, wird gezaehlt statt verschluckt.


async def test_stop_haengt_nicht_an_unerreichbarem_strom() -> None:
    class Haengend:
        async def connect(self) -> None: ...
        async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
            await asyncio.sleep(30)

    puffer = EventBuffer(capacity=100, client=Haengend(), batch=10, flush_interval=0.01)
    for i in range(50):
        puffer.record(_ev(i))
    await puffer.start()
    await asyncio.sleep(0.05)
    # Die Frist wird uebergeben statt abgewartet: geprueft wird, dass
    # ``stop()`` sie **einhaelt** -- und das ist bei 0,2 s dieselbe Aussage
    # wie bei der Vorgabe von 2 s, nur zwei Sekunden schneller. Ein
    # ``stop()``, das den haengenden Schreibvorgang aussitzt, liefe weiterhin
    # in die Zeitgrenze.
    t0 = time.perf_counter()
    await asyncio.wait_for(puffer.stop(deadline_s=0.2), timeout=5.0)
    dauer = time.perf_counter() - t0
    assert dauer < 1.0, f"stop() brauchte {dauer:.1f}s an einem toten Strom"


async def test_stop_zaehlt_was_es_nicht_mehr_loswird() -> None:
    class Haengend:
        async def connect(self) -> None: ...
        async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
            await asyncio.sleep(30)

    puffer = EventBuffer(capacity=100, client=Haengend(), batch=10, flush_interval=0.01)
    for i in range(50):
        puffer.record(_ev(i))
    await asyncio.wait_for(puffer.stop(deadline_s=0.2), timeout=5.0)
    erhalten = len(puffer.peek()) + puffer.counters.dropped + puffer.counters.flushed
    assert erhalten >= 50, f"nur {erhalten} von 50 Ereignissen erhalten oder gezaehlt"


# === Befunde aus der zweiten Codepruefung ===============================
#
# 18. Ein Weckruf waehrend eines laufenden Schreibvorgangs geht nicht verloren.
# 19. stop() ist endgueltig: ein gleichzeitiges Einreihen startet nicht neu.
# 20. stop() verwirft nichts, was nach dem Abbruch noch hereinkommt.
# 21. Ein Abbruch von aussen laesst keine verwaisten Warte-Tasks zurueck.
# 22. Ein Abbruch von aussen wird nicht in einen Erfolg verwandelt.


async def test_weckruf_waehrend_des_schreibens_geht_nicht_verloren() -> None:
    """Befund 1: _wake wurde nach dem Flush geleert, nicht davor."""
    schreibvorgaenge: list[int] = []

    class Langsam:
        async def connect(self) -> None: ...
        async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
            await asyncio.sleep(0.15)
            schreibvorgaenge.append(len(events))

    puffer = EventBuffer(capacity=100, client=Langsam(), batch=10, flush_interval=5.0)
    puffer.record(_ev(0))
    await puffer.start()
    await asyncio.sleep(0.05)          # der Flusher steckt im Schreiben
    puffer.record(_ev(1))
    puffer.nudge()                     # genau jetzt -- das Fenster
    await asyncio.sleep(0.5)           # reichlich Zeit; stop() ist noch nicht gelaufen
    vor_stop = len(schreibvorgaenge)
    await puffer.stop()
    assert vor_stop >= 2, (
        f"nur {vor_stop} Schreibvorgang vor stop(); der Weckruf wurde verschluckt "
        f"(nach stop(): {len(schreibvorgaenge)} — das leert ohnehin)"
    )


async def test_stop_ist_endgueltig() -> None:
    """Befund 2: ensure_started() startete den Flusher mitten im Beenden neu."""
    strom = _Strom()
    puffer = EventBuffer(capacity=100, client=strom, flush_interval=0.01)
    await puffer.start()
    await puffer.stop()
    await puffer.ensure_started()
    assert puffer.running is False, "der Puffer wurde nach stop() wieder gestartet"


async def test_stop_verwirft_nichts_was_danach_hereinkommt() -> None:
    """Befund 2: stop() leerte die Warteschlange des neu gestarteten Puffers."""
    strom = _Strom()
    puffer = EventBuffer(capacity=100, client=strom, flush_interval=0.01)
    await puffer.start()
    await puffer.stop()
    puffer.record(_ev(99))
    assert len(puffer.peek()) == 1, "ein Ereignis nach stop() ging verloren"


async def test_abbruch_von_aussen_laesst_keine_warte_tasks_zurueck() -> None:
    """Befund 7: asyncio.wait bricht die uebergebenen Tasks nicht selbst ab."""
    strom = _Strom()
    puffer = EventBuffer(capacity=10, client=strom, flush_interval=30.0)
    await puffer.start()
    await asyncio.sleep(0.02)
    puffer._task.cancel()              # type: ignore[union-attr]
    with contextlib.suppress(asyncio.CancelledError):
        await puffer._task             # type: ignore[arg-type]
    # Nicht ueber all_tasks pruefen -- abgebrochene Tasks sind einen Tick
    # spaeter fertig. Die Warteliste des Events ist der harte Beleg.
    warteliste = len(getattr(puffer._stop, "_waiters", [])) + len(getattr(puffer._wake, "_waiters", []))
    assert warteliste == 0, f"{warteliste} Eintraege blieben in den Wartelisten der Events stehen"


async def test_abbruch_von_aussen_wird_nicht_zu_erfolg() -> None:
    """Befund 6: CancelledError des Aufrufers wurde als normales Ende behandelt."""

    class Haengend:
        async def connect(self) -> None: ...
        async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
            await asyncio.sleep(30)

    puffer = EventBuffer(capacity=100, client=Haengend(), batch=10)
    for i in range(20):
        puffer.record(_ev(i))
    aufgabe = asyncio.create_task(puffer.stop(deadline_s=30))
    await asyncio.sleep(0.05)
    aufgabe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await aufgabe
