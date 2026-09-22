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
import math
import sqlite3
import time
from typing import Any

from swarmq_dashboard.directory.schema import GuardedConnection
from swarmq_dashboard.directory.workers import quelle_fuer
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
#: Stellen, auf die die beiden Teile einer Stromkennung aufgefuellt werden.
_ID_WIDTH = 20


def _sort_id(stream_id: str | None, ev: Event) -> str:
    """Stromkennung in eine Form bringen, in der Text- und Zahlenreihenfolge
    uebereinstimmen.

    Valkey vergibt ``<Millisekunden>-<Folgenummer>``. Die Millisekunden haben
    feste Stellenzahl, die Folgenummer **nicht**: als Text verglichen sortiert
    ``…-10`` vor ``…-9``. Ein Puffer, der elf Ereignisse einer Task in
    derselben Millisekunde leert -- der Normalfall, nicht die Ausnahme --
    liesse die Task-Ansicht den Abschluss vor dem Start zeigen. Deshalb wird
    beim Schreiben aufgefuellt, nicht beim Lesen gerechnet: so bleibt die
    Spalte ein brauchbarer Sortierschluessel und der Index greift.

    Ohne Kennung -- nur in Tests -- tritt der Zeitpunkt an ihre Stelle.
    """
    if stream_id:
        ms, trenner, folge = stream_id.partition("-")
        if trenner and ms.isdigit() and folge.isdigit():
            return f"{int(ms):0{_ID_WIDTH}d}-{int(folge):0{_ID_WIDTH}d}"
    return f"{int(ev.ts * 1000):0{_ID_WIDTH}d}-{ev.type}"


class DirectoryWriter:
    """Nimmt Ereignisse entgegen und fuehrt das Verzeichnis nach."""

    def __init__(self, con: GuardedConnection) -> None:
        self._con = con
        self._con.row_factory = sqlite3.Row
        #: Ereignisse, die die Datenbank nicht annahm. Kein stiller Verlust:
        #: der Zustandsbericht weist die Zahl aus.
        self.rejected = 0
        #: Steht Klartext aus einer abgelaufenen Zeile noch im Journal, weil
        #: der letzte Zusammenfuehrungsversuch auf einen Leser traf? Die
        #: Loeschzusage aus R7a ist dann noch nicht eingeloest.
        self.erasure_pending = False

    def close(self) -> None:
        """Verbindung schliessen und das Journal zusammenfuehren.

        Auch hier wird das Ergebnis vermerkt: der Dienst darf nicht mit einem
        Journal voller Klartext enden, ohne dass es jemand erfaehrt (R7a).
        """
        self.erasure_pending = self._checkpoint_besetzt()
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

    def newest_entry(self) -> float | None:
        """Zeitpunkt des juengsten Lebenszeichens -- die Aktualitaet aus R26.

        Gemessen am Verzeichnis, nicht am Strom: die Kopfzeile sagt, wie
        aktuell das ist, was das Teammitglied **sieht**, nicht was irgendwo
        ankam.
        """
        wert = self._con.execute("select max(last_seen_at) from task").fetchone()[0]
        return float(wert) if wert is not None else None

    # -- Schreiben -------------------------------------------------------

    def apply(self, events: list[tuple[str | None, Event]]) -> int:
        """Einen Stapel ``(Stromkennung, Ereignis)`` einarbeiten.

        Die Stromkennung wird mitgefuehrt, weil die Signalfolge einer
        Task-Ansicht danach geordnet wird und nicht nach den Zeitstempeln der
        Hosts (R11): Anwendungs- und Worker-Host haben getrennte Uhren, der
        Strom hat nur eine. ``None`` ist erlaubt -- dann ersetzt eine aus
        Zeitpunkt und Art gebildete Kennung sie, was fuer Tests genuegt.
        """
        beruehrt = 0
        # Ein Stapel, eine Transaktion. Die Verbindung laeuft im Autocommit,
        # also waere sonst jede der drei bis vier Anweisungen je Ereignis eine
        # eigene Transaktion: gemessen 197 ms statt 68 ms fuer tausend
        # Ereignisse. Und der Stapel waere nicht atomar, obwohl der Leser
        # seine Fortschrittsmarke erst danach setzt.
        # Die ganze Transaktion unter **einem** Riegel: die Verbindung nimmt
        # ihn zwar je Anweisung, aber ein fremder Schreibvorgang mitten in
        # einem Stapel liefe in dessen Transaktion mit -- und ein Rueckruf
        # riss ihn dann mit zurueck.
        with self._con.exklusiv():
            self._con.execute("begin")
            try:
                for stream_id, ev in events:
                    if ev.type == LOST_EVENT:
                        self._mark_gap(ev, in_transaktion=True)
                        continue
                    # Jedes Ereignis mit einer Worker-Kennung fuehrt auch das
                    # Worker-Register nach -- **vor** der Task-Pruefung, denn
                    # Stammdaten und Lebenszeichen tragen gar keinen Task.
                    self._worker_nachfuehren(ev)
                    task_id = ev.payload.get("task_id")
                    if not isinstance(task_id, str) or not task_id:
                        continue  # Worker-Stammdaten, Scheduler-Signale: kein Task
                    try:
                        self._upsert(task_id, ev, _sort_id(stream_id, ev))
                    except sqlite3.Error:
                        # Ein einzelnes missgestaltetes Ereignis darf die
                        # Einspeisung nicht anhalten. Ohne diesen Schutz blieb die
                        # Fortschrittsmarke stehen, der Leser las ewig denselben
                        # Stapel, und *alle* spaeteren Tasks fehlten im
                        # Verzeichnis -- der "liest sich wie nicht gelaufen"-Irrtum
                        # in seiner schlimmsten Form. Der Zaehler ist die Spur;
                        # die Nutzlast wird nicht mitgefuehrt, sie traegt
                        # Klartext-Argumente (R7a).
                        self.rejected += 1
                        self.bump_lost(rejected=1)
                        continue
                    beruehrt += 1
                self._con.execute("commit")
            except BaseException:
                self._con.execute("rollback")
                raise
        return beruehrt

    def _upsert(self, task_id: str, ev: Event, stream_id: str) -> None:
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
            (task_id, ev.type, ev.ts, stream_id,
             json.dumps(ev.payload, ensure_ascii=False, default=repr)),
        )

    def _worker_nachfuehren(self, ev: Event) -> None:
        """Das Worker-Register aus einem Ereignis ergaenzen (R14, R19).

        Zwei Regeln, und beide haben denselben Grund wie die
        Aufwaerts-Ergaenzung der Task-Zeilen:

        * Ein bekannter Wert wird nie geleert. Ein gewoehnliches Task-Signal
          traegt keine Gruppe -- sie deswegen zu loeschen hiesse, den Worker
          nach dem ersten Task aus seiner Gruppe zu verlieren. Das leistet
          die Auswahl unten: nur was die Nutzlast wirklich mitbringt, wird
          geschrieben. (Ein ``coalesce`` stand hier zusaetzlich und konnte von
          keiner Mutationsprobe gekippt werden -- es schuetzte nichts, was die
          Auswahl nicht schon schuetzt.)
        * Die Quelle wird nur **gehoben** (KTD10). Sonst faellt die Ansicht
          nach jedem gewoehnlichen Signal auf die schwaechste Aussage zurueck
          und meldet sich als eingeschraenkt, obwohl das Lebenszeichen laeuft.
        """
        wid = ev.payload.get("worker_id") or ev.source
        if not isinstance(wid, str) or not wid or wid == "app":
            return
        quelle = int(quelle_fuer(ev.type))
        self._con.execute(
            "insert into worker(worker_id, last_seen_at, source) values (?,?,?) "
            "on conflict(worker_id) do update set "
            "last_seen_at = max(worker.last_seen_at, excluded.last_seen_at), "
            "source = max(worker.source, excluded.source)",
            (wid, ev.ts, quelle),
        )
        felder: dict[str, Any] = {}
        for spalte, schluessel in (("worker_group", "group"), ("slot", "slot"),
                                   ("concurrency", "concurrency"), ("pool", "pool"),
                                   ("started_at", "started_at")):
            wert = ev.payload.get(schluessel)
            if wert is not None and not isinstance(wert, dict | list):
                felder[spalte] = wert
        for spalte in ("queues", "all_queues"):
            wert = ev.payload.get(spalte)
            if isinstance(wert, list):
                felder[spalte] = json.dumps(wert, ensure_ascii=False)
        if felder:
            satz = ", ".join(f"{k}=?" for k in felder)
            self._con.execute(
                f"update worker set {satz} where worker_id=?", (*felder.values(), wid)
            )

    def mark_cancel_requested(self, task_id: str) -> None:
        """Festhalten, dass jemand die Stornierung angefordert hat (R12a).

        Wissen des Dashboards, kein Signal: die Bibliothek setzt nur einen
        Merker in Valkey, und ob ein Worker ihn bemerkt, weiss erst der
        Ausgang. Deshalb steht es in einer eigenen Spalte und nicht im Status
        -- der gehoert den Signalen.
        """
        self._con.execute(
            "update task set cancel_requested_at=? where task_id=? "
            "and cancel_requested_at is null",
            (time.time(), task_id),
        )
        self._con.commit()

    def mark_incomplete(self, von: float, bis: float, *, in_transaktion: bool = False) -> int:
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
        if not in_transaktion:
            self._con.commit()
        return int(cur.rowcount)

    def _mark_gap(self, ev: Event, *, in_transaktion: bool = False) -> None:
        """Zeitspanne eines Verlust-Ereignisses uebernehmen."""
        von, bis = ev.payload.get("from_ts"), ev.payload.get("to_ts")
        if not isinstance(von, int | float) or not isinstance(bis, int | float):
            return
        self.mark_incomplete(float(von), float(bis), in_transaktion=in_transaktion)
        self.record_gap(float(von), float(bis), quelle="hook_lost",
                        in_transaktion=in_transaktion)

    #: Schluessel der beiden Verlustzaehler (R4a).
    DROPPED_KEY = "lost_dropped"
    REJECTED_KEY = "lost_rejected"

    def bump_lost(self, *, dropped: int = 0, rejected: int = 0) -> None:
        """Verlustzaehler fortschreiben -- **in der Datei**, nicht im Speicher.

        Vorher waren es reine Speicherzaehler. Nach dem ersten Neustart
        schwieg die Kopfzeile, obwohl die Eintraege dauerhaft fehlen, und
        Schweigen liest sich als "nichts verloren". Genau mit dieser
        Begruendung wurde ``behind`` fuer die Luecken-Anzeige verworfen --
        und der Fehler eine Zeile spaeter wiederholt.
        """
        for schluessel, zahl in ((self.DROPPED_KEY, dropped),
                                 (self.REJECTED_KEY, rejected)):
            if zahl:
                self._con.execute(
                    "insert into progress(key, value) values (?, ?) "
                    "on conflict(key) do update set "
                    "value = cast(cast(value as integer) + ? as text)",
                    (schluessel, str(zahl), zahl),
                )
        if dropped or rejected:
            self._con.commit()

    def lost_counts(self) -> tuple[int, int]:
        """``(dropped, rejected)`` aus der Datei."""
        werte: list[int] = []
        for schluessel in (self.DROPPED_KEY, self.REJECTED_KEY):
            zeile = self._con.execute(
                "select value from progress where key=?", (schluessel,)
            ).fetchone()
            try:
                werte.append(int(zeile[0]) if zeile is not None else 0)
            except (TypeError, ValueError):
                werte.append(0)
        return (werte[0], werte[1])

    #: Schluessel des Zeitpunkts, ab dem das Verzeichnis ueberhaupt liest.
    COVERAGE_KEY = "coverage_start"

    def set_coverage_start(self, ts: float) -> None:
        """Den Beginn der Abdeckung festhalten -- **einmal**, nie wieder.

        Der dritte Boden der Reichweite (R8). Aufbewahrungsgrenze und
        aeltester Eintrag reichen nicht: laeuft der Dienst erst seit zwei
        Tagen, kann er ueber Tag drei bis sieben nichts wissen, egal wie
        grosszuegig die Frist ist. Ohne diesen Wert meldete die Seite
        trotzdem sieben Tage.

        Nur beim ersten Mal: wanderte er mit jedem Durchlauf nach vorn,
        schrumpfte die gemeldete Reichweite auf den letzten Takt zusammen.
        """
        self._con.execute(
            "insert into progress(key, value) values (?, ?) "
            "on conflict(key) do nothing",
            (self.COVERAGE_KEY, repr(float(ts))),
        )
        self._con.commit()

    def coverage_start(self) -> float | None:
        """Der festgehaltene Beginn, oder ``None`` fuer ein Verzeichnis aus
        der Zeit vor diesem Merker."""
        zeile = self._con.execute(
            "select value from progress where key=?", (self.COVERAGE_KEY,)
        ).fetchone()
        if zeile is None:
            return None
        try:
            return float(zeile[0])
        except (TypeError, ValueError):
            return None

    def gap_count(self) -> int:
        """Wie viele Luecken festgehalten sind (R4a).

        Fuer die Kopfzeile. ``behind`` allein trug das nicht: es sagt, ob der
        Leser **gerade** hinterherhaengt, und ist nach einem Neustart und
        einem eingeholten Strom wieder falsch -- obwohl die Luecke bleibt.
        """
        return int(self._con.execute("select count(*) from gap").fetchone()[0])

    def record_gap(self, von: float, bis: float, *, quelle: str,
                   now: float | None = None, in_transaktion: bool = False) -> bool:
        """Eine Zeitspanne festhalten, aus der Ereignisse fehlen (R4a, U12a).

        Das Gegenstueck zu ``mark_incomplete`` und der wichtigere Teil: jene
        Markierung trifft nur **ueberlebende** Zeilen. Die verlorenen Tasks
        haben gar keine Zeile, und dann meldet sie schlicht 0 -- die Luecke
        waere bekannt und trotzdem unsichtbar. Hier steht sie fuer sich, und
        weil sie in der Datei steht, ueberlebt sie einen Neustart.

        Ueberlappende und angrenzende Spannen **derselben Quelle** werden
        zusammengelegt. Der Leser prueft seinen Rueckstand in jedem Durchlauf;
        ohne das Zusammenlegen waechst die Tabelle bei anhaltendem Rueckstand
        um einen Eintrag je Takt, und die Meldung spraeche von tausend
        Luecken, wo es eine ist.

        ``False`` heisst: die Spanne war unbrauchbar. Eine rueckwaerts
        laufende oder nicht endliche Spanne schnitte **jede** Suche und
        stuende dauerhaft auf der Seite -- und die Nutzlast kommt von aussen.
        """
        if not (math.isfinite(von) and math.isfinite(bis)) or bis < von:
            return False
        vorhanden = self._con.execute(
            "select von, bis from gap where quelle=? and bis >= ? and von <= ?",
            (quelle, von, bis),
        ).fetchall()
        if vorhanden:
            von = min([von, *(float(z["von"]) for z in vorhanden)])
            bis = max([bis, *(float(z["bis"]) for z in vorhanden)])
            self._con.execute(
                "delete from gap where quelle=? and bis >= ? and von <= ?",
                (quelle, von, bis),
            )
        self._con.execute(
            "insert or replace into gap(von, bis, quelle, erkannt_am) values (?,?,?,?)",
            (von, bis, quelle, now if now is not None else time.time()),
        )
        if not in_transaktion:
            self._con.commit()
        return True

    # -- Aufbewahrung ----------------------------------------------------

    def expire(self, *, older_than: float) -> int:
        """Zeilen loeschen, deren letztes Lebenszeichen vor der Frist liegt.

        Die Zusage aus R7a ist, dass danach nichts mehr da steht. Gemessen
        ueber alle sechzehn Kombinationen der vier Schritte traegt sie
        **secure_delete**, und unabhaengig davon auch **vacuum**: mit
        eingeschaltetem sicherem Loeschen bleiben nie Rohbytes zurueck, ohne
        es raeumt das Verdichten sie weg. Zwei unabhaengige Wege zu derselben
        Zusage sind hier Absicht, nicht Zufall -- der Preis dieser Datei sind
        sieben Tage Kundendaten im Klartext (KD11, KD16).

        Die beiden anderen Schritte tun ausdruecklich **nicht**, was hier
        frueher stand: das Aufraeumen des Volltextindex und das
        Zusammenfuehren des Journals haben auf das Verbleiben der Zeichenkette
        keinen messbaren Einfluss. Sie bleiben aus ihrem eigentlichen Grund --
        der Index bleibt sonst zersplittert und das Journal waechst -- und
        nicht als Teil der Loeschzusage.
        """
        self.erasure_pending = False
        cur = self._con.execute("delete from task where last_seen_at < ?", (older_than,))
        entfernt = cur.rowcount
        # Nur die Signale verwaister Tasks. Nach Zeitstempel zu loeschen nahm
        # einer langlaufenden, noch lebenden Task ihre fruehen Signale weg --
        # die Ansicht zeigte dann eine Historie, die mit dem Abschluss
        # beginnt, ohne zu sagen, dass davor etwas stand.
        self._con.execute(
            "delete from signal where task_id not in (select task_id from task)"
        )
        # Luecken mit derselben Frist. Sonst meldete das Dashboard ewig eine
        # Luecke in einem Zeitraum, aus dem es ohnehin nichts mehr zeigt --
        # ein Hinweis, der nie verschwindet, wird ueberlesen (R9).
        self._con.execute("delete from gap where bis < ?", (older_than,))
        self._con.commit()
        if entfernt:
            # Nur wenn wirklich etwas abgelaufen ist. Gemessen kostete ein
            # Lauf ohne eine einzige abgelaufene Zeile auf einer 656-MB-Datei
            # 1,8 s und 1973 MB Schreiblast, weil VACUUM die Datei
            # bedingungslos neu schreibt -- im Minutentakt waeren das 1,4 TB
            # am Tag fuer nichts.
            self._con.execute("insert into task_fts(task_fts) values('optimize')")
            self._con.commit()
            self._con.execute("vacuum")
        # Zusammenfuehren zuletzt, damit auch die Seiten des Verdichtens in
        # die Hauptdatei wandern. Und das Ergebnis **pruefen**: das Pragma
        # wirft nicht, es meldet "besetzt" als Zeile. Traf es auf einen
        # offenen Leser -- schon ein halb ausgelesener Cursor genuegt --,
        # blieben die alten Seitenabbilder mit Klartext stehen, und expire()
        # meldete trotzdem Erfolg. Die Zusage aus R7a waere still gebrochen.
        self.erasure_pending = self._checkpoint_besetzt()
        return int(entfernt)

    def _checkpoint_besetzt(self) -> bool:
        """Journal zusammenfuehren. ``True``, wenn ein Leser es verhindert hat."""
        zeile = self._con.execute("pragma wal_checkpoint(truncate)").fetchone()
        return bool(zeile is not None and zeile[0])


# --- Ergaenzungsregeln je Ereignisart -----------------------------------
#
# Je eine kleine Funktion statt eines Zweigbaums: jede Regel ist fuer sich
# lesbar, und eine neue Ereignisart kommt als Eintrag dazu, nicht als Zweig.


def _stammdaten(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    """Felder setzen, die noch leer sind. Ein bekannter Wert wird nie geleert."""
    felder: dict[str, Any] = {}
    for spalte in ("task_name", "queue", "workflow_id", "step_path"):
        wert = ev.payload.get(spalte)
        # Nur Zeichenketten. Die Nutzlasten der Bibliothek weichen an mehreren
        # Stellen von ihrer Dokumentation ab; ein Objekt oder eine Liste hier
        # liess die Bindung werfen und hielt die ganze Einspeisung an.
        if isinstance(wert, str) and zeile[spalte] is None:
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


def _veraltet(zeile: sqlite3.Row, ev: Event) -> bool:
    """Liegt ein verbuchter Ausgang **vor** diesem Ereignis?

    Dann hat nach jenem Ausgang ein neuer Versuch begonnen, und der Ausgang
    gehoert zum vorigen. Ihn stehen zu lassen ergaebe eine in sich
    widerspruechliche Zeile -- Status "gescheitert" bei einem Endzeitpunkt
    vor dem Startzeitpunkt -- und ein Teammitglied griffe womoeglich in einen
    Versuch ein, der gerade normal laeuft.
    """
    bisher = zeile["outcome_ts"]
    return bisher is not None and ev.ts > float(bisher)


def _verworfener_ausgang() -> dict[str, Any]:
    """Die Felder des ueberholten Ausgangs raeumen."""
    return {"outcome_ts": None, "ended_at": None, "duration_ms": None, "error": None}


def _started(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    """Start verbuchen -- aber nur nach vorn.

    Der Zeitvergleich ist noetig, weil die Stromreihenfolge die Reihenfolge
    des Pufferleerens ist. Nach einem Fehlschlag und einer Wiederholung laeuft
    dieselbe Task auf einem zweiten Worker; leert dessen Puffer zuerst, traefe
    der **aeltere** Start zuletzt ein und die Ansicht naennte den falschen
    Worker und eine zu lange Laufzeit. Dasselbe schuetzt vor einem doppelt
    eingearbeiteten Stapel nach einem Absturz zwischen Einarbeiten und
    Fortschrittsmarke.
    """
    bisher = zeile["started_at"]
    if bisher is not None and ev.ts < float(bisher):
        return {}  # ein aelterer Start sagt nichts Neues
    felder: dict[str, Any] = {"started_at": ev.ts, "worker_id": ev.source}
    if zeile["outcome_ts"] is None:
        felder["status"] = "laufend"
    elif _veraltet(zeile, ev):
        felder["status"] = "laufend"
        felder.update(_verworfener_ausgang())
    return felder


def _pickup(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    felder: dict[str, Any] = {}
    if zeile["started_at"] is None or ev.ts >= float(zeile["started_at"]):
        felder["worker_id"] = ev.source
    if zeile["status"] is None:
        felder["status"] = "laufend"
    return felder


def _retrying(zeile: sqlite3.Row, ev: Event) -> dict[str, Any]:
    """Wiederholung verbuchen. Der Zaehler wird nur gehoben.

    Eine Wiederholung nach einem verbuchten Fehlschlag holt den Status
    zurueck: der Task ist wieder unterwegs, nicht gescheitert (R6 kennt
    "Wiederholung" als eigenen Zustand).
    """
    felder: dict[str, Any] = {}
    zaehler = ev.payload.get("retry_count")
    if isinstance(zaehler, int):
        felder["retry_count"] = max(int(zeile["retry_count"]), zaehler)
    if _veraltet(zeile, ev):
        felder["status"] = "wiederholung"
        felder.update(_verworfener_ausgang())
    return felder


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
    dauer = ev.payload.get("duration_ms")
    if isinstance(dauer, int | float) and not isinstance(dauer, bool):
        felder["duration_ms"] = float(dauer)
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
