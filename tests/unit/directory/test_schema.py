"""Schema und Verbindungseinstellungen des Verzeichnisses.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Aufbau ===
1. Eine frische Datei traegt die erwarteten Tabellen.
2. Ein zweites Oeffnen derselben Datei ist wirkungslos.

=== ZYKLUS 2: Einstellungen, die Zusagen tragen ===
3. Die Verbindung laeuft im WAL-Modus.
4. Sicheres Loeschen ist eingeschaltet -- sonst bleiben Klartext-Argumente
   nach dem Ablauf als Rohbytes stehen (R7a).
5. Eine Wartefrist auf Sperren ist gesetzt -- ohne sie scheitern unter Last
   99 % der Schreibvorgaenge (gemessen, Anhang des Plans).

=== ZYKLUS 3: Volltext ===
6. Der Volltextindex nutzt Trigramm -- wortweise findet ein Fragment im
   Wortinneren gar nicht (R10, gemessen).
7. Ein Fragment quer ueber die Wortgrenze wird gefunden.

=== ZYKLUS 4: Dateirechte (R7a) ===
8. Die angelegte Datei ist fuer andere Benutzer nicht lesbar.

=== ZYKLUS 5: Kopie der Workflows (Workflow-Ansicht U2, KTD4) ===
9. Eine frische Datei traegt die Tabelle der Workflow-Kopie.
10. Die Task-Zeilen haben einen Index auf ``workflow_id`` -- die
    Detailansicht holt alle Schritte eines Workflows darueber.
11. Eine bestehende Datei aus Stufe 1 bekommt Tabelle und Index beim Oeffnen
    nachgezogen und behaelt ihre Zeilen.
12. Die Workflow-Kopie hat einen Index passend zu Sortierung und Keyset der
    Liste (``first_seen_at desc, workflow_id desc``), wie ``task_first_seen``.
"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

from swarmq_dashboard.directory.schema import TABLES, GuardedConnection, connect


def test_frische_datei_traegt_die_tabellen(tmp_path: Path) -> None:
    con = connect(tmp_path / "v.sqlite3")
    vorhanden = {r[0] for r in con.execute("select name from sqlite_master where type in ('table','view')")}
    assert vorhanden >= TABLES, f"fehlend: {TABLES - vorhanden}"
    con.close()


def test_zweites_oeffnen_ist_wirkungslos(tmp_path: Path) -> None:
    pfad = tmp_path / "v.sqlite3"
    con = connect(pfad)
    con.execute(
        "insert into task(task_id, task_name, origin, first_seen_at, last_seen_at) "
        "values ('a','t','aufgreifen',1.0,1.0)"
    )
    con.commit()
    con.close()
    con2 = connect(pfad)
    assert con2.execute("select count(*) from task").fetchone()[0] == 1
    con2.close()


def test_wal_modus(tmp_path: Path) -> None:
    con = connect(tmp_path / "v.sqlite3")
    assert con.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
    con.close()


def test_sicheres_loeschen_ist_an(tmp_path: Path) -> None:
    con = connect(tmp_path / "v.sqlite3")
    assert con.execute("pragma secure_delete").fetchone()[0] == 1, (
        "ohne sicheres Loeschen bleiben Klartext-Argumente als Rohbytes in der Datei (R7a)"
    )
    con.close()


def test_wartefrist_auf_sperren(tmp_path: Path) -> None:
    con = connect(tmp_path / "v.sqlite3")
    assert con.execute("pragma busy_timeout").fetchone()[0] >= 1000
    con.close()


def test_volltext_nutzt_trigramm(tmp_path: Path) -> None:
    con = connect(tmp_path / "v.sqlite3")
    sql = con.execute("select sql from sqlite_master where name='task_fts'").fetchone()[0]
    assert "trigram" in sql.lower(), f"kein Trigramm-Tokenizer: {sql}"
    con.close()


def test_fragment_quer_ueber_die_wortgrenze(tmp_path: Path) -> None:
    con = connect(tmp_path / "v.sqlite3")
    con.execute(
        "insert into task(task_id, task_name, origin, args_text, first_seen_at, last_seen_at) "
        "values (?,?,?,?,?,?)",
        ("a41f", "render_pdf", "einreihung", '{"mail": "kunde3@example.com"}', 1.0, 1.0),
    )
    con.commit()
    treffer = con.execute(
        "select count(*) from task_fts where task_fts match ?", ('"3@example"',)
    ).fetchone()[0]
    assert treffer == 1, "das Fragment quer ueber die Wortgrenze wurde nicht gefunden (R10)"
    con.close()


def test_datei_nur_fuer_den_dienstbenutzer(tmp_path: Path) -> None:
    pfad = tmp_path / "v.sqlite3"
    con = connect(pfad)
    con.close()
    modus = stat.S_IMODE(pfad.stat().st_mode)
    assert modus & (stat.S_IRGRP | stat.S_IROTH) == 0, (
        f"Rechte {oct(modus)}: sieben Tage Klartext-Argumente sind fuer andere lesbar (R7a)"
    )


def test_rechte_werden_auch_an_bestandsdateien_nachgezogen(tmp_path: Path) -> None:
    """Covers R7a.

    Eine Datei aus einer Ruecksicherung, aus einem ``ExecStartPre`` oder aus
    einer aelteren Fassung des Pakets bringt ihre eigenen Rechte mit. Wurden
    sie nur beim Anlegen gesetzt, lagen dort sieben Tage Klartext fuer jeden
    lesbar -- samt ``-wal`` und ``-shm``, die den Modus der Hauptdatei erben.
    """
    pfad = tmp_path / "v.sqlite3"
    pfad.touch(mode=0o644)
    assert pfad.stat().st_mode & 0o077, "Vorbedingung: die Datei ist fremdlesbar"

    con = connect(pfad)
    con.execute("insert into task(task_id, origin, first_seen_at, last_seen_at) "
                "values ('a','einreihung',1.0,1.0)")
    con.commit()
    con.close()

    for begleiter in ("", "-wal", "-shm"):
        f = pfad.with_name(pfad.name + begleiter)
        if f.exists():
            assert not (f.stat().st_mode & 0o077), (
                f"{f.name} ist fuer andere lesbar: {oct(f.stat().st_mode & 0o777)}"
            )


def test_fehlgeschlagenes_oeffnen_laesst_keinen_zeiger_offen(tmp_path: Path) -> None:
    """Ein Dienst, der es im Takt erneut versucht, liefe sonst in EMFILE."""
    import gc

    pfad = tmp_path / "kaputt.sqlite3"
    pfad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    for _ in range(5):
        try:
            connect(pfad)
        except sqlite3.DatabaseError:
            pass
    gc.collect()
    offen = [o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)]
    assert not offen, f"{len(offen)} Verbindungen blieben offen"


# --- Zyklus 5: Kopie der Workflows ----------------------------------------


def test_frische_datei_traegt_die_workflow_kopie(tmp_path: Path) -> None:
    """Covers R5."""
    con = connect(tmp_path / "v.sqlite3")
    vorhanden = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert "workflow" in vorhanden
    assert "workflow" in TABLES, "die Vollstaendigkeitspruefung kennt die Tabelle nicht"
    con.close()


def _index_spalten(con: GuardedConnection, tabelle: str) -> set[str]:
    """Erste Spalte jedes Index einer Tabelle."""
    spalten: set[str] = set()
    for idx in con.execute(f"pragma index_list({tabelle})"):
        info = con.execute(f"pragma index_info({idx[1]})").fetchall()
        if info:
            spalten.add(info[0][2])
    return spalten


def test_task_zeilen_haben_einen_index_auf_workflow_id(tmp_path: Path) -> None:
    """Covers R8. Ohne ihn ist jede Detailansicht ein voller Durchlauf."""
    con = connect(tmp_path / "v.sqlite3")
    assert "workflow_id" in _index_spalten(con, "task")
    con.close()


def test_bestehende_datei_bekommt_kopie_und_index_nachgezogen(tmp_path: Path) -> None:
    """Covers R5.

    Eine Datei aus Stufe 1 kennt weder die Tabelle noch den Index. Sie muss
    ohne Fehler oeffnen, beides bekommen und ihre Zeilen behalten -- der
    Neuaufbau aus dem Strom ist ein Weg, aber nicht der fuer eine neue Fassung.
    """
    pfad = tmp_path / "v.sqlite3"
    con = connect(pfad)
    con.execute("drop table workflow")
    con.execute("drop index task_workflow")
    con.execute("insert into task(task_id, workflow_id, origin, first_seen_at, last_seen_at) "
                "values ('a','w','einreihung',1.0,1.0)")
    con.commit()
    con.close()

    con2 = connect(pfad)
    tabellen = {r[0] for r in con2.execute("select name from sqlite_master where type='table'")}
    assert "workflow" in tabellen
    assert "workflow_id" in _index_spalten(con2, "task")
    assert con2.execute("select count(*) from task").fetchone()[0] == 1
    con2.close()


def test_workflow_liste_hat_einen_index_auf_ihre_sortierung(tmp_path: Path) -> None:
    """Covers R1. Die Liste sortiert und blaettert ueber
    ``(first_seen_at, workflow_id)`` absteigend; ohne passenden Index ist
    jede Seite ein voller Durchlauf plus Sortierung.
    """
    con = connect(tmp_path / "v.sqlite3")
    plan = " ".join(
        str(r[3]) for r in con.execute(
            "explain query plan select workflow_id from workflow "
            "where (first_seen_at, workflow_id) < (?, ?) "
            "order by first_seen_at desc, workflow_id desc limit 50",
            (1.0, "w"),
        )
    )
    assert "TEMP B-TREE" not in plan, f"die Liste sortiert ohne Index: {plan}"
    assert "INDEX" in plan, f"die Liste liest ohne Index: {plan}"
    con.close()
