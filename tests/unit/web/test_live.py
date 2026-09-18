"""Live-Schicht: neue Ereignisse in offenen Ansichten (U12, R13, R13a, R32).

Die gefaehrliche Stelle ist der **Ueberlauf**. KTD6 sagt woertlich: "Laeuft
sie ueber, wird nicht still verworfen, sondern die Ansicht zum Neuladen
aufgefordert." Die Ueberlaufbehandlung der Kanal-Verteilung leistet genau das
nicht -- nachgemessen an litestar 2.24: ``_sub_worker`` ruft
``subscriber.put_nowait`` und verwirft den Rueckgabewert, und beide Strategien
(``backoff``, ``dropleft``) werfen ohne jede Rueckmeldung weg. Mit Deckel 3
und zehn Nachrichten liefert ``backoff`` n0 n1 n2 und ``dropleft`` n7 n8 n9.

Deshalb traegt jede Nachricht eine **Folgenummer**, und die Verbindung
vergleicht sie mit der zuletzt weitergereichten. Eine Luecke heisst Verlust --
gleich wo er entstand -- und loest die Neuladen-Nachricht aus. Der Deckel
bleibt, er verhindert das unbegrenzte Wachsen; er ist nur nicht mehr die
Stelle, an der der Verlust bemerkt wird.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Zustellung (R13) ===
1. Ein veroeffentlichtes Ereignis erreicht eine abonnierte Verbindung.
2. Zwei Verbindungen bekommen dasselbe Ereignis.
3. Die Folgenummern laufen ohne Luecke durch.

=== ZYKLUS 2: Ueberlauf (KTD6, R13a) ===
4. Ein Ueberlauf erzeugt die Neuladen-Nachricht statt stiller Verluste --
   ueber den **tatsaechlich benutzten** Weg, nicht ueber einen Nebenpfad.
5. Ohne Ueberlauf kommt keine Neuladen-Nachricht.
6. Nach dem Neuladen laeuft die Folge wieder sauber weiter.

=== ZYKLUS 3: Herzschlag und Sitzung (R32) ===
7. Im Leerlauf geht ein Herzschlag hinaus.
8. Faellt die Sitzung weg, endet die Verbindung mit einer letzten Nachricht.
9. Eine offene Verbindung verlaengert die Sitzung nicht.
10. Der Dienst haelt die juengsten Nachrichten, nicht die aeltesten -- sonst
    faellt ein Ueberlauf erst spaeter oder nie auf.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from litestar.channels import ChannelsPlugin
from litestar.channels.backends.memory import MemoryChannelsBackend

from swarmq_dashboard.web.stream import (
    LIVE_CHANNEL,
    RELOAD_KIND,
    LivePublisher,
    live_events,
)


async def _plugin(max_backlog: int | None = None,
                  strategie: str = "dropleft") -> ChannelsPlugin:
    p = ChannelsPlugin(
        backend=MemoryChannelsBackend(history=0),
        channels=[LIVE_CHANNEL],
        subscriber_max_backlog=max_backlog,
        subscriber_backlog_strategy=strategie,  # type: ignore[arg-type]
    )
    await p._on_startup()
    return p


async def _sammeln(strom: object, wieviele: int, frist: float = 2.0) -> list[dict]:
    """Die ersten ``wieviele`` Nachrichten einsammeln."""
    gesehen: list[dict] = []

    async def lesen() -> None:
        async for nachricht in strom:  # type: ignore[attr-defined]
            gesehen.append(nachricht)
            if len(gesehen) >= wieviele:
                return

    # Eine Zeitueberschreitung ist hier kein Fehlschlag, sondern das Ende der
    # Geduld: der Aufrufer prueft, was bis dahin ankam.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(lesen(), frist)
    return gesehen


# --- Zyklus 1 -----------------------------------------------------------


async def test_ein_ereignis_erreicht_die_verbindung() -> None:
    p = await _plugin()
    try:
        sender = LivePublisher(p)
        async with p.start_subscription([LIVE_CHANNEL]) as abo:
            await sender.publish("task", {"task_id": "a"})
            gesehen = await _sammeln(live_events(abo, heartbeat_s=5.0), 1)
        assert [n["kind"] for n in gesehen] == ["task"]
        assert gesehen[0]["payload"] == {"task_id": "a"}
    finally:
        await p._on_shutdown()


async def test_zwei_verbindungen_bekommen_dasselbe() -> None:
    p = await _plugin()
    try:
        sender = LivePublisher(p)
        async with p.start_subscription([LIVE_CHANNEL]) as eine, \
                   p.start_subscription([LIVE_CHANNEL]) as andere:
            await sender.publish("task", {"task_id": "a"})
            a = await _sammeln(live_events(eine, heartbeat_s=5.0), 1)
            b = await _sammeln(live_events(andere, heartbeat_s=5.0), 1)
        assert a[0]["payload"] == b[0]["payload"] == {"task_id": "a"}
    finally:
        await p._on_shutdown()


async def test_die_folgenummern_laufen_ohne_luecke() -> None:
    p = await _plugin()
    try:
        sender = LivePublisher(p)
        async with p.start_subscription([LIVE_CHANNEL]) as abo:
            beginn = sender.current_seq
            for i in range(5):
                await sender.publish("task", {"n": i})
            gesehen = await _sammeln(
                live_events(abo, heartbeat_s=5.0, start_seq=beginn), 5
            )
        assert [n["payload"]["n"] for n in gesehen] == [0, 1, 2, 3, 4]
        assert not [n for n in gesehen if n["kind"] == RELOAD_KIND]
    finally:
        await p._on_shutdown()


# --- Zyklus 2 -----------------------------------------------------------


async def test_ein_ueberlauf_erzeugt_die_neuladen_nachricht() -> None:
    """Covers R13a, KTD6.

    Ueber den **tatsaechlich benutzten** Weg: die echte Kanal-Verteilung mit
    gesetztem Deckel. Ein handgebauter Nebenpfad prueefte eine Mechanik, die
    im Betrieb niemand faehrt -- und genau daran waere der Befund
    vorbeigelaufen.
    """
    p = await _plugin(max_backlog=3, strategie="dropleft")
    try:
        sender = LivePublisher(p)
        async with p.start_subscription([LIVE_CHANNEL]) as abo:
            beginn = sender.current_seq
            for i in range(20):
                await sender.publish("task", {"n": i})
            await asyncio.sleep(0.2)          # der Verteiler wirft weg
            gesehen = await _sammeln(
                live_events(abo, heartbeat_s=5.0, start_seq=beginn), 4
            )

        arten = [n["kind"] for n in gesehen]
        assert RELOAD_KIND in arten, (
            f"der Ueberlauf wurde still verworfen: {arten}"
        )
    finally:
        await p._on_shutdown()


async def test_ohne_ueberlauf_keine_neuladen_nachricht() -> None:
    p = await _plugin(max_backlog=100)
    try:
        sender = LivePublisher(p)
        async with p.start_subscription([LIVE_CHANNEL]) as abo:
            for i in range(5):
                await sender.publish("task", {"n": i})
            gesehen = await _sammeln(live_events(abo, heartbeat_s=5.0), 5)
        assert RELOAD_KIND not in [n["kind"] for n in gesehen]
    finally:
        await p._on_shutdown()


# --- Zyklus 3 -----------------------------------------------------------


async def test_im_leerlauf_geht_ein_herzschlag_hinaus() -> None:
    """Sonst raeumt ein zwischengeschalteter Proxy die Verbindung ab, und die
    Ansicht steht still, ohne dass jemand es merkt.
    """
    p = await _plugin()
    try:
        async with p.start_subscription([LIVE_CHANNEL]) as abo:
            gesehen = await _sammeln(live_events(abo, heartbeat_s=0.05), 2, frist=1.0)
        assert len(gesehen) >= 2
        assert all(n["kind"] == "heartbeat" for n in gesehen), [n["kind"] for n in gesehen]
    finally:
        await p._on_shutdown()


async def test_faellt_die_sitzung_weg_endet_die_verbindung() -> None:
    """Covers R32. Ohne das bleibt die Ansicht nach dem Sitzungsablauf offen
    und bekommt weiter Daten -- eine eingefrorene Seite, die aktuell aussieht.
    """
    p = await _plugin()
    gilt = {"ja": True}
    try:
        async with p.start_subscription([LIVE_CHANNEL]) as abo:
            strom = live_events(abo, heartbeat_s=0.05,
                                still_valid=lambda: gilt["ja"])
            gesehen: list[dict] = []

            async def lesen() -> None:
                async for n in strom:
                    gesehen.append(n)
                    gilt["ja"] = False        # nach der ersten Nachricht faellt sie weg

            await asyncio.wait_for(lesen(), 2.0)

        assert gesehen[-1]["kind"] == "session_ended", [n["kind"] for n in gesehen]
    finally:
        await p._on_shutdown()


async def test_der_dienst_haelt_die_juengsten_nachrichten(tmp_path: Path) -> None:
    """Die Strategie ist nicht beliebig.

    Gemessen mit Deckel 3 und dreissig Nachrichten: ``dropleft`` haelt die
    juengsten, und zusammen mit dem Anfangswert der Folgenummer faellt der
    Verlust **sofort** auf -- die Ansicht sieht ``reload`` und laedt neu. Mit
    ``backoff`` sieht sie die drei aeltesten und merkt erst bei der naechsten
    Nachricht etwas; bleibt es still, merkt sie es nie. Eine Ansicht will
    ausserdem den aktuellen Stand, nicht den aeltesten.
    """
    from swarmq_dashboard.config import DashboardConfig
    from swarmq_dashboard.web.app import Services

    config = DashboardConfig(
        broker_url="amqp://guest:guest@127.0.0.1:1/", backend_url="valkey://127.0.0.1:1/0",
        stream_url="valkey://127.0.0.1:1/1", retention_days=7, unconfirmed_after_s=15.0,
        disconnected_after_s=40.0, session_idle_s=1800.0, backlog_cap=300,
        stream_stale_after_s=120.0, data_dir=tmp_path / "sqd-data",
    )
    s = Services(config)
    assert s.live._backlog_strategy == "dropleft", (
        f"die Strategie ist {s.live._backlog_strategy!r} -- mit 'backoff' faellt ein "
        "Ueberlauf erst bei der naechsten Nachricht auf, und bleibt es still, nie"
    )
    assert s.live._max_backlog == config.backlog_cap, "der Deckel kommt nicht aus der Einstellung"
