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

=== ZYKLUS 5: Befunde aus der Pruefung von Stufe B ===
12. Die Signalfolge stimmt auch jenseits von Folgenummer neun.
13. Das Blaettern ueberlebt echte Zeitstempel (Fliesskomma-Genauigkeit).
14. Das Blaettern ueberlebt einen Gleichstand im Zeitpunkt.
15. Umschliessende Leerzeichen zaehlen nicht zur Laenge des Suchbegriffs.

=== ZYKLUS 6: Was aus einer Adresszeile kommt ===
16. Steuerzeichen im Suchtext werfen nicht.
17. Reiner Leerraum ist kein Filter ueber alles.
18. Unbrauchbare Seitengroessen werden begrenzt.
19. Unbrauchbare Anschlussmarken heissen "von vorn".
20. Die Liste laeuft ueber einen Index, nicht ueber einen vollen Durchlauf.
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


# --- Zyklus 5: Befunde aus der Pruefung von Stufe B ----------------------


def test_signalfolge_auch_jenseits_von_folgenummer_neun(tmp_path: Path) -> None:
    """Die Folgenummer einer Stromkennung hat keine feste Stellenzahl.

    Als Text verglichen sortiert ``…-10`` vor ``…-9``. Ein Puffer, der elf
    Ereignisse einer Task in derselben Millisekunde leert -- der Normalfall
    beim Leeren, nicht die Ausnahme -- liesse die Ansicht den Abschluss vor
    dem Start zeigen.
    """
    w, q = _setup(tmp_path)
    arten = [f"task_schritt_{i:02d}" for i in range(12)]
    w.apply([(f"1700000000000-{i}", _sig("t", art, 100.0)) for i, art in enumerate(arten)])
    folge = [s["type"] for s in q.signals("t")]
    assert folge == arten, f"nach Text statt nach Zahl geordnet: {folge}"


def test_blaettern_haelt_echte_zeitstempel_aus(tmp_path: Path) -> None:
    """Mit ``time.time()``-Werten, nicht mit runden Testzahlen.

    Ein Zeitpunkt um 1,8e9 traegt mehr Nachkommastellen, als sechs Stellen
    fassen. Eine gerundete Blaettermarke liegt mal ueber, mal unter dem
    gespeicherten Wert -- die Grenzzeile erscheint dann doppelt oder faellt
    aus. Gemessen fiel mit Seitengroesse eins jede vierte Zeile aus.
    """
    import time

    w, q = _setup(tmp_path)
    basis = time.time()
    w.apply([(None, _enq(f"t{i}", basis + i * 1e-7)) for i in range(6)])

    gesehen: list[str] = []
    cursor: str | None = None
    for _ in range(12):
        seite = q.search(Filter(), limit=1, cursor=cursor)
        if not seite.rows:
            break
        gesehen.append(seite.rows[0]["task_id"])
        cursor = seite.next_cursor
        if cursor is None:
            break
    assert sorted(gesehen) == sorted(f"t{i}" for i in range(6)), (
        f"beim Blaettern ueber echte Zeitstempel gingen Zeilen verloren: {gesehen}"
    )
    assert len(gesehen) == len(set(gesehen)), f"eine Zeile erschien doppelt: {gesehen}"


def test_blaettern_bei_gleichem_zeitpunkt(tmp_path: Path) -> None:
    """Ein Stapel gleichzeitig eingereihter Tasks traegt denselben Zeitpunkt.

    Ohne die Task-Kennung als Entscheid verschwaende ein Gleichstand eine
    Zeile oder zeigte sie zweimal.
    """
    w, q = _setup(tmp_path)
    w.apply([(None, _enq(f"t{i}", 100.0)) for i in range(5)])
    gesehen: list[str] = []
    cursor: str | None = None
    for _ in range(6):
        seite = q.search(Filter(), limit=2, cursor=cursor)
        if not seite.rows:
            break
        gesehen.extend(r["task_id"] for r in seite.rows)
        cursor = seite.next_cursor
        if cursor is None:
            break
    assert sorted(gesehen) == sorted(f"t{i}" for i in range(5)), f"unvollstaendig: {gesehen}"
    assert len(gesehen) == len(set(gesehen)), f"doppelt: {gesehen}"


def test_umschliessende_leerzeichen_zaehlen_nicht(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _enq("a41f", 100.0))])
    assert [r["task_id"] for r in q.search(Filter(text="  812  ")).rows] == ["a41f"]
    assert q.search(Filter(text="  8  ")).note is not None, (
        "nach dem Beschneiden ist der Begriff zu kurz und muss benannt werden"
    )


# --- Zyklus 6: Was aus einer Adresszeile kommt, ist nicht vertrauenswuerdig


def test_steuerzeichen_im_suchtext(tmp_path: Path) -> None:
    """Ein NUL-Byte ist aus einer Adresszeile als ``%00`` trivial zu setzen
    und liess SQLite mit "unterminated string" werfen, statt nichts zu finden.
    """
    w, q = _setup(tmp_path)
    w.apply([(None, _enq("a41f", 100.0))])
    assert [r["task_id"] for r in q.search(Filter(text="812\x00")).rows] == ["a41f"]
    assert q.search(Filter(text="\x00\x01\x02")).note is not None, (
        "nach dem Entfernen der Steuerzeichen bleibt nichts uebrig -- das muss "
        "benannt werden, nicht als 'keine Treffer' durchgehen"
    )


def test_nur_leerraum_ist_kein_filter_ueber_alles(tmp_path: Path) -> None:
    """Fiel der Leerraum-Text auf "kein Filter" durch, kamen **alle** Tasks
    zurueck -- die Oberflaeche zeigte dann eine Suche, die nichts einschraenkt.
    """
    w, q = _setup(tmp_path)
    w.apply([(None, _enq("a", 100.0)), (None, _enq("b", 200.0))])
    seite = q.search(Filter(text="   "))
    assert seite.rows == [], f"Leerraum lieferte {len(seite.rows)} Zeilen"
    assert seite.note is not None


def test_unbrauchbare_seitengroessen(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _enq(f"t{i}", 100.0 + i)) for i in range(5)])
    assert len(q.search(Filter(), limit=0).rows) == 1, "limit=0 griff ins Leere"
    assert len(q.search(Filter(), limit=-1).rows) == 1, "limit=-1 hiess 'alle Zeilen'"
    assert len(q.search(Filter(), limit=10_000).rows) == 5


def test_unbrauchbare_anschlussmarken(tmp_path: Path) -> None:
    """``float`` nimmt ``nan`` und ``inf`` an, und beide sind Falschaussagen:
    mit ``nan`` ist jeder Vergleich unwahr und die leere Seite liest sich als
    "keine Treffer"; mit ``inf`` ist Seite zwei wieder Seite eins.
    """
    w, q = _setup(tmp_path)
    w.apply([(None, _enq(f"t{i}", 100.0 + i)) for i in range(5)])
    for marke in ("nan|x", "inf|x", "-inf|x", "keine-zahl|x", ""):
        seite = q.search(Filter(), limit=10, cursor=marke)
        assert len(seite.rows) == 5, f"Marke {marke!r} lieferte {len(seite.rows)} statt 5"


def test_die_liste_laeuft_ueber_einen_index(tmp_path: Path) -> None:
    """Der Blaetterschluessel braucht einen Index.

    Ohne ihn ist jede Seite ein voller Durchlauf durch das Verzeichnis plus
    eine Sortierung im Arbeitsspeicher -- gemessen bei 60.000 Zeilen 469 ms
    fuer zwanzig Seiten statt 2 ms. Bei sieben Tagen Tasks bezahlt das jede
    einzelne Anfrage.
    """
    w, _ = _setup(tmp_path)
    w.apply([(None, _enq(f"t{i}", 100.0 + i)) for i in range(3)])
    plan = [z[3] for z in w._con.execute(
        "explain query plan "
        "select task.* from task order by task.first_seen_at desc, task.task_id desc limit 50"
    )]
    assert not any("TEMP B-TREE" in s for s in plan), f"sortiert im Speicher: {plan}"
    assert any("USING INDEX" in s for s in plan), f"kein Index im Plan: {plan}"
