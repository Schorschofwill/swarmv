"""Die Dead-Letter-Seite gegen echtes RabbitMQ (U15 Teil 2, R28, R28a, AE12a).

Der eine Punkt, den nur diese Ebene beweisen kann: **Ansehen kostet nichts.**

Die Attrappe in der Testreihe zaehlt mit, dass die Seite ``list_dead_letters``
nicht ruft -- das ist eine Aussage ueber meinen Code. Ob die Nachrichten
danach wirklich unangetastet in der Queue liegen, weiss nur der Broker. Und
die Gegenprobe gehoert dazu: ein Sichten **veraendert** sie messbar, sonst
waere die ganze Warnung in R28a Aberglaube und der Riegel unnoetiger Aufwand.

Test-Liste (AGENTS.md):

1. Covers AE12a. Zehn Seitenaufrufe lassen die Nachrichtenzahl unveraendert.
2. Die Gegenprobe: ein Sichten reiht wirklich um -- die Reihenfolge aendert
   sich, und der Zustellzaehler steigt.
3. Leeren entfernt die Nachrichten wirklich, und erst nach der Bestaetigung.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import aio_pika
import pytest

from swarmq_dashboard.broker import BrokerObserver
from swarmq_dashboard.dlq import DlqService, SwarmQDlqAdmin

pytestmark = pytest.mark.integration


@pytest.fixture
async def dlq(broker_url: str) -> AsyncIterator[str]:
    """Eine Arbeits-Queue mit gefuellter Dead-Letter-Queue ``{name}.XQ``.

    Von Hand aufgebaut statt ueber die Bibliothek: der Test soll die
    Bedienung pruefen, nicht die Frage, ob sich swarmq eine DLQ korrekt
    anlegt.
    """
    name = f"sqd-dlq-{uuid.uuid4().hex[:8]}"
    con = await aio_pika.connect_robust(broker_url)
    kanal = await con.channel()
    await kanal.declare_queue(name, durable=True)
    q = await kanal.declare_queue(f"{name}.XQ", durable=True)
    for i in range(3):
        await kanal.default_exchange.publish(
            aio_pika.Message(
                body=(f'{{"task_id": "t-{i}", "task_name": "reports.build", '
                      '"args": [], "kwargs": {}}').encode()
            ),
            routing_key=q.name,
        )
    await kanal.close()
    yield name
    kanal = await con.channel()
    for voll in (name, f"{name}.XQ"):
        try:
            await kanal.queue_delete(voll)
        except Exception:
            pass
    await con.close()


async def test_zehn_aufrufe_lassen_die_zahl_unveraendert(broker_url: str, dlq: str) -> None:
    """Covers AE12a. Der Dauerzustand der Seite ist die passive Tiefe.

    Holte die Seite ihre Zahl ueber ``list_dead_letters``, kostete jeder
    Aufruf eine komplette Umreihung -- und ein offener Bildschirm mit
    Selbstauffrischung waere selbst die Stoerung.
    """
    beobachter = BrokerObserver(broker_url)
    await beobachter.connect()
    try:
        for _ in range(10):
            stats = (await beobachter.stats_many([dlq]))[dlq]
            assert stats.dead_letter == 3, f"die Tiefe wanderte: {stats.dead_letter}"
    finally:
        await beobachter.close()

    # Und der Broker selbst bestaetigt es, nicht nur meine eigene Abfrage.
    con = await aio_pika.connect_robust(broker_url)
    kanal = await con.channel()
    q = await kanal.declare_queue(f"{dlq}.XQ", durable=True, passive=True)
    assert q.declaration_result.message_count == 3
    await con.close()


async def test_sichten_reiht_wirklich_um(broker_url: str, dlq: str) -> None:
    """Die Gegenprobe zu R28a.

    Ohne sie waere nicht belegt, dass die Warnung ueberhaupt etwas beschreibt
    -- und ein Riegel gegen einen Vorgang ohne Wirkung waere blosser Aufwand.

    Gemessen wird die **Reihenfolge**. Eine Sichtung mit Deckel 1 liest die
    vorderste Nachricht und veroeffentlicht sie neu; sie liegt danach hinten.
    Und sie liegt dort als *neue* Nachricht: ``redelivered`` ist falsch und
    der Zustellzaehler fehlt. Das ist kein Nebenbefund, sondern der Kern der
    Warnung -- eine Nachricht, die schon fuenfmal gescheitert ist, sieht nach
    einer Sichtung fabrikneu aus.
    """
    admin = SwarmQDlqAdmin(broker_url)
    try:
        ergebnis = await DlqService(admin).peek(dlq, limit=1)
        assert ergebnis.error is None, ergebnis.error
        assert ergebnis.gelesen == 1
    finally:
        await admin.close()

    con = await aio_pika.connect_robust(broker_url)
    kanal = await con.channel()
    q = await kanal.declare_queue(f"{dlq}.XQ", durable=True, passive=True)
    assert q.declaration_result.message_count == 3, "die Sichtung hat Nachrichten verloren"

    nachricht = await q.get(no_ack=False, fail=False)
    assert nachricht is not None
    assert b'"t-1"' in nachricht.body, (
        f"vorne liegt noch {nachricht.body!r} -- dann hat die Sichtung nichts "
        "umgereiht und R28a waere gegenstandslos"
    )
    assert not nachricht.redelivered and not nachricht.headers.get("x-delivery-count"), (
        "die Nachricht traegt noch ihre Zustellgeschichte -- die Warnung, dass "
        "der Zaehler von vorn beginnt, waere dann falsch"
    )
    await nachricht.nack(requeue=True)
    await con.close()


async def test_leeren_entfernt_wirklich(broker_url: str, dlq: str) -> None:
    """R30. Eine falsche Bestaetigung laesst alles stehen, die richtige
    raeumt endgueltig -- beides am echten Broker nachgemessen."""
    admin = SwarmQDlqAdmin(broker_url)
    dienst = DlqService(admin)
    try:
        abgewiesen = await dienst.purge(dlq, bestaetigung=dlq, erwartet=99)
        assert abgewiesen.error and abgewiesen.moved == 0

        con = await aio_pika.connect_robust(broker_url)
        kanal = await con.channel()
        q = await kanal.declare_queue(f"{dlq}.XQ", durable=True, passive=True)
        assert q.declaration_result.message_count == 3, "die Abweisung hat trotzdem geloescht"
        await con.close()

        geleert = await dienst.purge(dlq, bestaetigung=dlq, erwartet=3)
        assert geleert.error is None, geleert.error
        assert geleert.moved == 3
    finally:
        await admin.close()

    con = await aio_pika.connect_robust(broker_url)
    kanal = await con.channel()
    q = await kanal.declare_queue(f"{dlq}.XQ", durable=True, passive=True)
    assert q.declaration_result.message_count == 0, "es liegt noch etwas da"
    await con.close()
