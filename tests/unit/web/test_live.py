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
9. Ein gesperrtes Konto bekommt gar keinen Strom. (Dass die Pruefung
   **im laufenden** Strom den Sitzungsspeicher nicht anfasst, bleibt
   ungeprueft -- der Test sagt im Docstring, warum.)
10. Der Dienst haelt die juengsten Nachrichten, nicht die aeltesten -- sonst
    faellt ein Ueberlauf erst spaeter oder nie auf.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
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


async def test_nach_dem_neuladen_laeuft_die_folge_sauber_weiter() -> None:
    """Punkt 6 der Test-Liste -- er hatte bisher keinen Test.

    Die Neuladen-Nachricht ist nur dann eine Loesung, wenn danach wieder
    Verlass ist. Zaehlte der Strom hinterher weiter falsch, bekaeme die
    Ansicht bei jedem folgenden Ereignis erneut "neu laden" und haenge in
    einer Schleife -- aus einer einmaligen Stoerung wuerde eine dauerhafte.
    """
    p = await _plugin(max_backlog=3, strategie="dropleft")
    try:
        sender = LivePublisher(p)
        async with p.start_subscription([LIVE_CHANNEL]) as abo:
            beginn = sender.current_seq
            for i in range(20):
                await sender.publish("task", {"n": i})
            await asyncio.sleep(0.2)
            # Kurzer Herzschlag: der Rueckstau gibt nach dem Deckel nur
            # wenige Nachrichten her, und ohne ihn sass der Test die volle
            # Geduld von zwei Sekunden auf eine sechste Nachricht ab, die
            # nie kam. Die Zusicherungen filtern Herzschlaege ohnehin.
            gesehen = await _sammeln(
                live_events(abo, heartbeat_s=0.05, start_seq=beginn), 6
            )

        arten = [n["kind"] for n in gesehen]
        assert RELOAD_KIND in arten, arten
        # Nach der Neuladen-Nachricht darf keine zweite mehr kommen: der
        # Strom hat sich auf den neuen Stand gesetzt.
        danach = arten[arten.index(RELOAD_KIND) + 1:]
        assert RELOAD_KIND not in danach, (
            f"der Strom meldet nach dem Neuladen weiter Verlust: {arten}"
        )
        # Und die Folgenummern laufen ab da ohne Luecke.
        nummern = [n["seq"] for n in gesehen[arten.index(RELOAD_KIND) + 1:]
                   if n["kind"] not in ("heartbeat", RELOAD_KIND)]
        assert nummern == sorted(nummern), f"die Folge laeuft rueckwaerts: {nummern}"
        assert all(b - a == 1 for a, b in itertools.pairwise(nummern)), (
            f"Luecke in der Folge nach dem Neuladen: {nummern}"
        )
    finally:
        await p._on_shutdown()


async def test_ein_gesperrtes_konto_bekommt_keinen_live_strom(tmp_path: Path) -> None:
    """Punkt 9 der Test-Liste -- und eine offen benannte Luecke.

    **Was hier steht:** ein gesperrtes Konto kommt gar nicht erst an den
    Strom. Der Sitzungswaechter weist es ab, bevor der Handler laeuft; die
    Verbindung entsteht nie.

    **Was hier nicht steht:** der Fall, dass die Verbindung schon laeuft und
    erst dann gesperrt wird (AE10a). Den prueft
    ``test_eine_laufende_verbindung_endet_wenn_das_konto_gesperrt_wird``
    weiter unten -- er war lange offen, weil drei Anlaeufe scheiterten: der
    erste mass zwei Dinge, die einander nicht beruehrten; der zweite ging von
    der falschen Annahme aus, Lesen verlaengere die Sitzungsfrist nicht
    (``get`` mit ``renew_for`` schreibt sie sehr wohl fort); der dritte
    brauchte einen Strom ueber HTTP, der von selbst endet -- und der
    Testclient gibt den einer stehenden SSE-Verbindung nicht haeppchenweise
    heraus. Loesung war, den Waechter aus dem Handler zu ziehen und die
    Kette ohne HTTP zu fahren.

    Dass die Pruefung den **Kontenspeicher** nachschlaegt und nicht die
    Sitzung, ist dabei der Punkt, an dem R21 haengt.
    """
    import re as _re

    from argon2 import PasswordHasher
    from litestar.testing import AsyncTestClient

    from swarmq_dashboard.accounts import AccountStore
    from swarmq_dashboard.config import DashboardConfig
    from swarmq_dashboard.web.app import create_app

    billig = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
    config = DashboardConfig(
        broker_url="amqp://guest:guest@127.0.0.1:1/", backend_url="valkey://127.0.0.1:1/0",
        stream_url="valkey://127.0.0.1:1/1", retention_days=7, unconfirmed_after_s=15.0,
        disconnected_after_s=40.0, session_idle_s=1800.0, backlog_cap=300,
        stream_stale_after_s=120.0, data_dir=tmp_path / "sqd-data",
    )
    konten = AccountStore(config.accounts_file, hasher=billig)
    konten.create("ops", "ein-langes-passwort")
    konten.close()

    app = create_app(config, hasher=billig, maintenance_every_s=99.0, ingest_every_s=99.0)
    c = AsyncTestClient(app=app, base_url="https://testserver.local")
    await c.__aenter__()
    try:
        seite = await c.get("/login")
        merkmal = _re.search(r'name="_csrf_token"\s+value="([^"]*)"', seite.text)
        assert merkmal
        await c.post("/login", data={"account": "ops", "password": "ein-langes-passwort",
                                     "_csrf_token": merkmal.group(1)},
                     follow_redirects=False)
        app.state.services.accounts.lock("ops")

        antwort = await c.get("/live")
        assert antwort.status_code == 401, (
            f"ein gesperrtes Konto bekommt weiter einen Strom: {antwort.status_code}"
        )
    finally:
        await c.__aexit__(None, None, None)



async def test_eine_laufende_verbindung_endet_wenn_das_konto_gesperrt_wird(
    tmp_path: Path,
) -> None:
    """Covers AE10a, R32 -- die Luecke, die der Test darueber benannt hat.

    Der Nachbar prueft den Aufbau: ein gesperrtes Konto kommt gar nicht erst
    an den Strom. Ungeprueft blieb der Fall, um den es in R32 geht -- die
    Verbindung steht **schon**, und erst dann sperrt der Betreiber. Daran
    haengt alles an der Pruefung im laufenden Takt: wird sie nie gerufen,
    oder merkt sich der Kontenspeicher den Zustand vom Verbindungsaufbau,
    behaelt das gesperrte Konto seinen Datenstrom, bis jemand den Tab
    schliesst -- und niemand saehe es.

    Gefahren wird die Kette am Stueck: echter Kontenspeicher, echter
    Waechter aus der Route, echte Schleife. **Nicht** ueber HTTP -- der
    Testclient liefert den Strom einer stehenden SSE-Verbindung nicht
    haeppchenweise, daran sind schon drei Anlaeufe gescheitert. Die eine
    Stelle, die damit unbewiesen bleibt, ist die Zeile in ``live()``, die
    genau diesen Waechter uebergibt.
    """
    from argon2 import PasswordHasher

    from swarmq_dashboard.accounts import AccountStore
    from swarmq_dashboard.web.stream import kontowaechter

    konten = AccountStore(
        tmp_path / "accounts.sqlite3",
        hasher=PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1),
    )
    konten.create("ops", "ein-langes-passwort")

    class Dienst:
        accounts = konten

    waechter = kontowaechter(Dienst(), "ops")
    assert waechter() is True, "Vorbedingung: das offene Konto gilt"

    p = ChannelsPlugin(backend=MemoryChannelsBackend(history=0), channels=[LIVE_CHANNEL],
                       subscriber_max_backlog=10, subscriber_backlog_strategy="dropleft")
    await p._on_startup()
    try:
        async with p.start_subscription([LIVE_CHANNEL]) as abo:
            strom = live_events(abo, heartbeat_s=0.05, still_valid=waechter)
            gesehen: list[dict] = []

            async def lesen() -> None:
                async for n in strom:
                    gesehen.append(n)
                    # Erst sperren, wenn die Verbindung wirklich laeuft --
                    # sonst prueft der Test den Aufbau und nicht den Takt.
                    if len(gesehen) == 1:
                        konten.lock("ops")

            await asyncio.wait_for(lesen(), 3.0)

        assert len(gesehen) >= 2, "die Verbindung lief gar nicht erst an"
        assert gesehen[-1]["kind"] == "session_ended", (
            f"die Verbindung lief nach dem Sperren weiter: {[n['kind'] for n in gesehen]}"
        )
        assert waechter() is False, "der Waechter merkt die Sperre nicht"
    finally:
        await p._on_shutdown()
        konten.close()
