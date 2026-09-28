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

=== ZYKLUS 6: Befunde aus der Pruefung von Stufe B ===
14. Ein spaeter eintreffender aelterer Start verdraengt den juengeren nicht.
15. Eine Wiederholung holt den Status aus "gescheitert" zurueck -- auch
    allein, ohne einen folgenden Start.
16. Letztes Lebenszeichen und Wiederholungszaehler werden nur gehoben.
17. Der Fehlertext wird uebernommen.
18. apply() meldet die Zahl der beruehrten Zeilen.
19. Der Ablauf laesst die Signale lebender Tasks stehen und raeumt die
    verwaisten weg.
20. Ein missgestaltetes Ereignis haelt die Einspeisung nicht an.
21. Der Ablauf verdichtet nur, wenn wirklich etwas ablief.
22. Ein offener Leser laesst das Loeschversprechen erkennbar offen.
23. Verdichten und Index-Zusammenraeumen haben je einen eigenen, geprueften
    Zweck -- Platz und Suchdauer, nicht die Loeschzusage.

=== ZYKLUS 7: Zugehoerigkeit der Schritte (Workflow-Ansicht U2, KTD5) ===
24. Ein ``task_enqueue`` mit ``workflow_id`` legt einen bestaetigten Workflow an.
25. Ein ``task_cancelled`` mit Kennung ``<uuid>#/h/g3`` ohne ``workflow_id``
    legt einen unbestaetigten Kandidaten an; die Task-Zeile traegt Kennung
    und Pfad, damit die Detailansicht sie ueber den Index findet.
26. Ein ``task_completed`` mit ``<uuid>#/h/g0`` ohne ``workflow_id`` legt
    ebenso einen Kandidaten an -- der Normalfall ohne Anwendungs-Hook.
27. Eine Kennung mit ``#``, deren Teil davor keine Workflow-Kennung ist,
    legt nichts an.
28. Eine uebergrosse ``workflow_id`` wird verworfen und protokolliert, der
    Task selbst bleibt erhalten.
29. Ein missgestalteter ``step_path`` wird verworfen, die Zugehoerigkeit bleibt.
30. Ein spaeteres Ereignis mit ``workflow_id`` bestaetigt einen Kandidaten;
    das Muster senkt einen bestaetigten Workflow nicht zurueck.
31. Die letzte Aktivitaet eines Workflows folgt seinen Ereignissen und wird
    nur gehoben.

=== ZYKLUS 8: Kopie aus einer Lesung (KTD4, KTD6) ===
32. Eine Lesung schreibt Status, Wurzelform, Topologie, ``step_tasks``,
    Fehler und Zeitpunkt, bestaetigt und macht den Zustand "gesehen".
33. Eine Lesung ergaenzt ``step_tasks`` um neue Pfade und entfernt keinen.
34. Eine Lesung ueberschreibt Status und Fehler.
35. Ein gespeicherter Endstatus wird nie durch "running" ueberschrieben.
36. Eine Lesung fuer einen unbekannten Workflow legt nichts an.

=== ZYKLUS 9: Verfallen, nie gesehen, Kandidaten, Storno (R7, R16, R17) ===
Seit dem Aufraeumen (U2, KTD5) laufen 37-40 ueber die Wege, die der Code
wirklich nimmt: ``vermerke_fehlend`` und ``schreibe_lesung``.
37. "Verfallen" wird nur vermerkt, wenn die letzte Lesung "running" war;
    Zeitpunkt der letzten Lesung und Status bleiben stehen. Ein Endstatus
    wird nicht verfallen.
38. "Nie gesehen" wird nur ohne jede Lesung vermerkt, und dann nicht
    "verfallen".
39. Eine Lesung bestaetigt einen Kandidaten.
40. Ein fehlender Zustand loescht nur einen unbestaetigten Kandidaten.
41. Der eigene Storno-Vermerk wird einmal gesetzt und nie verschoben.
42. Offen sind alle Workflows ohne Endstatus, bestaetigt oder nicht.

=== ZYKLUS 10: Aufbewahrung der Kopie (R5, KTD4) ===
43. ``expire()`` entfernt einen Workflow, dessen letzte Aktivitaet aelter
    als die Aufbewahrung ist, und laesst einen juengeren stehen.

=== ZYKLUS 11: Was der Abgleich zusaetzlich braucht (Workflow-Ansicht U3) ===
44. Der zuletzt angezeigte Zustand wird vermerkt und ueberdauert
    "verfallen" (KTD6); fuer einen unbekannten Workflow legt der Vermerk
    nichts an.
45. Die Schritte eines Workflows sind alle Task-Zeilen mit seiner
    Kennung, auch die nur ueber das Muster erkannten -- und keine fremden.
46. Die Workflows mehrerer Tasks kommen in einer Abfrage: je einmal, in der
    Reihenfolge der Tasks; unbekannte und leere Kennungen und Tasks ohne
    Workflow fallen weg.
47. Ein fehlender Zustand wird in einem Zug vermerkt: ein Kandidat
    verworfen, ein zuletzt laufend gelesener verfallen, ein nie gelesener
    nie gesehen -- das Tupel nennt die drei Ausgaenge in dieser Reihenfolge.
48. Scheitert dieser Zug mittendrin, bleibt keine offene Transaktion
    zurueck: der naechste Schreibvorgang gelingt.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

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


def _sig(tid: str, typ: str, ts: float, quelle: str = "phx-w1:0", **kw: object) -> Event:
    return Event(type=typ, ts=ts, source=quelle, origin=Origin.PICKUP,
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


# --- Zyklus 6: Befunde aus der Pruefung von Stufe B ----------------------


def test_spaeter_eintreffender_aelterer_start_verdraengt_nicht(tmp_path: Path) -> None:
    """Nach Fehlschlag und Wiederholung laeuft dieselbe Task auf einem zweiten
    Worker. Leert dessen Puffer zuerst, trifft der **aeltere** Start zuletzt
    ein -- und die Ansicht naennte den falschen Worker und eine zu lange
    Laufzeit. Das ist genau die plausible, aber falsche Auskunft, die dieses
    Dashboard nicht geben darf.
    """
    w = _w(tmp_path)
    w.apply([
        (None, _sig("z", "task_started", 120.0, quelle="w2")),
        (None, _sig("z", "task_completed", 130.0, duration_ms=1.0, quelle="w2")),
        (None, _sig("z", "task_started", 100.0, quelle="w1")),   # Puffer von w1 hinkt
    ])
    z = w.row("z")
    assert z["worker_id"] == "w2", f"falscher Worker gemeldet: {z['worker_id']!r}"
    assert z["started_at"] == 120.0, f"Startzeitpunkt zurueckgesetzt: {z['started_at']}"


def test_wiederholung_holt_den_status_zurueck(tmp_path: Path) -> None:
    """Waehrend der zweite Versuch laeuft, darf die Zeile nicht "gescheitert"
    sagen -- wer da hinsieht, greift womoeglich in einen normal laufenden
    Versuch ein. Und eine Zeile mit Endzeitpunkt vor Startzeitpunkt
    widerspricht sich selbst.
    """
    w = _w(tmp_path)
    w.apply([
        (None, _sig("y", "task_started", 100.0, quelle="w1")),
        (None, _sig("y", "task_failed", 110.0, exception="boom", quelle="w1")),
        (None, _sig("y", "task_retrying", 111.0, retry_count=1, quelle="w1")),
        (None, _sig("y", "task_started", 120.0, quelle="w2")),
    ])
    z = w.row("y")
    assert z["status"] == "laufend", f"Status haengt auf {z['status']!r} fest"
    assert z["ended_at"] is None, "der ueberholte Endzeitpunkt steht noch"
    assert z["error"] is None, "der Fehlertext des vorigen Versuchs steht noch"
    assert z["retry_count"] == 1


def test_wiederholung_allein_holt_den_status_schon_zurueck(tmp_path: Path) -> None:
    """Zwischen dem Fehlschlag und dem naechsten Start liegt die Wartezeit der
    Wiederholung -- oft Minuten. Waehrenddessen darf die Ansicht nicht
    "gescheitert" sagen; R6 kennt dafuer den Zustand "Wiederholung".
    """
    w = _w(tmp_path)
    w.apply([
        (None, _sig("y", "task_started", 100.0, quelle="w1")),
        (None, _sig("y", "task_failed", 110.0, exception="boom", quelle="w1")),
        (None, _sig("y", "task_retrying", 111.0, retry_count=1, quelle="w1")),
    ])
    z = w.row("y")
    assert z["status"] == "wiederholung", f"Status haengt auf {z['status']!r} fest"
    assert z["ended_at"] is None, "der ueberholte Endzeitpunkt steht noch"


def test_letztes_lebenszeichen_wird_nur_gehoben(tmp_path: Path) -> None:
    """Sinkt es, verschwindet eine lebende Zeile beim naechsten Ablauf."""
    w = _w(tmp_path)
    w.apply([(None, _sig("a", "task_completed", 105.0, duration_ms=1.0)),
             (None, _enq("a", ts=99.0))])       # verspaetet, aelter
    assert w.row("a")["last_seen_at"] == 105.0, (
        f"das letzte Lebenszeichen wurde gesenkt: {w.row('a')['last_seen_at']}"
    )


def test_wiederholungszaehler_wird_nur_gehoben(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _sig("a", "task_retrying", 105.0, retry_count=5)),
             (None, _sig("a", "task_retrying", 102.0, retry_count=3))])
    assert w.row("a")["retry_count"] == 5, (
        f"der Zaehler lief rueckwaerts: {w.row('a')['retry_count']}"
    )


def test_fehlertext_wird_uebernommen(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _sig("a", "task_failed", 105.0, exception="ZeroDivisionError: x"))])
    assert w.row("a")["error"] == "ZeroDivisionError: x", (
        "ohne Fehlertext beantwortet die Ansicht die Frage 'warum' nicht"
    )


def test_apply_meldet_die_zahl_der_beruehrten_zeilen(tmp_path: Path) -> None:
    w = _w(tmp_path)
    ohne_task = Event(type="sqd_worker_meta", ts=100.0, source="w1", origin=Origin.PICKUP,
                      payload={"worker_id": "w1"})
    assert w.apply([(None, _enq("a")), (None, _enq("b")), (None, ohne_task)]) == 2


def test_ablauf_laesst_die_signale_lebender_tasks_stehen(tmp_path: Path) -> None:
    """Eine langlaufende Task verliert sonst ihre fruehen Signale, und die
    Ansicht zeigt eine Historie, die mit dem Abschluss beginnt -- ohne zu
    sagen, dass davor etwas stand.
    """
    w = _w(tmp_path)
    w.apply([(None, _enq("lang", ts=100.0)),
             (None, _sig("lang", "task_completed", 9000.0, duration_ms=1.0))])
    w.expire(older_than=5000.0)
    assert w.row("lang") is not None, "Vorbedingung: die Zeile muss ueberleben"
    arten = [z["type"] for z in w._con.execute(
        "select type from signal where task_id='lang' order by ts")]
    assert arten == ["task_enqueue", "task_completed"], (
        f"die fruehen Signale einer lebenden Task wurden geloescht: {arten}"
    )


def test_ablauf_raeumt_die_signale_geloeschter_tasks_weg(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _enq("alt", ts=1000.0))])
    w.expire(older_than=5000.0)
    uebrig = w._con.execute("select count(*) from signal").fetchone()[0]
    assert uebrig == 0, f"{uebrig} verwaiste Signale blieben stehen"


def test_missgestaltetes_ereignis_haelt_die_einspeisung_nicht_an(tmp_path: Path) -> None:
    """Die Nutzlasten der Bibliothek weichen an mehreren Stellen von ihrer
    Dokumentation ab. Ein Objekt statt einer Zeichenkette liess die Bindung
    werfen -- und weil der Leser seine Fortschrittsmarke erst danach setzt,
    las er ewig denselben Stapel: *alle* spaeteren Tasks fehlten dann im
    Verzeichnis, was sich als "nie gelaufen" liest.
    """
    w = _w(tmp_path)
    kaputt = Event(type="task_enqueue", ts=100.0, source="app", origin=Origin.ENQUEUE,
                   payload={"task_id": "b", "task_name": {"nicht": "erwartet"},
                            "queue": ["auch", "nicht"], "args": [], "kwargs": {}})
    beruehrt = w.apply([(None, _enq("a")), (None, kaputt), (None, _enq("c"))])
    assert w.row("c") is not None, "der Stapel wurde durch ein Ereignis angehalten"
    assert w.row("a") is not None
    assert beruehrt == 3


def test_ablauf_verdichtet_nur_wenn_etwas_ablief(tmp_path: Path) -> None:
    """Gemessen kostete ein Lauf ohne eine einzige abgelaufene Zeile auf einer
    656-MB-Datei 1,8 s und 1973 MB Schreiblast -- im Minutentakt 1,4 TB am Tag
    fuer nichts.
    """
    w = _w(tmp_path)
    w.apply([(None, _enq("neu", ts=9000.0))])
    vorher = w._con.execute("select count(*) from task").fetchone()[0]
    befehle: list[str] = []
    w._con.set_trace_callback(befehle.append)
    try:
        assert w.expire(older_than=5000.0) == 0
    finally:
        w._con.set_trace_callback(None)
    assert not any("vacuum" in b.lower() for b in befehle), (
        f"verdichtet, obwohl nichts ablief: {befehle}"
    )
    assert not any("optimize" in b.lower() for b in befehle), (
        "den Volltextindex aufgeraeumt, obwohl nichts ablief"
    )
    assert w._con.execute("select count(*) from task").fetchone()[0] == vorher


def test_offener_leser_laesst_das_loeschversprechen_offen(tmp_path: Path) -> None:
    """Covers R7a.

    ``pragma wal_checkpoint(truncate)`` wirft nicht -- es meldet "besetzt" als
    Zeile. Traf es auf einen offenen Leser, blieben die alten Seitenabbilder
    mit Klartext stehen, und ``expire()`` meldete trotzdem Erfolg. Die Zusage
    waere dann still gebrochen; jetzt sagt der Schreiber es.
    """
    pfad = tmp_path / "v.sqlite3"
    w = DirectoryWriter(connect(pfad))
    w.apply([(None, _enq("alt", ts=1000.0, kwargs={"kunde": "qzx-geheim"}))])

    # Kurze Wartefrist: sonst wartet der Zusammenfuehrungsversuch die vollen
    # fuenf Sekunden aus BUSY_TIMEOUT_MS ab und bremst die Testreihe.
    w._con.execute("pragma busy_timeout=200")
    leser = connect(pfad)
    leser.execute("begin")
    leser.execute("select count(*) from task").fetchone()
    try:
        w.expire(older_than=5000.0)
        assert w.erasure_pending is True, (
            "der Zusammenfuehrungsversuch traf auf einen Leser, wurde aber als "
            "Erfolg gemeldet"
        )
    finally:
        leser.execute("rollback")
        leser.close()

    w.expire(older_than=5000.0)
    assert w.erasure_pending is False, "ohne Leser muss die Zusage eingeloest sein"


def test_ablauf_gibt_den_platz_zurueck(tmp_path: Path) -> None:
    """Das Verdichten traegt die Loeschzusage nicht -- das tut ``secure_delete``
    allein, ueber alle sechzehn Kombinationen gemessen. Es hat einen eigenen
    Zweck: ohne es waechst die Datei nur, weil SQLite freie Seiten behaelt.
    Ohne diese Zusicherung stuende es ohne Grund im Code.
    """
    pfad = tmp_path / "v.sqlite3"
    w = DirectoryWriter(connect(pfad))
    w.apply([(None, _enq(f"t{i}", ts=1000.0, kwargs={"fuellung": "x" * 400}))
             for i in range(500)])
    w._con.execute("pragma wal_checkpoint(truncate)")
    vorher = pfad.stat().st_size
    assert vorher > 200_000, f"Vorbedingung: die Datei muss spuerbar gross sein ({vorher})"

    w.expire(older_than=5000.0)
    nachher = pfad.stat().st_size
    assert nachher < vorher // 2, (
        f"der Platz wurde nicht zurueckgegeben: {vorher} -> {nachher}"
    )


def test_ablauf_raeumt_den_volltextindex_zusammen(tmp_path: Path) -> None:
    """Auch der Index traegt die Loeschzusage nicht, sondern die Suchdauer:
    ohne das Zusammenraeumen bleibt er in viele Bloecke zersplittert.
    """
    w = _w(tmp_path)
    for i in range(200):
        w.apply([(None, _enq(f"t{i}", ts=1000.0, kwargs={"mail": f"kunde{i}@example.com"}))])
    vorher = w._con.execute("select count(*) from task_fts_data").fetchone()[0]
    assert vorher > 10, f"Vorbedingung: der Index muss zersplittert sein ({vorher})"

    w.expire(older_than=5000.0)
    nachher = w._con.execute("select count(*) from task_fts_data").fetchone()[0]
    assert nachher < vorher // 2, f"der Index blieb zersplittert: {vorher} -> {nachher}"


# --- Zyklus 7: Zugehoerigkeit der Schritte -------------------------------

#: Eine Kennung in der Form, die die Bibliothek vergibt (``uuid7``).
WF = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
WF2 = "0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c"


def test_einreihung_mit_workflow_id_legt_bestaetigten_workflow_an(tmp_path: Path) -> None:
    """Covers R1."""
    w = _w(tmp_path)
    w.apply([(None, _enq(f"{WF}#/0", workflow_id=WF, step_path="/0"))])
    wf = w.workflow(WF)
    assert wf is not None, "der Workflow wurde nicht angelegt"
    assert wf["bestaetigt"] == 1


def test_stornierung_mit_musterkennung_legt_kandidaten_an(tmp_path: Path) -> None:
    """Covers R1, R4.

    Der Worker gibt ``workflow_id`` nicht in seinen Kontext; ohne
    Anwendungs-Hook ist die erste Front nur ueber die Kennung erkennbar.
    """
    w = _w(tmp_path)
    w.apply([(None, _sig(f"{WF}#/h/g3", "task_cancelled", 105.0))])
    wf = w.workflow(WF)
    assert wf is not None, "der Kandidat wurde nicht angelegt"
    assert wf["bestaetigt"] == 0, "ein Kandidat nur aus dem Muster gilt als bestaetigt"
    z = w.row(f"{WF}#/h/g3")
    assert z["workflow_id"] == WF
    assert z["step_path"] == "/h/g3"


def test_abschluss_mit_musterkennung_legt_kandidaten_an(tmp_path: Path) -> None:
    """Covers R1, R4. Der Normalfall ohne Anwendungs-Hook: die erste Meldung
    eines Kopf-Glieds ist sein Abschluss, und sie traegt keine ``workflow_id``."""
    w = _w(tmp_path)
    w.apply([(None, _sig(f"{WF}#/h/g0", "task_completed", 105.0, duration_ms=1.0))])
    wf = w.workflow(WF)
    assert wf is not None and wf["bestaetigt"] == 0
    assert w.row(f"{WF}#/h/g0")["status"] == "fertig", "der Task selbst leidet nicht"


def test_raute_ohne_workflow_kennung_davor_legt_nichts_an(tmp_path: Path) -> None:
    """Covers R1. Eigene Task-Kennungen duerfen ein ``#`` enthalten."""
    w = _w(tmp_path)
    w.apply([(None, _enq("rechnung#2026-09")),
             (None, _enq("0190A1B2-C3D4-7E5F-8A9B-0C1D2E3F4A5B#/0")),   # Grossbuchstaben
             (None, _enq("0190a1b2c3d47e5f8a9b0c1d2e3f4a5b1234#/0"))])   # ohne Striche
    anzahl = w._con.execute("select count(*) from workflow").fetchone()[0]
    assert anzahl == 0, "eine fremde Kennung wurde als Workflow gefuehrt"
    assert w.row("rechnung#2026-09")["workflow_id"] is None


def test_uebergrosse_workflow_id_wird_verworfen_und_protokolliert(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Covers R1. Die Nutzlast kommt von aussen; eine Kennung, die die
    Bibliothek so nie vergibt, wird kein Workflow und kein Valkey-Schluessel
    fuer den Abgleich. Der Task selbst bleibt -- er ist gelaufen."""
    w = _w(tmp_path)
    riesig = "x" * 100_000
    with caplog.at_level(logging.WARNING, logger="swarmq_dashboard.directory.writer"):
        w.apply([(None, _enq("a", workflow_id=riesig, step_path="/0"))])
    assert w.row("a") is not None, "der Task ging mit verloren"
    assert w.row("a")["workflow_id"] is None, "die Kennung steht an der Task-Zeile"
    assert w._con.execute("select count(*) from workflow").fetchone()[0] == 0
    assert "workflow_id" in caplog.text, "das Verwerfen blieb stumm"
    assert riesig not in caplog.text, "die Protokollzeile traegt die ganze Nutzlast"


@pytest.mark.parametrize("pfad", ["/0" * 1000, "h/g3", "/h/<script>", 17],
                         ids=["uebergross", "ohne-schraegstrich", "fremde-zeichen", "zahl"])
def test_missgestalteter_step_path_wird_verworfen(tmp_path: Path, pfad: object) -> None:
    """Covers R8. Der Pfad landet im Baum und in Adressen; die Zugehoerigkeit
    haengt an der Kennung, nicht an ihm, und bleibt."""
    w = _w(tmp_path)
    w.apply([(None, _enq("a", workflow_id=WF, step_path=pfad))])
    assert w.row("a")["step_path"] is None, f"der Pfad {pfad!r:.20} wurde uebernommen"
    assert w.row("a")["workflow_id"] == WF
    assert w.workflow(WF) is not None


@pytest.mark.parametrize("pfad", ["", "/0", "/h/g3", "/2/b/g10/0"])
def test_gueltige_step_paths_werden_uebernommen(tmp_path: Path, pfad: str) -> None:
    """Der Pfad ``""`` der einfachen Wurzel ist gueltig und nicht leer (KTD7)."""
    w = _w(tmp_path)
    w.apply([(None, _enq("a", workflow_id=WF, step_path=pfad))])
    assert w.row("a")["step_path"] == pfad


def test_muster_mit_fremdem_pfad_legt_nichts_an(tmp_path: Path) -> None:
    """Covers R1. Vor der Raute eine UUID, dahinter kein Schrittpfad: das ist
    eine eigene Kennung, die zufaellig so beginnt."""
    w = _w(tmp_path)
    w.apply([(None, _enq(f"{WF}#rechnung 7"))])
    assert w.workflow(WF) is None
    assert w.row(f"{WF}#rechnung 7")["workflow_id"] is None


def test_ereignis_mit_workflow_id_bestaetigt_den_kandidaten(tmp_path: Path) -> None:
    """Covers R1. Der Kopf meldet sich per Muster, der Body reiht ein Worker
    ein -- sein ``workflow_step_enqueued`` traegt die Kennung."""
    w = _w(tmp_path)
    w.apply([(None, _sig(f"{WF}#/h/g0", "task_completed", 105.0, duration_ms=1.0))])
    w.apply([(None, _sig(f"{WF}#/b", "workflow_step_enqueued", 106.0,
                         workflow_id=WF, step_path="/b"))])
    assert w.workflow(WF)["bestaetigt"] == 1


def test_muster_senkt_die_bestaetigung_nicht(tmp_path: Path) -> None:
    w = _w(tmp_path)
    w.apply([(None, _enq(f"{WF}#/h/g0", workflow_id=WF, step_path="/h/g0")),
             (None, _sig(f"{WF}#/h/g1", "task_completed", 105.0, duration_ms=1.0))])
    assert w.workflow(WF)["bestaetigt"] == 1


def test_letzte_aktivitaet_des_workflows_wird_nur_gehoben(tmp_path: Path) -> None:
    """Covers R5. Sie ist der Schluessel der Aufbewahrung; sinkt sie durch ein
    verspaetetes Ereignis, laeuft die Kopie vor ihren Schritten ab."""
    w = _w(tmp_path)
    w.apply([(None, _enq(f"{WF}#/0", ts=100.0, workflow_id=WF, step_path="/0")),
             (None, _sig(f"{WF}#/1", "task_completed", 300.0, duration_ms=1.0)),
             (None, _sig(f"{WF}#/0", "task_completed", 200.0, duration_ms=1.0))])
    wf = w.workflow(WF)
    assert wf["first_seen_at"] == 100.0
    assert wf["last_seen_at"] == 300.0


# --- Zyklus 8: Kopie aus einer Lesung ------------------------------------

_TOPO = {"kind": "chord",
         "header": {"kind": "group", "members": [
             {"kind": "signature", "task": "download", "args": ["a.pdf"], "kwargs": {},
              "immutable": False},
             {"kind": "signature", "task": "download", "args": ["b.pdf"], "kwargs": {},
              "immutable": False}]},
         "body": {"kind": "signature", "task": "merge", "args": [], "kwargs": {},
                  "immutable": False}}


_FEHLER = {"type": "OSError", "message": "weg"}


def _mit_kandidat(tmp_path: Path) -> DirectoryWriter:
    w = _w(tmp_path)
    w.apply([(None, _sig(f"{WF}#/h/g0", "task_started", 100.0))])
    return w


def test_lesung_schreibt_die_kopie(tmp_path: Path) -> None:
    """Covers R5, AE5. Topologie, Status und Fehler stammen aus dem Zustand
    und ueberdauern ihn in der Kopie."""
    w = _mit_kandidat(tmp_path)
    assert w.schreibe_lesung(
        WF, status="failed", wurzel="chord", topologie=MappingProxyType(_TOPO),
        step_tasks={"/h/g0": f"{WF}#/h/g0", "/h/g1": f"{WF}#/h/g1"},
        fehler=MappingProxyType({"type": "ValueError", "message": "kaputt"}), gelesen_am=500.0,
    ) is True
    wf = w.workflow(WF)
    assert wf["status"] == "failed"
    assert wf["wurzel"] == "chord"
    assert json.loads(wf["topologie"]) == _TOPO
    assert json.loads(wf["step_tasks"]) == {"/h/g0": f"{WF}#/h/g0", "/h/g1": f"{WF}#/h/g1"}
    assert json.loads(wf["fehler"]) == {"type": "ValueError", "message": "kaputt"}
    assert wf["gelesen_am"] == 500.0
    assert wf["sichtbarkeit"] == "gesehen"
    assert wf["bestaetigt"] == 1, "eine Lesung, die den Zustand fand, bestaetigt den Kandidaten"


def _lies(w: DirectoryWriter, **kw: Any) -> bool:
    """Eine Lesung mit brauchbaren Vorgaben."""
    werte: dict[str, Any] = {"status": "running", "wurzel": "chord", "topologie": _TOPO,
                             "step_tasks": {}, "fehler": None, "gelesen_am": 500.0}
    werte.update(kw)
    return w.schreibe_lesung(WF, **werte)


def test_lesung_ergaenzt_step_tasks_und_entfernt_keinen(tmp_path: Path) -> None:
    """Covers R5, KTD4. Die Bibliothek vereinigt ``step_tasks`` nur; faellt
    ein Pfad aus einer Lesung heraus, ist das kein Wissen, sondern ein Loch."""
    w = _mit_kandidat(tmp_path)
    _lies(w, step_tasks={"/h/g0": "t0", "/h/g1": "t1"})
    _lies(w, step_tasks={"/h/g1": "t1", "/b": "tb"}, gelesen_am=600.0)
    assert json.loads(w.workflow(WF)["step_tasks"]) == {"/h/g0": "t0", "/h/g1": "t1", "/b": "tb"}


def test_lesung_ueberschreibt_status_und_fehler(tmp_path: Path) -> None:
    """Covers R6. Die juengste Lesung ist die Wahrheit, nicht die erste."""
    w = _mit_kandidat(tmp_path)
    _lies(w, status="running")
    _lies(w, status="failed", fehler=_FEHLER, gelesen_am=600.0)
    wf = w.workflow(WF)
    assert (wf["status"], json.loads(wf["fehler"]), wf["gelesen_am"]) == ("failed", _FEHLER, 600.0)


def test_endstatus_wird_nie_durch_running_ueberschrieben(tmp_path: Path) -> None:
    """Covers R6, AE4. Eine aeltere Lesung, die spaet zurueckkommt, oder ein
    Zustand, den jemand zurueckschrieb: ein einmal gesehenes Ende bleibt."""
    w = _mit_kandidat(tmp_path)
    _lies(w, status="failed", fehler=_FEHLER, step_tasks={"/h/g0": "t0"})
    assert _lies(w, status="running", fehler=None, step_tasks={"/h/g1": "t1"},
                 gelesen_am=700.0) is True
    wf = w.workflow(WF)
    assert wf["status"] == "failed", "ein Endstatus wurde durch 'running' ersetzt"
    assert json.loads(wf["fehler"]) == _FEHLER, "der Fehlertext des Endstatus ging verloren"
    assert json.loads(wf["step_tasks"]) == {"/h/g0": "t0", "/h/g1": "t1"}
    assert wf["gelesen_am"] == 700.0


def test_lesung_fuer_unbekannten_workflow_legt_nichts_an(tmp_path: Path) -> None:
    """Eine Lesung, die nach dem Ablauf zurueckkommt, holt die Kopie nicht
    zurueck -- sie ueberlebte sonst ihre Schritte."""
    w = _w(tmp_path)
    assert _lies(w) is False
    assert w.workflow(WF) is None


# --- Zyklus 9: Verfallen, nie gesehen, Kandidaten, Storno ----------------


def test_verfallen_nach_letzter_lesung_running(tmp_path: Path) -> None:
    """Covers R17. Die Ansicht nennt "zuletzt: laeuft" mit dem Zeitpunkt der
    letzten Lesung -- beides muss stehen bleiben."""
    w = _mit_kandidat(tmp_path)
    _lies(w, status="running", gelesen_am=500.0)
    assert w.vermerke_fehlend(WF)[1] is True
    wf = w.workflow(WF)
    assert wf["sichtbarkeit"] == "verfallen"
    assert wf["status"] == "running"
    assert wf["gelesen_am"] == 500.0


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"],
                         ids=["fertig", "gescheitert", "storniert"])
def test_verfallen_nur_nach_running(tmp_path: Path, status: str) -> None:
    """Covers R17, AE5. Ein Endstatus in der Kopie ist die Auskunft fuer die
    vollen sieben Tage; "verfallen" darf ihn nicht verdecken. Den Fall ohne
    jede Lesung ("nie gesehen", nicht "verfallen") prueft der Test danach."""
    w = _mit_kandidat(tmp_path)
    _lies(w, status=status)
    vorher = w.workflow(WF)["sichtbarkeit"]
    assert w.vermerke_fehlend(WF)[1] is False
    assert w.workflow(WF)["sichtbarkeit"] == vorher


def test_nie_gesehen_nur_ohne_jede_lesung(tmp_path: Path) -> None:
    """Covers R7, AE6. Ein einmal gelesener Zustand war gesehen; faellt er
    danach weg, ist das "verfallen", nicht "nie gesehen". Bestaetigt, weil
    ein fehlender Zustand einen Kandidaten verwuerfe (KTD5)."""
    w = _w(tmp_path)
    w.apply([(None, _enq(f"{WF}#/0", workflow_id=WF, step_path="/0"))])
    verfallen, nie_gesehen = w.vermerke_fehlend(WF)[1:]
    assert (verfallen, nie_gesehen) == (False, True), "ohne Lesung nie gesehen, nicht verfallen"
    assert w.workflow(WF)["sichtbarkeit"] == "nie_gesehen"

    w.apply([(None, _sig(f"{WF2}#/h/g0", "task_started", 100.0))])
    w.schreibe_lesung(WF2, status="completed", wurzel="chord", topologie=_TOPO,
                      step_tasks={}, fehler=None, gelesen_am=500.0)
    assert w.vermerke_fehlend(WF2)[2] is False
    assert w.workflow(WF2)["sichtbarkeit"] == "gesehen"


def test_kandidat_wird_bestaetigt(tmp_path: Path) -> None:
    """Covers R1. Eine Lesung, die den Zustand fand, bestaetigt ihn."""
    w = _mit_kandidat(tmp_path)
    assert w.workflow(WF)["bestaetigt"] == 0
    assert _lies(w) is True
    assert w.workflow(WF)["bestaetigt"] == 1


def test_verwerfen_loescht_nur_einen_kandidaten(tmp_path: Path) -> None:
    """Covers R1. Eine eigene Kennung mit Raute, zu der es nie einen Zustand
    gab, soll nicht sieben Tage als Kandidat mitlaufen. Ein bestaetigter
    Workflow dagegen bleibt -- ihn traegt ein Ereignis oder eine Lesung."""
    w = _mit_kandidat(tmp_path)
    w.apply([(None, _enq(f"{WF2}#/0", workflow_id=WF2, step_path="/0"))])
    assert w.vermerke_fehlend(WF)[0] is True
    assert w.workflow(WF) is None
    assert w.vermerke_fehlend(WF2)[0] is False
    assert w.workflow(WF2) is not None
    assert w.row(f"{WF}#/h/g0") is not None, "die Task-Zeile gehoert nicht zur Kopie"


def test_storno_vermerk_wird_einmal_gesetzt(tmp_path: Path) -> None:
    """Covers R16, AE8. Die zweite Anforderung ist kein Fehler und verschiebt
    den Zeitpunkt der ersten nicht."""
    w = _mit_kandidat(tmp_path)
    w.vermerke_workflow_storno(WF, jetzt=700.0)
    w.vermerke_workflow_storno(WF, jetzt=800.0)
    assert w.workflow(WF)["cancel_requested_at"] == 700.0


def test_offen_sind_alle_ohne_endstatus(tmp_path: Path) -> None:
    """Covers R5, R6. Der Abgleich liest jeden nicht beendeten Workflow im
    Takt, auch Kandidaten -- erst eine Lesung bestaetigt sie (KTD3, KTD5)."""
    w = _w(tmp_path)
    kennungen = [f"0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a{i:02d}" for i in range(5)]
    w.apply([(None, _sig(f"{k}#/0", "task_started", 100.0)) for k in kennungen])
    for k, status in zip(kennungen[1:], ["running", "completed", "failed", "cancelled"],
                         strict=True):
        w.schreibe_lesung(k, status=status, wurzel="chain", topologie=None,
                          step_tasks={}, fehler=None, gelesen_am=500.0)
    offen = {o.workflow_id: o for o in w.offene_workflows()}
    assert set(offen) == {kennungen[0], kennungen[1]}
    assert offen[kennungen[0]].bestaetigt is False
    assert offen[kennungen[0]].status is None
    assert offen[kennungen[0]].sichtbarkeit is None
    assert offen[kennungen[1]].bestaetigt is True
    assert offen[kennungen[1]].status == "running"
    assert offen[kennungen[1]].sichtbarkeit == "gesehen"
    assert offen[kennungen[1]].gelesen_am == 500.0


# --- Zyklus 10: Aufbewahrung der Kopie -----------------------------------


def test_ablauf_entfernt_alte_workflows_und_laesst_junge(tmp_path: Path) -> None:
    """Covers R5, KTD4. Derselbe Schluessel wie bei den Task-Zeilen; eine
    Lesung verlaengert die Kopie nicht, sonst ueberlebte sie ihre Schritte.
    Die Topologie traegt Klartext-Argumente (R7a)."""
    w = _w(tmp_path)
    w.apply([(None, _sig(f"{WF}#/0", "task_started", 1000.0)),
             (None, _sig(f"{WF2}#/0", "task_started", 9000.0))])
    w.schreibe_lesung(WF, status="completed", wurzel="chain", topologie=_TOPO,
                      step_tasks={}, fehler=None, gelesen_am=8000.0)
    w.expire(older_than=5000.0)
    assert w.workflow(WF) is None, "die Kopie ueberlebte die Aufbewahrung"
    assert w.workflow(WF2) is not None


# --- Zyklus 11: Was der Abgleich zusaetzlich braucht ------------------------


def test_zuletzt_angezeigter_zustand_ueberdauert_den_verfall(tmp_path: Path) -> None:
    """Covers R17, KTD6. "Zustand verfallen, zuletzt: Stornierung
    angefordert" braucht den Stand von vor dem Verfall."""
    w = _mit_kandidat(tmp_path)
    _lies(w, status="running")
    assert w.vermerke_anzeige(WF, "angefordert_nichts_aktiv") is True
    w.vermerke_fehlend(WF)
    wf = w.workflow(WF)
    assert (wf["sichtbarkeit"], wf["zuletzt_angezeigt"]) == ("verfallen", "angefordert_nichts_aktiv")
    assert w.vermerke_anzeige(WF2, "laeuft") is False
    assert w.workflow(WF2) is None


def test_schritte_eines_workflows(tmp_path: Path) -> None:
    """Covers R16. Der Abgleich leitet daraus "nichts mehr aktiv" ab; ein
    Kopf-Glied ohne Anwendungs-Hook kennt nur das Muster (KTD5)."""
    w = _w(tmp_path)
    w.apply([(None, _sig(f"{WF}#/h/g0", "task_completed", 105.0, duration_ms=1.0)),
             (None, _enq(f"{WF}#/b", ts=106.0, workflow_id=WF, step_path="/b")),
             (None, _enq(f"{WF2}#/0", ts=107.0, workflow_id=WF2, step_path="/0")),
             (None, _enq("fremd", ts=108.0))])
    schritte = {z["task_id"]: z for z in w.workflow_schritte(WF)}
    assert set(schritte) == {f"{WF}#/h/g0", f"{WF}#/b"}
    assert schritte[f"{WF}#/h/g0"]["step_path"] == "/h/g0"
    assert schritte[f"{WF}#/h/g0"]["status"] == "fertig"


def test_workflows_mehrerer_tasks(tmp_path: Path) -> None:
    """Covers KTD3. Der Abgleich fragt nach jedem Stapel des Stroms, zu
    welchen Workflows dessen Ergebnisse gehoeren -- eine Abfrage, nicht eine
    je Task."""
    w = _w(tmp_path)
    w.apply([(None, _enq(f"{WF2}#/0", ts=101.0, workflow_id=WF2, step_path="/0")),
             (None, _enq(f"{WF}#/0", ts=102.0, workflow_id=WF, step_path="/0")),
             (None, _enq(f"{WF}#/1", ts=103.0, workflow_id=WF, step_path="/1")),
             (None, _enq("fremd", ts=104.0))])
    kennungen = [f"{WF}#/1", "fremd", "", "unbekannt", f"{WF2}#/0", f"{WF}#/0"]
    assert w.workflows_der_tasks(kennungen) == [WF, WF2]
    assert w.workflows_der_tasks([]) == []


def test_fehlender_zustand_in_einem_zug(tmp_path: Path) -> None:
    """Covers R7, R17, KTD5. Der Abgleich vermerkt "fehlt" in einer
    Transaktion statt in dreien; die Ausgaenge stehen in der Reihenfolge
    verworfen, verfallen, nie gesehen."""
    w = _mit_kandidat(tmp_path)
    assert w.vermerke_fehlend(WF) == (True, False, False)
    assert w.workflow(WF) is None

    w = _mit_kandidat(tmp_path / "gelesen")
    _lies(w, status="running")
    assert w.vermerke_fehlend(WF) == (False, True, False)
    assert w.workflow(WF)["sichtbarkeit"] == "verfallen"

    w.apply([(None, _enq(f"{WF2}#/0", workflow_id=WF2, step_path="/0"))])
    assert w.vermerke_fehlend(WF2) == (False, False, True)
    assert w.workflow(WF2)["sichtbarkeit"] == "nie_gesehen"


def test_gescheiterter_zug_laesst_keine_transaktion_offen(tmp_path: Path) -> None:
    """Covers R33. Die Verbindung laeuft im Autocommit; ein ``begin`` ohne
    ``rollback`` liesse jeden weiteren Schreibvorgang an "cannot start a
    transaction within a transaction" scheitern."""
    w = _w(tmp_path)
    w.apply([(None, _enq(f"{WF}#/0", workflow_id=WF, step_path="/0"))])
    fremd = connect(tmp_path / "v.sqlite3")
    fremd.execute("create trigger kaputt before update on workflow "
                  "begin select raise(abort, 'kaputt'); end")
    with pytest.raises(Exception, match="kaputt"):
        w.vermerke_fehlend(WF)
    fremd.execute("drop trigger kaputt")
    fremd.close()
    assert w.vermerke_fehlend(WF) == (False, False, True)
