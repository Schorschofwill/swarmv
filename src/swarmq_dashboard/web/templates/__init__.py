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
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.router import Router
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from markupsafe import Markup, escape

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
)

#: Wortmarken der drei Kopfzeilen-Zustaende. Farbe allein traegt nie die
#: Aussage: ein Bildschirmfoto im Ticket verliert seine Farben, und rund jeder
#: zwoelfte Mann sieht Rot und Gruen nicht auseinander.
_ERREICHBAR = "erreichbar"
_NICHT_ERREICHBAR = "nicht erreichbar"
_AKTUELL = "aktuell"
_VERZOEGERT = "verzoegert"


def zeitpunkt(ts: float | None) -> str:
    """Einen Zeitstempel lesbar machen. ``None`` heisst "noch nie"."""
    if ts is None:
        return "noch nie"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def marke(name: str, zustand: dict[str, Any], strom: bool = False) -> str:
    """Eine Zustandsmarke der Kopfzeile: Punkt **und** Wort (R26).

    Der Strom hat drei Lagen statt zwei -- erreichbar, aber verzoegert ist
    etwas anderes als nicht erreichbar, und "noch nie ein Signal" ist
    ausdruecklich nicht "aktuell": ein stehender Strom liesse sich sonst als
    ruhiges System lesen.
    """
    if not zustand.get("ok"):
        art, wort = "fehler", _NICHT_ERREICHBAR
        grund = zustand.get("reason") or ""
    elif strom and zustand.get("stale"):
        art, wort = "wartet", _VERZOEGERT
        grund = f"zuletzt {zeitpunkt(zustand.get('last_event_at'))}"
    elif strom:
        art, wort = "ok", _AKTUELL
        grund = f"zuletzt {zeitpunkt(zustand.get('last_event_at'))}"
    else:
        art, wort = "ok", _ERREICHBAR
        grund = ""

    zusatz = f' <span class="muted">{escape(grund)}</span>' if grund else ""
    return Markup(
        f'<span class="chip chip--{art}" title="{escape(name)}">'
        f'<span class="dot dot--{art}"></span> {escape(name)}: {wort}{zusatz}</span>'
    )


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        undefined=__import__("jinja2").StrictUndefined,
    )
    env.globals["marke"] = marke
    env.globals["zeitpunkt"] = zeitpunkt
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
    engine.engine.globals["marke"] = marke
    engine.engine.globals["zeitpunkt"] = zeitpunkt
    engine.engine.globals["bereiche"] = BEREICHE


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
    )
