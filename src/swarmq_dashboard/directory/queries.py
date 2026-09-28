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

4. **Der Workflow-Status kommt aus dem Baum, nicht aus SQL** (Workflow-
   Ansicht, U5). Die Liste baut je Zeile ``baue_baum`` aus Kopie und
   Task-Zeilen und filtert auf dessen Anzeige-Code. SQL trifft nur eine
   Vorauswahl, die nichts Passendes ausschliesst: ob eine angeforderte
   Stornierung noch aktive Schritte hat, weiss nur der Baum (R16), und eine
   zweite Fassung der Vorrangregel (KTD6) liefe irgendwann auseinander.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from swarmq_dashboard.directory.schema import GuardedConnection
from swarmq_dashboard.workflow_baum import (
    EINGEREIHT,
    Baum,
    Knoten,
    Taskzeile,
    Workflowkopie,
    baue_baum,
)

#: Kuerzestes Fragment, das ein Trigramm-Index finden kann.
MIN_FRAGMENT = 3
#: Zeilen je Seite, wenn nichts anderes verlangt wird.
DEFAULT_LIMIT = 50
#: Fertige Glieder einer Gruppe je Seite (KTD13).
GLIEDER_JE_SEITE = 100
#: Obergrenze je Seite. Ohne sie holte ein ``limit`` aus einer Adresszeile
#: das ganze Verzeichnis in den Speicher.
MAX_LIMIT = 500
#: Steuerzeichen, die vor der Suche wegfallen. Ein NUL-Byte -- aus einer
#: Adresszeile als ``%00`` trivial zu setzen -- liess SQLite mit
#: "unterminated string" werfen, statt nichts zu finden.
_STEUERZEICHEN = re.compile(r"[\x00-\x1f\x7f]")

_TOO_SHORT = (
    f"Suchbegriffe brauchen mindestens {MIN_FRAGMENT} Zeichen -- "
    "kürzer kann der Trigramm-Index nichts finden."
)


def _lesbar(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _luecken_hinweis(luecken: list[sqlite3.Row], was: str = "Tasks") -> str:
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
            f"verlorengegangen. Dort gelaufene {was} können in dieser Liste "
            "fehlen -- eine leere oder kurze Liste heisst für diesen Zeitraum "
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


#: Der stehende Hinweis der Workflow-Liste (R4, wie R31 der Task-Liste). Er
#: steht auf **jeder** Seite: dass kein betroffener Workflow zu sehen ist,
#: ist selbst keine Aussage.
WORKFLOW_HINWEIS = (
    "Ein Workflow, dessen erster Schritt nicht über den Anwendungs-Hook "
    "eingereiht wurde, erscheint erst, wenn ein Worker einen seiner Schritte "
    "aufgreift."
)


@dataclass(frozen=True)
class Workflowfilter:
    """Die Einschraenkungen aus der Kopfzeile der Workflow-Liste.

    Alle leer heisst: alles. Gesetzte Felder wirken zusammen (UND).
    """

    #: Anzeige-Code aus U4 (:attr:`Statusanzeige.code`): laeuft, fertig,
    #: gescheitert, storniert, angefordert, angefordert_nichts_aktiv,
    #: verfallen, nie_gesehen.
    status: str = ""
    #: Kennung eines Workflows oder eines seiner Schritte (R3), exakt.
    suche: str = ""
    #: Zeitraum auf dem Start des Workflows (``first_seen_at``), beide
    #: Grenzen einschliesslich -- derselbe Zeitpunkt, nach dem die Liste
    #: sortiert und blaettert.
    since: float | None = None
    until: float | None = None


@dataclass(frozen=True)
class Workflowzeile:
    """Eine Zeile der Workflow-Liste."""

    kopie: Workflowkopie
    #: Status, Fortschritt, Form und erster Schritt kommen aus dem Baum (U4),
    #: nicht aus einer eigenen Rechnung dieser Schicht.
    baum: Baum
    #: Start und letzte Aktivitaet laut Strom (R2).
    first_seen_at: float
    last_seen_at: float


@dataclass(frozen=True)
class Workflowdetail:
    """Kopie und alle Task-Zeilen eines Workflows fuer die Detailansicht."""

    kopie: Workflowkopie
    #: Alle Task-Zeilen des Workflows, frueheste zuerst.
    zeilen: list[Taskzeile]
    first_seen_at: float
    last_seen_at: float

    @property
    def baum(self) -> Baum:
        return baue_baum(self.kopie, self.zeilen)


@dataclass
class Workflowseite:
    """Eine Seite der Workflow-Liste."""

    zeilen: list[Workflowzeile] = field(default_factory=list)
    next_cursor: str | None = None
    #: Stets gesetzt: mindestens der stehende Hinweis (R4).
    note: str = WORKFLOW_HINWEIS


@dataclass
class Gliederseite:
    """Eine Seite fertiger Glieder einer Gruppe (R9, KTD13)."""

    glieder: list[Knoten] = field(default_factory=list)
    #: Undurchsichtiger Schluessel fuer die naechste Seite, oder ``None``.
    next_cursor: str | None = None
    #: Warum die Seite leer ist, wenn es keine Gruppe gibt; sonst ``None``.
    note: str | None = None


def _quote(text: str) -> str:
    """Freitext als FTS5-Wortgruppe schuetzen.

    Ohne Anfuehrungszeichen liest FTS5 Zeichen wie ``@``, ``:`` oder ``*`` als
    Syntax und wirft bei einer Adresse oder einem Pfad eine Ausnahme, statt zu
    suchen. Ein eingebettetes Anfuehrungszeichen wird verdoppelt.
    """
    return '"' + text.replace('"', '""') + '"'


def _encode(zeile: sqlite3.Row, kennung: str = "task_id") -> str:
    """Schluessel einer Zeile als Anschlussmarke.

    ``repr`` statt fester Nachkommastellen: ein Zeitstempel um 1,8e9 traegt
    mehr Nachkommastellen, als sechs Stellen fassen. Gerundet liegt die Marke
    mal ueber, mal unter dem gespeicherten Wert -- und die Grenzzeile
    erschiene dann entweder doppelt oder gar nicht. Gemessen rundeten von 400
    echten Zeitstempeln 162 nach oben; beim Durchblaettern mit Seitengroesse
    eins fiel jede vierte Zeile aus. ``repr`` eines ``float`` laesst sich
    verlustfrei zurueckuebersetzen.
    """
    return f"{float(zeile['first_seen_at'])!r}|{zeile[kennung]}"


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


def _kopie(zeile: sqlite3.Row) -> Workflowkopie:
    """Eine Zeile der Tabelle ``workflow`` als Kopie fuer den Baum (U4)."""
    return Workflowkopie(
        workflow_id=zeile["workflow_id"],
        topologie=json.loads(zeile["topologie"]) if zeile["topologie"] else None,
        step_tasks=json.loads(zeile["step_tasks"]),
        status=zeile["status"],
        sichtbarkeit=zeile["sichtbarkeit"] or "",
        fehler=_fehlertext(zeile["fehler"]),
        storno_angefordert_at=zeile["cancel_requested_at"],
        zuletzt_angezeigt=zeile["zuletzt_angezeigt"],
        gelesen_at=zeile["gelesen_am"],
    )


def _fehlertext(roh: str | None) -> str | None:
    """Das Fehlerobjekt ``{"type", "message"}`` des Zustands als eine Zeile,
    wie Python sie druckt: ``ValueError: kaputt``."""
    if not roh:
        return None
    fehler = json.loads(roh)
    teile = [str(fehler[k]) for k in ("type", "message") if fehler.get(k)]
    return ": ".join(teile) or None


#: Anzeige-Code eines Endstatus -> Status der Bibliothek (R6).
_ENDSTATUS_CODE = {"fertig": "completed", "gescheitert": "failed", "storniert": "cancelled"}
#: Anzeige-Codes ohne Endstatus -> was die Kopie dafuer mindestens traegt.
_OHNE_ENDSTATUS = {
    "verfallen": "sichtbarkeit = 'verfallen'",
    "nie_gesehen": "sichtbarkeit = 'nie_gesehen'",
    "angefordert": "cancel_requested_at is not null",
    "angefordert_nichts_aktiv": "cancel_requested_at is not null",
}


def _vorauswahl(code: str) -> tuple[list[str], list[object]]:
    """SQL-Bedingungen, die jeder Workflow mit Anzeige-Code *code* erfuellt.

    Bewusst eine Obermenge der Vorrangregel aus KTD6, keine Nachbildung: ein
    Endstatus kommt nur aus dem Status der Kopie, alle anderen Codes setzen
    voraus, dass sie keinen traegt. Den Rest -- etwa ob nach einer
    Stornierung noch ein Schritt aktiv ist -- entscheidet ``baue_baum``.
    """
    if code in _ENDSTATUS_CODE:
        return ["status = ?"], [_ENDSTATUS_CODE[code]]
    ende = ",".join("?" * len(_ENDSTATUS_CODE))
    bedingungen = [f"(status is null or status not in ({ende}))"]
    if code in _OHNE_ENDSTATUS:
        bedingungen.append(_OHNE_ENDSTATUS[code])
    return bedingungen, list(_ENDSTATUS_CODE.values())


def _glied_marke(cursor: str | None) -> int:
    """Stelle des zuletzt gezeigten Glieds. Unbrauchbares heisst "von vorn"
    (-1), wie in ``_decode``: eine Marke aus der Adresszeile darf nicht werfen."""
    try:
        return int(cursor) if cursor else -1
    except ValueError:
        return -1


def _knoten_bei(knoten: Knoten, pfad: str) -> Knoten | None:
    """Den Knoten mit *pfad* im Baum suchen."""
    if knoten.pfad == pfad:
        return knoten
    for kind in knoten.kinder:
        if (treffer := _knoten_bei(kind, pfad)) is not None:
            return treffer
    return None


class DirectoryQueries:
    """Lesender Zugang zum Verzeichnis. Aendert nichts."""

    def __init__(self, con: GuardedConnection) -> None:
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

    # -- Workflows (Workflow-Ansicht, U5) ---------------------------------

    def workflows(
        self,
        f: Workflowfilter,
        *,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> Workflowseite:
        """Die Workflow-Liste: nur bestaetigte Workflows, neueste zuerst.

        Der Statusfilter wirkt auf den Anzeige-Code des Baums (U4), nicht auf
        eine Nachbildung in SQL: ob "Stornierung angefordert" noch aktive
        Schritte hat, weiss nur der Baum (R16). Darum liest die Abfrage in
        Stapeln und behaelt, was der Baum bestaetigt, bis die Seite voll ist.
        Blaettern ueber ``(first_seen_at, workflow_id)`` wie die Task-Liste;
        die Marke ist die letzte **gezeigte** Zeile, so kommt eine
        verworfene nie doppelt und eine gezeigte nie zweimal.
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        # Ein Kandidat aus dem Muster bleibt unsichtbar, bis eine Lesung ihn
        # bestaetigt (KTD5): eigene Task-Kennungen duerfen ein ``#`` tragen.
        bedingungen = ["bestaetigt=1"]
        werte: list[object] = []
        suche = _STEUERZEICHEN.sub("", f.suche).strip()
        if suche:
            # ``<wf>#<pfad>`` fuehrt auf ``<wf>``, auch wenn der Schritt nie
            # eine Zeile bekam (KTD5); eine eigene Schrittkennung ueber die
            # Spalte ``workflow_id`` ihrer Task-Zeile.
            bedingungen.append(
                "(workflow_id in (?, ?) or workflow_id = "
                "(select task.workflow_id from task where task.task_id = ?))"
            )
            werte.extend((suche, suche.partition("#")[0], suche))
        if f.status:
            # Nur eine Vorauswahl, die nichts Passendes ausschliesst; das
            # Urteil faellt der Baum unten. Sie erspart ihm die Workflows, die
            # schon an Status oder Sichtbarkeit scheitern.
            vorauswahl, vorwerte = _vorauswahl(f.status)
            bedingungen.extend(vorauswahl)
            werte.extend(vorwerte)
        if f.since is not None:
            bedingungen.append("first_seen_at >= ?")
            werte.append(f.since)
        if f.until is not None:
            bedingungen.append("first_seen_at <= ?")
            werte.append(f.until)
        marke = _decode(cursor) if cursor else None
        notiz = self._workflow_notiz(f)

        ergebnis: list[Workflowzeile] = []
        while True:
            anschluss: list[object] = []
            wo = list(bedingungen)
            if marke is not None:
                wo.append("(first_seen_at, workflow_id) < (?, ?)")
                anschluss.extend(marke)
            stapel: list[sqlite3.Row] = self._con.execute(
                f"select * from workflow where {' and '.join(wo)} "
                "order by first_seen_at desc, workflow_id desc limit ?",
                (*werte, *anschluss, limit),
            ).fetchall()
            schritte = self._taskzeilen([z["workflow_id"] for z in stapel])
            for z in stapel:
                kopie = _kopie(z)
                baum = baue_baum(kopie, schritte.get(kopie.workflow_id, []))
                if f.status and baum.status.code != f.status:
                    continue
                ergebnis.append(Workflowzeile(
                    kopie=kopie, baum=baum, first_seen_at=float(z["first_seen_at"]),
                    last_seen_at=float(z["last_seen_at"]),
                ))
                if len(ergebnis) == limit:
                    # Anschluss nur bei voller Seite, wie in ``search``.
                    return Workflowseite(
                        zeilen=ergebnis, next_cursor=_encode(z, "workflow_id"), note=notiz)
            if len(stapel) < limit:
                return Workflowseite(zeilen=ergebnis, note=notiz)
            marke = (float(stapel[-1]["first_seen_at"]), stapel[-1]["workflow_id"])

    def workflow_detail(self, workflow_id: str) -> Workflowdetail | None:
        """Kopie und alle Task-Zeilen eines Workflows, ueber den Index
        ``task(workflow_id)``. ``None``, wenn es ihn nicht gibt -- oder nur
        als unbestaetigten Kandidaten (KTD5)."""
        zeile = self._con.execute(
            "select * from workflow where workflow_id=? and bestaetigt=1", (workflow_id,)
        ).fetchone()
        if zeile is None:
            return None
        schritte = self._taskzeilen([workflow_id]).get(workflow_id, [])
        return Workflowdetail(
            kopie=_kopie(zeile),
            zeilen=sorted(schritte, key=lambda z: (z.first_seen_at, z.task_id)),
            first_seen_at=float(zeile["first_seen_at"]),
            last_seen_at=float(zeile["last_seen_at"]),
        )

    def fertige_glieder(
        self, workflow_id: str, gruppenpfad: str, *, cursor: str | None = None
    ) -> Gliederseite:
        """Die fertigen Glieder einer Gruppe, seitenweise zu 100 (KTD13).

        Der Schluessel ist die Stelle des Glieds in der Gruppe: die Topologie
        aendert sich nie, also verschiebt sich die Reihenfolge nicht. Ein
        Glied, das waehrend des Blaetterns fertig wird, taucht hinter der
        Marke auf oder gar nicht mehr davor -- doppelt nie.
        """
        detail = self.workflow_detail(workflow_id)
        if detail is None:
            return Gliederseite(note="Diesen Workflow kennt das Verzeichnis nicht.")
        gruppe = _knoten_bei(detail.baum.wurzel, gruppenpfad)
        if gruppe is None or gruppe.art != "gruppe":
            return Gliederseite(
                note=f"Unter dem Pfad {gruppenpfad!r} steht in diesem Workflow keine Gruppe.")
        ab = _glied_marke(cursor)
        fertig = [(i, k) for i, k in enumerate(gruppe.kinder) if i > ab and k.ist_fertig]
        seite = fertig[:GLIEDER_JE_SEITE]
        weiter = str(seite[-1][0]) if len(fertig) > GLIEDER_JE_SEITE else None
        return Gliederseite(glieder=[k for _i, k in seite], next_cursor=weiter)

    def _workflow_notiz(self, f: Workflowfilter) -> str:
        """Der stehende Hinweis (R4), dazu der zu Luecken im Zeitraum (R4a).

        Wie in ``search`` haengt der Luecken-Hinweis am Filterzeitraum, nicht
        an den Treffern: ein verlorener Workflow hat keine Zeile.
        """
        luecken = self.gaps(since=f.since, until=f.until)
        if not luecken:
            return WORKFLOW_HINWEIS
        return f"{WORKFLOW_HINWEIS} {_luecken_hinweis(luecken, 'Workflows')}"

    def _taskzeilen(self, workflow_ids: list[str]) -> dict[str, list[Taskzeile]]:
        """Die Task-Zeilen mehrerer Workflows in einer Abfrage, ueber den Index
        ``task(workflow_id)``."""
        if not workflow_ids:
            return {}
        platz = ",".join("?" * len(workflow_ids))
        nach_workflow: dict[str, list[Taskzeile]] = {}
        for z in self._con.execute(
            "select workflow_id, task_id, step_path, status, task_name, first_seen_at, "
            f"started_at, ended_at, error from task where workflow_id in ({platz})",
            workflow_ids,
        ):
            nach_workflow.setdefault(z["workflow_id"], []).append(Taskzeile(
                # Ohne Status kennt die Zeile nur die Meldung eines
                # Folgeschritts (``workflow_step_enqueued``): eingereiht, noch
                # nicht aufgegriffen -- und damit aktiv (KTD7, R16).
                task_id=z["task_id"], step_path=z["step_path"],
                status=z["status"] or EINGEREIHT,
                task_name=z["task_name"], first_seen_at=float(z["first_seen_at"]),
                started_at=z["started_at"], ended_at=z["ended_at"], error=z["error"],
            ))
        return nach_workflow

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

    def reach(self, *, retention_start: float | None = None) -> float | None:
        """Wie weit die Suche wirklich zurueckreicht (R8).

        Nicht einfach der aelteste Eintrag. Geloescht wird nach
        ``last_seen_at``, gemessen wurde bisher ``min(first_seen_at)`` -- eine
        einzige langlebige Zeile (Dauerlaeufer, Workflow,
        Wiederholungskette) zog die gemeldete Reichweite beliebig weit
        zurueck, waehrend alles dazwischen laengst geraeumt war. Die Seite
        sagte dann "die Suche reicht zurueck bis vor 30 Tagen" und antwortete
        auf eine Suche in Tag 15 mit "keine Treffer". R8 nennt diesen Fehler
        woertlich vorweg.

        Drei Boeden, und es gilt der spaeteste:

        * der aelteste Eintrag,
        * die Aufbewahrungsgrenze (``retention_start``) -- davor wurde
          geloescht,
        * der Beginn des Verzeichnisses -- davor hat es nie gelesen.

        Der dritte fehlte im ersten Anlauf, und damit blieb die Zusage
        gebrochen: ein Dienst, der seit zwei Tagen laeuft, meldete sieben
        Tage. Die Zahl kann jetzt **zu vorsichtig** sein -- eine noch lebende
        Zeile von davor ist ja da --, und das ist die richtige Richtung: sie
        verspricht nie mehr Abdeckung, als es gibt.
        """
        wert = self._con.execute("select min(first_seen_at) from task").fetchone()[0]
        if wert is None:
            return None
        boeden = [float(wert)]
        if retention_start is not None:
            boeden.append(retention_start)
        # Der dritte Boden: vor dem Beginn des Verzeichnisses kann es nichts
        # wissen. Ohne ihn meldete ein Dienst, der seit zwei Tagen laeuft,
        # bei sieben Tagen Aufbewahrung sieben Tage Reichweite -- weil eine
        # einzige lebende Zeile von vor dreissig Tagen den aeltesten Eintrag
        # unter die Grenze zog und ``max`` dann auf die Grenze sprang.
        beginn = self._beginn()
        if beginn is not None:
            boeden.append(beginn)
        return max(boeden)

    def _beginn(self) -> float | None:
        zeile = self._con.execute(
            "select value from progress where key='coverage_start'"
        ).fetchone()
        if zeile is None:
            return None
        try:
            return float(zeile[0])
        except (TypeError, ValueError):
            return None
