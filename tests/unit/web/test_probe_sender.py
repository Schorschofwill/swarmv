"""Der Weg vom Formular in den Broker.

Das Dashboard hat bisher **gelesen** und an zwei Stellen eingegriffen:
Stornieren setzt einen Merker, die Dead-Letter-Bedienung reiht um. Einreihen
ist das erste, was es von sich aus in eine Queue legt -- und damit die
Stelle, an der ein Formularfehler echte Arbeit erzeugt.

Test-Liste (AGENTS.md):

1. [ ] Ein Auftrag geht mit seinen Argumenten in die gewaehlte Queue.
2. [ ] Die Zahl der Auftraege wird eingehalten, jeder bekommt eine eigene Kennung.
3. [ ] Verzoegerung und Zeitpunkt zugleich werden abgewiesen.
4. [ ] Die Entdoppelung wird durchgereicht.
5. [ ] Ein nicht erreichbarer Broker wird benannt, nicht als Traceback geworfen.
6. [ ] Eine Zahl ausserhalb des Erlaubten wird vor dem Senden abgewiesen.
"""

from __future__ import annotations

from typing import Any

import pytest

from swarmq_dashboard.web.probe import ProbeAuftrag, ProbeSender


class FakeClient:
    """Statt SwarmQ: schreibt mit, womit eingereiht worden waere."""

    def __init__(self) -> None:
        self.gesendet: list[tuple[str, dict[str, Any]]] = []
        self.zaehler = 0

    async def schedule(self, task_name: str, **kwargs: Any) -> str:
        self.zaehler += 1
        self.gesendet.append((task_name, kwargs))
        return f"task-{self.zaehler}"


async def test_ein_auftrag_geht_mit_seinen_argumenten_in_die_queue() -> None:
    client = FakeClient()
    sender = ProbeSender(client=client)
    kennungen = await sender.senden(ProbeAuftrag(queue="cpu", sleep_s=1.5, note="Probe A"))
    assert kennungen == ["task-1"]
    name, kwargs = client.gesendet[0]
    assert name == "sqd.probe"
    assert kwargs["queue"] == "cpu"
    assert kwargs["sleep_s"] == 1.5
    assert kwargs["note"] == "Probe A"


async def test_die_zahl_der_auftraege_wird_eingehalten() -> None:
    """Zwanzig Proben sind zwanzig Tasks, jeder mit eigener Kennung.

    Die Zahl ist das Mittel, um einen Rueckstau ueberhaupt zu sehen: ein
    einzelner Task steht nie in der Queue, er ist sofort beim Worker.
    """
    client = FakeClient()
    kennungen = await ProbeSender(client=client).senden(
        ProbeAuftrag(queue="io", anzahl=20, sleep_s=0.1)
    )
    assert len(kennungen) == 20
    assert len(set(kennungen)) == 20, "zwei Proben teilen sich eine Kennung"
    assert len(client.gesendet) == 20


async def test_verzoegerung_und_zeitpunkt_zugleich_werden_abgewiesen() -> None:
    """Die Bibliothek nimmt eines von beidem -- und wirft sonst mitten im Senden.

    Abgewiesen wird vorher: sonst laegen die ersten Proben schon in der
    Queue, wenn die Ausnahme kommt, und niemand wuesste wie viele.
    """
    from datetime import UTC, datetime

    from swarmq_dashboard.web.probe import ProbeFehleingabe

    client = FakeClient()
    with pytest.raises(ProbeFehleingabe, match="zugleich"):
        await ProbeSender(client=client).senden(
            ProbeAuftrag(delay_s=5, eta=datetime.now(UTC))
        )
    assert client.gesendet == [], "es wurde trotzdem eingereiht"


async def test_die_entdoppelung_wird_durchgereicht() -> None:
    """R3.5 im Kern: eine zweite gleiche Einreihung gibt die erste Kennung zurueck."""
    client = FakeClient()
    await ProbeSender(client=client).senden(
        ProbeAuftrag(unique_for=60, unique_until="start")
    )
    _, kwargs = client.gesendet[0]
    assert kwargs["unique_for"] == 60
    assert kwargs["unique_until"] == "start"


async def test_eine_fremde_queue_wird_abgewiesen() -> None:
    """Der Task ist auf zwei Queues angemeldet; eine dritte erreicht keinen Worker.

    Ungeprueft laege die Probe in einer Queue, die niemand liest -- sie
    stuende als "eingereiht" in der Liste und liefe nie.
    """
    from swarmq_dashboard.web.probe import ProbeFehleingabe

    client = FakeClient()
    with pytest.raises(ProbeFehleingabe, match="angemeldeten"):
        await ProbeSender(client=client).senden(ProbeAuftrag(queue="gibtsnicht"))
    assert client.gesendet == []


async def test_eine_unsinnige_anzahl_wird_vor_dem_senden_abgewiesen() -> None:
    """Tausend Proben von Hand sind ein Tippfehler, keine Absicht."""
    from swarmq_dashboard.web.probe import ProbeFehleingabe

    client = FakeClient()
    for anzahl in (0, -3, 1000):
        with pytest.raises(ProbeFehleingabe, match="Anzahl"):
            await ProbeSender(client=client).senden(ProbeAuftrag(anzahl=anzahl))
    assert client.gesendet == []
