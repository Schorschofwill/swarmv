"""Lesende Abfragen auf dem Verzeichnis: Suche, Filter, Blaettern, Signalfolge.

Drei Entscheidungen tragen diese Schicht, und jede hat einen Test:

1. **Volltext ueber Trigramme.** Gemessen findet der wortweise Index das
   Fragment ``3@example`` in ``kunde3@example.com`` null-mal, der
   Trigramm-Index 10.000-mal (R10, AE1a). Der Preis ist eine Untergrenze:
   kuerzer als drei Zeichen kann ein Trigramm-Index nichts finden. Das wird
   **benannt** statt als leere Trefferliste ausgegeben -- eine leere Liste
   liest sich wie "nicht gelaufen", und genau diesen Irrtum vermeidet das
   Dashboard an jeder Stelle.

2. **Zeitraum auf ``coalesce(enqueued_at, first_seen_at)``.** Scheduler- und
   verzoegerte Einreihungen haben keinen Einreihungszeitpunkt (R31). Ohne die
   Ersatzregel fielen genau die Tasks aus jeder Zeitraumsuche, die man am
   ehesten sucht.

3. **Blaettern ueber einen Schluessel, nicht ueber einen Versatz.**
   ``offset`` verschiebt sich, sobald nebenher geschrieben wird -- und
   geschrieben wird immer. Der Schluessel ist ``(first_seen_at, task_id)``:
   ``first_seen_at`` wird nach dem Anlegen nie wieder geaendert, ``last_seen_at``
   dagegen bei jedem Signal. Ueber ``last_seen_at`` zu blaettern hiesse, dass
   eine Zeile waehrend des Blaetterns nach vorn springt und dahinter eine
   andere ungesehen durchrutscht.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

#: Kuerzestes Fragment, das ein Trigramm-Index finden kann.
MIN_FRAGMENT = 3
#: Zeilen je Seite, wenn nichts anderes verlangt wird.
DEFAULT_LIMIT = 50

_TOO_SHORT = (
    f"Suchbegriffe brauchen mindestens {MIN_FRAGMENT} Zeichen -- "
    "kuerzer kann der Trigramm-Index nichts finden."
)


@dataclass(frozen=True)
class Filter:
    """Die Einschraenkungen aus der Kopfzeile der Task-Liste.

    Alle leer heisst: alles. Gesetzte Felder wirken zusammen (UND), weil das
    der Erwartung beim Eingrenzen entspricht.
    """

    text: str = ""
    status: str = ""
    task_name: str = ""
    worker_id: str = ""
    #: Zeitraum auf dem Einreihungszeitpunkt, ersatzweise dem ersten
    #: bekannten Zeitpunkt (R31). ``since`` ist einschliesslich.
    since: float | None = None
    until: float | None = None


@dataclass
class Page:
    """Eine Seite Treffer samt Anschluss und etwaiger Erlaeuterung."""

    rows: list[sqlite3.Row] = field(default_factory=list)
    #: Undurchsichtiger Schluessel fuer die naechste Seite, oder ``None``.
    next_cursor: str | None = None
    #: Grund, warum die Seite leer oder unvollstaendig ist -- nie ``None``
    #: schweigend. Die Oberflaeche zeigt ihn statt "keine Treffer".
    note: str | None = None


def _quote(text: str) -> str:
    """Freitext als FTS5-Wortgruppe schuetzen.

    Ohne Anfuehrungszeichen liest FTS5 Zeichen wie ``@``, ``:`` oder ``*`` als
    Syntax und wirft bei einer Adresse oder einem Pfad eine Ausnahme, statt zu
    suchen. Ein eingebettetes Anfuehrungszeichen wird verdoppelt.
    """
    return '"' + text.replace('"', '""') + '"'


def _encode(zeile: sqlite3.Row) -> str:
    """Schluessel einer Zeile als Anschlussmarke."""
    return f"{float(zeile['first_seen_at']):017.6f}|{zeile['task_id']}"


def _decode(cursor: str) -> tuple[float, str] | None:
    """Anschlussmarke zurueckuebersetzen. Unbrauchbares heisst "von vorn"."""
    marke, _, task_id = cursor.partition("|")
    try:
        return (float(marke), task_id)
    except ValueError:
        return None


class DirectoryQueries:
    """Lesender Zugang zum Verzeichnis. Aendert nichts."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con
        self._con.row_factory = sqlite3.Row

    # -- Suche und Filter -------------------------------------------------

    def search(
        self,
        f: Filter,
        *,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> Page:
        """Gefilterte Task-Liste, neueste zuerst, seitenweise."""
        text = f.text.strip()
        if text and len(text) < MIN_FRAGMENT:
            return Page(note=_TOO_SHORT)

        quelle = "task"
        bedingungen: list[str] = []
        werte: list[object] = []

        if text:
            # Join ueber die rowid statt ``where task_fts match``-Unterabfrage:
            # so bleibt der Index die treibende Seite der Abfrage.
            quelle = "task join task_fts on task_fts.rowid = task.rowid"
            bedingungen.append("task_fts match ?")
            werte.append(_quote(text))

        for spalte, wert in (
            ("status", f.status),
            ("task_name", f.task_name),
            ("worker_id", f.worker_id),
        ):
            if wert:
                bedingungen.append(f"task.{spalte} = ?")
                werte.append(wert)

        # R31: ohne Einreihungszeitpunkt gilt der erste bekannte Zeitpunkt,
        # sonst faellt jede Scheduler-Einreihung aus der Zeitraumsuche.
        zeit = "coalesce(task.enqueued_at, task.first_seen_at)"
        if f.since is not None:
            bedingungen.append(f"{zeit} >= ?")
            werte.append(f.since)
        if f.until is not None:
            bedingungen.append(f"{zeit} <= ?")
            werte.append(f.until)

        if cursor and (marke := _decode(cursor)) is not None:
            # Streng kleiner als die zuletzt gesehene Zeile, mit task_id als
            # Entscheid bei gleichem Zeitpunkt -- sonst verschwaende ein
            # Gleichstand entweder eine Zeile oder zeigte sie doppelt.
            bedingungen.append("(task.first_seen_at, task.task_id) < (?, ?)")
            werte.extend(marke)

        wo = (" where " + " and ".join(bedingungen)) if bedingungen else ""
        sql = (
            f"select task.* from {quelle}{wo} "
            "order by task.first_seen_at desc, task.task_id desc limit ?"
        )
        zeilen: list[sqlite3.Row] = self._con.execute(sql, (*werte, limit)).fetchall()

        # Anschluss nur, wenn die Seite voll ist: eine halbe Seite heisst, dass
        # nichts mehr kommt.
        weiter = _encode(zeilen[-1]) if len(zeilen) == limit else None
        return Page(rows=zeilen, next_cursor=weiter)

    # -- Einzelne Task ----------------------------------------------------

    def signals(self, task_id: str) -> list[sqlite3.Row]:
        """Signalfolge einer Task in **Stromreihenfolge** (R11).

        Nicht nach ``ts``: Anwendungs- und Worker-Host haben getrennte Uhren,
        und ein Versatz von Sekunden reicht, damit das Aufgreifen vor der
        Einreihung erscheint. Der Strom hat nur eine Uhr, also ordnet seine
        Kennung. Sie ist Text und wird als Text verglichen; Valkey vergibt die
        Millisekunden mit gleichbleibender Stellenzahl, damit stimmen
        Text- und Zahlenreihenfolge ueberein.
        """
        zeilen: list[sqlite3.Row] = self._con.execute(
            "select * from signal where task_id=? order by stream_id", (task_id,)
        ).fetchall()
        return zeilen

    # -- Zustand ----------------------------------------------------------

    def reach(self) -> float | None:
        """Zeitpunkt des aeltesten Eintrags -- wie weit die Ansicht zurueckreicht (R8)."""
        wert = self._con.execute("select min(first_seen_at) from task").fetchone()[0]
        return float(wert) if wert is not None else None
