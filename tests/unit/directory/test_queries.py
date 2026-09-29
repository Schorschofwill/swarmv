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

=== ZYKLUS 7: Workflow-Liste (Workflow-Ansicht U5; R1, R2, R7, R17) ===
21. Ein leeres Verzeichnis liefert eine leere Seite mit Notiz, nie ``None``.
22. Die Liste zeigt nur bestaetigte Workflows, neueste zuerst.
23. Covers AE1. Jede Zeile traegt den Baum aus U4: "47/51" und "laeuft".
24. Der Filter "gescheitert" findet einen gescheiterten und keinen laufenden.
25. Der Filter "verfallen" findet einen verfallenen.
26. "angefordert" und "angefordert_nichts_aktiv" trennt der Baum, nicht SQL.
27. Covers AE6. "nie_gesehen" filtert und zaehlt nur die bekannten Schritte.
28. Der Zeitraum wirkt auf den Start des Workflows.
29. Das Blaettern ueber 250 Workflows liefert drei Seiten ohne Doppel und
    ohne Luecke.
30. Unbrauchbare Anschlussmarken heissen "von vorn", Seitengroessen werden
    begrenzt.
31. Eine Luecke im Zeitraum haengt ihren Hinweis an die stehende Notiz.

=== ZYKLUS 8: Suche (R3) ===
32. Die Suche nach der Workflow-Kennung findet ihn und nur ihn.
33. Die Suche nach ``wf1#/h/g3`` findet ``wf1``.
34. Die Suche nach der eigenen Kennung eines Schritts findet seinen Workflow.
35. Ein unbestaetigter Kandidat bleibt auch fuer die Suche unsichtbar.

=== ZYKLUS 9: Detail (R5, R8) ===
36. Das Detail liefert die Kopie dekodiert und alle Task-Zeilen des Workflows.
37. Ein unbekannter oder unbestaetigter Workflow ergibt ``None``.
38. Ohne jede Lesung: keine Topologie, leere ``step_tasks``, kein Fehler.
42. Eine Zeile, die nur ``workflow_step_enqueued`` kennt, hat noch keinen
    Status; sie zaehlt als eingereiht und damit als aktiv (KTD7, R16).

=== ZYKLUS 10: Fertige Glieder einer Gruppe (R9, KTD13) ===
39. Covers AE2. 498 fertige Glieder ergeben fuenf Seiten zu hoechstens 100,
    ohne Doppel und ohne Luecke; offene Glieder stehen nicht darin.
40. Eine unbekannte Gruppe oder ein unbekannter Workflow ergibt eine leere
    Seite mit Notiz.
41. Eine unbrauchbare Anschlussmarke heisst auch hier "von vorn".

=== ZYKLUS 11: Nachtraege ===
43. Der zuletzt angezeigte Zustand kommt in die Kopie: "verfallen, zuletzt:
    Stornierung angefordert" (KTD6, R17).
44. Eine Kopie, deren Topologie erst unter der Wurzel kaputt ist, reisst die
    Liste nicht mit: sie steht mit dem flachen Baum aus ihren Task-Zeilen
    und ``topologie_lesbar`` False da, die anderen Zeilen daneben.
45. Dasselbe im Detail: flacher Baum statt Ausnahme.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from swarmq_dashboard.directory.queries import (
    WORKFLOW_HINWEIS,
    DirectoryQueries,
    Filter,
    Workflowfilter,
    Workflowseite,
)
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


def _wf(i: int) -> str:
    """Eine Workflow-Kennung in der UUID-Form der Bibliothek (KTD5)."""
    return f"{i:08x}-0000-4000-8000-000000000000"


def _schritt(wf: str, pfad: str, ts: float, *, bestaetigt: bool = True,
             **kw: object) -> Event:
    """Einreihung eines Schritts mit der Kennung ``<wf>#<pfad>``.

    ``bestaetigt`` traegt ``workflow_id`` mit; ohne sie legt nur das Muster
    einen Kandidaten an.
    """
    zusatz: dict[str, object] = {"workflow_id": wf, "step_path": pfad} if bestaetigt else {}
    return _enq(f"{wf}#{pfad}", ts, **zusatz, **kw)


def _sig_top(name: str) -> dict[str, object]:
    return {"kind": "signature", "task": name, "args": [], "kwargs": {}, "immutable": False}


def _download_chord(anzahl: int) -> dict[str, object]:
    """Chord mit *anzahl* Downloads im Kopf, im Format von ``node.to_dict()``."""
    return {"kind": "chord",
            "header": {"kind": "group", "members": [_sig_top("download")] * anzahl},
            "body": _sig_top("verarbeiten")}


def _lesung(w: DirectoryWriter, wf: str, *, status: str = "running",
            topologie: Mapping[str, object] | None = None,
            step_tasks: dict[str, str] | None = None,
            fehler: dict[str, object] | None = None, gelesen_am: float = 500.0) -> None:
    assert w.schreibe_lesung(
        wf, status=status, wurzel=None if topologie is None else "chord",
        topologie=topologie, step_tasks=step_tasks or {}, fehler=fehler,
        gelesen_am=gelesen_am,
    )


def _ids(seite: Workflowseite) -> list[str]:
    return [z.kopie.workflow_id for z in seite.zeilen]


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

    Geprueft wird die Abfrage, die ``search()`` **wirklich** baut. Vorher
    stand sie als Zeichenkette im Test: der Plan war dann eine Aussage ueber
    den Test, nicht ueber den Code. Nachgemessen durfte die Sortierung auf
    ``last_seen_at`` wechseln, ohne dass irgendein Test kippte -- obwohl der
    Modulkopf genau davor warnt.
    """
    w, q = _setup(tmp_path)
    w.apply([(None, _enq(f"t{i}", 100.0 + i)) for i in range(3)])

    # ``set_trace_callback`` statt eines Austauschs von ``execute``: die
    # Verbindung laesst ihre Methoden nicht ersetzen, und mitzuschreiben ist
    # ohnehin der ehrlichere Weg -- es faengt genau das ab, was SQLite
    # wirklich zu sehen bekommt.
    gesehen: list[str] = []
    q._con.set_trace_callback(gesehen.append)
    try:
        q.search(Filter())
    finally:
        q._con.set_trace_callback(None)
    listen = [s for s in gesehen if s.lstrip().startswith("select task.*")]
    assert listen, f"die Listenabfrage wurde nicht erkannt: {gesehen}"

    plan = [z[3] for z in q._con.execute("explain query plan " + listen[-1])]
    assert not any("TEMP B-TREE" in s for s in plan), f"sortiert im Speicher: {plan}"
    assert any("USING INDEX" in s for s in plan), f"kein Index im Plan: {plan}"


def test_das_blaettern_haengt_am_ersten_zeitpunkt(tmp_path: Path) -> None:
    """Der Schluessel ist ``first_seen_at`` und darf es nicht anders sein.

    Der Modulkopf sagt warum: ueber ``last_seen_at`` zu blaettern hiesse,
    dass eine Zeile waehrend des Blaetterns nach vorn springt und dahinter
    eine andere ungesehen durchrutscht. Alle bisherigen Blaetter-Tests
    benutzten Vorrichtungen, in denen beide Spalten dieselbe Reihenfolge
    ergaben -- der Wechsel blieb darum unbemerkt.

    Hier laufen sie ausdruecklich gegeneinander: der **aelteste** Task
    bekommt das **juengste** Lebenszeichen.
    """
    w, q = _setup(tmp_path)
    for i in range(4):
        w.apply([(None, _enq(f"t{i}", 100.0 + i))])
    # t0 ist der aelteste, bekommt aber das juengste Signal.
    w._con.execute("update task set last_seen_at=? where task_id='t0'", (9_000.0,))
    w._con.commit()

    erste = q.search(Filter(), limit=2)
    zweite = q.search(Filter(), limit=2, cursor=erste.next_cursor)
    gesehen = [z["task_id"] for z in erste.rows] + [z["task_id"] for z in zweite.rows]
    assert gesehen == ["t3", "t2", "t1", "t0"], (
        f"das Blaettern folgt nicht dem Einreihungszeitpunkt: {gesehen}"
    )
    assert len(set(gesehen)) == 4, f"eine Zeile fiel aus oder kam doppelt: {gesehen}"


# --- Zyklus 7: Workflow-Liste -------------------------------------------


def test_leeres_verzeichnis_liefert_leere_workflow_seite_mit_notiz(tmp_path: Path) -> None:
    _w, q = _setup(tmp_path)
    seite = q.workflows(Workflowfilter())
    assert seite.zeilen == []
    assert seite.next_cursor is None
    assert seite.note, "eine leere Liste ohne Hinweis liest sich wie 'nichts gelaufen'"


def test_liste_zeigt_nur_bestaetigte_workflows_neueste_zuerst(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([
        (None, _schritt(_wf(1), "/0", 100.0)),
        (None, _schritt(_wf(2), "/0", 200.0)),
        (None, _schritt(_wf(3), "/0", 300.0, bestaetigt=False)),
    ])
    assert _ids(q.workflows(Workflowfilter())) == [_wf(2), _wf(1)]


def test_zeile_traegt_fortschritt_und_status_aus_dem_baum(tmp_path: Path) -> None:
    """Covers AE1. Die Liste rechnet nicht selbst -- sie nutzt ``baue_baum``."""
    w, q = _setup(tmp_path)
    wf = _wf(1)
    kopf = [f"/h/g{i}" for i in range(50)]
    w.apply([(None, _schritt(wf, p, 100.0 + i)) for i, p in enumerate(kopf)])
    w.apply([(None, _sig(f"{wf}#{p}", "task_completed", 200.0, duration_ms=1.0))
             for p in kopf[:47]])
    _lesung(w, wf, topologie=_download_chord(50), step_tasks={p: f"{wf}#{p}" for p in kopf})

    (zeile,) = q.workflows(Workflowfilter()).zeilen
    assert (zeile.baum.fortschritt.fertig, zeile.baum.fortschritt.gesamt) == (47, 51)
    assert zeile.baum.status.code == "laeuft"
    assert zeile.baum.form == "chord"
    assert zeile.baum.erster_schritt == "download"


def test_filter_gescheitert_findet_nur_den_gescheiterten(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(1), "", 100.0)), (None, _schritt(_wf(2), "", 200.0))])
    _lesung(w, _wf(1), status="failed", topologie=_sig_top("a"),
            fehler={"type": "ValueError", "message": "kaputt"})
    _lesung(w, _wf(2), topologie=_sig_top("a"))
    assert _ids(q.workflows(Workflowfilter(status="gescheitert"))) == [_wf(1)]


def test_filter_verfallen_findet_den_verfallenen(tmp_path: Path) -> None:
    """R17: "verfallen" ist kein Endstatus, aber die Liste filtert danach."""
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(1), "", 100.0)), (None, _schritt(_wf(2), "", 200.0))])
    _lesung(w, _wf(1), topologie=_sig_top("a"))
    _lesung(w, _wf(2), topologie=_sig_top("a"))
    assert w.vermerke_verfallen(_wf(1))
    assert _ids(q.workflows(Workflowfilter(status="verfallen"))) == [_wf(1)]
    assert _ids(q.workflows(Workflowfilter(status="laeuft"))) == [_wf(2)]


def test_angefordert_und_nichts_mehr_aktiv_trennt_der_baum(tmp_path: Path) -> None:
    """R16: ob noch ein Schritt aktiv ist, weiss nur der Baum -- der Filter
    folgt ihm, statt die Regel in SQL nachzubauen."""
    w, q = _setup(tmp_path)
    aktiv, still = _wf(1), _wf(2)
    w.apply([(None, _schritt(aktiv, "", 100.0)), (None, _schritt(still, "", 200.0)),
             (None, _sig(f"{still}#", "task_cancelled", 210.0))])
    for wf in (aktiv, still):
        _lesung(w, wf, topologie=_sig_top("a"))
        w.vermerke_workflow_storno(wf, jetzt=300.0)
    assert _ids(q.workflows(Workflowfilter(status="angefordert"))) == [aktiv]
    assert _ids(q.workflows(Workflowfilter(status="angefordert_nichts_aktiv"))) == [still]


def test_nie_gesehen_filtert_und_zaehlt_nur_bekannte_schritte(tmp_path: Path) -> None:
    """Covers AE6. Ohne Topologie zaehlt der Fortschritt die bekannten Schritte (R7)."""
    w, q = _setup(tmp_path)
    wf = _wf(1)
    w.apply([(None, _schritt(wf, "/0", 100.0)), (None, _schritt(wf, "/1", 101.0)),
             (None, _sig(f"{wf}#/0", "task_completed", 102.0, duration_ms=1.0)),
             (None, _schritt(_wf(2), "", 200.0))])
    assert w.vermerke_nie_gesehen(wf)
    (zeile,) = q.workflows(Workflowfilter(status="nie_gesehen")).zeilen
    assert zeile.kopie.workflow_id == wf
    assert (zeile.baum.fortschritt.fertig, zeile.baum.fortschritt.gesamt) == (1, 2)
    assert not zeile.baum.status.endstatus


def test_zeitraum_wirkt_auf_den_start_des_workflows(tmp_path: Path) -> None:
    """Beide Grenzen einschliesslich; die letzte Aktivitaet zaehlt nicht."""
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(i), "", ts)) for i, ts in enumerate((100.0, 200.0, 300.0))])
    # Der aelteste lebt noch -- er beginnt trotzdem vor dem Zeitraum.
    w.apply([(None, _schritt(_wf(0), "/spaet", 250.0))])
    assert _ids(q.workflows(Workflowfilter(since=200.0, until=300.0))) == [_wf(2), _wf(1)]


def test_blaettern_ueber_250_workflows_ohne_doppel_und_ohne_luecke(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    # Je zwei mit demselben Start: der Gleichstand entscheidet die Kennung.
    w.apply([(None, _schritt(_wf(i), "", 1.8e9 + (i // 2) * 0.1)) for i in range(250)])
    seiten = [q.workflows(Workflowfilter(), limit=100)]
    while seiten[-1].next_cursor is not None:
        seiten.append(q.workflows(Workflowfilter(), limit=100, cursor=seiten[-1].next_cursor))
    assert [len(s.zeilen) for s in seiten] == [100, 100, 50]
    gesehen = [i for s in seiten for i in _ids(s)]
    assert gesehen == [_wf(i) for i in reversed(range(250))]


def test_statusfilter_blaettert_ueber_verworfene_zeilen_hinweg(tmp_path: Path) -> None:
    """Der Filter folgt dem Baum, also liest die Abfrage in Stapeln: eine
    Seite fuellt sich auch, wenn der erste Stapel nur Verworfenes traegt."""
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(i), "", 100.0 + i)) for i in range(9)])
    for i in range(9):
        _lesung(w, _wf(i), status="failed" if i % 3 == 0 else "running",
                topologie=_sig_top("a"))
    f = Workflowfilter(status="gescheitert")
    erste = q.workflows(f, limit=2)
    zweite = q.workflows(f, limit=2, cursor=erste.next_cursor)
    assert _ids(erste) == [_wf(6), _wf(3)]
    assert _ids(zweite) == [_wf(0)]
    assert zweite.next_cursor is None


def test_unbrauchbare_marke_und_seitengroesse_bei_workflows(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(i), "", 100.0 + i)) for i in range(3)])
    for marke in ("", "kaputt", "nan|x", "inf|x"):
        assert len(q.workflows(Workflowfilter(), cursor=marke).zeilen) == 3, marke
    assert len(q.workflows(Workflowfilter(), limit=0).zeilen) == 1
    assert len(q.workflows(Workflowfilter(), limit=-1).zeilen) == 1


def test_luecke_im_zeitraum_haengt_ihren_hinweis_an_die_notiz(tmp_path: Path) -> None:
    """R4a wie in der Task-Liste: ein verlorener Workflow hat keine Zeile, also
    kann nur die Notiz auf ihn hinweisen -- zusaetzlich zum stehenden Hinweis."""
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(1), "", 100.0))])
    w.record_gap(150.0, 160.0, quelle="stream_trim")
    notiz = q.workflows(Workflowfilter()).note
    assert WORKFLOW_HINWEIS in notiz
    assert "verlorengegangen" in notiz and "Workflows" in notiz
    assert "verlorengegangen" not in q.workflows(Workflowfilter(until=140.0)).note


# --- Zyklus 8: Suche ----------------------------------------------------


def test_suche_nach_der_workflow_kennung(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(1), "", 100.0)), (None, _schritt(_wf(2), "", 200.0))])
    assert _ids(q.workflows(Workflowfilter(suche=f"  {_wf(1)} "))) == [_wf(1)]


def test_suche_nach_schrittkennung_findet_den_workflow(tmp_path: Path) -> None:
    """Die Schrittkennung muss nicht als Task-Zeile existieren: der Teil vor
    dem ``#`` fuehrt auf den Workflow."""
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(1), "/h/g0", 100.0)), (None, _schritt(_wf(2), "", 200.0))])
    assert _ids(q.workflows(Workflowfilter(suche=f"{_wf(1)}#/h/g3"))) == [_wf(1)]


def test_suche_nach_eigener_kennung_eines_schritts_findet_den_workflow(
        tmp_path: Path) -> None:
    """Eine eigene Task-Kennung folgt nicht dem Muster; dann fuehrt die
    Spalte ``workflow_id`` ihrer Zeile auf den Workflow."""
    w, q = _setup(tmp_path)
    w.apply([(None, _enq("rechnung-7", 100.0, workflow_id=_wf(1), step_path="/0")),
             (None, _schritt(_wf(2), "", 200.0))])
    assert _ids(q.workflows(Workflowfilter(suche="rechnung-7"))) == [_wf(1)]


def test_unbestaetigter_kandidat_bleibt_fuer_die_suche_unsichtbar(tmp_path: Path) -> None:
    """KTD5: eigene Task-Kennungen duerfen ein ``#`` tragen."""
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(1), "/0", 100.0, bestaetigt=False))])
    assert q.workflows(Workflowfilter(suche=f"{_wf(1)}#/0")).zeilen == []


# --- Zyklus 9: Detail ---------------------------------------------------


def test_detail_liefert_die_kopie_dekodiert_und_alle_task_zeilen(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    wf = _wf(1)
    topologie = {"kind": "chain", "steps": [_sig_top("a"), _sig_top("b")]}
    w.apply([(None, _schritt(wf, "/0", 100.0)),
             (None, _sig(f"{wf}#/0", "task_failed", 103.0, error="boom")),
             (None, _schritt(wf, "/1", 101.0)),
             (None, _schritt(_wf(2), "/0", 102.0))])
    _lesung(w, wf, status="failed", topologie=topologie,
            step_tasks={"/0": f"{wf}#/0", "/1": f"{wf}#/1"},
            fehler={"type": "ValueError", "message": "kaputt"}, gelesen_am=510.0)
    w.vermerke_workflow_storno(wf, jetzt=520.0)

    detail = q.workflow_detail(wf)
    assert detail is not None
    kopie = detail.kopie
    assert kopie.workflow_id == wf
    assert kopie.topologie == topologie
    assert kopie.step_tasks == {"/0": f"{wf}#/0", "/1": f"{wf}#/1"}
    assert kopie.status == "failed"
    assert kopie.sichtbarkeit == "gesehen"
    assert kopie.fehler == "ValueError: kaputt"
    assert kopie.storno_angefordert_at == 520.0
    assert kopie.gelesen_at == 510.0
    assert [(z.task_id, z.step_path, z.status) for z in detail.zeilen] == [
        (f"{wf}#/0", "/0", "gescheitert"), (f"{wf}#/1", "/1", "wartend")]
    assert detail.zeilen[0].error is not None
    assert detail.first_seen_at == 100.0
    assert detail.baum.status.code == "gescheitert"


def test_detail_unbekannt_oder_unbestaetigt_ist_none(tmp_path: Path) -> None:
    """Ein Kandidat aus dem Muster bleibt unsichtbar, auch ueber die Adresse."""
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(1), "/0", 100.0, bestaetigt=False))])
    assert q.workflow_detail(_wf(2)) is None
    assert q.workflow_detail(_wf(1)) is None


def test_detail_ohne_jede_lesung(tmp_path: Path) -> None:
    """Vor der ersten Lesung traegt die Kopie nichts, was nach Zustand aussieht."""
    w, q = _setup(tmp_path)
    w.apply([(None, _schritt(_wf(1), "/0", 100.0))])
    detail = q.workflow_detail(_wf(1))
    assert detail is not None
    k = detail.kopie
    assert (k.topologie, k.step_tasks, k.status, k.fehler, k.gelesen_at) == (
        None, {}, None, None, None)
    assert not detail.baum.status.endstatus
    assert [z.task_id for z in detail.zeilen] == [f"{_wf(1)}#/0"]


# --- Zyklus 10: Fertige Glieder einer Gruppe ----------------------------


def test_498_fertige_glieder_ergeben_fuenf_seiten_zu_hoechstens_100(tmp_path: Path) -> None:
    """Covers AE2. KTD13: die gefaltete Zeile oeffnet sich seitenweise."""
    w, q = _setup(tmp_path)
    wf = _wf(1)
    pfade = [f"/g{i}" for i in range(500)]
    w.apply([(None, _schritt(wf, p, 100.0)) for p in pfade])
    w.apply([(None, _sig(f"{wf}#{p}", "task_completed", 200.0, duration_ms=1.0))
             for p in pfade if p not in ("/g7", "/g300")])
    w.apply([(None, _sig(f"{wf}#/g7", "task_started", 150.0)),
             (None, _sig(f"{wf}#/g300", "task_failed", 150.0, error="boom"))])
    _lesung(w, wf, topologie={"kind": "group", "members": [_sig_top("x")] * 500},
            step_tasks={p: f"{wf}#{p}" for p in pfade})

    seiten = [q.fertige_glieder(wf, "")]
    while seiten[-1].next_cursor is not None:
        seiten.append(q.fertige_glieder(wf, "", cursor=seiten[-1].next_cursor))
    assert [len(s.glieder) for s in seiten] == [100, 100, 100, 100, 98]
    gesehen = [g.pfad for s in seiten for g in s.glieder]
    assert gesehen == [p for p in pfade if p not in ("/g7", "/g300")]


def _kleine_gruppe(w: DirectoryWriter, wf: str) -> None:
    pfade = ["/g0", "/g1", "/g2"]
    w.apply([(None, _schritt(wf, p, 100.0)) for p in pfade])
    w.apply([(None, _sig(f"{wf}#{p}", "task_completed", 200.0, duration_ms=1.0))
             for p in pfade])
    _lesung(w, wf, topologie={"kind": "group", "members": [_sig_top("x")] * 3},
            step_tasks={p: f"{wf}#{p}" for p in pfade})


def test_unbekannte_gruppe_ergibt_leere_gliederseite_mit_notiz(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    _kleine_gruppe(w, _wf(1))
    for wf, pfad in ((_wf(2), ""), (_wf(1), "/g9"), (_wf(1), "/g0")):
        seite = q.fertige_glieder(wf, pfad)
        assert seite.glieder == [] and seite.next_cursor is None, (wf, pfad)
        assert seite.note, f"leere Seite ohne Begruendung fuer {pfad!r}"


def test_unbrauchbare_gliedermarke_heisst_von_vorn(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    _kleine_gruppe(w, _wf(1))
    for marke in ("kaputt", "1e400", "-5", ""):
        seite = q.fertige_glieder(_wf(1), "", cursor=marke)
        assert [g.pfad for g in seite.glieder] == ["/g0", "/g1", "/g2"], marke


def test_zeile_nur_aus_folgeschritt_meldung_zaehlt_als_eingereiht(tmp_path: Path) -> None:
    """Der Worker meldet den Folgeschritt, bevor ihn jemand aufgreift: die
    Zeile hat noch keinen Status. Als leerer Status fiele der Schritt aus
    "aktiv", und eine Stornierung hiesse faelschlich "nichts mehr aktiv"."""
    w, q = _setup(tmp_path)
    wf = _wf(1)
    w.apply([(None, _schritt(wf, "/0", 100.0)),
             (None, _sig(f"{wf}#/0", "task_completed", 101.0, duration_ms=1.0)),
             (None, _sig(f"{wf}#/1", "workflow_step_enqueued", 102.0,
                         workflow_id=wf, step_path="/1"))])
    _lesung(w, wf, topologie={"kind": "chain", "steps": [_sig_top("a"), _sig_top("b")]},
            step_tasks={"/0": f"{wf}#/0"})
    w.vermerke_workflow_storno(wf, jetzt=300.0)
    detail = q.workflow_detail(wf)
    assert detail is not None
    assert [z.status for z in detail.zeilen] == ["fertig", "eingereiht"]
    assert detail.baum.status.code == "angefordert"


def test_verfallen_nennt_den_zuletzt_angezeigten_zustand(tmp_path: Path) -> None:
    """KTD6: die eigene Stornierung bleibt nach dem Verfall sichtbar."""
    w, q = _setup(tmp_path)
    wf = _wf(1)
    w.apply([(None, _schritt(wf, "", 100.0))])
    _lesung(w, wf, topologie=_sig_top("a"), gelesen_am=510.0)
    assert w.vermerke_anzeige(wf, "angefordert")
    assert w.vermerke_verfallen(wf)
    (zeile,) = q.workflows(Workflowfilter(status="verfallen")).zeilen
    assert zeile.kopie.zuletzt_angezeigt == "angefordert"
    assert zeile.baum.status.text == "Zustand verfallen, zuletzt: Stornierung angefordert"
    assert zeile.baum.status.gelesen_at == 510.0


def test_kaputte_topologie_reisst_die_liste_nicht_mit(tmp_path: Path) -> None:
    """Der Leser prueft nur die Wurzel; ``chain`` ohne ``steps`` besteht das."""
    w, q = _setup(tmp_path)
    kaputt, heil = _wf(1), _wf(2)
    w.apply([(None, _schritt(kaputt, "/0", 100.0)), (None, _schritt(heil, "", 200.0))])
    _lesung(w, kaputt, topologie={"kind": "chain"}, step_tasks={"/0": f"{kaputt}#/0"})
    _lesung(w, heil, topologie=_sig_top("a"))

    zeilen = {z.kopie.workflow_id: z for z in q.workflows(Workflowfilter()).zeilen}
    assert set(zeilen) == {kaputt, heil}
    assert not zeilen[kaputt].topologie_lesbar
    assert zeilen[kaputt].baum.form is None
    assert [b.task_id for b in zeilen[kaputt].baum.blaetter] == [f"{kaputt}#/0"]
    assert zeilen[kaputt].baum.status.code == "laeuft"
    assert zeilen[heil].topologie_lesbar


def test_kaputte_topologie_ergibt_im_detail_den_flachen_baum(tmp_path: Path) -> None:
    """Chord mit leerem Body: die Wurzel ist ein Chord, darunter steht nichts."""
    w, q = _setup(tmp_path)
    wf = _wf(1)
    w.apply([(None, _schritt(wf, "/h/0", 100.0))])
    _lesung(w, wf, topologie={"kind": "chord", "header": {"kind": "group", "members": []},
                              "body": {"kind": "group", "members": []}})
    detail = q.workflow_detail(wf)
    assert detail is not None
    assert not detail.topologie_lesbar
    assert detail.baum.form is None
    assert [b.task_id for b in detail.baum.blaetter] == [f"{wf}#/h/0"]
