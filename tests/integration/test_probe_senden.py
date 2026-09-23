"""Eine Probe geht wirklich in den Broker (ausserhalb des Plans, U-Probe).

Test-Liste (AGENTS.md):

1. [ ] Der Sender baut seine Verbindung selbst auf und reiht wirklich ein.
2. [ ] Ein toter Broker wird benannt, nicht als Traceback geworfen.
"""

from __future__ import annotations

import uuid

import pytest

from swarmq_dashboard.probe import PROBE_NAME
from swarmq_dashboard.stream import StreamUnavailable
from swarmq_dashboard.web.probe import ProbeAuftrag, ProbeSender

pytestmark = pytest.mark.timeout(60)


async def test_der_sender_reiht_wirklich_ein(broker_url: str, backend_url: str) -> None:
    """Bis hierher lief alles gegen eine Attrappe.

    Geprueft wird die Naht zur Bibliothek: dass ``schedule`` mit diesen
    Argumenten existiert, dass der Task registriert sein muss und dass eine
    Kennung zurueckkommt.
    """
    queue = "io"
    sender = ProbeSender(broker_url=broker_url, backend_url=backend_url)
    try:
        kennungen = await sender.senden(
            ProbeAuftrag(queue=queue, anzahl=2, sleep_s=0.05,
                         note=f"itest-{uuid.uuid4().hex[:6]}")
        )
        assert len(kennungen) == 2
        assert all(k and isinstance(k, str) for k in kennungen), kennungen
        assert len(set(kennungen)) == 2
    finally:
        await sender.close()


async def test_ein_toter_broker_wird_benannt(backend_url: str) -> None:
    """R33 gilt auch hier: eine Auskunft, kein Traceback."""
    sender = ProbeSender(broker_url="amqp://guest:guest@127.0.0.1:1/",
                         backend_url=backend_url)
    try:
        with pytest.raises(StreamUnavailable, match="Broker"):
            await sender.senden(ProbeAuftrag(queue="io", anzahl=1))
    finally:
        await sender.close()


async def test_der_probe_task_ist_unter_seinem_namen_bekannt() -> None:
    """Der Name im Formular und der Name am Task sind dieselbe Zeichenkette."""
    from swarmq_dashboard.probe import ProbeTask

    assert ProbeTask.task_info.name == PROBE_NAME
    assert "io" in ProbeTask.task_info.queues and "cpu" in ProbeTask.task_info.queues


async def test_die_probe_steht_mit_ihren_argumenten_im_strom(
    broker_url: str, backend_url: str, stream_url: str
) -> None:
    """Die Notiz ist der Faden, an dem der Versuch in der Liste haengt.

    Ohne den Anwendungs-Hook kennt das Verzeichnis einen Task erst ab dem
    Aufgreifen -- ohne Einreihungszeitpunkt und **ohne Argumente**. Genau das
    ist im Betrieb aufgefallen: die Probe stand als Zeile da, aber die Notiz,
    nach der man sie sucht, war nirgends. Das Formular verspricht sie, also
    muss der Weg sie auch tragen.
    """
    import msgspec

    from swarmq_dashboard.events import TASK_STREAM
    from swarmq_dashboard.stream import ValkeyStreamClient

    notiz = f"itest-{uuid.uuid4().hex[:8]}"
    sender = ProbeSender(broker_url=broker_url, backend_url=backend_url,
                         stream_url=stream_url)
    strom = ValkeyStreamClient(stream_url)
    await strom.connect()
    try:
        await sender.senden(ProbeAuftrag(queue="io", anzahl=1, sleep_s=0.05, note=notiz))
        # Der Puffer schreibt im Takt; das Schliessen leert ihn.
        await sender.close()

        eintraege = await strom.read_after(TASK_STREAM, "0-0", 5000)
        einreihungen = [
            msgspec.json.decode(roh) for _, roh in eintraege
            if b'"task_enqueue"' in roh
        ]
        assert einreihungen, "keine Einreihung im Strom -- der Hook haengt nicht dran"
        traeger = [e for e in einreihungen if notiz in str(e.get("payload"))]
        assert traeger, f"die Notiz {notiz!r} steht in keiner Einreihung"
        nutzlast = str(traeger[0]["payload"])
        assert "sqd.probe" in nutzlast
    finally:
        await strom.close()
