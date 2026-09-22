"""Fristen und Aufraeumen beim Abbruch (Nachlauf ueber die Stufe-D-Korrekturen).

Der Befund: die Frist, die den Auffrischungstakt gegen Haenger sichern sollte,
war **kuerzer** als die Fristen, die sie umschloss -- aussen 2 s, innen 5 s
(Valkey) und 10 s (Broker). Damit brach die aeussere Frist die innere
**immer** zuerst ab, und zwar mit ``CancelledError``. Das ist eine
``BaseException`` und laeuft an jedem ``except Exception`` vorbei: der halb
aufgebaute Klient wurde nie geschlossen.

Gemessen ergab das 25 offene Dateizeiger nach 25 Takten und 10 offene
AMQP-Kanaele nach 10 Takten. Bei zehn Sekunden Takt und dem ueblichen
Deckel von 1024 Dateizeigern ist der Dienst nach gut einer Stunde tot --
ausgeloest von genau der Stoerung, die er sichtbar machen soll. Und der
Kanal-Pfad war ausdruecklich **gegen** ein frueheres Kanalleck geschrieben.

Zwei Lehren, beide hier festgehalten:

1. Eine umschliessende Frist muss laenger sein als die, die sie umschliesst
   -- sonst ist die innere tot und ihre Aufraeumarbeit unerreichbar.
2. Aufraeumen muss gegen ``BaseException`` stehen, nicht gegen ``Exception``.
   Ein Abbruch ist kein Fehler, aber er hinterlaesst denselben Schmutz.

Test-Liste (AGENTS.md):

1. Die aeussere Frist ist laenger als jede von ihr umschlossene.
2. Ein abgebrochener Strom-Aufbau laesst keinen Klienten zurueck.
3. Ein abgebrochener Strom-Aufbau auf einem stehenden Klienten ebenso wenig.
4. Eine abgebrochene Broker-Pruefung laesst keinen Kanal offen.
5. Eine abgebrochene Broker-Pruefung laesst keine Verbindung zurueck.
6. Eine **gelungene** Broker-Pruefung schliesst ihren Probekanal.
"""

from __future__ import annotations

import asyncio
from typing import Any


def test_die_aeussere_frist_ist_laenger_als_die_inneren() -> None:
    """Die Schachtelung selbst, als Zusicherung.

    Sie ist billig und faengt den Fehler an der Wurzel: eine aeussere Frist
    unter der inneren macht die innere unerreichbar, samt ihrem Aufraeumen.
    """
    from swarmq_dashboard.broker import CONNECT_TIMEOUT_S as BROKER_S
    from swarmq_dashboard.stream import DEFAULT_CONNECT_TIMEOUT_S as STROM_S
    from swarmq_dashboard.web.app import PROBE_TIMEOUT_S

    for name, innen in (("Strom", STROM_S), ("Broker", BROKER_S)):
        assert innen < PROBE_TIMEOUT_S, (
            f"die aeussere Frist ({PROBE_TIMEOUT_S} s) ist nicht laenger als die "
            f"des {name}s ({innen} s) -- sie bricht ihn ab, bevor er aufraeumen kann"
        )


class HaengenderClient:
    """Ein Klient, dessen ``ping`` nie zurueckkehrt -- das schweigende
    Gegenueber, das der Modulkopf von ``stream.py`` ausdruecklich nennt."""

    def __init__(self) -> None:
        self.geschlossen = False

    async def ping(self) -> bool:
        await asyncio.Event().wait()
        raise AssertionError("unerreichbar")

    async def aclose(self) -> None:
        self.geschlossen = True


async def test_ein_abgebrochener_aufbau_laesst_keinen_klienten_zurueck() -> None:
    """Sonst leckt jeder Takt einen Socket, solange die Stoerung anhaelt."""
    from swarmq_dashboard.stream import ValkeyStreamClient

    client = HaengenderClient()

    class Pruefbar(ValkeyStreamClient):
        def _bauen(self) -> Any:
            return client

    c = Pruefbar("valkey://127.0.0.1:1/1")
    aufgabe = asyncio.ensure_future(c.connect())
    await asyncio.sleep(0.02)
    aufgabe.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await aufgabe

    assert client.geschlossen, (
        "der halb aufgebaute Klient wurde nicht geschlossen -- jeder Takt "
        "laesst so einen Socket zurueck"
    )


async def test_ein_abgebrochener_ping_auf_einem_stehenden_klienten() -> None:
    """Derselbe Weg, nur mit schon stehender Verbindung: auch dort darf der
    Abbruch den Klienten nicht liegen lassen."""
    from swarmq_dashboard.stream import ValkeyStreamClient

    class ErstGutDannStumm:
        def __init__(self) -> None:
            self.pings = 0
            self.geschlossen = False

        async def ping(self) -> bool:
            self.pings += 1
            if self.pings == 1:
                return True
            await asyncio.Event().wait()
            raise AssertionError("unerreichbar")

        async def aclose(self) -> None:
            self.geschlossen = True

    client = ErstGutDannStumm()

    class Pruefbar(ValkeyStreamClient):
        def _bauen(self) -> Any:
            return client

    c = Pruefbar("valkey://127.0.0.1:1/1")
    await c.connect()
    aufgabe = asyncio.ensure_future(c.connect())
    await asyncio.sleep(0.02)
    aufgabe.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await aufgabe

    assert client.geschlossen, "der stehende Klient blieb beim Abbruch liegen"
    assert c._client is None, "der Dienst haelt weiter einen toten Klienten"


async def test_eine_abgebrochene_broker_pruefung_laesst_nichts_offen() -> None:
    """Das Kanalleck, gegen das die Pruefung geschrieben wurde -- diesmal
    ueber den Abbruch statt ueber ``is_closed``."""
    from swarmq_dashboard.broker import BrokerObserver

    class Kanal:
        def __init__(self) -> None:
            self.geschlossen = False

        async def close(self) -> None:
            self.geschlossen = True

    kanaele: list[Kanal] = []

    class LangsameVerbindung:
        is_closed = False

        def __init__(self) -> None:
            self.geschlossen = False

        async def channel(self) -> Kanal:
            await asyncio.sleep(5)
            k = Kanal()
            kanaele.append(k)
            return k

        async def close(self) -> None:
            self.geschlossen = True

    verbindung = LangsameVerbindung()
    o = BrokerObserver("amqp://guest:guest@127.0.0.1:1/")
    o._connection = verbindung                     # type: ignore[assignment]

    aufgabe = asyncio.ensure_future(o.connect())
    await asyncio.sleep(0.02)
    aufgabe.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await aufgabe

    assert all(k.geschlossen for k in kanaele), (
        f"{sum(not k.geschlossen for k in kanaele)} Kanaele blieben offen"
    )
    assert verbindung.geschlossen or o._connection is None, (
        "die Verbindung wurde beim Abbruch weder geschlossen noch verworfen"
    )


async def test_die_broker_pruefung_schliesst_ihren_probekanal() -> None:
    """Der **Erfolgsfall** -- und der ist der haeufige.

    Der Test daneben bricht ab, bevor der Kanal ueberhaupt entsteht; er kann
    darum nicht bemerken, ob ein entstandener Kanal wieder zugeht. Genau so
    ueberlebte die Mutation "schliesse den Probekanal nicht" eine erste
    Fassung dieser Datei. Bei zehn Sekunden Takt waeren das 8640 Kanaele am
    Tag gegen eine Obergrenze von 2047 je Verbindung.
    """
    from swarmq_dashboard.broker import BrokerObserver

    class Kanal:
        def __init__(self) -> None:
            self.geschlossen = False

        async def close(self) -> None:
            self.geschlossen = True

    kanaele: list[Kanal] = []

    class GesundeVerbindung:
        is_closed = False

        async def channel(self) -> Kanal:
            k = Kanal()
            kanaele.append(k)
            return k

        async def close(self) -> None: ...

    o = BrokerObserver("amqp://guest:guest@127.0.0.1:1/")
    o._connection = GesundeVerbindung()        # type: ignore[assignment]

    for _ in range(5):
        await o.connect()

    assert len(kanaele) == 5, f"es wurden {len(kanaele)} Kanaele geoeffnet"
    offen = [k for k in kanaele if not k.geschlossen]
    assert not offen, (
        f"{len(offen)} von {len(kanaele)} Probekanaelen blieben offen -- "
        "je Takt einer, gegen eine Obergrenze von 2047 je Verbindung"
    )
