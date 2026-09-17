"""Broker-Beobachtung: die beiden Zahlen, die die Bibliothek nicht herausgibt.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Eine Queue abfragen ===
1. Tiefe und Konsumentenzahl einer bestehenden Queue kommen zurueck.
2. Jede passive Deklaration bekommt einen eigenen Kanal (KTD11).
3. Jeder Kanal wird geschlossen -- auch im Erfolgsfall, sonst leckt jede
   Abfrage fuenf Kanaele, bis der Broker keine mehr gibt.

=== ZYKLUS 2: Fehlende Queues ===
4. Eine unbekannte Queue liefert "nicht vorhanden" statt eines Fehlers.
5. Nach einem Fehlschlag laeuft die naechste Abfrage normal weiter.
6. Ein Verbindungsfehler ist kein "nicht vorhanden" -- er wird benannt.

=== ZYKLUS 3: Prioritaetsvarianten (R14, R18, R28) ===
7. Die Tiefe summiert Basis- und Prioritaetsvarianten.
8. Eine fehlende Variante zaehlt als null, nicht als Fehler.
9. Die Konsumentenzahl ist das Maximum der Varianten, nicht ihre Summe.
10. Die verzoegerte Queue zaehlt getrennt, nicht in die Tiefe hinein.
11. Die Dead-Letter-Tiefe kommt aus derselben Quelle (R28).
"""

from __future__ import annotations

import pytest
from aio_pika.exceptions import ChannelNotFoundEntity

from swarmq_dashboard.broker import BrokerObserver, BrokerUnavailable


class _FakeDeclarationResult:
    def __init__(self, messages: int, consumers: int) -> None:
        self.message_count = messages
        self.consumer_count = consumers


class _FakeQueue:
    def __init__(self, messages: int, consumers: int) -> None:
        self.declaration_result = _FakeDeclarationResult(messages, consumers)


class _FakeChannel:
    def __init__(self, besitzer: _FakeConnection) -> None:
        self._besitzer = besitzer
        self.is_closed = False

    async def declare_queue(self, name: str, *, passive: bool = False) -> _FakeQueue:
        assert passive, "eine nicht-passive Deklaration wuerde die Queue anlegen"
        self._besitzer.declared.append(name)
        if name in self._besitzer.bestand:
            return _FakeQueue(*self._besitzer.bestand[name])
        # AMQP schliesst den Kanal bei einer fehlgeschlagenen Deklaration.
        self.is_closed = True
        raise ChannelNotFoundEntity(f"NOT_FOUND - no queue '{name}'")

    async def close(self) -> None:
        self.is_closed = True


class _FakeConnection:
    def __init__(self, bestand: dict[str, tuple[int, int]]) -> None:
        self.bestand = bestand
        self.declared: list[str] = []
        self.channels: list[_FakeChannel] = []
        self.is_closed = False

    async def channel(self) -> _FakeChannel:
        k = _FakeChannel(self)
        self.channels.append(k)
        return k

    async def close(self) -> None:
        self.is_closed = True


def _obs(bestand: dict[str, tuple[int, int]]) -> tuple[BrokerObserver, _FakeConnection]:
    con = _FakeConnection(bestand)
    o = BrokerObserver("amqp://unbenutzt/")
    o._connection = con  # type: ignore[assignment]
    return o, con


# --- Zyklus 1 -----------------------------------------------------------


async def test_tiefe_und_konsumenten_einer_queue() -> None:
    o, _ = _obs({"reports": (17, 3)})
    s = await o.stats("reports")
    assert s.exists
    assert s.depth == 17
    assert s.consumers == 3


async def test_je_deklaration_ein_eigener_kanal() -> None:
    """KTD11: ein gemeinsamer Kanal waere nach dem ersten Fehlschlag tot."""
    o, con = _obs({"reports": (1, 1)})
    await o.stats("reports")
    assert len(con.channels) == len(con.declared), (
        f"{len(con.declared)} Deklarationen auf {len(con.channels)} Kanaelen"
    )
    assert len(con.channels) >= 3, "Basis und beide Prioritaetsvarianten fehlen"


async def test_kanal_wird_immer_geschlossen() -> None:
    """Der Fehlerfall schliesst den Kanal von selbst -- AMQP tut das. Der
    Erfolgsfall nicht: dort muss der Beobachter selbst aufraeumen, sonst
    leckt jede Abfrage fuenf Kanaele und der Broker weist irgendwann ab.
    """
    for bestand in ({}, {"reports": (1, 1), "reports.high": (2, 1)}):
        o, con = _obs(bestand)
        await o.stats("reports" if bestand else "unbekannt")
        offen = [i for i, k in enumerate(con.channels) if not k.is_closed]
        assert not offen, f"Kanaele blieben offen (Bestand {bestand}): {offen}"


# --- Zyklus 2 -----------------------------------------------------------


async def test_unbekannte_queue_ist_kein_fehler() -> None:
    o, _ = _obs({})
    s = await o.stats("gibtesnicht")
    assert not s.exists
    assert s.depth == 0
    assert s.consumers == 0


async def test_nach_fehlschlag_laeuft_die_naechste_abfrage() -> None:
    o, _ = _obs({"reports": (5, 2)})
    await o.stats("gibtesnicht")
    s = await o.stats("reports")
    assert s.depth == 5, "der Fehlschlag hat die Verbindung vergiftet"


async def test_verbindungsfehler_wird_benannt() -> None:
    """Ein Netzfehler darf nicht als 'Queue leer' durchgehen -- das laese
    sich als 'kein Rueckstau' lesen, waehrend in Wahrheit nichts bekannt ist.
    """
    o, con = _obs({"reports": (5, 2)})

    async def kaputt() -> _FakeChannel:
        raise OSError("connection reset")

    con.channel = kaputt  # type: ignore[method-assign]
    with pytest.raises(BrokerUnavailable):
        await o.stats("reports")


# --- Zyklus 3 -----------------------------------------------------------


async def test_tiefe_summiert_prioritaetsvarianten() -> None:
    o, _ = _obs({"reports": (10, 2), "reports.high": (4, 2), "reports.low": (1, 2)})
    s = await o.stats("reports")
    assert s.depth == 15, (
        "nur die Basis-Queue gezaehlt -- die Prioritaetsvarianten sind eigene "
        f"AMQP-Queues: {s.parts}"
    )


async def test_fehlende_variante_zaehlt_als_null() -> None:
    o, _ = _obs({"reports": (10, 2)})
    s = await o.stats("reports")
    assert s.exists and s.depth == 10
    assert s.parts["reports.high"] is None


async def test_konsumenten_sind_das_maximum_nicht_die_summe() -> None:
    """Ein Worker mit Prioritaeten konsumiert aus allen drei Queues. Die
    Summe zaehlte ihn dreifach und liesse R18 melden, es seien genug Worker da.
    """
    o, _ = _obs({"reports": (0, 2), "reports.high": (0, 2), "reports.low": (0, 2)})
    s = await o.stats("reports")
    assert s.consumers == 2, f"dreifach gezaehlt: {s.consumers}"


async def test_verzoegerte_queue_zaehlt_getrennt() -> None:
    """Eine verzoegerte Nachricht wartet auf ihre Zeit, nicht auf einen
    Worker. In die Tiefe gerechnet liesse sie einen Rueckstau vortaeuschen.
    """
    o, _ = _obs({"reports": (3, 1), "reports.DQ": (40, 0)})
    s = await o.stats("reports")
    assert s.depth == 3, f"die verzoegerte Queue ist in die Tiefe geraten: {s.parts}"
    assert s.delayed == 40


async def test_dead_letter_tiefe_kommt_mit() -> None:
    o, _ = _obs({"reports": (0, 1), "reports.XQ": (7, 0)})
    s = await o.stats("reports")
    assert s.dead_letter == 7
    assert s.depth == 0, "die Dead-Letter-Queue ist in die Tiefe geraten"
