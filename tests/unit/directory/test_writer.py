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
