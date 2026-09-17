"""Schema und Verbindungseinstellungen des Verzeichnisses.

Eine Datei, zwei Tabellen und ein Volltextindex. Die Einstellungen der
Verbindung sind keine Feinabstimmung, sondern tragen je eine Zusage aus dem
Plan -- der Kommentar an jedem Pragma sagt welche.

Die Kontendatei liegt **daneben**, nicht hier (KTD8): der Neuaufbau des
Verzeichnisses aus dem Strom ist ein vorgesehener Weg, und laege alles
zusammen, wuerde sein Loeschen das Team aussperren.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

#: Tabellen und Sichten, die eine vollstaendige Datei traegt.
TABLES = {"task", "signal", "task_fts", "progress"}

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
    first_seen_at real not null,
    last_seen_at  real not null,
    incomplete    integer not null default 0
);

create index if not exists task_last_seen on task(last_seen_at);
create index if not exists task_status    on task(status);
create index if not exists task_name_idx  on task(task_name);
create index if not exists task_worker    on task(worker_id);

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

-- Lesefortschritt im Strom und der Zustand des Neuaufbaus (R8).
create table if not exists progress (
    key   text primary key,
    value text
) without rowid;
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Datei oeffnen oder anlegen, Schema sicherstellen, Pragmas setzen.

    Idempotent: ein zweites Oeffnen laesst vorhandene Daten unberuehrt.
    """
    pfad = Path(path)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    neu = not pfad.exists()
    if neu:
        # Rechte vor dem ersten Schreiben setzen, nicht danach -- zwischen
        # Anlegen und Nachziehen laege sonst ein Fenster, in dem sieben Tage
        # Klartext-Argumente fuer jeden lesbar waeren (R7a).
        os.close(os.open(pfad, os.O_CREAT | os.O_WRONLY, 0o600))

    con = sqlite3.connect(pfad, isolation_level=None)
    con.execute("pragma journal_mode=wal")
    con.execute("pragma synchronous=normal")
    # Ohne sicheres Loeschen gibt ein DELETE die Seiten nur frei und laesst die
    # Klartext-Argumente als Rohbytes stehen -- die Zusage "der Ablauf loescht
    # dort wirklich" waere unwahr (R7a).
    con.execute("pragma secure_delete=on")
    con.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
    con.execute("pragma foreign_keys=on")
    con.executescript(_SCHEMA)
    return con
