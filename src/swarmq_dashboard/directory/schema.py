"""Schema und Verbindungseinstellungen des Verzeichnisses.

Eine Datei, ihre Tabellen und ein Volltextindex. Die Einstellungen der
Verbindung sind keine Feinabstimmung, sondern tragen je eine Zusage aus dem
Plan -- der Kommentar an jedem Pragma sagt welche.

Die Kontendatei liegt **daneben**, nicht hier (KTD8): der Neuaufbau des
Verzeichnisses aus dem Strom ist ein vorgesehener Weg, und laege alles
zusammen, wuerde sein Loeschen das Team aussperren.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

#: Tabellen und Sichten, die eine vollstaendige Datei traegt.
TABLES = {"task", "signal", "task_fts", "progress", "worker", "gap", "workflow"}

#: Wartefrist auf gesperrte Schreibvorgaenge. Gemessen: ohne sie scheitern
#: unter Last 99 % der Vorgaenge, mit ihr keiner bis 16 gleichzeitige Schreiber.
BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
create table if not exists task (
    task_id       text primary key,
    task_name     text,
    queue         text,
    status        text,
    worker_id     text,
    -- Bleibt NULL, solange unbekannt. Ein gerateneer Zeitpunkt waere eine
    -- Falschaussage: Scheduler- und verzoegerte Einreihungen kennt das
    -- Verzeichnis erst ab dem Aufgreifen (R31).
    enqueued_at   real,
    started_at    real,
    ended_at      real,
    duration_ms   real,
    retry_count   integer not null default 0,
    -- 'einreihung' oder 'aufgreifen'. Wird nur gehoben, nie gesenkt.
    origin        text not null,
    error         text,
    result_ref    text,
    workflow_id   text,
    step_path     text,
    -- Argumente im Klartext (R7, KD11). Als Text, damit der Volltextindex
    -- darauf arbeiten kann.
    args_text     text,
    -- Zeitstempel des zuletzt verarbeiteten Ausgangs. Entscheidet, welcher
    -- Ausgang gewinnt -- nicht die Stromreihenfolge, die wegen der Puffer die
    -- Reihenfolge des Leerens ist, nicht die der Ereignisse.
    outcome_ts    real,
    -- Wann ein Teammitglied die Stornierung angefordert hat (R12a). Das ist
    -- **Wissen des Dashboards**, kein Signal: die Bibliothek setzt nur einen
    -- Merker, und ob ein Worker ihn bemerkt, weiss erst der Ausgang.
    cancel_requested_at real,
    first_seen_at real not null,
    last_seen_at  real not null,
    incomplete    integer not null default 0
);

create index if not exists task_last_seen on task(last_seen_at);
-- Der Blaetterschluessel der Task-Liste. Ohne ihn ist jede Seite ein voller
-- Durchlauf plus eine Sortierung im Arbeitsspeicher: gemessen bei 60.000
-- Zeilen 469 ms fuer zwanzig Seiten statt 2 ms.
create index if not exists task_first_seen on task(first_seen_at desc, task_id desc);
create index if not exists task_status    on task(status);
create index if not exists task_name_idx  on task(task_name);
create index if not exists task_worker    on task(worker_id);
-- Die Detailansicht eines Workflows holt alle seine Schritte (Workflow-
-- Ansicht, U5). Die Spalte gab es schon in Stufe 1, darum genuegt das
-- ``if not exists`` auch an einer bestehenden Datei.
create index if not exists task_workflow  on task(workflow_id);

-- Zeitspannen, aus denen Ereignisse fehlen (R4a, U12a).
--
-- Eigene Tabelle und nicht bloss ``task.incomplete``: die Markierung an der
-- Zeile trifft nur **ueberlebende** Zeilen. Die verlorenen Tasks haben
-- definitionsgemaess gar keine Zeile, und dann meldet ``mark_incomplete``
-- schlicht 0 -- die Luecke waere bekannt und trotzdem unsichtbar. Und weil
-- sie hier steht statt im Arbeitsspeicher des Lesers, ueberlebt sie einen
-- Neustart.
create table if not exists gap (
    von         real not null,
    bis         real not null,
    quelle      text not null,
    erkannt_am  real not null,
    primary key (von, bis, quelle)
) without rowid;
-- Die Suche fragt "schneidet der Filterzeitraum eine Luecke?" -- also
-- ueberlappende Spannen, nicht Gleichheit.
create index if not exists gap_span on gap(bis, von);

create table if not exists signal (
    task_id   text not null,
    type      text not null,
    ts        real not null,
    stream_id text,
    payload   text,
    primary key (task_id, stream_id)
) without rowid;

create index if not exists signal_task on signal(task_id, ts);

-- Trigramm, nicht wortweise: gemessen findet der wortweise Index das Fragment
-- "3@example" in "kunde3@example.com" null-mal, der Trigramm-Index
-- 10.000-mal. R10 verlangt genau diese Suche. Preis ist ein knapp doppelt so
-- grosses Verzeichnis (750 statt 409 Byte je Zeile).
create virtual table if not exists task_fts using fts5(
    task_name, args_text,
    content='task', content_rowid='rowid',
    tokenize="trigram"
);

create trigger if not exists task_fts_ai after insert on task begin
    insert into task_fts(rowid, task_name, args_text)
    values (new.rowid, new.task_name, new.args_text);
end;

create trigger if not exists task_fts_ad after delete on task begin
    insert into task_fts(task_fts, rowid, task_name, args_text)
    values ('delete', old.rowid, old.task_name, old.args_text);
end;

create trigger if not exists task_fts_au after update on task begin
    insert into task_fts(task_fts, rowid, task_name, args_text)
    values ('delete', old.rowid, old.task_name, old.args_text);
    insert into task_fts(rowid, task_name, args_text)
    values (new.rowid, new.task_name, new.args_text);
end;

-- Wer laeuft, seit wann, und **woher wir das wissen** (KTD10). Die Quelle
-- gehoert zur Aussage: das Lebenszeichen setzt einen Metrik-Port und ein
-- optionales Paket voraus und schweigt sonst ohne Fehlermeldung -- sein
-- Ausbleiben darf nicht als "getrennt" gelesen werden.
create table if not exists worker (
    worker_id    text primary key,
    worker_group text,
    slot         integer,
    queues       text,
    all_queues   text,
    concurrency  integer,
    pool         text,
    started_at   real,
    last_seen_at real not null,
    -- 1 = irgendein Signal, 2 = Lebenszeichen, 3 = Supervisor. Wird nur
    -- gehoben, nie gesenkt -- sonst faellt die Ansicht nach jedem
    -- gewoehnlichen Signal auf die schwaechste Aussage zurueck.
    source       integer not null default 1
) without rowid;

create index if not exists worker_group_idx on worker(worker_group);

-- Die eigene Kopie des Workflow-Zustands (Workflow-Ansicht, KTD4).
--
-- Sie stammt aus **Lesungen** des Zustands in Valkey, nicht aus dem Strom:
-- nur der Zustand kennt das Ende eines Workflows (R6). Der Strom legt die
-- Zeile an und fuehrt ihre letzte Aktivitaet nach; alles andere schreibt der
-- Abgleich. Ein Neuaufbau aus dem Strom verliert darum die Lesungen, und
-- betroffene Workflows fallen ehrlich auf "Zustand nie gesehen" zurueck.
create table if not exists workflow (
    workflow_id   text primary key,
    -- 1, wenn ein Ereignis ``workflow_id`` trug oder eine Lesung den Zustand
    -- fand. 0 heisst Kandidat: nur die Kennung eines Schritts folgt dem
    -- Muster ``<workflow_id>#<step_path>``, und eigene Task-Kennungen duerfen
    -- ein ``#`` enthalten (KTD5). Wird nur gehoben, nie gesenkt.
    bestaetigt    integer not null default 0,
    -- 'gesehen', 'nie_gesehen' oder 'verfallen'; NULL, solange keine Lesung
    -- etwas ergab.
    sichtbarkeit  text,
    -- Status der letzten Lesung in der Sprache der Bibliothek (running,
    -- completed, failed, cancelled). Ein Endstatus wird nie durch "running"
    -- ueberschrieben.
    status        text,
    -- Form der Wurzel, wie der Zustandsleser sie nennt (``FORMEN`` in
    -- ``workflows.py``: signatur, kette, gruppe, chord).
    wurzel        text,
    -- Topologie als JSON. Traegt die Argumente der Schritte im Klartext
    -- (KD11, KD16 aus Stufe 1).
    topologie     text,
    -- Pfad -> Task-Kennung als JSON. Waechst nur, wie die Bibliothek es
    -- vereinigt.
    step_tasks    text not null default '{}',
    -- Fehlerobjekt des Zustands ``{"type", "message"}`` als JSON.
    fehler        text,
    -- Zeitpunkt der letzten Lesung, die den Zustand fand (R17).
    gelesen_am    real,
    -- Die eigene Stornierung (R16). Wissen des Dashboards, kein Zustand.
    cancel_requested_at real,
    -- Anzeige-Code (``Statusanzeige.code`` in ``workflow_baum.py``) nach der
    -- letzten gelungenen Lesung. Traegt "Zustand verfallen, zuletzt: ..."
    -- (KTD6), auch fuer die eigene Stornierung; der Abgleich schreibt ihn.
    zuletzt_angezeigt text,
    first_seen_at real not null,
    -- Letzte Aktivitaet laut Strom -- derselbe Schluessel, nach dem auch die
    -- Task-Zeilen ablaufen, sonst ueberlebt eine der Quellen die andere.
    last_seen_at  real not null
) without rowid;

create index if not exists workflow_last_seen on workflow(last_seen_at);
-- Sortierung und Keyset der Liste, wie ``task_first_seen`` fuer die Tasks:
-- ohne ihn ist jede Seite ein voller Durchlauf plus Sortierung.
create index if not exists workflow_first_seen on workflow(first_seen_at desc, workflow_id desc);

-- Lesefortschritt im Strom und der Zustand des Neuaufbaus (R8).
create table if not exists progress (
    key   text primary key,
    value text
) without rowid;
"""


class _Ergebnis:
    """Das Ergebnis einer Abfrage, **innerhalb** des Riegels eingesammelt.

    Ein nackter Cursor waere hier die Falle: seine Zeilen holt der Aufrufer
    erst spaeter, also ausserhalb des Riegels, und genau dabei kam der
    ``IndexError`` heraus. Hier sind die Zeilen schon da.
    """

    __slots__ = ("_zeilen", "rowcount")

    def __init__(self, zeilen: list[Any], rowcount: int) -> None:
        self._zeilen = zeilen
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        return self._zeilen[0] if self._zeilen else None

    def fetchall(self) -> list[Any]:
        return self._zeilen

    def __iter__(self) -> Iterator[Any]:
        return iter(self._zeilen)


class GuardedConnection:
    """Die Verbindung des Verzeichnisses samt Riegel.

    ``check_same_thread=False`` erlaubt den Gebrauch aus mehreren Faeden --
    sicher macht es ihn nicht. Der Dienst schiebt die blockierende Arbeit mit
    ``to_thread`` in einen Arbeitsfaden und liest den Zustandsbericht auf der
    Ereignisschleife, und zwar bei **jedem** Seitenaufruf. Ohne Riegel kippte
    etwa jeder vierte Lauf, mit zwei verschiedenen Fehlern: ``IndexError``
    beim Lesen des Fortschritts und ``InterfaceError`` beim Schreiben. Die
    Kontendatei hat fuer genau diesen Fall seit je einen Riegel; das
    Verzeichnis hatte keinen.

    Der Riegel sitzt **in der Verbindung** und nicht in jeder Methode des
    Schreibers: so ist keine der vierzig Aufrufstellen zu aendern, und eine
    neue kann ihn nicht vergessen.
    """

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con
        # ``RLock``: ``apply`` haelt ihn ueber die ganze Transaktion und ruft
        # darin Methoden, die ihn erneut nehmen.
        self._riegel = threading.RLock()

    @contextlib.contextmanager
    def exklusiv(self) -> Iterator[None]:
        """Den Riegel ueber mehrere Anweisungen halten.

        Fuer Transaktionen: ein Stapel ist eine Transaktion, und ein fremder
        Schreibvorgang mittendrin liefe in ihr mit.
        """
        with self._riegel:
            yield

    def execute(self, sql: str, parameters: Any = ()) -> _Ergebnis:
        with self._riegel:
            cur = self._con.execute(sql, parameters)
            zeilen = cur.fetchall() if cur.description is not None else []
            return _Ergebnis(zeilen, cur.rowcount)

    def executescript(self, sql: str) -> None:
        with self._riegel:
            self._con.executescript(sql)

    def commit(self) -> None:
        with self._riegel:
            self._con.commit()

    def close(self) -> None:
        with self._riegel:
            self._con.close()

    def set_trace_callback(self, rueckruf: Any) -> None:
        with self._riegel:
            self._con.set_trace_callback(rueckruf)

    @property
    def row_factory(self) -> Any:
        return self._con.row_factory

    @row_factory.setter
    def row_factory(self, wert: Any) -> None:
        with self._riegel:
            self._con.row_factory = wert


def connect(path: str | Path) -> GuardedConnection:
    """Datei oeffnen oder anlegen, Schema sicherstellen, Pragmas setzen.

    Idempotent: ein zweites Oeffnen laesst vorhandene Daten unberuehrt.
    """
    pfad = Path(path)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    _sichern(pfad)

    # ``check_same_thread=False``: die blockierende SQLite-Arbeit laeuft in
    # einem Arbeitsthread, damit sie den Ereignis-Loop nicht anhaelt (gemessen
    # sonst 6,4 s je Pflegelauf und ein Neuaufbau von rund sieben Minuten, in
    # denen Granians einziger Arbeiter keine Anfrage beantwortet). Das ist nur
    # dann sicher, wenn immer **ein** Thread zur Zeit diese Verbindung in der
    # Hand hat -- dafuer sorgt der Riegel im Strom-Leser. Eine zweite,
    # lesende Verbindung fuer den Anfrageweg ist eine eigene Verbindung, kein
    # geteilter Gebrauch dieser hier.
    con = sqlite3.connect(pfad, isolation_level=None, check_same_thread=False)
    try:
        con.execute("pragma journal_mode=wal")
        con.execute("pragma synchronous=normal")
        # Ohne sicheres Loeschen gibt ein DELETE die Seiten nur frei und laesst
        # die Klartext-Argumente als Rohbytes stehen -- die Zusage "der Ablauf
        # loescht dort wirklich" waere unwahr (R7a). Gemessen ueber alle
        # sechzehn Kombinationen der Loeschschritte: dieses Pragma traegt die
        # Zusage allein, das Verdichten unabhaengig davon ebenso.
        con.execute("pragma secure_delete=on")
        con.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
        con.execute("pragma foreign_keys=on")
        con.executescript(_SCHEMA)
        _spalten_nachziehen(con)
    except Exception:
        # Sonst bleibt bei jedem Fehlversuch ein Dateizeiger offen. Ein Dienst,
        # der laut Plan auch mit fehlenden Diensten hochfaehrt und es im Takt
        # erneut versucht, liefe damit in EMFILE -- gemessen: 200 Fehlversuche,
        # 200 offene Zeiger.
        con.close()
        raise
    return GuardedConnection(con)


def _sichern(pfad: Path) -> None:
    """Datei anlegen, falls noetig, und in jedem Fall auf 0600 bringen.

    Die Rechte werden **vor** dem ersten Schreiben gesetzt: zwischen Anlegen
    und Nachziehen laege sonst ein Fenster, in dem sieben Tage
    Klartext-Argumente fuer jeden lesbar waeren (R7a).

    Und sie werden auch an einer **vorhandenen** Datei nachgezogen. Frueher
    geschah das nur beim Anlegen -- eine Datei, die aus einer Ruecksicherung
    kam, von einem ``ExecStartPre`` angelegt wurde oder aus einer aelteren
    Fassung stammte, behielt dann ihre 0644, und damit auch ``-wal`` und
    ``-shm``, die den Modus der Hauptdatei erben.
    """
    if not pfad.exists():
        os.close(os.open(pfad, os.O_CREAT | os.O_WRONLY, 0o600))
        return
    modus = pfad.stat().st_mode & 0o777
    if modus != 0o600:
        os.chmod(pfad, 0o600)


#: Spalten, die spaeter dazugekommen sind. ``create table if not exists`` legt
#: sie an einer vorhandenen Datei **nicht** an -- und das Verzeichnis ist zwar
#: aus dem Strom wiederherstellbar (R8), aber es stillschweigend unbrauchbar
#: zu machen waere trotzdem die falsche Art, eine Fassung zu wechseln.
_NACHGEREICHT = {
    "task": (("cancel_requested_at", "real"),),
}


def _spalten_nachziehen(con: sqlite3.Connection) -> None:
    """Spalten ergaenzen, die in einer aelteren Datei fehlen."""
    for tabelle, spalten in _NACHGEREICHT.items():
        vorhanden = {z[1] for z in con.execute(f"pragma table_info({tabelle})")}
        for name, typ in spalten:
            if name not in vorhanden:
                con.execute(f"alter table {tabelle} add column {name} {typ}")
