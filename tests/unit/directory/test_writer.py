"""Der Strom-Leser: aus Ereignissen ein Verzeichnis bauen.

Der Kern ist die Aufwaerts-Ergaenzung. Eine Task-Zeile entsteht aus Ereignissen,
die in beliebiger Reihenfolge und unvollstaendig eintreffen koennen -- weil
Scheduler-Einreihungen den Anwendungs-Hook nie passieren (R31) und weil die
Puffer der Hooks unterschiedlich schnell leeren.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Der gewoehnliche Weg ===
1. Einreihung, Start, Ausgang ergeben eine Zeile mit allen Zeitpunkten.
2. Die Herkunft ist "einreihung", der Einreihungszeitpunkt gesetzt.

=== ZYKLUS 2: Aufgreifen ohne Einreihung (R31) ===
3. Ein Aufgreifen ohne vorherige Einreihung ergibt eine Zeile mit leerem
   Einreihungszeitpunkt und Herkunft "aufgreifen".
4. Eine verspaetete Einreihungsmeldung hebt die Herkunft und fuellt den Zeitpunkt nach.
5. Eine zweite Einreihungsmeldung senkt die Herkunft nicht zurueck.

=== ZYKLUS 3: Reihenfolge ===
6. Vertauschte Ereignisse ergeben denselben Endzustand wie geordnete.
7. Der zeitlich spaetere Ausgang gewinnt, nicht der zuletzt gelesene.
8. Wiederholungen erhoehen den Zaehler, ohne den Ausgang zu ueberschreiben.

=== ZYKLUS 4: Luecken (R4a) ===
9. Ein Verlust-Ereignis markiert die betroffene Zeitspanne als unvollstaendig.

=== ZYKLUS 5: Aufbewahrung (R5, R7a) ===
10. Der Ablauf loescht Zeilen aelter als die Frist und laesst juengere stehen.
11. Nach dem Ablauf steht die Argumentzeichenkette nicht mehr als Rohbytes in der Datei.
12. Die gemeldete Reichweite folgt dem aeltesten verbliebenen Eintrag (R8).
"""

from __future__ import annotations

import json
from pathlib import Path

from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin


def _w(tmp_path: Path) -> DirectoryWriter:
    return DirectoryWriter(connect(tmp_path / "v.sqlite3"))


def _enq(tid: str, ts: float = 100.0, **kw: object) -> Event:
    return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
                 payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                          "args": [], "kwargs": {"report_id": 812}, **kw})


def _pick(tid: str, ts: float = 101.0) -> Event:
    return Event(type="task_pickup", ts=ts, source="phx-w1:0", origin=Origin.PICKUP,
                 payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                          "args": [], "kwargs": {"report_id": 812}})


def _sig(tid: str, typ: str, ts: float, **kw: object) -> Event:
    return Event(type=typ, ts=ts, source="phx-w1:0", origin=Origin.PICKUP,
                 payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports", **kw})


# --- Zyklus 1 -----------------------------------------------------------


def test_gewoehnlicher_weg(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _enq("a")), (None, _sig("a", "task_started", 101.0)),
             (None, _sig("a", "task_completed", 105.0, duration_ms=4812.0))])
    z = w.row("a")
    assert z["status"] == "fertig"
    assert z["enqueued_at"] == 100.0
    assert z["started_at"] == 101.0
    assert z["ended_at"] == 105.0
    assert z["duration_ms"] == 4812.0
    assert json.loads(z["args_text"])["kwargs"] == {"report_id": 812}


def test_herkunft_einreihung(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _enq("a"))])
    assert w.row("a")["origin"] == "einreihung"
    assert w.row("a")["status"] == "wartend"


# --- Zyklus 2 -----------------------------------------------------------


def test_aufgreifen_ohne_einreihung(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _pick("a")), (None, _sig("a", "task_started", 101.5))])
    z = w.row("a")
    assert z["origin"] == "aufgreifen"
    assert z["enqueued_at"] is None, "ein geratener Einreihungszeitpunkt waere eine Falschaussage"
    assert z["status"] == "laufend"


def test_verspaetete_einreihung_hebt_die_herkunft(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _pick("a")), (None, _enq("a", ts=99.0))])
    z = w.row("a")
    assert z["origin"] == "einreihung"
    assert z["enqueued_at"] == 99.0


def test_zweite_einreihung_senkt_die_herkunft_nicht(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _enq("a", ts=99.0)), (None, _pick("a"))])
    assert w.row("a")["origin"] == "einreihung"
    assert w.row("a")["enqueued_at"] == 99.0


# --- Zyklus 3 -----------------------------------------------------------


def test_vertauschte_reihenfolge_ergibt_denselben_zustand(tmp_path: Path) -> None:
    geordnet = [(None, _enq("a")), (None, _sig("a", "task_started", 101.0)),
                (None, _sig("a", "task_completed", 105.0, duration_ms=1.0))]
    a = _w(tmp_path / "a")
    a.apply(geordnet)
    b = _w(tmp_path / "b")
    b.apply(list(reversed(geordnet)))
    for feld in ("status", "enqueued_at", "started_at", "ended_at", "origin"):
        assert a.row("a")[feld] == b.row("a")[feld], f"{feld} weicht ab"


def test_zeitlich_spaeterer_ausgang_gewinnt(tmp_path: Path) -> None:
    w = _w(tmp_path)
    # Der Fehlschlag traegt den frueheren Zeitstempel, kommt aber zuletzt an --
    # so sieht es aus, wenn zwei Puffer ungleich schnell leeren.
    w.apply([(None, _sig("a", "task_completed", 105.0, duration_ms=1.0)),
             (None, _sig("a", "task_failed", 103.0, exception="boom"))])
    assert w.row("a")["status"] == "fertig", "der aeltere Ausgang hat den juengeren verdraengt"


def test_wiederholung_ueberschreibt_den_ausgang_nicht(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _sig("a", "task_retrying", 102.0, retry_count=2, delay=1.0)),
             (None, _sig("a", "task_completed", 105.0, duration_ms=1.0))])
    z = w.row("a")
    assert z["retry_count"] == 2
    assert z["status"] == "fertig"


# --- Zyklus 4 -----------------------------------------------------------


def test_verlustereignis_markiert_die_zeitspanne(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _enq("a", ts=100.0)), (None, _enq("b", ts=200.0))])
    verlust = Event(type="sqd_events_lost", ts=250.0, source="phx-w1:0", origin=Origin.PICKUP,
                    payload={"worker_id": "phx-w1:0", "count": 12, "from_ts": 90.0, "to_ts": 150.0})
    w.apply([(None, verlust)])
    assert w.row("a")["incomplete"] == 1, "die Zeile in der Luecke ist nicht markiert"
    assert w.row("b")["incomplete"] == 0, "eine Zeile ausserhalb der Luecke wurde markiert"


# --- Zyklus 5 -----------------------------------------------------------


def test_ablauf_loescht_alte_und_laesst_junge(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _enq("alt", ts=1000.0)), (None, _enq("neu", ts=9000.0))])
    entfernt = w.expire(older_than=5000.0)
    assert entfernt == 1
    assert w.row("alt") is None
    assert w.row("neu") is not None


def test_nach_dem_ablauf_keine_rohbytes(tmp_path: Path) -> None:
    """Covers R7a.

    Geprueft werden **alle** Dateien, die SQLite anlegt. Die Hauptdatei allein
    beweist nichts: frisch geschriebene Zeilen liegen zunaechst nur im
    Journal, und ein Test, der nur die Hauptdatei liest, besteht auch mit
    abgeschaltetem sicherem Loeschen.
    """
    pfad = tmp_path / "v.sqlite3"
    w = DirectoryWriter(connect(pfad))
    geheim = "Maria-Musterfrau-AT611904300234573201"
    w.apply([(None, _enq("alt", ts=1000.0, kwargs={"kunde": geheim}))])

    begleiter = [pfad.with_name(pfad.name + s) for s in ("", "-wal", "-shm")]
    assert any(geheim.encode() in f.read_bytes() for f in begleiter if f.exists()), (
        "Vorbedingung: das Geheimnis muss vor dem Ablauf irgendwo stehen, "
        "sonst prueft der Test nichts"
    )

    w.expire(older_than=5000.0)
    w.close()

    uebrig = [f.name for f in begleiter if f.exists() and geheim.encode() in f.read_bytes()]
    assert not uebrig, f"die geloeschte Zeichenkette steht noch in {uebrig} (R7a)"


def test_reichweite_folgt_dem_aeltesten_eintrag(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _enq("a", ts=1000.0)), (None, _enq("b", ts=9000.0))])
    assert w.oldest_entry() == 1000.0
    w.expire(older_than=5000.0)
    assert w.oldest_entry() == 9000.0
