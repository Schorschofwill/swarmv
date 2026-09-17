"""Aus Ereignissen ein Verzeichnis bauen.

Der Kern ist die **Aufwaerts-Ergaenzung**: eine Task-Zeile entsteht nicht durch
Einfuegen und spaeteres Aendern, sondern indem jedes Ereignis die Zeile nur um
das ergaenzt, was es sicher weiss. Das ist noetig, weil die Ereignisse in
beliebiger Reihenfolge und unvollstaendig eintreffen:

* Scheduler-Ausloesungen und verzoegerte Einreihungen passieren den
  Anwendungs-Hook nie -- von ihnen erfaehrt das Verzeichnis erst beim
  Aufgreifen, und ohne Einreihungszeitpunkt (R31).
* Die Puffer der Hooks leeren unterschiedlich schnell, also ist die
  Stromreihenfolge die Reihenfolge des Leerens, nicht die der Ereignisse.

Zwei Regeln folgen daraus, und beide haben einen Test:

1. Die Herkunft wird nur **gehoben** (aufgreifen -> einreihung), nie gesenkt.
   Der Einreihungszeitpunkt bleibt leer, solange er unbekannt ist -- ein
   geratener Zeitpunkt waere eine Falschaussage.
2. Der Ausgang mit dem **spaeteren Zeitstempel** gewinnt, nicht der zuletzt
   gelesene.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from swarmq_dashboard.events import Event, Origin

#: Ereignisart -> Status, den sie setzt. Nur Ausgaenge stehen hier; alles
#: andere ergaenzt Felder, ohne den Status zu beruehren.
_OUTCOMES = {
    "task_completed": "fertig",
    "task_failed": "gescheitert",
    "task_cancelled": "storniert",
    "task_dlq": "dead_letter",
    "task_expired": "abgelaufen",
    "task_rejected": "abgewiesen",
}
#: Ereignisart des Verlust-Ereignisses aus den Hooks (R4a).
LOST_EVENT = "sqd_events_lost"


class DirectoryWriter:
    """Nimmt Ereignisse entgegen und fuehrt das Verzeichnis nach."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con
        self._con.row_factory = sqlite3.Row

    def close(self) -> None:
        """Verbindung schliessen und das Journal zusammenfuehren."""
        self._con.execute("pragma wal_checkpoint(truncate)")
        self._con.close()

    # -- Lesen -----------------------------------------------------------

    def row(self, task_id: str) -> sqlite3.Row | None:
        """Eine Task-Zeile, oder ``None``."""
        cur = self._con.execute("select * from task where task_id=?", (task_id,))
        zeile: sqlite3.Row | None = cur.fetchone()
        return zeile

    def oldest_entry(self) -> float | None:
        """Zeitpunkt des aeltesten Eintrags -- die Reichweite aus R8.

        Gemessen wird das Verzeichnis, nicht der Strom: das Verzeichnis haelt
        seine eigenen sieben Tage, unabhaengig vom Budget des Stroms.
        """
        wert = self._con.execute("select min(first_seen_at) from task").fetchone()[0]
        return float(wert) if wert is not None else None

    # -- Schreiben -------------------------------------------------------

    def apply(self, events: list[Event]) -> int:
        """Einen Stapel Ereignisse einarbeiten. Gibt die Zahl der Zeilen zurueck."""
        beruehrt = 0
        for ev in events:
            if ev.type == LOST_EVENT:
                self._mark_gap(ev)
                continue
            task_id = ev.payload.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue  # Worker-Stammdaten, Scheduler-Signale: kein Task
            self._upsert(task_id, ev)
            beruehrt += 1
        self._con.commit()
        return beruehrt

    def _upsert(self, task_id: str, ev: Event) -> None:
        """Eine Zeile um das ergaenzen, was dieses Ereignis sicher weiss."""
        vorhanden = self.row(task_id)
        if vorhanden is None:
            self._con.execute(
                "insert into task(task_id, origin, first_seen_at, last_seen_at) values (?,?,?,?)",
                (task_id, ev.origin.value, ev.ts, ev.ts),
            )
            vorhanden = self.row(task_id)
            assert vorhanden is not None

        felder: dict[str, Any] = {"last_seen_at": max(float(vorhanden["last_seen_at"]), ev.ts)}
        felder.update(_stammdaten(vorhanden, ev))
        felder.update(_herkunft(vorhanden, ev))
        felder.update(_HANDLERS.get(ev.type, _outcome)(vorhanden, ev))

        satz = ", ".join(f"{k}=?" for k in felder)
        self._con.execute(f"update task set {satz} where task_id=?", (*felder.values(), task_id))
        self._con.execute(
            "insert or ignore into signal(task_id, type, ts, stream_id, payload) values (?,?,?,?,?)",
            (task_id, ev.type, ev.ts, f"{ev.ts}:{ev.type}",
             json.dumps(ev.payload, ensure_ascii=False, default=repr)),
        )

    def mark_incomplete(self, von: float, bis: float) -> int:
        """Zeilen in einer Zeitspanne als unvollstaendig markieren (R4a).

        Zwei Quellen benutzen das: das Verlust-Ereignis eines Hooks, und der
        Rueckstand des Strom-Lesers hinter dem Laengendeckel. Beide bedeuten
        dasselbe fuer das Teammitglied -- in dieser Zeitspanne fehlt
        moeglicherweise etwas -- und sollen deshalb gleich aussehen.
        """
        cur = self._con.execute(
            "update task set incomplete=1 where last_seen_at >= ? and first_seen_at <= ?",
            (von, bis),
        )
        self._con.commit()
        return int(cur.rowcount)

    def _mark_gap(self, ev: Event) -> None:
        """Zeitspanne eines Verlust-Ereignisses uebernehmen."""
        von, bis = ev.payload.get("from_ts"), ev.payload.get("to_ts")
        if von is None or bis is None:
            return
        self.mark_incomplete(float(von), float(bis))

    # -- Aufbewahrung ----------------------------------------------------

    def expire(self, *, older_than: float) -> int:
        """Zeilen loeschen, deren letztes Lebenszeichen vor der Frist liegt.

        Danach wird der Volltextindex aufgeraeumt und das Journal
        zusammengefuehrt -- ohne beides blieben die Klartext-Argumente im Index
        und im Journal stehen, und die Zusage "der Ablauf loescht dort
        wirklich" waere unwahr (R7a).
        """
        cur = self._con.execute("delete from task where last_seen_at < ?", (older_than,))
        entfernt = cur.rowcount
        self._con.execute("delete from signal where ts < ?", (older_than,))
        self._con.commit()
        self._con.execute("insert into task_fts(task_fts) values('optimize')")
        self._con.commit()
        self._con.execute("pragma wal_checkpoint(truncate)")
        self._con.execute("vacuum")
        return int(entfernt)


# --- Ergaenzungsregeln je Ereignisart -----------------------------------
#
# Je eine kleine Funktion statt eines Zweigbaums: jede Regel ist fuer sich
# lesbar, und eine neue Ereignisart kommt als Eintrag dazu, nicht als Zweig.


def _stammdaten(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    """Felder setzen, die noch leer sind. Ein bekannter Wert wird nie geleert."""
    felder: dict[str, Any] = {}
    for spalte in ("task_name", "queue", "workflow_id", "step_path"):
        wert = ev.payload.get(spalte)
        if wert is not None and zeile[spalte] is None:
            felder[spalte] = wert
    if ev.type in ("task_pickup", "task_enqueue") and zeile["args_text"] is None:
        felder["args_text"] = json.dumps(
            {"args": ev.payload.get("args", []), "kwargs": ev.payload.get("kwargs", {})},
            ensure_ascii=False, sort_keys=True,
        )
    return felder


def _herkunft(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    """Die Herkunft nur heben, nie senken (R31)."""
    if ev.origin is Origin.ENQUEUE and zeile["origin"] != Origin.ENQUEUE.value:
        return {"origin": Origin.ENQUEUE.value}
    return {}


def _enqueued(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    felder: dict[str, Any] = {"enqueued_at": ev.ts}
    if zeile["status"] is None:
        felder["status"] = "wartend"
    return felder


def _started(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    felder: dict[str, Any] = {"started_at": ev.ts, "worker_id": ev.source}
    if zeile["outcome_ts"] is None:
        felder["status"] = "laufend"
    return felder


def _pickup(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    felder: dict[str, Any] = {"worker_id": ev.source}
    if zeile["status"] is None:
        felder["status"] = "laufend"
    return felder


def _retrying(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    zaehler = ev.payload.get("retry_count")
    if isinstance(zaehler, int):
        return {"retry_count": max(int(zeile["retry_count"]), zaehler)}
    return {}


def _outcome(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    """Ausgang setzen -- aber nur, wenn er der zeitlich spaetere ist.

    Die Stromreihenfolge taugt dafuer nicht: sie ist die Reihenfolge des
    Pufferleerens, nicht die der Ereignisse. Zwei ungleich verzoegerte Puffer
    liessen sonst einen aelteren Fehlschlag einen juengeren Erfolg verdraengen.
    """
    status = _OUTCOMES.get(ev.type)
    if status is None:
        return {}
    bisher = zeile["outcome_ts"]
    if bisher is not None and ev.ts < float(bisher):
        return {}
    felder: dict[str, Any] = {"status": status, "outcome_ts": ev.ts, "ended_at": ev.ts}
    if (dauer := ev.payload.get("duration_ms")) is not None:
        felder["duration_ms"] = dauer
    for schluessel in ("exception", "error"):
        if (fehler := ev.payload.get(schluessel)) is not None:
            felder["error"] = str(fehler)
    return felder


_HANDLERS: dict[str, Any] = {
    "task_enqueue": _enqueued,
    "task_started": _started,
    "task_pickup": _pickup,
    "task_retrying": _retrying,
}
