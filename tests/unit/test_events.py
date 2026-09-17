"""Ereignishuelle: das Format, das alle drei Aufnahmestellen teilen.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Huelle ===
1. Ein Ereignis traegt Typ, Zeitstempel, Quelle, Herkunft und Nutzlast.
2. Die Herkunft kennt genau die beiden Faelle aus R31.

=== ZYKLUS 2: Kodierung ===
3. Kodieren und Dekodieren ist verlustfrei.
4. Ein unbekanntes Zusatzfeld in der Nutzlast ueberlebt die Runde (R3: vollstaendige Nutzlast).
5. Nicht kodierbare Werte in der Nutzlast lassen das Kodieren nicht scheitern.

=== ZYKLUS 3: Schluessel ===
6. Der Stromschluessel traegt das eigene Praefix, nicht das der Bibliothek.
7. Task-Strom und Audit-Strom sind verschiedene Schluessel (R9, R27).
"""

from __future__ import annotations

from swarmq_dashboard.events import AUDIT_STREAM, TASK_STREAM, Event, Origin, decode, encode


def test_huelle_traegt_alle_felder() -> None:
    ev = Event(
        type="task_enqueued",
        ts=1789600000.5,
        source="phx-w1:0",
        origin=Origin.ENQUEUE,
        payload={"task_id": "a41f", "task_name": "render_pdf", "queue": "reports"},
    )
    assert ev.type == "task_enqueued"
    assert ev.ts == 1789600000.5
    assert ev.source == "phx-w1:0"
    assert ev.origin is Origin.ENQUEUE
    assert ev.payload["task_name"] == "render_pdf"


def test_herkunft_kennt_beide_faelle() -> None:
    assert Origin.ENQUEUE.value == "einreihung"
    assert Origin.PICKUP.value == "aufgreifen"


def test_kodieren_und_dekodieren_ist_verlustfrei() -> None:
    ev = Event(type="task_started", ts=1.5, source="phx-w1:0", origin=Origin.PICKUP,
               payload={"task_id": "x", "args": [1, "zwei"], "kwargs": {"a": None}})
    assert decode(encode(ev)) == ev


def test_unbekanntes_zusatzfeld_ueberlebt() -> None:
    ev = Event(type="task_started", ts=1.0, source="w:0", origin=Origin.PICKUP,
               payload={"task_id": "x", "queue": "reports", "was_auch_immer": 42})
    assert decode(encode(ev)).payload["was_auch_immer"] == 42


def test_nicht_kodierbare_nutzlast_scheitert_nicht() -> None:
    class Fremd:
        def __repr__(self) -> str:
            return "<Fremd>"

    ev = Event(type="task_failed", ts=1.0, source="w:0", origin=Origin.PICKUP,
               payload={"task_id": "x", "exception": Fremd()})
    zurueck = decode(encode(ev))
    assert "Fremd" in str(zurueck.payload["exception"])


def test_stromschluessel_traegt_eigenes_praefix() -> None:
    assert TASK_STREAM.startswith("sqd:")
    assert not TASK_STREAM.startswith("sq:")


def test_task_und_audit_sind_verschiedene_stroeme() -> None:
    assert TASK_STREAM != AUDIT_STREAM
    assert AUDIT_STREAM.startswith("sqd:")
