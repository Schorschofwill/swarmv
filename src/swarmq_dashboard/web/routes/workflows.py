"""Workflow-Liste, Baum und fertige Glieder (Workflow-Ansicht, U6).

Der Hauptfluss (F1, F2): ein Teammitglied oeffnet Workflows, sieht Status
und Fortschritt, oeffnet den Workflow und findet im Baum das schuldige
Glied -- ohne Logs oder Valkey-Schluessel zu lesen.

Die Seiten rechnen nichts selbst. Status, Zaehler und Gruende kommen aus dem
Baum (``workflow_baum``, U4), Zeilen und Seiten aus den Abfragen (U5). Eine
eigene Rechnung hier liefe irgendwann neben der Vorrangregel (KTD6) her --
und die Ansicht behauptete dann einen Endstatus, den keine Lesung geliefert
hat (R6).

Die Live-Aktualisierung (R14) braucht nichts Eigenes: die Seiten rendern
ihren Stand vollstaendig vom Server, und ``live.js`` holt ihn bei jeder
Meldung neu (KTD10).
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import urlencode

from litestar import Request, get
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.response import Template

from swarmq_dashboard.directory.queries import Workflowfilter
from swarmq_dashboard.web.auth import SESSION_ACCOUNT_KEY
from swarmq_dashboard.web.ergebnis import Ergebnisbild
from swarmq_dashboard.web.routes.tasks import BEENDET, ZEITRAEUME, _abfragen, _zahl
from swarmq_dashboard.workflow_baum import Knoten

#: Waehlbare Status der Leiste: die Anzeige-Codes aus U4 (``Statusanzeige``)
#: mit ihrem Wort. "alle" zuerst -- wer nichts waehlt, soll nicht heimlich
#: eingeschraenkt werden.
STATUSWAHL = (
    ("", "alle"),
    ("laeuft", "läuft"),
    ("fertig", "fertig"),
    ("gescheitert", "gescheitert"),
    ("storniert", "storniert"),
    ("angefordert", "Stornierung angefordert"),
    ("angefordert_nichts_aktiv", "Stornierung angefordert, nichts mehr aktiv"),
    ("verfallen", "Zustand verfallen"),
    ("nie_gesehen", "Zustand nie gesehen"),
)


def leerzustand(f: Workflowfilter, status: dict[str, Any],
                aufbewahrung_tage: float) -> tuple[str, str]:
    """Titel und **wahrscheinliche Ursache** fuer eine leere Liste (R33).

    Wie bei der Task-Liste: "keine Treffer" allein liest sich wie "es ist
    nichts passiert". Die Reihenfolge ist die der Wahrscheinlichkeit.
    """
    if status["directory"].get("rebuilding"):
        return ("Das Verzeichnis wird gerade neu aufgebaut",
                "Die Liste ist noch unvollständig. Sobald der Strom eingeholt ist, "
                "steht hier mehr.")
    reichweite = (f"Das Verzeichnis hält Workflows {aufbewahrung_tage:g} Tage "
                  "nach ihrer letzten Aktivität -- älteres steht nicht mehr darin.")
    if f.suche or f.status:
        return ("Kein Workflow passt zu dieser Suche", reichweite)
    if f.since is not None or f.until is not None:
        return ("Kein Workflow im gewählten Zeitraum", reichweite)
    if not status["stream"]["ok"]:
        return ("Noch kein Workflow im Verzeichnis",
                f"Der Ereignisstrom ist nicht erreichbar: {status['stream']['reason']}")
    return ("Noch kein Workflow im Verzeichnis",
            "Ein Workflow erscheint, sobald ein Worker einen seiner Schritte "
            "aufgreift und eine Lesung seines Zustands ihn bestätigt.")


def _filter_aus(request: Request[Any, Any, Any]) -> Workflowfilter:
    """Die Filter aus der Adresszeile, wie bei der Task-Liste (R3).

    ``seit`` ist die Fassung der Leiste (Tage); ``since`` und ``until``
    bleiben als Zeitstempel erhalten, damit ein Verweis aus einem Ticket
    weiter gilt.
    """
    p = request.query_params
    seit = _zahl(p.get("seit"))
    since = _zahl(p.get("since"))
    if since is None and seit:
        since = time.time() - seit * 86400.0
    return Workflowfilter(
        status=p.get("status", ""),
        suche=p.get("q", ""),
        since=since,
        until=_zahl(p.get("until")),
    )


def _weiterlink(request: Request[Any, Any, Any], cursor: str | None) -> str:
    """Die Adresszeile der naechsten Seite: alle Filter und die Marke.

    Ohne die Filter zeigte Seite zwei eines anderen Status oder Zeitraums
    Zeilen, und wer blaettert, merkte es nicht.
    """
    if cursor is None:
        return ""
    p = request.query_params
    teile = {k: p.get(k, "") for k in ("q", "status", "seit", "since", "until")}
    teile["cursor"] = cursor
    return urlencode({k: v for k, v in teile.items() if v})


def _kontext(request: Request[Any, Any, Any], **kontext: Any) -> dict[str, Any]:
    """Was jede Seite des Bereichs braucht, dazu *kontext*."""
    return {
        **kontext,
        "status": request.app.state.services.status(),
        "account": request.session.get(SESSION_ACCOUNT_KEY, ""),
        "aktiv": "workflows",
        "csrf_token": request.session.get("csrf", ""),
    }


@get("/workflows")
async def workflow_liste(request: Request[Any, Any, Any]) -> Template:
    """Die Workflow-Liste (R1, R2)."""
    q = _abfragen(request)
    f = _filter_aus(request)
    seite = q.workflows(f, cursor=request.query_params.get("cursor"))
    status = request.app.state.services.status()
    leer = leerzustand(f, status, float(request.app.state.services._config.retention_days))
    return Template(
        "workflows/liste.html",
        context=_kontext(
            request,
            zeilen=seite.zeilen,
            hinweis=seite.note,
            weiter=_weiterlink(request, seite.next_cursor),
            filter=f,
            statuswahl=STATUSWAHL,
            zeitraeume=ZEITRAEUME,
            seit=request.query_params.get("seit", ""),
            leer_titel=leer[0],
            leer_ursache=leer[1],
        ),
    )


def _sichtbare_kopfglieder(knoten: Knoten) -> Iterator[Knoten]:
    """Die Kopf-Glieder, die im Baum als eigene Zeile stehen. Gefaltete
    liest erst die Seite der fertigen Glieder -- sonst kostete jede
    Live-Meldung einen Backend-Zugriff je fertigem Glied."""
    if knoten.ergebnis is not None:
        yield knoten
    for kind in knoten.offene_kinder:
        yield from _sichtbare_kopfglieder(kind)


def _blatt(knoten: Knoten, task_id: str) -> Knoten | None:
    """Das Blatt mit *task_id* unter *knoten*."""
    if knoten.art == "schritt":
        return knoten if knoten.task_id == task_id else None
    for kind in knoten.kinder:
        if (treffer := _blatt(kind, task_id)) is not None:
            return treffer
    return None


async def _ergebnisse(request: Request[Any, Any, Any],
                      glieder: Iterable[Knoten]) -> dict[str | None, Ergebnisbild]:
    """Die Ergebnisse der Kopf-Glieder als Eingabe des Body (R13, KTD8).

    Gelesen wird nur, was die Regel der Bibliothek zulaesst (der Verweis aus
    U4) und was beendet ist: ein Glied, das auf einer Gruppe endet, hat kein
    einzeln lesbares Ergebnis (R18), ein laufendes noch keines -- in beiden
    Faellen waere der Aufruf ein Backend-Zugriff fuer nichts.
    """
    leser = request.app.state.services.ergebnisse
    bilder: dict[str | None, Ergebnisbild] = {}
    for glied in glieder:
        verweis = glied.ergebnis
        if verweis is None or not verweis.anzeigbar or verweis.task_id is None:
            continue
        blatt = _blatt(glied, verweis.task_id)
        if blatt is None or blatt.status not in BEENDET:
            continue
        bilder[glied.pfad] = await leser.lesen(verweis.task_id)
    return bilder


@get("/workflows/{workflow_id:str}")
async def workflow_detail(request: Request[Any, Any, Any],
                          workflow_id: FromPath[str]) -> Template:
    """Der Baum eines Workflows (R8-R13)."""
    detail = _abfragen(request).workflow_detail(workflow_id)
    if detail is None:
        # Benannt, nicht als Absturz -- und ohne zu behaupten, es habe ihn nie
        # gegeben: er kann aelter sein als die Aufbewahrung, oder erst ein
        # Kandidat aus dem Muster, den noch keine Lesung bestaetigt hat (KTD5).
        raise NotFoundException(
            detail=f"Kein Workflow mit der Kennung {workflow_id!r} im Verzeichnis. "
            "Er kann älter sein als die Aufbewahrung, oder noch keine Lesung "
            "hat ihn bestätigt."
        )
    baum = detail.baum
    return Template(
        "workflows/detail.html",
        context=_kontext(
            request,
            workflow_id=workflow_id,
            baum=baum,
            first_seen_at=detail.first_seen_at,
            last_seen_at=detail.last_seen_at,
            gelesen_at=detail.kopie.gelesen_at,
            ergebnisse=await _ergebnisse(request, _sichtbare_kopfglieder(baum.wurzel)),
            jetzt=time.time(),
        ),
    )


@get("/workflows/{workflow_id:str}/glieder")
async def workflow_glieder(request: Request[Any, Any, Any],
                           workflow_id: FromPath[str]) -> Template:
    """Die fertigen Glieder einer Gruppe, seitenweise (R9, KTD13).

    ``gruppe`` ist der Topologie-Pfad; der Pfad ``""`` der Wurzelgruppe ist
    gueltig und nicht leer (KTD7).
    """
    gruppe = request.query_params.get("gruppe", "")
    seite = _abfragen(request).fertige_glieder(
        workflow_id, gruppe, cursor=request.query_params.get("cursor"))
    weiter = ("" if seite.next_cursor is None
              else urlencode({"gruppe": gruppe, "cursor": seite.next_cursor}))
    return Template(
        "workflows/glieder.html",
        context=_kontext(
            request,
            workflow_id=workflow_id,
            gruppe=gruppe,
            glieder=seite.glieder,
            weiter=weiter,
            leer_ursache=seite.note or (
                "Diese Gruppe hat noch kein fertiges Glied -- was läuft, wartet "
                "oder scheiterte, steht im Baum."),
            ergebnisse=await _ergebnisse(request, seite.glieder),
            jetzt=time.time(),
        ),
    )
