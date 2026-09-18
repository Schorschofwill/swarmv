"""Worker-Uebersicht -- die Startseite (U14, F5).

Zwei Dinge machen diese Seite heikel:

* **Sie ist die Startseite.** Wer das Dashboard oeffnet, sieht sie zuerst --
  auch und gerade im Vorfall. Eine leere Tabelle waere dann die schlechteste
  aller Antworten.
* **Der Kontrollkanal fehlt.** Er ist in swarmq gemergt, aber unveroeffentlicht.
  Spielraum, Slot-Kapazitaet und Sollwert lassen sich also nicht fuellen. An
  ihrer Stelle steht **eine** benannte Ursache, kein leerer Bereich (R33), und
  die Zahl wartender Nachrichten bleibt -- sie kommt aus der passiven
  Broker-Beobachtung und ist in der ersten Auslieferung die einzige Antwort
  auf "staut es sich?".

Der dritte Punkt ist R18: ein Ueberschuss an Konsumenten heisst, dass jemand
arbeitet, dessen Ereignisse nie im Verzeichnis landen -- und dessen Tasks sich
darum als "nicht gelaufen" lesen. Der Fall "der Strom kennt gar keinen Worker"
ist ausdruecklich eingeschlossen; dann sind **alle** Konsumenten ohne Hook.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from litestar import Request, get, post
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.response import Redirect, Template

from swarmq_dashboard.broker import QueueStats
from swarmq_dashboard.directory.workers import Quelle, Worker, WorkerRegistry
from swarmq_dashboard.web.auth import SESSION_ACCOUNT_KEY

#: Der eine Satz, der sagt, was ohne Kontrollkanal fehlt (R33, AE15). Er steht
#: genau einmal auf der Seite -- an jeder Zelle wiederholt waere er Laerm und
#: verdeckte, was **da** ist.
OHNE_KONTROLLKANAL = (
    "Steuerung nicht verfuegbar: die installierte swarmq-Fassung bringt den "
    "Kontrollkanal noch nicht mit. Spielraum, gemeldete Slot-Kapazitaet und "
    "Sollwerte fehlen darum. Alles andere auf dieser Seite gilt."
)
#: Wie die Quellen heissen, wenn die Seite sie nennt (KTD10, R14a).
QUELLENNAME = {
    Quelle.SUPERVISOR: "Supervisor-Rueckmeldung",
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


@dataclass(frozen=True, slots=True)
class Gruppe:
    """Eine Worker-Gruppe mit ihren Queues."""

    name: str
    worker: list[Worker]
    now: float

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
        raise NotFoundException(detail="das Verzeichnis ist nicht geoeffnet")
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

    gruppen = [
        Gruppe(name=name, worker=worker, now=jetzt)
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
            "ohne_kontrollkanal": OHNE_KONTROLLKANAL,
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
                "unvollstaendig.")
    if not status["stream"]["ok"]:
        return f"Der Ereignisstrom ist nicht erreichbar: {status['stream']['reason']}"
    return ("Es sind bisher keine Worker-Ereignisse eingetroffen. Laeuft in den "
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
