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
"""

from __future__ import annotations

import stat
from pathlib import Path

from swarmq_dashboard.directory.schema import TABLES, connect


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
