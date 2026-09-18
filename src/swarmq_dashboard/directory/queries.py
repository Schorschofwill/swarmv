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

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

#: Kuerzestes Fragment, das ein Trigramm-Index finden kann.
MIN_FRAGMENT = 3
#: Zeilen je Seite, wenn nichts anderes verlangt wird.
DEFAULT_LIMIT = 50
#: Obergrenze je Seite. Ohne sie holte ein ``limit`` aus einer Adresszeile
#: das ganze Verzeichnis in den Speicher.
MAX_LIMIT = 500
#: Steuerzeichen, die vor der Suche wegfallen. Ein NUL-Byte -- aus einer
#: Adresszeile als ``%00`` trivial zu setzen -- liess SQLite mit
#: "unterminated string" werfen, statt nichts zu finden.
_STEUERZEICHEN = re.compile(r"[\x00-\x1f\x7f]")

_TOO_SHORT = (
    f"Suchbegriffe brauchen mindestens {MIN_FRAGMENT} Zeichen -- "
    "kuerzer kann der Trigramm-Index nichts finden."
)


def _lesbar(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _luecken_hinweis(luecken: list[sqlite3.Row]) -> str:
    """Die Rueckmeldung zu einer Suche, die eine Luecke schneidet (R4a).

    Der Hinweis nennt die Spanne, nicht bloss "unvollstaendig": ohne sie
    liesse sich nicht sagen, ob der gesuchte Task betroffen ist. Und er sagt
    ausdruecklich, was fehlen **kann** -- nicht, dass etwas fehlt: ob in der
    Spanne ueberhaupt ein Task lief, weiss niemand mehr. Genau das ist der
    Punkt.
    """
    von = min(float(z["von"]) for z in luecken)
    bis = max(float(z["bis"]) for z in luecken)
    zahl = (f"In {len(luecken)} Zeitspannen zwischen" if len(luecken) > 1
            else "In der Zeitspanne von")
    return (f"{zahl} {_lesbar(von)} und {_lesbar(bis)} sind Ereignisse "
            "verlorengegangen. Dort gelaufene Tasks koennen in dieser Liste "
            "fehlen -- eine leere oder kurze Liste heisst fuer diesen Zeitraum "
            "nicht, dass nichts passiert ist.")


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
    """Schluessel einer Zeile als Anschlussmarke.

    ``repr`` statt fester Nachkommastellen: ein Zeitstempel um 1,8e9 traegt
    mehr Nachkommastellen, als sechs Stellen fassen. Gerundet liegt die Marke
    mal ueber, mal unter dem gespeicherten Wert -- und die Grenzzeile
    erschiene dann entweder doppelt oder gar nicht. Gemessen rundeten von 400
    echten Zeitstempeln 162 nach oben; beim Durchblaettern mit Seitengroesse
    eins fiel jede vierte Zeile aus. ``repr`` eines ``float`` laesst sich
    verlustfrei zurueckuebersetzen.
    """
    return f"{float(zeile['first_seen_at'])!r}|{zeile['task_id']}"


def _decode(cursor: str) -> tuple[float, str] | None:
    """Anschlussmarke zurueckuebersetzen. Unbrauchbares heisst "von vorn".

    ``float`` nimmt auch ``nan`` und ``inf`` an, und beide sind hier
    Falschaussagen: mit ``nan`` ist jeder Vergleich unwahr, die Seite kommt
    leer zurueck und liest sich als "keine Treffer"; mit ``inf`` steht die
    Marke ueber allem, Seite zwei ist wieder Seite eins und das Blaettern
    endet nie.
    """
    marke, _, task_id = cursor.partition("|")
    try:
        wert = float(marke)
    except ValueError:
        return None
    return (wert, task_id) if math.isfinite(wert) else None


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
        text = _STEUERZEICHEN.sub("", f.text).strip()
        # Ein nicht leeres Suchfeld, von dem nichts Brauchbares uebrig bleibt,
        # ist kein "kein Filter". Fiel es darauf durch, kamen **alle** Tasks
        # zurueck, und die Oberflaeche zeigte eine Suche, die nichts
        # einschraenkt.
        if f.text and len(text) < MIN_FRAGMENT:
            return Page(note=_TOO_SHORT)
        # Eine Seitengroesse aus einer Adresszeile ist nicht vertrauenswuerdig:
        # 0 liess die Anschlussmarke ins Leere greifen, -1 hiess fuer SQLite
        # "alle Zeilen".
        limit = max(1, min(int(limit), MAX_LIMIT))

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
        # Der Hinweis haengt am **Filterzeitraum**, nicht an den Treffern: die
        # verlorenen Tasks haben keine Zeile, also kann keine Zeile auf sie
        # hinweisen. Ohne gesetzten Filter gilt die ganze Reichweite -- den
        # Hinweis dann zu unterschlagen hiesse, ihn ausgerechnet im
        # haeufigsten Aufruf wegzulassen.
        luecken = self.gaps(since=f.since, until=f.until)
        hinweis = _luecken_hinweis(luecken) if luecken else None
        return Page(rows=zeilen, next_cursor=weiter, note=hinweis)

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

    def gaps(self, *, since: float | None = None, until: float | None = None
             ) -> list[sqlite3.Row]:
        """Festgehaltene Luecken, die den Zeitraum schneiden (R4a, U12a).

        Ohne Grenzen: alle. Zwei Spannen schneiden sich, wenn jede vor dem
        Ende der anderen beginnt -- die uebliche Ueberlappungsbedingung, und
        sie ist einschliesslich: eine Luecke, die genau am Filterrand endet,
        betrifft den Rand noch.
        """
        bedingungen: list[str] = []
        werte: list[object] = []
        if since is not None:
            bedingungen.append("bis >= ?")
            werte.append(since)
        if until is not None:
            bedingungen.append("von <= ?")
            werte.append(until)
        wo = (" where " + " and ".join(bedingungen)) if bedingungen else ""
        zeilen: list[sqlite3.Row] = self._con.execute(
            f"select * from gap{wo} order by von", werte
        ).fetchall()
        return zeilen

    def reach(self) -> float | None:
        """Zeitpunkt des aeltesten Eintrags -- wie weit die Ansicht zurueckreicht (R8)."""
        wert = self._con.execute("select min(first_seen_at) from task").fetchone()[0]
        return float(wert) if wert is not None else None
