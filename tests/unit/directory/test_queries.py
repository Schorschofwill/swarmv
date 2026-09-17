"""Suche und Filter -- die Fragen aus dem Hauptfluss in je einer Abfrage.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Volltext (R10) ===
1. Covers AE1. Eine Suche nach einer Zahl aus den Argumenten findet den Task.
2. Covers AE1a. Ein Fragment innerhalb eines zusammengesetzten Wertes findet ihn auch.
3. Ein Fragment kuerzer als die Trigramm-Laenge liefert eine benannte
   Rueckmeldung statt einer leeren Liste.
4. Die Suche findet auch ueber den Task-Namen.

=== ZYKLUS 2: Filter ===
5. Status, Name, Worker und Zeitraum wirken einzeln.
6. Sie wirken zusammen.
7. Der Zeitraumfilter schliesst Zeilen ohne Einreihungszeitpunkt nach der
   Ersatzregel ein (R31).

=== ZYKLUS 3: Seitenweises Lesen ===
8. Zwei Seiten decken die Treffer vollstaendig und ueberschneidungsfrei ab,
   und die letzte meldet keinen Anschluss mehr.
9. Ein nebenher geschriebener Task laesst keine Zeile ausfallen.

=== ZYKLUS 4: Signalfolge (R11) ===
10. Die Signalfolge kommt in Stromreihenfolge, auch wenn Host-Zeitstempel
    gegeneinander laufen.
11. Die Reichweite folgt dem aeltesten Eintrag des Verzeichnisses (R8).
"""

from __future__ import annotations

from pathlib import Path

from swarmq_dashboard.directory.queries import DirectoryQueries, Filter
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin


def _enq(tid: str, ts: float, **kw: object) -> Event:
    nutzlast: dict[str, object] = {
        "task_id": tid, "task_name": "render_pdf", "queue": "reports",
        "args": [], "kwargs": {"report_id": 812, "mail": "kunde3@example.com"},
    }
    nutzlast.update(kw)
    return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE, payload=nutzlast)


def _sig(tid: str, typ: str, ts: float, quelle: str = "phx-w1:0", **kw: object) -> Event:
    return Event(type=typ, ts=ts, source=quelle, origin=Origin.PICKUP,
                 payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports", **kw})


def _setup(tmp_path: Path) -> tuple[DirectoryWriter, DirectoryQueries]:
    con = connect(tmp_path / "v.sqlite3")
    return DirectoryWriter(con), DirectoryQueries(con)


# --- Zyklus 1 -----------------------------------------------------------


def test_suche_nach_zahl_aus_den_argumenten(tmp_path: Path) -> None:
    """Covers AE1."""
    w, q = _setup(tmp_path)
    w.apply([(None, _enq("a41f", 100.0))])
    treffer = q.search(Filter(text="812"))
    assert [r["task_id"] for r in treffer.rows] == ["a41f"]


def test_fragment_innerhalb_eines_wertes(tmp_path: Path) -> None:
    """Covers AE1a. Wortweise faende das gar nichts."""
    w, q = _setup(tmp_path)
    w.apply([(None, _enq("a41f", 100.0))])
    assert [r["task_id"] for r in q.search(Filter(text="3@example")).rows] == ["a41f"]


def test_zu_kurzes_fragment_wird_benannt(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _enq("a41f", 100.0))])
    seite = q.search(Filter(text="81"))
    assert seite.rows == []
    assert seite.note is not None and "3" in seite.note, (
        "eine leere Liste ohne Begruendung liest sich wie 'nicht gelaufen'"
    )


def test_suche_ueber_den_task_namen(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _enq("a41f", 100.0))])
    assert [r["task_id"] for r in q.search(Filter(text="render")).rows] == ["a41f"]


# --- Zyklus 2 -----------------------------------------------------------


def test_filter_wirken_einzeln(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([
        (None, _enq("a", 100.0)),
        (None, _sig("a", "task_completed", 105.0, duration_ms=1.0)),
        (None, _enq("b", 200.0, task_name="sync_invoice")),
        (None, _sig("b", "task_started", 201.0, quelle="phx-b1:0")),
    ])
    assert [r["task_id"] for r in q.search(Filter(status="fertig")).rows] == ["a"]
    assert [r["task_id"] for r in q.search(Filter(task_name="sync_invoice")).rows] == ["b"]
    assert [r["task_id"] for r in q.search(Filter(worker_id="phx-b1:0")).rows] == ["b"]
    assert [r["task_id"] for r in q.search(Filter(since=150.0)).rows] == ["b"]


def test_filter_wirken_zusammen(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([
        (None, _enq("a", 100.0)),
        (None, _sig("a", "task_completed", 105.0, duration_ms=1.0)),
        (None, _enq("b", 200.0)),
        (None, _sig("b", "task_completed", 205.0, duration_ms=1.0)),
    ])
    seite = q.search(Filter(text="812", status="fertig", since=150.0))
    assert [r["task_id"] for r in seite.rows] == ["b"]


def test_zeitraum_schliesst_zeilen_ohne_einreihung_ein(tmp_path: Path) -> None:
    """R31: ohne Einreihungszeitpunkt gilt der erste bekannte Zeitpunkt."""
    w, q = _setup(tmp_path)
    aufgegriffen = Event(type="task_pickup", ts=300.0, source="phx-w1:0", origin=Origin.PICKUP,
                         payload={"task_id": "c", "task_name": "render_pdf", "queue": "reports",
                                  "args": [], "kwargs": {}})
    w.apply([(None, aufgegriffen)])
    assert w.row("c")["enqueued_at"] is None
    assert [r["task_id"] for r in q.search(Filter(since=250.0)).rows] == ["c"]


# --- Zyklus 3 -----------------------------------------------------------


def test_zwei_seiten_decken_vollstaendig_ab(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _enq(f"t{i}", 100.0 + i)) for i in range(5)])
    erste = q.search(Filter(), limit=2)
    zweite = q.search(Filter(), limit=2, cursor=erste.next_cursor)
    dritte = q.search(Filter(), limit=2, cursor=zweite.next_cursor)
    gesehen = [r["task_id"] for r in (*erste.rows, *zweite.rows, *dritte.rows)]
    assert sorted(gesehen) == sorted(f"t{i}" for i in range(5))
    assert len(gesehen) == len(set(gesehen)), "eine Zeile erschien auf zwei Seiten"
    assert dritte.next_cursor is None, (
        "die halbe letzte Seite meldet einen Anschluss -- die Oberflaeche zeigt "
        "'weiter' auf eine leere Seite"
    )


def test_nebenher_geschriebener_task_laesst_nichts_ausfallen(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _enq(f"t{i}", 100.0 + i)) for i in range(4)])
    erste = q.search(Filter(), limit=2)
    w.apply([(None, _enq("neu", 500.0))])      # waehrend des Blaetterns
    zweite = q.search(Filter(), limit=2, cursor=erste.next_cursor)
    gesehen = {r["task_id"] for r in (*erste.rows, *zweite.rows)}
    assert {"t0", "t1", "t2", "t3"} - gesehen == set(), (
        f"uebersprungen: {{'t0','t1','t2','t3'}} - {gesehen}"
    )


# --- Zyklus 4 -----------------------------------------------------------


def test_signalfolge_in_stromreihenfolge(tmp_path: Path) -> None:
    """R11: die Host-Uhren koennen gegeneinander laufen, der Strom nicht."""
    w, q = _setup(tmp_path)
    w.apply([
        ("1000-0", _enq("a", 100.0)),
        # Der Start traegt einen frueheren Zeitstempel als die Einreihung --
        # so sieht Uhrversatz zwischen Anwendungs- und Worker-Host aus.
        ("2000-0", _sig("a", "task_started", 95.0)),
        ("3000-0", _sig("a", "task_completed", 99.0, duration_ms=1.0)),
    ])
    folge = [s["type"] for s in q.signals("a")]
    assert folge == ["task_enqueue", "task_started", "task_completed"], (
        f"nach Host-Zeit geordnet statt nach Strom: {folge}"
    )


def test_reichweite_folgt_dem_aeltesten_eintrag(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _enq("a", 1000.0)), (None, _enq("b", 9000.0))])
    assert q.reach() == 1000.0
