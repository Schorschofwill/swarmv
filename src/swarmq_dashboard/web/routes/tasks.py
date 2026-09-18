"""Tasks-Liste und Detail (U13, F1).

Der Hauptfluss: ein Teammitglied sucht nach einem Fragment aus den Argumenten,
sieht Status und Argumente, oeffnet den Task und liest die Signalfolge.

Drei stehende Hinweise gehoeren zwingend dazu, und alle drei verhindern
dieselbe Sorte Irrtum -- eine Seite, die vollstaendig **aussieht**, es aber
nicht ist:

* **Reichweite** (R8). Ohne sie liest sich "keine Treffer" wie "hat es nie
  gegeben", auch wenn der Task schlicht aelter ist als die Aufbewahrung.
* **Herkunft** (R31). Scheduler- und verzoegerte Einreihungen kennt das
  Verzeichnis erst ab dem Aufgreifen; ein geratener Einreihungszeitpunkt waere
  eine Falschaussage, ein leeres Feld ohne Erklaerung ein Raetsel.
* **Luecken** (R4a). Eine Zeitspanne, aus der Ereignisse fehlen, muss
  erkennbar sein -- sonst liest sich das Fehlende als "nicht gelaufen".
"""

from __future__ import annotations

import time
from typing import Any

from litestar import Request, get, post
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.response import Redirect, Template

from swarmq_dashboard.directory.queries import DirectoryQueries, Filter
from swarmq_dashboard.web.auth import SESSION_ACCOUNT_KEY

#: Der stehende Hinweis aus R31. Er steht auf **jeder** Listenseite, nicht nur
#: wenn gerade eine betroffene Zeile sichtbar ist: die Abwesenheit einer
#: solchen Zeile ist selbst keine Aussage.
HERKUNFT_HINWEIS = (
    "Scheduler-Auslösungen und verzögerte Einreihungen kennt das Verzeichnis "
    "erst beim Aufgreifen -- für sie steht kein Einreihungszeitpunkt da."
)


def leerzustand(f: Filter, status: dict[str, Any], reichweite: float | None) -> tuple[str, str]:
    """Titel und **wahrscheinliche Ursache** fuer eine leere Liste (R33).

    "Keine Treffer" allein ist die gefaehrlichste Antwort dieser Seite: sie
    liest sich wie "es ist nichts passiert", auch wenn in Wahrheit der Strom
    fehlt, das Verzeichnis gerade neu aufgebaut wird oder der Task schlicht
    aelter ist als die Aufbewahrung. Die Reihenfolge hier ist die der
    Wahrscheinlichkeit, und jede Stufe nennt etwas Ueberpruefbares.
    """
    if status["directory"].get("rebuilding"):
        return ("Das Verzeichnis wird gerade neu aufgebaut",
                "Die Liste ist noch unvollständig. Sobald der Strom eingeholt ist, "
                "steht hier mehr.")
    if reichweite is None:
        # Gar kein Eintrag. Hier ist der fehlende Strom die wahrscheinliche
        # Ursache und nicht bloss Beiwerk.
        if not status["stream"]["ok"]:
            return ("Keine Ereignisse erreichbar",
                    f"Der Ereignisstrom ist nicht erreichbar: {status['stream']['reason']}")
        return ("Noch kein Task im Verzeichnis",
                "Es sind bisher keine Ereignisse eingetroffen. Läuft in den "
                "Anwendungen und Workern der Hook des Dashboards?")
    if any((f.text, f.status, f.task_name, f.worker_id)):
        # Das Verzeichnis hat Eintraege, nur keinen passenden. Dass der Strom
        # daneben stockt, steht in der Kopfzeile -- hier waere es die
        # unwahrscheinlichere Erklaerung und lenkte von der Suche ab.
        return ("Kein Task passt zu dieser Suche",
                f"Das Verzeichnis reicht bis {_lesbar(reichweite)} zurück -- "
                "älteres steht nicht mehr darin.")
    return ("Kein Task im gewählten Zeitraum",
            f"Das Verzeichnis reicht bis {_lesbar(reichweite)} zurück.")


def _lesbar(ts: float | None) -> str:
    from swarmq_dashboard.web.templates import zeitpunkt

    return zeitpunkt(ts)


def _aufbewahrungsgrenze(request: Request[Any, Any, Any]) -> float:
    """Der aelteste Zeitpunkt, fuer den die Aufbewahrung noch einsteht."""
    config = request.app.state.services._config
    return time.time() - float(config.retention_days) * 86400.0


def _abfragen(request: Request[Any, Any, Any]) -> DirectoryQueries:
    services = request.app.state.services
    schreiber = services.services_directory_writer
    if schreiber is None:
        raise NotFoundException(detail="das Verzeichnis ist nicht geöffnet")
    return DirectoryQueries(schreiber._con)


#: Waehlbare Zeitraeume der Leiste, in Tagen. ``0`` heisst "alles, was das
#: Verzeichnis hat" -- und steht bewusst zuerst: wer nichts waehlt, soll
#: nicht heimlich eingeschraenkt werden.
ZEITRAEUME = ((0, "beliebig"), (1, "letzter Tag"), (7, "letzte 7 Tage"),
              (10, "letzte 10 Tage"), (30, "letzte 30 Tage"))


def _filter_aus(request: Request[Any, Any, Any]) -> Filter:
    p = request.query_params
    # ``seit`` ist die Fassung fuer die Leiste: eine Zahl Tage. ``since`` und
    # ``until`` bleiben als Zeitstempel erhalten -- sie sind praeziser, und
    # ein Verweis aus einem Ticket soll weiter gelten.
    seit = _zahl(p.get("seit"))
    since = _zahl(p.get("since"))
    if since is None and seit:
        since = time.time() - seit * 86400.0
    return Filter(
        text=p.get("q", ""),
        status=p.get("status", ""),
        task_name=p.get("task_name", ""),
        worker_id=p.get("worker_id", ""),
        since=since,
        until=_zahl(p.get("until")),
    )


def _zahl(wert: str | None) -> float | None:
    try:
        return float(wert) if wert else None
    except ValueError:
        return None


#: Statuswerte, bei denen der Task beendet ist. Eine Stornierung ist dann
#: gegenstandslos -- und ein Merker, den niemand mehr liest, waere nur eine
#: Zeile in Valkey, die 24 Stunden lang den naechsten Task derselben Kennung
#: treffen koennte.
BEENDET = frozenset({"fertig", "gescheitert", "storniert", "dead_letter",
                     "abgelaufen", "abgewiesen"})


@get("/tasks")
async def liste(request: Request[Any, Any, Any]) -> Template:
    """Die Task-Liste mit Suche und vier Filtern (R10)."""
    q = _abfragen(request)
    f = _filter_aus(request)
    seite = q.search(f, cursor=request.query_params.get("cursor"))
    status = request.app.state.services.status()
    # An der Aufbewahrungsgrenze abgeschnitten (R8). Ohne das zog eine
    # einzige langlebige Zeile die gemeldete Reichweite beliebig weit
    # zurück, waehrend alles dazwischen laengst geraeumt war.
    reichweite = q.reach(retention_start=_aufbewahrungsgrenze(request))
    leer = leerzustand(f, status, reichweite)
    return Template(
        "tasks/liste.html",
        context={
            "zeilen": seite.rows,
            "hinweis": seite.note,
            "weiter": seite.next_cursor,
            "filter": f,
            "zeitraeume": ZEITRAEUME,
            "seit": request.query_params.get("seit", ""),
            "reichweite": reichweite,
            "leer_titel": leer[0],
            "leer_ursache": leer[1],
            "herkunft_hinweis": HERKUNFT_HINWEIS,
            "status": status,
            "account": request.session.get(SESSION_ACCOUNT_KEY, ""),
            "aktiv": "tasks",
            "csrf_token": request.session.get("csrf", ""),
        },
    )


@get("/tasks/{task_id:str}")
async def detail(request: Request[Any, Any, Any], task_id: FromPath[str]) -> Template:
    """Die Task-Ansicht mit allen Feldern, Argumenten und der Signalfolge."""
    q = _abfragen(request)
    schreiber = request.app.state.services.services_directory_writer
    zeile = schreiber.row(task_id)
    if zeile is None:
        # Benannt, nicht als Absturz -- und ohne zu behaupten, es habe den
        # Task nie gegeben: er kann schlicht aelter sein als die Aufbewahrung.
        raise NotFoundException(
            detail=f"Kein Task mit der Kennung {task_id!r} im Verzeichnis. "
            "Er kann älter sein als die Aufbewahrung."
        )
    return Template(
        "tasks/detail.html",
        context={
            "task": zeile,
            "signale": q.signals(task_id),
            "hinweis": request.query_params.get("hinweis"),
            "beendet": BEENDET,
            "herkunft_hinweis": HERKUNFT_HINWEIS,
            "status": request.app.state.services.status(),
            "account": request.session.get(SESSION_ACCOUNT_KEY, ""),
            "aktiv": "tasks",
            "csrf_token": request.session.get("csrf", ""),
        },
    )


@post("/tasks/{task_id:str}/cancel")
async def cancel(request: Request[Any, Any, Any], task_id: FromPath[str]) -> Redirect:
    """Eine Stornierung **anfordern** (R12, R12a).

    Drei Faelle, und keiner davon ist ein Absturz:

    * Der Task ist beendet: abgewiesen, begruendet, **ohne** etwas zu
      schreiben. Ein Merker, den niemand mehr liest, bliebe 24 Stunden in
      Valkey stehen und koennte den naechsten Task derselben Kennung treffen.
    * Es lief schon eine Anforderung: das ist eine Auskunft, kein Fehler
      (AE3a). Sonst meldet das Dashboard einem zweiten Teammitglied eine
      Stoerung, wo nur jemand schneller war.
    * Das Backend ist weg: benannt. Die Ansicht darf dann **nicht**
      behaupten, die Stornierung sei angefordert.
    """
    services = request.app.state.services
    schreiber = services.services_directory_writer
    if schreiber is None:
        raise NotFoundException(detail="das Verzeichnis ist nicht geöffnet")
    zeile = schreiber.row(task_id)
    if zeile is None:
        raise NotFoundException(detail=f"Kein Task mit der Kennung {task_id!r}.")

    konto = str(request.session.get(SESSION_ACCOUNT_KEY, "unbekannt"))
    if zeile["status"] in BEENDET:
        await services.audit.record("task_cancel", account=konto, target=task_id,
                                    outcome="abgelehnt", detail="der Task ist bereits beendet")
        return Redirect(f"/tasks/{task_id}?hinweis=beendet", status_code=303)

    try:
        neu = await services.canceller.cancel(task_id)
    except Exception as exc:
        await services.audit.record("task_cancel", account=konto, target=task_id,
                                    outcome="fehlgeschlagen", detail=str(exc)[:200])
        return Redirect(f"/tasks/{task_id}?hinweis=backend", status_code=303)

    # Erst **nach** dem gelungenen Merker festhalten: sonst zeigte die Ansicht
    # eine Anforderung, die nie ankam.
    schreiber.mark_cancel_requested(task_id)
    await services.audit.record(
        "task_cancel", account=konto, target=task_id,
        outcome="ok" if neu else "lief bereits",
    )
    return Redirect(f"/tasks/{task_id}", status_code=303)
