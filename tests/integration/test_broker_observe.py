"""Broker-Beobachtung gegen echtes RabbitMQ.

Test-Liste (AGENTS.md):

1. Tiefe und Konsumentenzahl einer echten Queue kommen zurueck.
2. Zehn Abfragen hintereinander lassen die Nachrichtenzahl unveraendert --
   die Zusage aus R28: beliebig oft, ohne Wirkung.
3. Eine nicht vorhandene Queue liefert "nicht vorhanden" und laesst die
   Verbindung brauchbar.
4. Die Tiefe summiert ueber die Prioritaetsvarianten.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import aio_pika
import pytest

from swarmq_dashboard.broker import BrokerObserver

pytestmark = pytest.mark.integration


@pytest.fixture
async def beobachter(broker_url: str) -> AsyncIterator[BrokerObserver]:
    o = BrokerObserver(broker_url)
    await o.connect()
    yield o
    await o.close()


@pytest.fixture
async def queues(broker_url: str) -> AsyncIterator[str]:
    """Eine Basis-Queue plus Prioritaetsvariante, mit Nachrichten gefuellt."""
    name = f"sqd-test-{uuid.uuid4().hex[:8]}"
    con = await aio_pika.connect_robust(broker_url)
    kanal = await con.channel()
    for voll, zahl in ((name, 3), (f"{name}.high", 2)):
        # durable=True: neuere RabbitMQ-Versionen lehnen fluechtige, nicht
        # exklusive Queues ab. Die Vorrichtung raeumt sie unten selbst weg.
        q = await kanal.declare_queue(voll, durable=True)
        for i in range(zahl):
            await kanal.default_exchange.publish(
                aio_pika.Message(body=f"{i}".encode()), routing_key=q.name
            )
    yield name
    for voll in (name, f"{name}.high"):
        try:
            await kanal.queue_delete(voll)
        except Exception:
            pass
    await con.close()


async def test_tiefe_einer_echten_queue(beobachter: BrokerObserver, queues: str) -> None:
    s = await beobachter.stats(queues)
    assert s.exists
    assert s.depth == 5, f"Basis und .high nicht zusammengezaehlt: {s.parts}"
    assert s.consumers == 0


async def test_zehn_abfragen_aendern_nichts(beobachter: BrokerObserver, queues: str) -> None:
    """R28: die Tiefe ist beliebig oft abfragbar, ohne dass ein Konsument
    etwas davon merkt. Eine mutierende Abfrage -- etwa ein Drain wie in
    ``list_dead_letters`` -- liesse die Zahl hier sinken.
    """
    vorher = (await beobachter.stats(queues)).depth
    for _ in range(10):
        await beobachter.stats(queues)
    assert (await beobachter.stats(queues)).depth == vorher == 5


async def test_unbekannte_queue_laesst_die_verbindung_brauchbar(
    beobachter: BrokerObserver, queues: str
) -> None:
    s = await beobachter.stats(f"gibtesnicht-{uuid.uuid4().hex[:8]}")
    assert not s.exists and s.depth == 0
    # Dieselbe Verbindung muss danach weiter antworten -- der Kanal ist
    # weggeworfen, nicht die Verbindung.
    assert (await beobachter.stats(queues)).depth == 5
