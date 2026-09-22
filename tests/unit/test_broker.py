"""Broker-Beobachtung: die beiden Zahlen, die die Bibliothek nicht herausgibt.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Eine Queue abfragen ===
1. Tiefe und Konsumentenzahl einer bestehenden Queue kommen zurueck.
2. Jede passive Deklaration bekommt einen eigenen Kanal (KTD11).
3. Jeder Kanal wird zurueckgegeben -- gemessen am Aufruf von close(),
   nicht an is_closed: ein robuster Kanal heilt sich selbst und bleibt
   dem Broker sonst erhalten, bis dessen Obergrenze faellt.

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

=== ZYKLUS 4: Was aus fremden Nutzlasten kommt ===
12. Ein unbrauchbarer Queue-Name ist "nicht vorhanden", kein Broker-Ausfall.
13. Ein langer Name sprengt nicht die Grenze der Prioritaetsvarianten.
14. stats_many() kapselt je Queue und liefert alle Queues.
15. Zulaessige Namen werden nicht abgewiesen -- die Regel ist nicht enger
    als das Protokoll.
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
    """Bildet den Kanal so nach, wie er sich wirklich verhaelt.

    Zwei Eigenheiten sind hier entscheidend, und beide hat eine nachgiebige
    Attrappe frueher verdeckt:

    * Nach einer fehlgeschlagenen Deklaration ist der Kanal tot. Eine weitere
      Deklaration darauf wirft; eine Attrappe, die weiter antwortet, laesst
      einen geteilten Kanal gruen durchgehen.
    * ``is_closed`` allein sagt nichts darueber, ob der Broker den Kanal los
      ist: ein robuster Kanal stellt sich nach dem Fehler selbst wieder her.
      Gemessen blieben so 50 Kanaele stehen, obwohl ``is_closed`` True war.
      Deshalb zaehlt hier, ob ``close()`` **aufgerufen** wurde.
    """

    def __init__(self, besitzer: _FakeConnection) -> None:
        self._besitzer = besitzer
        self.is_closed = False
        self.close_aufrufe = 0

    async def declare_queue(self, name: str, *, passive: bool = False) -> _FakeQueue:
        assert passive, "eine nicht-passive Deklaration wuerde die Queue anlegen"
        if self.is_closed:
            raise RuntimeError("Deklaration auf einem geschlossenen Kanal")
        # Wie die echte Protokollschicht: sie prueft Zeichenvorrat und Groesse
        # lokal und wirft, bevor irgendetwas zum Broker geht. Eine Attrappe,
        # die das durchgehen laesst, verdeckt genau diesen Fall.
        import pamqp.commands

        pamqp.commands.Queue.Declare(queue=name, passive=True).marshal()
        self._besitzer.declared.append(name)
        if name in self._besitzer.bestand:
            return _FakeQueue(*self._besitzer.bestand[name])
        # AMQP schliesst den Kanal bei einer fehlgeschlagenen Deklaration.
        self.is_closed = True
        raise ChannelNotFoundEntity(f"NOT_FOUND - no queue '{name}'")

    async def close(self) -> None:
        self.close_aufrufe += 1
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


async def test_jeder_kanal_wird_zurueckgegeben() -> None:
    """Geprueft wird der **Aufruf** von ``close()``, nicht ``is_closed``.

    Nach einer fehlgeschlagenen Deklaration meldet der Kanal sich selbst als
    geschlossen -- der Broker ist ihn deswegen aber nicht los: ein robuster
    Kanal stellt sich im Hintergrund wieder her. Gemessen blieben nach 50
    Fehlproben 50 Kanaele auf dem Broker stehen, obwohl jeder einzelne
    ``is_closed`` meldete. Bei dreissig Queues ist die ausgehandelte
    Obergrenze von 2047 nach siebzehn Takten erreicht, und dann ist die
    ganze Verbindung fort.
    """
    for bestand in ({}, {"reports": (1, 1), "reports.high": (2, 1)}):
        o, con = _obs(bestand)
        await o.stats("reports" if bestand else "unbekannt")
        ohne = [i for i, k in enumerate(con.channels) if k.close_aufrufe == 0]
        assert not ohne, (
            f"Kanaele wurden nicht zurueckgegeben (Bestand {bestand}): {ohne}"
        )


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


# --- Zyklus 4: Was aus fremden Nutzlasten kommt --------------------------


async def test_unbrauchbarer_queue_name_ist_kein_broker_ausfall() -> None:
    """Die Protokollschicht lehnt Namen lokal ab, noch vor dem Netz. Frueher
    wurde daraus ein Broker-Ausfall -- also genau die Aussage, die einem
    Netzfehler vorbehalten ist. Ein einziger fauler Name aus einem
    Stammdaten-Ereignis blendete damit die Tiefen *aller* Queues aus.
    """
    o, _ = _obs({"reports": (5, 1)})
    for name in ("müller", "q\nx", "a" * 300, ""):
        s = await o.stats(name)
        assert not s.exists and s.depth == 0, f"{name!r} lieferte {s}"
    assert (await o.stats("reports")).depth == 5


async def test_langer_name_sprengt_nicht_die_variantengrenze() -> None:
    """Der Zusatz ``.high`` sprengt die Grenze von 255 Zeichen. Ein fuer
    RabbitMQ voellig zulaessiger Basisname ab 252 Zeichen kippte so die
    ganze Abfrage.
    """
    lang = "q" * 252
    o, _ = _obs({lang: (7, 1)})
    s = await o.stats(lang)
    assert s.depth == 7, f"der lange Name kippte die Abfrage: {s}"
    assert s.parts[f"{lang}.high"] is None


async def test_stats_many_kapselt_je_queue() -> None:
    """Faellt der Broker mitten im Lauf weg, sind die bereits gemessenen
    Queues trotzdem etwas wert. Frueher verwarf eine einzige Ausnahme den
    ganzen Lauf.
    """
    o, con = _obs({"a": (1, 1), "b": (2, 1)})
    echte_channel = con.channel
    zaehler = {"n": 0}

    async def manchmal_kaputt() -> _FakeChannel:
        zaehler["n"] += 1
        if zaehler["n"] > 5:      # ab der zweiten Queue
            raise OSError("connection reset")
        return await echte_channel()

    con.channel = manchmal_kaputt  # type: ignore[method-assign]
    ergebnis = await o.stats_many(["a", "b"])
    assert ergebnis["a"].depth == 1, "die fertige Messung wurde mitverworfen"
    assert ergebnis["b"].error is not None, "der Ausfall wurde nicht benannt"
    assert not ergebnis["b"].exists


async def test_stats_many_liefert_alle_queues() -> None:
    o, _ = _obs({"a": (1, 1), "b": (2, 3)})
    e = await o.stats_many(["a", "b"])
    assert {k: v.depth for k, v in e.items()} == {"a": 1, "b": 2}


async def test_zulaessige_namen_werden_nicht_abgewiesen() -> None:
    """Die Namensregel darf nicht **strenger** sein als das Protokoll.

    ``ablage/eingang`` und ``queue 1`` sind zulaessige AMQP-Namen. Sie als
    "gibt es nicht" zu melden waere dieselbe stille Falschaussage, gegen die
    dieses Modul sonst argumentiert -- nur in die andere Richtung.
    """
    bestand = {n: (3, 1) for n in ("ablage/eingang", "queue 1", "a+b,c#d@e", "q" * 255)}
    o, _ = _obs(bestand)
    for name in bestand:
        s = await o.stats(name)
        assert s.exists and s.depth == 3, f"zulaessiger Name abgewiesen: {name!r} -> {s}"
