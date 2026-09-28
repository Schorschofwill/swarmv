"""Baum, Zaehler und Gruende eines Workflows (KTD7) -- rein, ohne I/O.

Eingaben sind die Kopie des Workflow-Zustands (:class:`Workflowkopie`) und
die Task-Zeilen des Workflows aus dem Verzeichnis (:class:`Taskzeile`).
Ausgabe ist ein :class:`Baum`, aus dem Liste und Detailansicht alles
ablesen, ohne selbst zu rechnen.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cached_property
from types import MappingProxyType
from typing import Any, Protocol

#: Status des Workflow-Zustands in der Sprache der Bibliothek (Schema
#: ``v=1``, ``workflow_state.py``). Hier und nicht im Leser
#: (``workflows.py``) definiert, damit dieses Modul ohne dessen
#: Backend-Abhaengigkeiten auskommt; der Leser reicht die Namen weiter.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
#: Die Endstatus der Bibliothek: alle ausser ``running`` (R6).
ENDSTATUS = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED})

#: Blattstatus: steht in ``step_tasks``, hat aber noch keine Task-Zeile. Die
#: Bibliothek veroeffentlicht zuerst und vermerkt danach -- und der Hook
#: meldet die Einreihung mitunter spaeter als der Vermerk (KTD7).
EINGEREIHT = "eingereiht"
#: Blattstatus: laut Topologie da, aber weder vermerkt noch gemeldet (R11).
NICHT_EINGEREIHT = "nicht_eingereiht"

#: Sichtbarkeit und Anzeige-Code: der Zustand war gelesen und ist verfallen.
VERFALLEN = "verfallen"

#: Sichtbarkeit: der Zustand war nie lesbar, bevor er verfiel (R7).
NIE_GESEHEN = "nie_gesehen"
#: Sichtbarkeit: der Zustand wurde mindestens einmal gelesen.
GESEHEN = "gesehen"

#: Gruende fuer einen nicht eingereihten Schritt (R11).
GRUND_WARTET = "wartet auf Vorgänger"
GRUND_GESCHEITERT = "nie gestartet, Workflow gescheitert"
GRUND_STORNIERT = "nie gestartet, Workflow storniert"
GRUND_STORNO_GLIED = "wartet auf storniertes Glied, startet nie"
GRUND_VORGAENGER_FERTIG = "Vorgänger fertig, nicht eingereiht"

#: Blattstatus, die fuer "nichts mehr aktiv" als aktiv zaehlen (R16): was
#: laeuft oder wartet, und was eingereiht, aber noch nicht gemeldet ist.
_AKTIV = frozenset({"wartend", "laufend", "wiederholung", EINGEREIHT})

#: Ausgaenge, die einen Schritt als gescheitert nennen (R12).
_GESCHEITERT = frozenset({"gescheitert", "dead_letter"})


@dataclass(frozen=True)
class Taskzeile:
    """Was der Baum von einer Task-Zeile des Verzeichnisses braucht."""

    task_id: str
    step_path: str | None
    #: Status im Vokabular des Verzeichnisses (``directory/writer.py``).
    status: str
    task_name: str | None = None
    first_seen_at: float = 0.0
    started_at: float | None = None
    ended_at: float | None = None
    error: str | None = None


class _Spalten(Protocol):
    """Eine Zeile mit Spaltenzugriff, etwa ``sqlite3.Row``."""

    def __getitem__(self, spalte: str) -> Any: ...


def taskzeile_aus(zeile: _Spalten) -> Taskzeile:
    """Eine Task-Zeile des Verzeichnisses als :class:`Taskzeile` -- die eine
    Stelle dafuer, damit Ansicht und Abgleich denselben Baum sehen.

    Ohne Status kennt die Zeile nur die Meldung eines Folgeschritts
    (``workflow_step_enqueued``): eingereiht, noch nicht aufgegriffen -- und
    damit aktiv (KTD7, R16).
    """
    return Taskzeile(
        task_id=zeile["task_id"], step_path=zeile["step_path"],
        status=zeile["status"] or EINGEREIHT,
        task_name=zeile["task_name"], first_seen_at=float(zeile["first_seen_at"]),
        started_at=zeile["started_at"], ended_at=zeile["ended_at"], error=zeile["error"],
    )


@dataclass(frozen=True)
class Workflowkopie:
    """Die Kopie des Workflow-Zustands im Verzeichnis (KTD4)."""

    workflow_id: str
    topologie: Mapping[str, Any] | None
    step_tasks: Mapping[str, str]
    #: Status aus der letzten Lesung: running, completed, failed,
    #: cancelled -- oder None ohne jede Lesung.
    status: str | None
    sichtbarkeit: str
    #: Fehlertext aus dem Zustand (R12).
    fehler: str | None = None
    #: Wann das Dashboard selbst die Stornierung angefordert hat (KTD6, KTD9).
    storno_angefordert_at: float | None = None
    #: Anzeige-Code (:attr:`Statusanzeige.code`), der zuletzt galt, solange
    #: der Zustand noch lesbar war -- fuer "verfallen, zuletzt: ..." (KTD6).
    zuletzt_angezeigt: str | None = None
    #: Zeitpunkt der letzten gelungenen Lesung (R17).
    gelesen_at: float | None = None


@dataclass(frozen=True)
class Ergebnisverweis:
    """Wessen Ergebnis ein Kopf-Glied als Eingabe des Body zeigt (KTD8, R18).

    ``anzeigbar`` False heisst "nicht einzeln anzeigbar": das Glied endet auf
    einer Gruppe, deren Sammelergebnis die Bibliothek nicht herausgibt (KD9).
    ``task_id`` None bei anzeigbarem Glied heisst: der Schritt ist noch nicht
    eingereiht, es gibt noch nichts zu lesen.
    """

    anzeigbar: bool
    task_id: str | None = None


@dataclass(frozen=True)
class Knoten:
    """Ein Element der Topologie."""

    #: Topologie-Pfad; ``""`` ist die einfache Wurzel, nicht "leer" (KTD7).
    #: None nur im flachen Baum, wenn der Pfad eines Schritts unbekannt ist.
    pfad: str | None
    art: str
    kinder: tuple[Knoten, ...] = ()
    #: "kopf" oder "body" unter einem Chord, sonst None.
    rolle: str | None = None
    task_name: str | None = None
    #: Bei Blaettern: der Status der Task-Zeile; ohne Zeile :data:`EINGEREIHT`
    #: oder :data:`NICHT_EINGEREIHT`. Bei inneren Knoten None.
    status: str | None = None
    task_id: str | None = None
    zeile: Taskzeile | None = None
    #: Warum ein Blatt nicht eingereiht ist (R11), sonst None.
    grund: str | None = None
    #: Beim noch nicht eingereihten Chord-Body: auf wie viele direkte
    #: Kopf-Glieder er noch wartet (R10, KTD7).
    wartet_auf: int | None = None
    #: Nur an den direkten Gliedern eines Chord-Kopfs, sonst None.
    ergebnis: Ergebnisverweis | None = None

    # Zwischengespeichert (``cached_property`` schreibt am eingefrorenen
    # ``__setattr__`` vorbei in ``__dict__``): ein Knoten aendert sich nach
    # dem Bau nie -- ``replace`` baut einen neuen --, und Faltung, Zaehler
    # und Fortschritt fragen dieselben Blaetter sonst je Ebene erneut ab.

    @cached_property
    def blaetter(self) -> tuple[Knoten, ...]:
        """Die Blaetter unter diesem Knoten, in Topologie-Reihenfolge."""
        if self.art == "schritt":
            return (self,)
        return tuple(b for kind in self.kinder for b in kind.blaetter)

    @cached_property
    def ist_fertig(self) -> bool:
        """Alle Blaetter darunter sind fertig."""
        return all(b.status == "fertig" for b in self.blaetter)

    @property
    def offene_kinder(self) -> tuple[Knoten, ...]:
        """Die Kinder, die als eigene Zeile stehen (R9). Nur eine Gruppe
        faltet; die Glieder einer Kette stehen immer einzeln."""
        if self.art != "gruppe":
            return self.kinder
        return tuple(k for k in self.kinder if not k.ist_fertig)

    @property
    def gefaltete_kinder(self) -> tuple[Knoten, ...]:
        """Die fertigen Glieder einer Gruppe, als eine aufklappbare Zeile (R9)."""
        if self.art != "gruppe":
            return ()
        return tuple(k for k in self.kinder if k.ist_fertig)

    @property
    def zaehler(self) -> dict[str, int]:
        """Blaetter unter diesem Knoten je Status (R10)."""
        zaehler: dict[str, int] = {}
        for blatt in self.blaetter:
            schluessel = blatt.status or NICHT_EINGEREIHT
            zaehler[schluessel] = zaehler.get(schluessel, 0) + 1
        return zaehler


@dataclass(frozen=True)
class Fortschritt:
    """Fertige Blaetter zu allen Blaettern (R2, KTD7)."""

    fertig: int
    gesamt: int


#: Anzeige-Code -> Text.
_STATUSTEXT = {
    "laeuft": "läuft",
    "fertig": "fertig",
    "gescheitert": "gescheitert",
    "storniert": "storniert",
    "nie_gesehen": "Zustand nie gesehen",
    "angefordert": "Stornierung angefordert",
    "angefordert_nichts_aktiv": "Stornierung angefordert, nichts mehr aktiv",
}
#: Was "verfallen, zuletzt: ..." nennen darf (KTD6).
_VOR_DEM_VERFALL = frozenset({"laeuft", "angefordert", "angefordert_nichts_aktiv"})
#: Endstatus der Bibliothek -> Anzeige-Code. Nur diese drei sind Endstatus
#: (R6). Oeffentlich, weil die Abfragen daraus ihre Vorauswahl ableiten.
ENDSTATUS_ANZEIGE: Mapping[str, str] = MappingProxyType({
    STATUS_COMPLETED: "fertig", STATUS_FAILED: "gescheitert", STATUS_CANCELLED: "storniert",
})


@dataclass(frozen=True)
class Statusanzeige:
    """Der angezeigte Workflow-Status nach der Vorrangregel (KTD6).

    ``code`` ist stabil und zum Ablegen und Filtern gedacht: laeuft, fertig,
    gescheitert, storniert, angefordert, angefordert_nichts_aktiv,
    verfallen, nie_gesehen. Nur fertig, gescheitert und storniert sind
    Endstatus, und die kommen nur aus einer Lesung (R6).
    """

    code: str
    #: Bei "verfallen": der Anzeige-Code, der zuletzt galt.
    zuletzt: str | None = None
    #: Zeitpunkt der letzten gelungenen Lesung (R17).
    gelesen_at: float | None = None

    @property
    def text(self) -> str:
        if self.code == VERFALLEN:
            zuletzt = self.zuletzt or "laeuft"
            if zuletzt not in _VOR_DEM_VERFALL:
                return "Zustand verfallen"
            return f"Zustand verfallen, zuletzt: {_STATUSTEXT[zuletzt]}"
        return _STATUSTEXT[self.code]

    @property
    def endstatus(self) -> bool:
        """Fertig, gescheitert oder storniert -- nur aus einer Lesung (R6)."""
        return self.code in ENDSTATUS_ANZEIGE.values()

    @property
    def stornierbar(self) -> bool:
        """Ob die Ansicht "Workflow stornieren" anbietet (R15, R16).

        Eine bestehende Anforderung bleibt stornierbar -- ein zweites Klicken
        ist kein Fehler (AE8). Bei "nichts mehr aktiv" nicht (AE11), und ohne
        lesbaren Zustand auch nicht: Stornieren liest ihn vorher neu (KTD9).
        """
        return self.code in ("laeuft", "angefordert")


@dataclass(frozen=True)
class Baum:
    """Alles, was die Ansicht ueber einen Workflow zeigt."""

    wurzel: Knoten
    status: Statusanzeige
    fehlertext: str | None = None
    #: Gescheiterte Schritte laut Verzeichnis, fruehester zuerst (R12).
    fehlschlaege: tuple[Knoten, ...] = ()

    @cached_property
    def blaetter(self) -> tuple[Knoten, ...]:
        return self.wurzel.blaetter

    @property
    def form(self) -> str | None:
        """Form der Wurzel: schritt, kette, gruppe, chord; None ohne Topologie."""
        return None if self.wurzel.art == "unbekannt" else self.wurzel.art

    @property
    def erster_schritt(self) -> str | None:
        """Task-Name des ersten Schritts (R2)."""
        blaetter = self.blaetter
        return blaetter[0].task_name if blaetter else None

    @property
    def fortschritt(self) -> Fortschritt:
        blaetter = self.blaetter
        fertig = sum(1 for b in blaetter if b.status == "fertig")
        return Fortschritt(fertig=fertig, gesamt=len(blaetter))


def knoten_wobei(knoten: Knoten, treffer: Callable[[Knoten], bool]) -> Knoten | None:
    """Den ersten Knoten unter *knoten* (ihn selbst eingeschlossen, zuerst
    der Knoten, dann seine Kinder der Reihe nach), fuer den *treffer* gilt."""
    if treffer(knoten):
        return knoten
    for kind in knoten.kinder:
        if (gefunden := knoten_wobei(kind, treffer)) is not None:
            return gefunden
    return None


def baue_baum(kopie: Workflowkopie, zeilen: Sequence[Taskzeile]) -> Baum:
    """Den Baum aus Kopie und Task-Zeilen ableiten."""
    if kopie.topologie is None:
        wurzel = _flach(kopie.workflow_id, zeilen)
    else:
        wurzel = _Bauer(kopie, zeilen).knoten(kopie.topologie, "", None)
    status = _anzeige(kopie, wurzel.blaetter)
    fehlschlaege: tuple[Knoten, ...] = ()
    if status.code == "gescheitert":
        fehlschlaege = tuple(
            sorted(
                (b for b in wurzel.blaetter if b.status in _GESCHEITERT),
                key=_zeitpunkt,
            )
        )
    return Baum(
        wurzel=wurzel,
        status=status,
        fehlertext=kopie.fehler,
        fehlschlaege=fehlschlaege,
    )


def _zeitpunkt(blatt: Knoten) -> float:
    """Wann ein Schritt endete; ohne gemeldetes Ende, wann er zuerst auftauchte."""
    zeile = blatt.zeile
    if zeile is None:
        return 0.0
    return zeile.ended_at if zeile.ended_at is not None else zeile.first_seen_at


def _ergebnisverweis(knoten: Knoten) -> Ergebnisverweis:
    """Die Regel von ``_terminal_result_id`` der Bibliothek (KTD8): ein Blatt
    nutzt seine Kennung, eine Kette ihr letztes Element, ein Chord seinen
    Body -- und eine Gruppe, auch am Ende einer Kette, hat keinen Verweis."""
    if knoten.art == "schritt":
        return Ergebnisverweis(anzeigbar=True, task_id=knoten.task_id)
    if knoten.art == "kette":
        return _ergebnisverweis(knoten.kinder[-1])
    if knoten.art == "chord":
        return _ergebnisverweis(knoten.kinder[1])
    return Ergebnisverweis(anzeigbar=False)


def _flach(workflow_id: str, zeilen: Sequence[Taskzeile]) -> Knoten:
    """Ohne Topologie: die bekannten Schritte nebeneinander, frueheste
    zuerst (R7). Es gibt nichts, woraus ein Grund oder ein Vorgaenger folgte."""
    geordnet = sorted(zeilen, key=lambda z: (z.first_seen_at, z.step_path or ""))
    kinder = tuple(
        Knoten(
            pfad=_pfad_der_zeile(workflow_id, z),
            art="schritt",
            task_name=z.task_name,
            status=z.status,
            task_id=z.task_id,
            zeile=z,
        )
        for z in geordnet
    )
    return Knoten(pfad="", art="unbekannt", kinder=kinder)


def _pfad_der_zeile(workflow_id: str, zeile: Taskzeile) -> str | None:
    """Der Pfad an der Zeile, sonst der aus dem Muster ``<wf>#<pfad>`` (KTD5)."""
    if zeile.step_path is not None:
        return zeile.step_path
    praefix = f"{workflow_id}#"
    if zeile.task_id.startswith(praefix):
        return zeile.task_id[len(praefix) :]
    return None


def _anzeige(kopie: Workflowkopie, blaetter: tuple[Knoten, ...]) -> Statusanzeige:
    """Die Vorrangregel aus KTD6."""
    if kopie.status is not None and kopie.status in ENDSTATUS_ANZEIGE:
        return Statusanzeige(ENDSTATUS_ANZEIGE[kopie.status])
    if kopie.sichtbarkeit == VERFALLEN:
        return Statusanzeige(
            VERFALLEN, zuletzt=kopie.zuletzt_angezeigt, gelesen_at=kopie.gelesen_at
        )
    if kopie.storno_angefordert_at is not None:
        if any(b.status in _AKTIV for b in blaetter):
            return Statusanzeige("angefordert")
        return Statusanzeige("angefordert_nichts_aktiv")
    if kopie.sichtbarkeit == NIE_GESEHEN:
        return Statusanzeige("nie_gesehen")
    return Statusanzeige("laeuft")


class _Bauer:
    """Traegt Kopie und Zeilen-Index durch die Rekursion ueber die Topologie."""

    def __init__(self, kopie: Workflowkopie, zeilen: Sequence[Taskzeile]) -> None:
        self._kopie = kopie
        self._nach_pfad = {
            pfad: z
            for z in zeilen
            if (pfad := _pfad_der_zeile(kopie.workflow_id, z)) is not None
        }
        self._nach_kennung = {z.task_id: z for z in zeilen}

    def knoten(
        self,
        element: Mapping[str, Any],
        pfad: str,
        rolle: str | None,
        tor: tuple[Knoten, ...] = (),
    ) -> Knoten:
        """Den Knoten fuer *element* bauen.

        *tor* sind die Blaetter, die vor diesem Element fertig sein muessen:
        das vorige Kettenglied, bei einem Chord-Body der ganze Kopf. Sie sind
        schon gebaut, weil die Rekursion in Ausfuehrungsreihenfolge laeuft.
        """
        art = element.get("kind")
        if art == "chain":
            schritte: list[Knoten] = []
            for i, schritt in enumerate(element["steps"]):
                vor = schritte[-1].blaetter if schritte else tor
                schritte.append(self.knoten(schritt, f"{pfad}/{i}", None, vor))
            return Knoten(pfad=pfad, art="kette", kinder=tuple(schritte), rolle=rolle)
        if art == "group":
            kinder = tuple(
                self.knoten(glied, f"{pfad}/g{i}", None, tor)
                for i, glied in enumerate(element["members"])
            )
            return Knoten(pfad=pfad, art="gruppe", kinder=kinder, rolle=rolle)
        if art == "chord":
            kopf = self.knoten(element["header"], f"{pfad}/h", "kopf", tor)
            kopf = replace(
                kopf,
                kinder=tuple(replace(g, ergebnis=_ergebnisverweis(g)) for g in kopf.kinder),
            )
            body = self.knoten(element["body"], f"{pfad}/b", "body", kopf.blaetter)
            if body.blaetter[0].grund == GRUND_WARTET:
                offen = sum(1 for glied in kopf.kinder if not glied.ist_fertig)
                body = replace(body, wartet_auf=offen)
            return Knoten(pfad=pfad, art="chord", kinder=(kopf, body), rolle=rolle)
        return self._blatt(element, pfad, rolle, tor)

    def _blatt(
        self,
        element: Mapping[str, Any],
        pfad: str,
        rolle: str | None,
        tor: tuple[Knoten, ...],
    ) -> Knoten:
        vermerkt = self._kopie.step_tasks.get(pfad)
        zeile = self._nach_pfad.get(pfad)
        if zeile is None and vermerkt is not None:
            zeile = self._nach_kennung.get(vermerkt)
        grund: str | None = None
        if zeile is not None:
            status = zeile.status
        elif vermerkt is not None:
            status = EINGEREIHT
        else:
            status = NICHT_EINGEREIHT
            grund = self._grund(tor)
        return Knoten(
            pfad=pfad,
            art="schritt",
            rolle=rolle,
            task_name=element.get("task"),
            status=status,
            task_id=zeile.task_id if zeile is not None else vermerkt,
            zeile=zeile,
            grund=grund,
        )

    def _grund(self, tor: tuple[Knoten, ...]) -> str:
        """Warum ein Blatt nicht eingereiht ist, in der Reihenfolge aus U4."""
        if self._kopie.status == STATUS_FAILED:
            return GRUND_GESCHEITERT
        if self._kopie.status == STATUS_CANCELLED:
            return GRUND_STORNIERT
        if any(v.status == "storniert" or v.grund == GRUND_STORNO_GLIED for v in tor):
            return GRUND_STORNO_GLIED
        if tor and self._kopie.status == STATUS_RUNNING and all(v.status == "fertig" for v in tor):
            return GRUND_VORGAENGER_FERTIG
        return GRUND_WARTET
