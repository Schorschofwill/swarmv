"""Verfuegbarkeit des Ereignisstroms -- benannt statt geraten.

Befund aus der Pruefung von Stufe B: ``connect()`` baute gar keine Verbindung
auf. ``Valkey.from_url`` erzeugt nur ein Objekt; gemessen kam ein Aufruf gegen
einen toten Port in 0,1 ms **ohne Fehler** zurueck. Fuer die Zusage aus R33 --
"fehlt eine Voraussetzung, sagt die Seite das benannt" -- gab es an dieser
Stelle also nichts zu pruefen, und ein Fehler erschien erst spaeter, als rohe
Valkey-Ausnahme ohne Gegenstueck zu ``BrokerUnavailable``.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Der Aufbau sagt die Wahrheit ===
1. Ein Aufbau gegen einen toten Port scheitert benannt als StreamUnavailable.
2. Ein Aufbau mit unbrauchbarer Adresse ebenso.
3. Ein Gegenueber, das TCP annimmt und dann schweigt, haelt den Takt nicht
   endlos an.
3a. Die Zeitgrenzen erreichen den Client selbst, nicht nur den Aufruf.
3b. Eine Haengestelle, die keine Socket-Grenze abfaengt, wird dennoch
    abgebrochen.

=== ZYKLUS 2: Auch im Betrieb ===
4. Ein Netzfehler beim Lesen kommt als StreamUnavailable, nicht als fremder
   Ausnahmetyp -- auch bei einer bestehenden Verbindung, die wegbricht.
5. Ein Aufruf ohne Verbindung wird benannt abgewiesen.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from swarmq_dashboard.stream import StreamUnavailable, ValkeyStreamClient


def _toter_port() -> int:
    """Ein Port, auf dem sicher niemand lauscht."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --- Zyklus 1 -----------------------------------------------------------


async def test_aufbau_gegen_toten_port_wird_benannt() -> None:
    c = ValkeyStreamClient(f"valkey://127.0.0.1:{_toter_port()}/1")
    with pytest.raises(StreamUnavailable):
        await c.connect()
    await c.close()


async def test_aufbau_mit_unbrauchbarer_adresse_wird_benannt() -> None:
    c = ValkeyStreamClient("kein-schema-und-kein-wirt")
    with pytest.raises(StreamUnavailable):
        await c.connect()


async def test_aufbau_gegen_ein_schweigendes_gegenueber(tmp_path: object) -> None:
    """Ein Gegenueber, das TCP annimmt und dann schweigt -- eine Firewall, die
    verwirft statt abzulehnen.

    Die Verbindung wird **offen gehalten**: schliesst der Testserver sie
    sofort, kommt ein Verbindungsfehler statt einer Zeitueberschreitung
    zurueck, und der Pfad, um den es hier geht, wird nie betreten.
    """
    offen: list[asyncio.StreamWriter] = []

    async def schweigt(_: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        offen.append(w)
        await asyncio.Event().wait()

    server = await asyncio.start_server(schweigt, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        c = ValkeyStreamClient(f"valkey://127.0.0.1:{port}/1", connect_timeout_s=0.3)
        beginn = asyncio.get_running_loop().time()
        with pytest.raises(StreamUnavailable):
            await c.connect()
        gedauert = asyncio.get_running_loop().time() - beginn
        assert gedauert < 2.0, f"der Aufbau hing {gedauert:.1f} s"
        await c.close()
    finally:
        for w in offen:
            w.close()
        server.close()
        await server.wait_closed()


async def test_zeitgrenzen_erreichen_den_client(valkey_adresse: tuple[str, int]) -> None:
    """Die beiden Grenzen muessen im Client ankommen, nicht nur im Aufruf.

    ``wait_for`` umspannt nur den Aufbau. Alles danach -- und jede
    Verbindung, die der Verbindungspool spaeter selbst nachzieht -- haengt an
    diesen beiden Werten. Ohne sie wartet ein Aufruf gegen eine tote Leitung
    so lange, wie das Betriebssystem meint.
    """
    wirt, port = valkey_adresse
    c = ValkeyStreamClient(f"valkey://{wirt}:{port}/1",
                           connect_timeout_s=1.5, socket_timeout_s=2.5)
    try:
        await c.connect()
    except StreamUnavailable:
        pytest.skip("Valkey laeuft nicht -- dieser Test braucht eine echte Verbindung")
    kwargs = c.client.connection_pool.connection_kwargs
    assert kwargs.get("socket_connect_timeout") == 1.5, kwargs
    assert kwargs.get("socket_timeout") == 2.5, kwargs
    await c.close()


async def test_haengender_aufruf_wird_abgebrochen() -> None:
    """Nicht jede Haengestelle faengt eine Socket-Zeitgrenze ab -- eine
    Namensaufloesung etwa laeuft davor und unbegrenzt. Deshalb liegt um den
    Aufbau zusaetzlich eine eigene Grenze.
    """
    class HaengenderClient:
        async def ping(self) -> bool:
            await asyncio.Event().wait()
            return True

        async def aclose(self) -> None:
            return None

    c = ValkeyStreamClient("valkey://127.0.0.1:6399/1", connect_timeout_s=0.2)
    c._bauen = lambda: HaengenderClient()  # type: ignore[assignment,return-value]
    beginn = asyncio.get_running_loop().time()
    with pytest.raises(StreamUnavailable):
        await c.connect()
    assert asyncio.get_running_loop().time() - beginn < 2.0, "der Aufbau haengt"


async def test_fehler_eines_aufrufs_wird_benannt() -> None:
    """Der Lesepfad muss denselben Typ liefern wie der Aufbau -- auch bei
    einer **bestehenden** Verbindung, die mitten im Betrieb wegbricht.
    """
    from valkey.exceptions import ConnectionError as ValkeyConnectionError

    class KaputterClient:
        async def xlen(self, key: str) -> int:
            raise ValkeyConnectionError("connection reset by peer")

        async def aclose(self) -> None:
            return None

    c = ValkeyStreamClient("valkey://127.0.0.1:6399/1")
    c._client = KaputterClient()  # type: ignore[assignment]
    with pytest.raises(StreamUnavailable):
        await c.length("sqd:events")


# --- Zyklus 2 -----------------------------------------------------------


async def test_netzfehler_beim_lesen_wird_benannt() -> None:
    """Sonst muss jede Taktschleife Valkey-Ausnahmetypen importieren, statt
    einen Domaenentyp zu fangen -- und fasst dabei unvermeidlich zu weit oder
    zu eng.
    """
    c = ValkeyStreamClient(f"valkey://127.0.0.1:{_toter_port()}/1")
    with pytest.raises(StreamUnavailable):
        await c.connect()
    # Auch ohne gelungenen Aufbau muss der Lesepfad denselben Typ liefern.
    with pytest.raises(StreamUnavailable):
        await c.length("sqd:events")


async def test_aufruf_ohne_verbindung_wird_benannt() -> None:
    c = ValkeyStreamClient("valkey://127.0.0.1:6399/1")
    with pytest.raises(StreamUnavailable):
        await c.length("sqd:events")
