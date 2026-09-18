"""Die Dead-Letter-Seite (U15 Teil 2, F4, R6, R27, R28, R28a, R29, R30).

Die einzige Seite im Dashboard, deren **Ansehen** Schaden anrichten koennte.
``list_dead_letters`` heisst "non-destructive", und im Wortsinn stimmt das --
aber jede gelesene Nachricht wird *neu* eingereiht: die Reihenfolge aendert
sich, der Zustellzaehler beginnt von vorn, und ein Absturz mitten im Vorgang
verliert die schon gelesenen.

Daraus folgt der ganze Aufbau. Der **Dauerzustand** kommt aus zwei Quellen,
die beide nichts kosten:

* die Tiefe je Queue aus der passiven Broker-Beobachtung (R28) -- sie ist die
  massgebliche Zahl;
* die Dead-Letter-Tasks aus dem Verzeichnis (R6) mit Name, Argumenten und
  Fehlertext -- sie beantworten den Alltagsfall nach einem Fehlerdeploy
  ("warum sind sie gescheitert?") ohne eine einzige Umreihung.

Diese Liste ist ausdruecklich als **moeglicherweise unvollstaendig** markiert.
Eine Nachricht, deren Ereignisse nie ankamen -- Worker ohne Hook, Luecke im
Strom --, fehlt dort. Ohne den Hinweis laese sich "drei Zeilen" als "drei
Nachrichten", und wer danach leert, vernichtet vier. Das Sichten nach R28a
bleibt der Weg zu genau diesen Nachrichten.
"""

from __future__ import annotations

from typing import Annotated, Any

from litestar import Request, get, post
from litestar.enums import RequestEncodingType
from litestar.exceptions import NotFoundException
from litestar.params import Body, FromPath
from litestar.response import Template

from swarmq_dashboard.directory.queries import DirectoryQueries, Filter
from swarmq_dashboard.directory.workers import WorkerRegistry
from swarmq_dashboard.dlq import DlqBusy, MoveResult, PeekResult
from swarmq_dashboard.web.auth import SESSION_ACCOUNT_KEY

#: Der Status, den das Verzeichnis einer gescheiterten Nachricht gibt (R6).
DLQ_STATUS = "dead_letter"

#: Was das Sichten wirklich tut -- **vor** dem Knopf, nicht danach (R28a).
#: Jeder der drei Saetze benennt eine Folge, die die Zusage "non-destructive"
#: im Wortsinn zulaesst und die trotzdem niemand erwartet.
SICHT_WARNUNG = (
    "Sichten liest die Nachrichten, und jede gelesene wird sofort neu "
    "eingereiht. Dabei ändert sich ihre Reihenfolge, ihr Zustellzähler "
    "beginnt von vorn, "
    "und stürzt das Dashboard mitten im Vorgang ab, sind die schon gelesenen "
    "Nachrichten verloren. Die Tiefe oben und die Liste aus dem Verzeichnis "
    "kosten nichts davon -- sichten Sie nur, was dort fehlt."
)

#: Warum die Verzeichnis-Liste kuerzer sein kann als die Tiefe (R6, R4a).
UNVOLLSTAENDIG_HINWEIS = (
    "Diese Liste stammt aus dem Verzeichnis und ist möglicherweise "
    "unvollständig: Nachrichten von Workern ohne Hook und solche aus einer "
    "Lücke im Strom fehlen. Massgeblich ist die Tiefe aus der "
    "Broker-Beobachtung."
)


def _abfragen(request: Request[Any, Any, Any]) -> DirectoryQueries:
    schreiber = request.app.state.services.services_directory_writer
    if schreiber is None:
        raise NotFoundException(detail="das Verzeichnis ist nicht geöffnet")
    return DirectoryQueries(schreiber._con)


def _queues(request: Request[Any, Any, Any]) -> list[str]:
    schreiber = request.app.state.services.services_directory_writer
    if schreiber is None:
        raise NotFoundException(detail="das Verzeichnis ist nicht geöffnet")
    return WorkerRegistry(schreiber._con).known_queues()


async def _seite(
    request: Request[Any, Any, Any],
    *,
    meldung: str = "",
    gesichtet: PeekResult | None = None,
) -> Template:
    """Die Seite mit ihrem Dauerzustand -- und, wenn eine Aktion lief, deren
    Ergebnis.

    Bewusst kein Umleiten nach einer Aktion: das Ergebnis des Sichtens
    existiert nur hier und einmal. Es in die Sitzung zu legen, um es nach der
    Umleitung wieder herauszuholen, hiesse Klartext-Argumente in die
    Kontendatei zu schreiben (R7a) -- oder, bei Verlust, die Umreihung
    umsonst bezahlt zu haben.
    """
    services = request.app.state.services
    queues = _queues(request)
    stats = await services.queue_stats(queues)
    seite = _abfragen(request).search(Filter(status=DLQ_STATUS))

    zeilen = [
        {
            "queue": q,
            "stats": stats.get(q),
            "busy": services.dlq.busy(q),
        }
        for q in queues
    ]
    return Template(
        "dlq/uebersicht.html",
        context={
            "queues": zeilen,
            "tasks": seite.rows,
            "hinweis": seite.note,
            "meldung": meldung,
            "gesichtet": gesichtet,
            "sicht_warnung": SICHT_WARNUNG,
            "unvollstaendig_hinweis": UNVOLLSTAENDIG_HINWEIS,
            "leer_titel": "Keine gescheiterte Arbeit bekannt",
            "leer_ursache": _leergrund(services.status()),
            # Ohne bekannte Queue **keine leere Tabelle**: die Queue-Namen
            # kommen aus dem Verzeichnis, und dass es keine kennt, heisst
            # nicht, dass der Broker keine Dead-Letter-Queue haette (R33).
            "queues_leer_ursache": _ohne_queues(services.status()),
            "status": services.status(),
            "account": request.session.get(SESSION_ACCOUNT_KEY, ""),
            "aktiv": "dlq",
            "csrf_token": request.session.get("csrf", ""),
        },
    )


def _leergrund(status: dict[str, Any]) -> str:
    """Warum die Liste leer ist (R33).

    "Keine gescheiterte Arbeit" ist die beruhigendste Aussage der ganzen
    Oberflaeche -- und darum die, die am wenigsten unbelegt dastehen darf.
    """
    if status["directory"].get("rebuilding"):
        return ("Das Verzeichnis wird gerade neu aufgebaut -- die Liste ist noch "
                "unvollständig.")
    if not status["stream"]["ok"]:
        return f"Der Ereignisstrom ist nicht erreichbar: {status['stream']['reason']}"
    if not status["broker"]["ok"]:
        return ("Der Broker ist nicht erreichbar -- die Tiefe der Dead-Letter-Queues "
                "ist gerade unbekannt.")
    return "Es ist kein Task im Verzeichnis als gescheitert vermerkt."


#: Der Satz, der die leere Tabelle daran hindert, sich als Entwarnung zu
#: lesen. Er haengt an **jedem** Grund, nicht nur am haeufigsten: welcher
#: Grund gerade zutrifft, aendert nichts daran, dass die Frage offen ist.
OFFEN_GEBLIEBEN = ("Ob im Broker gescheiterte Arbeit liegt, ist damit nicht "
                   "beantwortet.")


def _ohne_queues(status: dict[str, Any]) -> str:
    """Warum die Seite keine einzige Queue nennt (R33).

    Die Namen kommen aus dem Verzeichnis -- aus den bekannten Workern und den
    gesehenen Tasks. Kennt es keine, weiss das Dashboard schlicht nicht, wo es
    nachsehen soll. Das ist etwas anderes als "es gibt keine gescheiterte
    Arbeit", und die Seite darf die beiden nicht verwechseln.
    """
    if status["directory"].get("rebuilding"):
        grund = ("Das Verzeichnis wird gerade neu aufgebaut -- die Queue-Namen "
                 "stehen noch nicht fest.")
    elif not status["stream"]["ok"]:
        grund = (f"Der Ereignisstrom ist nicht erreichbar: {status['stream']['reason']} "
                 "-- ohne ihn kennt das Dashboard keine Queue-Namen.")
    else:
        grund = ("Es ist noch keine Queue bekannt. Die Namen stammen aus den "
                 "Worker- und Task-Ereignissen; ohne sie weiss das Dashboard "
                 "nicht, wo es nach gescheiterter Arbeit sehen soll.")
    return f"{grund} {OFFEN_GEBLIEBEN}"


def _konto(request: Request[Any, Any, Any]) -> str:
    return str(request.session.get(SESSION_ACCOUNT_KEY, "unbekannt"))


async def _vermerken(request: Request[Any, Any, Any], queue: str, detail: str) -> None:
    """Jede der drei Aktionen hinterlaesst eine Spur (R27)."""
    await request.app.state.services.audit.record(
        "dlq_action", account=_konto(request), target=queue, detail=detail,
    )


def _bericht(e: MoveResult, was: str) -> str:
    """Beide Zahlen, immer getrennt (R29).

    "Zwei" allein liest sich als "alles erledigt", wo eine Nachricht liegen
    blieb -- und genau das ist der Irrtum, der jemanden die Queue leeren
    laesst, in der noch Arbeit steht.
    """
    if e.error:
        return f"{was} fehlgeschlagen: {e.error}"
    return (f"{was}: {e.moved} von {e.scanned} verarbeitet, "
            f"{e.unverarbeitet} unverarbeitet.")


@get("/dlq")
async def uebersicht(request: Request[Any, Any, Any]) -> Template:
    """Der Dauerzustand. Ruehrt keine Queue an (R28, AE12a)."""
    return await _seite(request)


@post("/dlq/{queue:str}/peek")
async def sichten(
    request: Request[Any, Any, Any],
    queue: FromPath[str],
    data: Annotated[dict[str, Any], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Template:
    """Ausdrueckliches Sichten (R28a).

    Die Bestaetigung im Formular ist kein Zierrat: die Wirkung ist zu gross
    fuer einen Fehlklick, und ein Knopf allein ist ein Fehlklick.
    """
    if str(data.get("bestaetigt", "")) != "sichten":
        return await _seite(
            request,
            meldung="Nicht gesichtet: die Bestätigung fehlte. " + SICHT_WARNUNG,
        )
    try:
        ergebnis = await request.app.state.services.dlq.peek(queue)
    except DlqBusy as exc:
        # Kein Fehler, sondern eine Auskunft: jemand anderes ist schon dabei,
        # und ein zweiter Durchlauf wuerde dieselben Nachrichten doppelt
        # umreihen.
        return await _seite(request, meldung=str(exc))
    await _vermerken(
        request, queue,
        f"gesichtet, {ergebnis.gelesen} Nachrichten gelesen und umgereiht"
        if not ergebnis.error else f"Sichten fehlgeschlagen: {ergebnis.error}",
    )
    return await _seite(request, gesichtet=ergebnis)


@post("/dlq/{queue:str}/requeue")
async def wiedereinreihen(
    request: Request[Any, Any, Any],
    queue: FromPath[str],
    data: Annotated[dict[str, Any], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Template:
    """Eine einzelne Nachricht zurueck in die Arbeits-Queue (R29)."""
    task_id = str(data.get("task_id", "")).strip()
    if not task_id:
        return await _seite(request, meldung="Nicht eingereiht: es fehlte die Kennung.")
    ergebnis = await request.app.state.services.dlq.requeue(queue, task_id)
    bericht = _bericht(ergebnis, f"Wiedereinreihen von {task_id}")
    await _vermerken(request, queue, bericht)
    return await _seite(request, meldung=bericht)


@post("/dlq/{queue:str}/requeue-all")
async def alle_wiedereinreihen(
    request: Request[Any, Any, Any], queue: FromPath[str]
) -> Template:
    """Alles zurueck in die Arbeits-Queue (R29)."""
    ergebnis = await request.app.state.services.dlq.requeue_all(queue)
    bericht = _bericht(ergebnis, "Wiedereinreihen aller Nachrichten")
    await _vermerken(request, queue, bericht)
    return await _seite(request, meldung=bericht)


@post("/dlq/{queue:str}/purge")
async def leeren(
    request: Request[Any, Any, Any],
    queue: FromPath[str],
    data: Annotated[dict[str, Any], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Template:
    """Die Dead-Letter-Queue leeren (R30).

    Die einzige Handlung im Dashboard, die Daten endgültig vernichtet. Sie
    verlangt Queue-Name **und** Zahl -- und der Dienst prueft die Zahl gegen
    den Stand von **jetzt**, nicht gegen den, der beim Rendern galt.
    """
    try:
        erwartet = int(str(data.get("erwartet", "")).strip())
    except ValueError:
        return await _seite(
            request, meldung="Nicht geleert: die Zahl war keine Zahl.",
        )
    ergebnis = await request.app.state.services.dlq.purge(
        queue, bestaetigung=str(data.get("bestaetigung", "")), erwartet=erwartet,
    )
    bericht = (f"Leeren abgewiesen: {ergebnis.error}" if ergebnis.error
               else f"Geleert: {ergebnis.moved} Nachrichten endgültig entfernt.")
    await _vermerken(request, queue, bericht)
    return await _seite(request, meldung=bericht)
