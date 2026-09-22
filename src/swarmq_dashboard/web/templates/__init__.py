"""Seitengeruest: Vorlagen, Bereiche, statische Dateien (U11, R24-R26).

Die Gestaltung ist freigegeben (``docs/reference/oberflaeche.md``), die
sichtbare Wahrheit ist das Mockup. Dieses Modul liefert sie als lauffaehige
Vorlagen -- ohne Build-Schritt und **ohne Zugriff nach aussen** (KTD7).

Der letzte Punkt ist keine Feinheit: eine Seite, die Schrift oder Skript aus
einem fremden Netz holt, verraet jeden Aufruf des Dashboards an Dritte -- und
faellt aus, sobald der Dashboard-Host keinen Ausgang hat. Bei einem
Ops-Werkzeug ist genau das im Vorfall der Normalfall, nicht die Ausnahme.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    select_autoescape,
)
from litestar.datastructures import CacheControlHeader
from litestar.enums import ScopeType
from litestar.middleware import ASGIMiddleware
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.router import Router
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from markupsafe import Markup, escape

if TYPE_CHECKING:
    from litestar.types import ASGIApp, Message, Receive, Scope, Send

TEMPLATE_DIR = Path(__file__).parent
STATIC_DIR = Path(__file__).parent.parent / "static"


@dataclass(frozen=True, slots=True)
class Bereich:
    """Ein Eintrag der linken Navigation."""

    slug: str
    titel: str
    pfad: str


#: Genau drei Bereiche (R24). Ein vierter waere eine Zusage, die es nicht
#: gibt -- R25 haelt die Oberflaeche ausdruecklich auf das Noetige.
BEREICHE = (
    Bereich("worker", "Worker", "/"),
    Bereich("tasks", "Tasks", "/tasks"),
    Bereich("dlq", "Dead Letter", "/dlq"),
    Bereich("probe", "Probe", "/probe"),
)

#: Wortmarken der drei Kopfzeilen-Zustaende. Farbe allein traegt nie die
#: Aussage: ein Bildschirmfoto im Ticket verliert seine Farben, und rund jeder
#: zwoelfte Mann sieht Rot und Gruen nicht auseinander.
_ERREICHBAR = "erreichbar"
_NICHT_ERREICHBAR = "nicht erreichbar"
_AKTUELL = "aktuell"
_VERZOEGERT = "verzögert"


def zeitpunkt(ts: float | None) -> str:
    """Einen Zeitstempel lesbar machen. ``None`` heisst "noch nie"."""
    if ts is None:
        return "noch nie"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def zahl(wert: int | None) -> str:
    """Eine Zahl mit deutscher Tausendertrennung. ``None`` heisst "kein Wert".

    ``1200`` und ``12000`` unterscheiden sich um eine Stelle, und der
    Rueckstau einer Queue ist die Zahl, auf die im Vorfall zuerst geschaut
    wird. "Ist das jetzt zwoelfhundert oder zwoelftausend" kostet genau dort
    eine Sekunde.

    ``None`` wird **nicht** zu ``0``: eine Null, wo eine Auskunft fehlt, ist
    die gefaehrlichste stille Falschaussage dieser Oberflaeche.
    """
    if wert is None:
        return "--"
    if not isinstance(wert, int) or isinstance(wert, bool):
        # ``Gruppe.belegt()`` gibt "2 von 4" zurueck, nicht 2. Ohne diese
        # Pruefung antwortete die Worker-Seite mit 500 und einer Meldung
        # ueber Formatangaben -- die sagt nicht, was los ist.
        raise TypeError(f"zahl() erwartet eine ganze Zahl oder None, nicht {wert!r}")
    return f"{wert:,}".replace(",", ".")


def marke(name: str, zustand: dict[str, Any], strom: bool = False) -> str:
    """Eine Zustandsmarke der Kopfzeile: Punkt **und** Wort (R26).

    Der Strom hat drei Lagen statt zwei -- erreichbar, aber verzoegert ist
    etwas anderes als nicht erreichbar, und "noch nie ein Signal" ist
    ausdruecklich nicht "aktuell": ein stehender Strom liesse sich sonst als
    ruhiges System lesen.
    """
    #: Ein Grund aus der Bibliothek ist englisch ("Error 111 connecting
    #: to ..."); ein Vorleseprogramm spricht ihn sonst deutsch aus, und aus
    #: "connecting" wird etwas, das niemand wiedererkennt. Der Zeitpunkt
    #: daneben ist deutsch formatiert und bleibt es.
    sprache = ""
    if not zustand.get("ok"):
        art, wort = "is-fail", _NICHT_ERREICHBAR
        grund = zustand.get("reason") or ""
        sprache = ' lang="en"'
    elif strom and zustand.get("stale"):
        art, wort = "is-wait", _VERZOEGERT
        grund = f"zuletzt {zeitpunkt(zustand.get('last_event_at'))}"
    elif strom:
        art, wort = "is-ok", _AKTUELL
        grund = f"zuletzt {zeitpunkt(zustand.get('last_event_at'))}"
    else:
        art, wort = "is-ok", _ERREICHBAR
        grund = ""

    zusatz = f' <span class="muted"{sprache}>{escape(grund)}</span>' if grund else ""
    return Markup(
        f'<span class="tag {art}" title="{escape(name)}">'
        f'<span class="tag-dot"></span> {escape(name)}: {wort}{zusatz}</span>'
    )


#: Status-Wert -> Zustandsart der Gestaltung. Die Zuordnung steht hier und
#: nicht in der Vorlage, damit sie an **einer** Stelle liegt: eine Vorlage,
#: die sie nachbaut, laeuft irgendwann auseinander.
_ZUSTANDSART = {
    "fertig": "is-ok",
    "laufend": "is-run",
    "wartend": "is-idle",
    "wiederholung": "is-wait",
    "storniert": "is-idle",
    "gescheitert": "is-fail",
    "dead_letter": "is-fail",
    "abgelaufen": "is-fail",
    "abgewiesen": "is-fail",
}


def zustandsart(status: str | None) -> str:
    """Die Zustandsart zu einem Task-Status. Unbekanntes ist neutral.

    Neutral und nicht "fehler": ein Status, den diese Fassung nicht kennt, ist
    keine Stoerung -- er ist eine Aussage, die wir noch nicht einordnen.
    """
    return _ZUSTANDSART.get(status or "", "is-idle")


def weiterlink(f: object, cursor: str, seit: str = "") -> str:
    """Die Adresszeile fuer die naechste Seite, mit allen Filtern."""
    from urllib.parse import urlencode

    teile = {
        "q": getattr(f, "text", ""),
        "status": getattr(f, "status", ""),
        "task_name": getattr(f, "task_name", ""),
        "worker_id": getattr(f, "worker_id", ""),
        # **Der Zeitraum muss mit.** Ohne ihn zeigte Seite zwei einer auf
        # "letzter Tag" eingeschraenkten Suche Zeilen von vor fuenf Tagen,
        # und die Leiste stellte sich wieder auf "beliebig" -- wer blaettert,
        # sucht ab Seite zwei in einem anderen Zeitraum, als er gewaehlt hat,
        # und merkt es nicht.
        "seit": seit,
        "cursor": cursor,
    }
    return urlencode({k: v for k, v in teile.items() if v})


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
    )
    env.globals["marke"] = marke
    env.globals["zeitpunkt"] = zeitpunkt
    env.globals["zahl"] = zahl
    env.globals["weiterlink"] = weiterlink
    env.globals["zustandsart"] = zustandsart
    env.globals["bereiche"] = BEREICHE
    return env


_env = _environment()


def render(vorlage: str, **kontext: Any) -> str:
    """Eine Vorlage rendern. Fuer die Testreihe und fuer Fragmente."""
    kontext.setdefault("csrf_token", "")
    kontext.setdefault("account", "")
    kontext.setdefault("aktiv", "")
    return _env.get_template(vorlage).render(**kontext)


def template_config() -> TemplateConfig[JinjaTemplateEngine]:
    """Vorlagen-Einstellung fuer die Anwendung."""
    return TemplateConfig(
        directory=TEMPLATE_DIR,
        engine=JinjaTemplateEngine,
        engine_callback=_engine_callback,
    )


def _engine_callback(engine: JinjaTemplateEngine) -> None:
    # **StrictUndefined**, wie in der eigenen Umgebung oben. Ohne das rendert
    # Litestars Jinja eine unbekannte Variable still zu Leerraum -- gemessen
    # verschwand so ein ganzer stehender Hinweis von der Dead-Letter-Seite,
    # nachdem ein Umbau den Variablennamen verschoben hatte, und kein Test
    # und kein Fehler wies darauf hin. Eine Vorlage, die schweigt, wo sie
    # etwas sagen sollte, ist genau die Fehlerklasse, gegen die dieses
    # Dashboard gebaut ist.
    engine.engine.undefined = StrictUndefined
    engine.engine.globals["marke"] = marke
    engine.engine.globals["zeitpunkt"] = zeitpunkt
    engine.engine.globals["zahl"] = zahl
    engine.engine.globals["weiterlink"] = weiterlink
    engine.engine.globals["zustandsart"] = zustandsart
    engine.engine.globals["bereiche"] = BEREICHE


class NurWennGeaendert(ASGIMiddleware):
    """Beantwortet eine Rueckfrage mit 304 statt mit der ganzen Datei.

    ``no-cache`` heisst "vor dem Benutzen nachfragen", nicht "nicht
    speichern". Litestar schickt zu jeder statischen Datei ein ETag, wertet
    ``If-None-Match`` aber nicht aus -- die Rueckfrage kostete damit jedes
    Mal die ganze Antwort. Gemessen sind das 186 KB je Seitenaufruf
    (Stylesheet, HTMX, Idiomorph, ``live.js``, Schriften), auf einer Seite,
    die ein Team den ganzen Tag offen hat.

    Die Pruefung ist die schwache (RFC 9110, 13.1.2): ``If-None-Match``
    vergleicht Zeichenketten, und ein ``W/``-Vorsatz spielt dabei keine
    Rolle.
    """

    scopes = (ScopeType.HTTP,)

    async def handle(self, scope: Scope, receive: Receive, send: Send,
                     next_app: ASGIApp) -> None:
        angefragt = {
            wert.strip().removeprefix("W/")
            for kopf, roh in scope["headers"]
            if kopf == b"if-none-match"
            for wert in roh.decode("latin-1").split(",")
        }
        if not angefragt:
            await next_app(scope, receive, send)
            return

        passt = False

        async def pruefen(nachricht: Message) -> None:
            nonlocal passt
            if nachricht["type"] == "http.response.start":
                if nachricht["status"] != 200:
                    await send(nachricht)
                    return
                marke = next(
                    (roh.decode("latin-1").strip().removeprefix("W/")
                     for kopf, roh in nachricht["headers"] if kopf == b"etag"),
                    None,
                )
                passt = marke is not None and ("*" in angefragt or marke in angefragt)
                if not passt:
                    await send(nachricht)
                    return
                # Kein ``content-length`` und kein Koerper: eine 304 mit
                # Laengenangabe bringt manchen Klienten dazu, auf Bytes zu
                # warten, die nie kommen.
                await send({
                    "type": "http.response.start",
                    "status": 304,
                    "headers": [(k, v) for k, v in nachricht["headers"]
                                if k not in (b"content-length", b"content-type")],
                })
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            if not passt:
                await send(nachricht)

        await next_app(scope, receive, pruefen)


def statische_dateien() -> Router:
    """Router fuer Stylesheet, Skripte und Schriften.

    Von der Anmeldepflicht ausgenommen: die Anmeldeseite braucht das
    Stylesheet, und wer es sieht, erfaehrt nichts. Vom CSRF-Waechter ebenso --
    es sind ausschliesslich lesende Anfragen.
    """
    return create_static_files_router(
        path="/static",
        directories=[STATIC_DIR],
        name="static",
        opt={"public": True, "no_csrf": True},
        # ``no-cache`` heisst nicht "nicht speichern", sondern "vor dem
        # Benutzen nachfragen". Ohne diese Anweisung entscheidet der Browser
        # selbst -- und eine Stylesheet-Aenderung kam dreimal hintereinander
        # nicht an: der Dienst lieferte die neue Datei aus, der Browser
        # zeigte die alte. Bei einem Ops-Werkzeug hiesse das, dass ein Team
        # nach einem Deploy tagelang das alte ``live.js`` faehrt, ohne es zu
        # merken. Mit ETag kostet die Rueckfrage eine Antwort ohne Koerper.
        cache_control=CacheControlHeader(no_cache=True),
        middleware=[NurWennGeaendert()],
    )
