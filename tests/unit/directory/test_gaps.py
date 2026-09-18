"""Luecken dauerhaft halten und in der Suche benennen (U12a, R4a, R8).

Der Befund aus der Pruefung von Stufe B: Luecken waren auf zwei Wegen
bekannt, und beide reichten nicht.

* ``incomplete=1`` markiert nur **ueberlebende** Zeilen. Die verlorenen Tasks
  haben definitionsgemaess gar keine Zeile -- ``mark_incomplete`` meldet dann
  schlicht ``0``, und niemand erfaehrt etwas.
* ``behind``/``gap`` leben nur im Arbeitsspeicher. Ein Neustart vergisst sie.

Zusammen ergibt das genau die stille Falschaussage, gegen die das vierte
Erfolgskriterium steht: der Leser steht bei t=100, der Laengendeckel wirft die
500 Tasks zwischen t=200 und t=700 ungelesen weg, ein Durchlauf spaeter steht
``behind=False``. Ein Teammitglied filtert auf 300...600 und bekommt "keine
Treffer" ueber einen Zeitraum, der nie gelesen wurde.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Die Luecke ueberlebt ===
1. Eine festgehaltene Luecke ist nach einem Neustart noch da.
2. Zwei Meldungen derselben Spanne ergeben einen Eintrag, nicht zwei.
3. Eine wachsende Spanne verschmilzt mit der vorhandenen, statt sich zu
   haeufen.
4. Zwei getrennte Spannen bleiben getrennt.

=== ZYKLUS 2: Die Suche benennt sie ===
5. Eine Suche ueber die Lueckenspanne liefert eine benannte Rueckmeldung
   statt "keine Treffer".
6. Eine Suche nach der Spanne bekommt keinen Hinweis.
6a. Eine Suche vor der Spanne ebenso wenig -- eine Grenze allein genuegt
    nicht.
7. Ohne Zeitfilter gilt die ganze Reichweite -- der Hinweis steht.
8. Mehrere Luecken werden zusammengefasst und gezaehlt, nicht aneinander-
   gereiht.
9. Der Hinweis verdraengt den zu kurzen Suchbegriff nicht.

=== ZYKLUS 3: Beide Quellen und die Frist ===
10. Das Verlust-Ereignis eines Hooks landet in der Tabelle, mit Quelle.
11. Luecken laufen mit derselben Frist ab wie die Tasks.
12. Eine Luecke ohne brauchbare Spanne wird nicht festgehalten.
"""

from __future__ import annotations

from pathlib import Path

from swarmq_dashboard.directory.queries import DirectoryQueries, Filter
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin

#: 2023-11-14 22:13:20 UTC -- echte Groessenordnung, nicht 100.0: die
#: Spannen werden fuer die Meldung in Datum und Uhrzeit uebersetzt.
T0 = 1_700_000_000.0


def _setup(tmp_path: Path) -> tuple[DirectoryWriter, DirectoryQueries]:
    con = connect(tmp_path / "v.sqlite3")
    return DirectoryWriter(con), DirectoryQueries(con)


def _task(tid: str, ts: float) -> Event:
    return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
                 payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                          "args": [], "kwargs": {}})


def _verlust(von: float, bis: float) -> Event:
    """Das Verlust-Ereignis, wie ein Hook es schickt (R4a)."""
    return Event(type="sqd_events_lost", ts=bis, source="phx-w1:0", origin=Origin.PICKUP,
                 payload={"from_ts": von, "to_ts": bis, "count": 500})


# --- Zyklus 1 -----------------------------------------------------------


def test_eine_luecke_ueberlebt_den_neustart(tmp_path: Path) -> None:
    """Der Kern des Befunds. Vorher lebte die Luecke nur im Arbeitsspeicher
    des Lesers; ein Neustart machte aus "da fehlt etwas" ein "alles gut"."""
    pfad = tmp_path / "v.sqlite3"
    w = DirectoryWriter(connect(pfad))
    w.record_gap(T0 + 200, T0 + 700, quelle="stream_trim")
    w.close()

    q = DirectoryQueries(connect(pfad))        # neuer Prozess, neue Verbindung
    luecken = q.gaps()
    assert len(luecken) == 1
    assert luecken[0]["von"] == T0 + 200 and luecken[0]["bis"] == T0 + 700


def test_dieselbe_spanne_zweimal_ergibt_einen_eintrag(tmp_path: Path) -> None:
    """Der Leser prueft den Rueckstand in **jedem** Durchlauf. Ohne
    Zusammenlegen waechst die Tabelle bei anhaltendem Rueckstand um einen
    Eintrag je Takt -- und die Meldung spraeche von tausend Luecken, wo es
    eine ist."""
    w, q = _setup(tmp_path)
    for _ in range(5):
        w.record_gap(T0 + 200, T0 + 700, quelle="stream_trim")
    assert len(q.gaps()) == 1


def test_eine_wachsende_spanne_verschmilzt(tmp_path: Path) -> None:
    """Ein anhaltender Rueckstand meldet jeden Takt eine etwas andere Spanne.
    Aneinandergereiht waeren das dutzende Eintraege ueber denselben
    Zeitraum."""
    w, q = _setup(tmp_path)
    w.record_gap(T0 + 200, T0 + 500, quelle="stream_trim")
    w.record_gap(T0 + 400, T0 + 700, quelle="stream_trim")
    luecken = q.gaps()
    assert len(luecken) == 1, f"nicht verschmolzen: {[dict(z) for z in luecken]}"
    assert luecken[0]["von"] == T0 + 200 and luecken[0]["bis"] == T0 + 700


def test_zwei_getrennte_spannen_bleiben_getrennt(tmp_path: Path) -> None:
    """Sonst spannte die Meldung ueber einen Zeitraum, in dem nachweislich
    nichts fehlt -- und entwertete sich selbst."""
    w, q = _setup(tmp_path)
    w.record_gap(T0 + 100, T0 + 200, quelle="stream_trim")
    w.record_gap(T0 + 5000, T0 + 5100, quelle="stream_trim")
    assert len(q.gaps()) == 2


# --- Zyklus 2 -----------------------------------------------------------


def test_suche_ueber_die_luecke_wird_benannt(tmp_path: Path) -> None:
    """Der Fall aus dem Befund: gefiltert auf einen Zeitraum, der nie gelesen
    wurde. "Keine Treffer" hiesse dort "ist nichts passiert"."""
    w, q = _setup(tmp_path)
    w.record_gap(T0 + 200, T0 + 700, quelle="stream_trim")
    seite = q.search(Filter(since=T0 + 300, until=T0 + 600))
    assert seite.rows == []
    assert seite.note, "keine Rueckmeldung -- die Liste liest sich als Entwarnung"
    assert "fehlen" in seite.note.lower()
    assert "2023-11-14" in seite.note, "die Spanne wird nicht genannt"


def test_suche_ausserhalb_der_spanne_bekommt_keinen_hinweis(tmp_path: Path) -> None:
    """Ein Hinweis, der immer dasteht, wird ueberlesen."""
    w, q = _setup(tmp_path)
    w.record_gap(T0 + 200, T0 + 700, quelle="stream_trim")
    assert q.search(Filter(since=T0 + 1000, until=T0 + 2000)).note is None


def test_suche_vor_der_spanne_bekommt_keinen_hinweis(tmp_path: Path) -> None:
    """Das Gegenstueck zur Suche danach.

    Nur eine der beiden Grenzen zu pruefen genuegt nicht: mit der Untergrenze
    allein faellt jede Suche *vor* der Luecke trotzdem durch -- aber jede
    *danach* bekaeme den Hinweis zu Unrecht. Gemessen: ohne diese Zusicherung
    ueberlebte genau diese Mutation.
    """
    w, q = _setup(tmp_path)
    w.record_gap(T0 + 5000, T0 + 5100, quelle="stream_trim")
    assert q.search(Filter(since=T0 + 100, until=T0 + 200)).note is None


def test_ohne_zeitfilter_gilt_die_ganze_reichweite(tmp_path: Path) -> None:
    """Die Liste ohne Filter zeigt alles -- also auch den Zeitraum, aus dem
    etwas fehlt. Den Hinweis nur bei gesetztem Filter zu zeigen hiesse, ihn
    ausgerechnet im haeufigsten Aufruf zu unterschlagen."""
    w, q = _setup(tmp_path)
    w.apply([(None, _task("a1", T0))])
    w.record_gap(T0 + 200, T0 + 700, quelle="stream_trim")
    seite = q.search(Filter())
    assert seite.rows, "die vorhandene Zeile fehlt"
    assert seite.note, "der Hinweis fehlt im haeufigsten Aufruf"


def test_mehrere_luecken_werden_zusammengefasst(tmp_path: Path) -> None:
    w, q = _setup(tmp_path)
    w.record_gap(T0 + 100, T0 + 200, quelle="stream_trim")
    w.record_gap(T0 + 5000, T0 + 5100, quelle="hook_lost")
    note = q.search(Filter()).note
    assert note and "2" in note, f"die Zahl der Luecken fehlt: {note!r}"


def test_der_hinweis_verdraengt_den_kurzen_suchbegriff_nicht(tmp_path: Path) -> None:
    """Zwei Gruende, eine Zeile. Der zu kurze Begriff ist der naehere: er
    erklaert, warum **diese** Suche nichts ergab."""
    w, q = _setup(tmp_path)
    w.record_gap(T0 + 200, T0 + 700, quelle="stream_trim")
    note = q.search(Filter(text="ab")).note
    assert note and "Zeichen" in note, f"der naehere Grund fehlt: {note!r}"


# --- Zyklus 3 -----------------------------------------------------------


def test_das_verlust_ereignis_landet_in_der_tabelle(tmp_path: Path) -> None:
    """Beide Quellen schreiben dorthin -- der Strom-Leser und der Hook. Fuer
    das Teammitglied bedeuten sie dasselbe und sollen gleich aussehen."""
    w, q = _setup(tmp_path)
    w.apply([(None, _verlust(T0 + 200, T0 + 700))])
    luecken = q.gaps()
    assert len(luecken) == 1
    assert luecken[0]["quelle"] == "hook_lost", "die Quelle ist nicht benannt"


def test_luecken_laufen_mit_derselben_frist_ab(tmp_path: Path) -> None:
    """Sonst meldete das Dashboard ewig eine Luecke in einem Zeitraum, aus
    dem es ohnehin nichts mehr zeigt (R9)."""
    w, q = _setup(tmp_path)
    w.record_gap(T0 - 10_000, T0 - 9_000, quelle="stream_trim")
    w.record_gap(T0 + 200, T0 + 700, quelle="stream_trim")
    w.expire(older_than=T0)
    assert [z["von"] for z in q.gaps()] == [T0 + 200]


def test_eine_luecke_ohne_brauchbare_spanne_wird_verworfen(tmp_path: Path) -> None:
    """Aus einer Nutzlast von aussen. Eine Spanne, die rueckwaerts laeuft,
    schnitte jede Suche und stuende dauerhaft auf der Seite."""
    w, q = _setup(tmp_path)
    w.record_gap(T0 + 700, T0 + 200, quelle="stream_trim")     # rueckwaerts
    w.record_gap(float("nan"), float("inf"), quelle="stream_trim")
    assert q.gaps() == []


# --- Zyklus 4: Was die Kopfzeile sagt -----------------------------------


def test_die_kopfzeile_kennt_die_luecke_nach_dem_neustart(tmp_path: Path) -> None:
    """``behind`` allein trug das nicht.

    Es sagt, ob der Leser **gerade** hinterherhaengt. Nach einem Neustart mit
    eingeholtem Strom ist es wieder falsch -- und die Kopfzeile meldete
    Vollstaendigkeit, obwohl die Luecke von gestern bleibt.
    """
    pfad = tmp_path / "v.sqlite3"
    w = DirectoryWriter(connect(pfad))
    w.record_gap(T0 + 200, T0 + 700, quelle="stream_trim")
    w.close()

    w2 = DirectoryWriter(connect(pfad))
    try:
        assert w2.gap_count() == 1, "die Kopfzeile erfaehrt nichts von der Luecke"
    finally:
        w2.close()
