"""Worker-Uebersicht -- die Startseite (U14, F5).

Zwei Dinge machen diese Seite heikel:

* **Sie ist die Startseite.** Wer das Dashboard oeffnet, sieht sie zuerst --
  auch und gerade im Vorfall. Eine leere Tabelle waere dann die schlechteste
  aller Antworten.
* **Der Kontrollkanal kann fehlen.** Seit U16 fuellt er Spielraum,
  Slot-Kapazitaet und Sollwert -- aber nur, wenn die installierte
  Bibliotheksfassung ihn mitbringt und das Backend antwortet. Fehlt eines von
  beidem, steht **eine** benannte Ursache da, kein leerer Bereich (R33), und
  die Zahl wartender Nachrichten bleibt: sie kommt aus der passiven
  Broker-Beobachtung und haengt an keinem der beiden.

Der dritte Punkt ist R18: ein Ueberschuss an Konsumenten heisst, dass jemand
arbeitet, dessen Ereignisse nie im Verzeichnis landen -- und dessen Tasks sich
darum als "nicht gelaufen" lesen. Der Fall "der Strom kennt gar keinen Worker"
ist ausdruecklich eingeschlossen; dann sind **alle** Konsumenten ohne Hook.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import quote

from litestar import Request, get, post
from litestar.enums import RequestEncodingType
from litestar.exceptions import NotFoundException
from litestar.params import Body, FromPath
from litestar.response import Redirect, Template

from swarmq_dashboard.broker import QueueStats
from swarmq_dashboard.control import Sollwert, Steuerbild, Zustand
from swarmq_dashboard.directory.workers import (
    KEINE_GRUPPE,
    Quelle,
    Worker,
    WorkerRegistry,
)
from swarmq_dashboard.web.auth import SESSION_ACCOUNT_KEY

#: Wie die Quellen heissen, wenn die Seite sie nennt (KTD10, R14a).
QUELLENNAME = {
    Quelle.SUPERVISOR: "Supervisor-Rückmeldung",
    Quelle.HEARTBEAT: "Lebenszeichen",
    Quelle.SIGNAL: "letzten Signal",
}


@dataclass(frozen=True, slots=True)
class QueueZeile:
    """Eine Queue mit Rueckstau und dem Ueberschuss an Konsumenten (R18)."""

    name: str
    stats: QueueStats
    #: Zahl der **verbundenen** Worker, die diese Queue bedienen. Ein
    #: getrennter Worker konsumiert nicht mehr; ihn mitzuzaehlen verdeckte
    #: genau den Ueberschuss, um den es geht.
    bekannt: int

    @property
    def ohne_hook(self) -> int:
        """Konsumenten, deren Ereignisse nie im Verzeichnis landen.

        Ihre Tasks lesen sich darum als "nicht gelaufen" -- das ist der Grund,
        warum R18 diesen Vergleich ueberhaupt verlangt.
        """
        if self.stats.error or not self.stats.exists:
            return 0
        return max(0, self.stats.consumers - self.bekannt)


def laufzeit(started_at: float | None, *, now: float) -> str:
    """Wie lange der Worker laeuft (R14).

    ``None`` heisst "unbekannt", nicht "gerade erst": ein Worker, den nur ein
    gewoehnliches Signal bekannt gemacht hat, hat keine Stammdaten und damit
    keinen Startzeitpunkt. Eine gerechnete Laufzeit waere dann erfunden.
    """
    if started_at is None or started_at <= 0 or now < started_at:
        return "--"
    sekunden = int(now - started_at)
    tage, rest = divmod(sekunden, 86400)
    stunden, rest = divmod(rest, 3600)
    minuten = rest // 60
    if tage:
        return f"{tage} d {stunden} h"
    if stunden:
        return f"{stunden} h {minuten} min"
    return f"{minuten} min"


@dataclass(frozen=True, slots=True)
class Gruppe:
    """Eine Worker-Gruppe mit ihren Queues."""

    name: str
    worker: list[Worker]
    now: float
    #: Je Worker: laufende und erledigte Tasks aus dem Verzeichnis.
    last: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: Wartende Nachrichten der Queues dieser Gruppe, je Queue.
    stats: dict[str, QueueStats] = field(default_factory=dict)

    def belegt(self, w: Worker) -> str:
        """"2 von 4" -- Belegung **und** Kapazitaet.

        Die Kapazitaet allein ("nebenlaeufig 4") beantwortet die Frage aus
        Fluss F2 nicht: sie lautet "sind alle Slots belegt?", nicht "wie
        viele gaebe es".

        Fuer einen **getrennten** Worker steht hier "--". Die Zahl kommt aus
        Zeilen mit Status "laufend", und die bleiben es fuer immer, wenn der
        Abschluss ausfaellt -- Luecke im Strom, verworfener Eintrag,
        abgestuerzter Worker. Die Seite meldete damit "5 von 2" fuer einen
        Worker, den dieselbe Zeile als getrennt auswies: arithmetisch
        unmoeglich, und im Vorfall die teuerste Fehldeutung, weil sie in die
        falsche Richtung schickt. Ein getrennter Worker belegt nichts; was
        wir sehen, sind Karteileichen.
        """
        if not w.connected(now=self.now):
            return "--"
        laufend = self.last.get(w.worker_id, (0, 0))[0]
        if w.concurrency is None:
            return f"{laufend} von ?"
        # Mehr laufende Zeilen als Slots heisst nicht Ueberlast, sondern
        # liegengebliebene Zeilen. Die Zahl so stehen zu lassen waere eine
        # Behauptung ueber den Worker; so ist sie eine ueber das Verzeichnis.
        if laufend > w.concurrency:
            return f"{w.concurrency} von {w.concurrency} (+{laufend - w.concurrency} ohne Abschluss)"
        return f"{laufend} von {w.concurrency}"

    def erledigt(self, w: Worker) -> int:
        return self.last.get(w.worker_id, (0, 0))[1]

    @property
    def queues(self) -> list[str]:
        """Die Queues, die diese Gruppe bedient."""
        gesehen: list[str] = []
        for w in self.worker:
            for q in w.queues or []:
                if q not in gesehen:
                    gesehen.append(q)
        return gesehen

    def _gemessen(self, q: str) -> bool:
        """Liegt fuer diese Queue wirklich eine Messung vor?

        ``error`` allein reicht nicht: ``QueueStats(exists=False)`` traegt
        keinen Fehler und ist trotzdem keine Messung -- die Queue gibt es am
        Broker nicht, oder ihr Name wurde oertlich abgelehnt. Als Null in die
        Summe zu nehmen ist genau die Null, wo eine Auskunft fehlt.
        """
        s = self.stats.get(q)
        return s is not None and not s.error and bool(s.exists)

    @property
    def wartend(self) -> int | None:
        """Summe der wartenden Nachrichten ihrer Queues (R14 Satz 2).

        Ohne Kontrollkanal ist das die einzige Antwort auf "staut es sich?"
        -- und sie gehoert an die Gruppe, nicht in eine Tabelle am
        Seitenende, wo die Frage nicht entsteht.

        ``None`` heisst **nicht gemessen** und nicht "null". Eine Gruppe,
        deren Worker keine Stammdaten hat, kennt gar keine Queue; ueber die
        leere Menge zu summieren ergab 0, und "0 wartende Nachrichten" ist
        die beruhigendste Antwort auf "staut es sich?", die es gibt.
        """
        gemessen = [q for q in self.queues if self._gemessen(q)]
        if not gemessen:
            return None
        return sum(self.stats[q].depth for q in gemessen)

    @property
    def rueckstau_unvollstaendig(self) -> list[str]:
        """Queues ohne Messung -- nicht nur die mit Fehlgrund."""
        return [q for q in self.queues if not self._gemessen(q)]

    @property
    def verbunden(self) -> list[Worker]:
        return [w for w in self.worker if w.connected(now=self.now)]

    @property
    def eingeschraenkt(self) -> bool:
        """Beruht die Aussage nur auf der schwaechsten Quelle? (R14a)"""
        return any(w.source is Quelle.SIGNAL for w in self.worker)


def _register(request: Request[Any, Any, Any]) -> WorkerRegistry:
    schreiber = request.app.state.services.services_directory_writer
    if schreiber is None:
        raise NotFoundException(detail="das Verzeichnis ist nicht geöffnet")
    return WorkerRegistry(schreiber._con)


@get("/")
async def uebersicht(request: Request[Any, Any, Any]) -> Template:
    """Die Startseite: Worker nach Gruppe, mit Quelle und Rueckstau."""
    services = request.app.state.services
    register = _register(request)
    # ``jetzt`` aus der Adresszeile ist die Naht fuer die Testreihe: ohne sie
    # muesste sie echte Fristen abwarten, um "getrennt" zu pruefen.
    jetzt = _zahl(request.query_params.get("jetzt")) or time.time()

    gebuendelt = register.by_group()
    alle = register.all()
    gebraucht = register.known_queues()
    stats = await services.queue_stats(gebraucht)

    last = register.load()
    # Je Gruppe ein Steuerbild. Das Lesen ist Pflicht und nicht Kuer: erst
    # dabei faellt eine Ablehnung auf, und erst dann wird der verworfene
    # Wert aus dem Kanal genommen (R16a, KTD13).
    steuerung: dict[str, Steuerbild] = {}
    kanal_fehler: str | None = None
    if services.steuerung.verfuegbar:
        for name in sorted(gebuendelt):
            # **Nicht** fuer die Sammelgruppe: "ohne Gruppe" ist ein
            # Platzhalter fuer Worker ohne Gruppenangabe, kein Gruppenname.
            # Ein Sollwert unter diesem Schluessel erreicht keinen
            # Supervisor und laege fuer immer im Kanal -- die Ansicht
            # fuehrte ihn trotzdem als gesetzt.
            if name == KEINE_GRUPPE:
                continue
            steuerung[name] = await services.steuerung.bild(name, now=jetzt)
            # Einmal genannt, nicht je Gruppe: dieselbe Leitung, derselbe
            # Grund -- an jeder Karte wiederholt verdeckte er den Rest.
            kanal_fehler = kanal_fehler or steuerung[name].fehler
    gruppen = [
        Gruppe(name=name, worker=worker, now=jetzt, last=last, stats=stats)
        for name, worker in sorted(gebuendelt.items())
    ]
    # Die Queue-Uebersicht steht **neben** den Gruppen, nicht in ihnen: ohne
    # bekannten Worker gibt es keine Gruppe, und genau dann sind alle
    # Konsumenten ohne Hook (R18, AE7a). In den Gruppen verschachtelt waere
    # die Auskunft ausgerechnet im wichtigsten Fall unsichtbar.
    verbunden = [w for w in alle if w.connected(now=jetzt)]
    queues = [
        QueueZeile(
            name=q,
            stats=stats.get(q, QueueStats(queue=q)),
            bekannt=len([w for w in verbunden if q in (w.queues or [])]),
        )
        for q in gebraucht
    ]
    return Template(
        "worker/uebersicht.html",
        context={
            "gruppen": gruppen,
            "queues": queues,
            "quellenname": QUELLENNAME,
            "laufzeit": laufzeit,
            # ``None``, wenn nichts fehlt -- sonst stuende der Satz ueber
            # das Fehlende auch da, wo der Kanal antwortet.
            "ohne_kontrollkanal": services.steuerung.grund,
            "steuerung": steuerung,
            "kanal_fehler": kanal_fehler,
            "ZUSTAND": Zustand,
            "jetzt": jetzt,
            "leer_titel": "Noch kein Worker bekannt",
            "leer_ursache": _leergrund(services.status()),
            "status": services.status(),
            "account": request.session.get(SESSION_ACCOUNT_KEY, ""),
            "aktiv": "worker",
            "csrf_token": request.session.get("csrf", ""),
        },
    )


def _leergrund(status: dict[str, Any]) -> str:
    """Die wahrscheinliche Ursache, wenn kein Worker bekannt ist (R33)."""
    if status["directory"].get("rebuilding"):
        return ("Das Verzeichnis wird gerade neu aufgebaut -- die Liste ist noch "
                "unvollständig.")
    if not status["stream"]["ok"]:
        return f"Der Ereignisstrom ist nicht erreichbar: {status['stream']['reason']}"
    return ("Es sind bisher keine Worker-Ereignisse eingetroffen. Läuft in den "
            "Workern der Hook des Dashboards (--configure)?")


def _zahl(wert: str | None) -> float | None:
    try:
        return float(wert) if wert else None
    except ValueError:
        return None


@post("/worker/{worker_id:str}/remove")
async def entfernen(request: Request[Any, Any, Any], worker_id: FromPath[str]) -> Redirect:
    """Einen Worker aus der Uebersicht entfernen (R19, R27).

    Ein Aufraeumen, kein Bann: kommt der Worker zurueck, legt ihn das naechste
    Signal wieder an. Alles andere waere ein Zustand, den niemand mehr sieht
    und den niemand aufhebt.
    """
    register = _register(request)
    konto = str(request.session.get(SESSION_ACCOUNT_KEY, "unbekannt"))
    entfernt = register.remove(worker_id)
    await request.app.state.services.audit.record(
        "worker_remove", account=konto, target=worker_id,
        outcome="ok" if entfernt else "nicht gefunden",
    )
    return Redirect("/", status_code=303)


def _steuerung(request: Request[Any, Any, Any]) -> Any:
    return request.app.state.services.steuerung


@get("/worker/gruppe/{name:str}/sollwert")
async def sollwert_dialog(request: Request[Any, Any, Any], name: FromPath[str]) -> Template:
    """Der Dialog zum Sollwert einer Gruppe (U16 Punkt 5, AE4).

    Bewusst eine eigene Seite und keine editierbare Tabellenzelle: die
    Beschriftung "je Supervisor", die Zahl der Supervisoren, die daraus
    folgende Gesamtzahl und die Pruefung vor dem Schreiben (R15b) brauchen
    mehr Platz, als eine Zelle hergibt -- und sie sind der Unterschied
    zwischen der Haelfte und dem Doppelten dessen, was jemand setzen wollte.
    """
    steuerung = _steuerung(request)
    if not steuerung.verfuegbar:
        raise NotFoundException(detail=steuerung.grund or "die Steuerung ist nicht verfügbar")
    jetzt = _zahl(request.query_params.get("jetzt")) or time.time()
    bild = await steuerung.bild(name, now=jetzt)
    return Template(
        "worker/sollwert.html",
        context={
            "bild": bild,
            "gruppe": bild.gruppe,
            "hinweis": request.query_params.get("hinweis"),
            "jetzt": jetzt,
            "status": request.app.state.services.status(),
            "account": request.session.get(SESSION_ACCOUNT_KEY, ""),
            "aktiv": "worker",
            "csrf_token": request.session.get("csrf", ""),
            "ZUSTAND": Zustand,
        },
    )


def _ganzzahl(wert: Any) -> int | None:
    """Eine ganze Zahl aus dem Formular -- oder ``None``.

    Kein Umweg ueber ``float``: "2.7" ist keine Prozesszahl, und still auf 2
    abzurunden waere eine Zahl, die niemand eingegeben hat.
    """
    try:
        return int(str(wert).strip())
    except (TypeError, ValueError):
        return None


def _zurueck(name: str, hinweis: str) -> Redirect:
    return Redirect(f"/worker/gruppe/{quote(name)}/sollwert?hinweis={quote(hinweis)}",
                    status_code=303)


@post("/worker/gruppe/{name:str}/sollwert")
async def sollwert_setzen(
    request: Request[Any, Any, Any],
    name: FromPath[str],
    data: Annotated[dict[str, Any], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Redirect:
    """Einen Sollwert setzen oder korrigieren (R15, R15b, R27).

    Die Pruefung laeuft **vor** dem Schreiben: was hier schon als unmoeglich
    erkennbar ist, gehoert nicht in den Kanal, wo es auf einen Supervisor
    wartet, der es ablehnt.
    """
    steuerung = _steuerung(request)
    if not steuerung.verfuegbar:
        raise NotFoundException(detail=steuerung.grund or "die Steuerung ist nicht verfügbar")
    # Zwei Formen: Bereich oder feste Zahl (R15). Die feste Zahl ist der
    # Sollwert N..N und schaltet die Autoskalierung ab.
    fest = _ganzzahl(data.get("fest"))
    unten = fest if fest is not None else _ganzzahl(data.get("unten"))
    oben = fest if fest is not None else _ganzzahl(data.get("oben"))
    if unten is None or oben is None:
        return _zurueck(name, "Nicht gesetzt: die Grenzen waren keine ganzen Zahlen.")

    konto = str(request.session.get(SESSION_ACCOUNT_KEY, "unbekannt"))
    wert = Sollwert(unten, oben)
    ergebnis = await steuerung.setzen(name, wert, now=time.time())
    await request.app.state.services.audit.record(
        "setpoint_set", account=konto, target=name,
        outcome="ok" if ergebnis.erlaubt else "abgelehnt",
        detail=f"{unten}..{oben} je Supervisor"
               + ("" if ergebnis.erlaubt else f" -- {ergebnis.grund}"),
    )
    if not ergebnis.erlaubt:
        return _zurueck(name, f"Nicht gesetzt: {ergebnis.grund}")
    if ergebnis.wirkungsunbekannt:
        return _zurueck(name, "Gesetzt -- es meldet sich aber kein Supervisor dieser "
                              "Gruppe; ob und wann der Wert wirkt, ist unbekannt.")
    return _zurueck(name, f"Gesetzt: {unten}..{oben} je Supervisor.")


@post("/worker/gruppe/{name:str}/sollwert/aufheben")
async def sollwert_aufheben(
    request: Request[Any, Any, Any],
    name: FromPath[str],
    data: Annotated[dict[str, Any], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Redirect:
    """Den Sollwert aufheben; danach gelten wieder die Startargumente (R15, R27)."""
    steuerung = _steuerung(request)
    if not steuerung.verfuegbar:
        raise NotFoundException(detail=steuerung.grund or "die Steuerung ist nicht verfügbar")
    konto = str(request.session.get(SESSION_ACCOUNT_KEY, "unbekannt"))
    try:
        await steuerung.aufheben(name)
    except Exception as exc:
        await request.app.state.services.audit.record(
            "setpoint_clear", account=konto, target=name,
            outcome="fehlgeschlagen", detail=str(exc)[:200],
        )
        return _zurueck(name, f"Nicht aufgehoben: {exc}")
    await request.app.state.services.audit.record(
        "setpoint_clear", account=konto, target=name, outcome="ok",
    )
    return _zurueck(name, "Aufgehoben -- es gelten wieder die Startargumente des Deployments.")
