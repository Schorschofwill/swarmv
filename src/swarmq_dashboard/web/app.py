"""App-Bau und Verdrahtung.

Die Aussenverbindungen und ihre Takte liegen in ``web/services.py`` (KTD4).
Diese Datei baut daraus die Anwendung: Lebenszyklus, Sitzung, Fehlerseiten
und Routen. Die bisherigen Namen -- ``Services``, ``PROBE_TIMEOUT_S`` und die
Takt-Konstanten -- bleiben von hier aus importierbar (R4).
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from argon2 import PasswordHasher
from litestar import Litestar, Request, get
from litestar.exceptions import HTTPException, NotAuthorizedException
from litestar.exceptions.responses import create_exception_response
from litestar.middleware.session.server_side import ServerSideSessionConfig
from litestar.response import Redirect, Response
from litestar.status_codes import HTTP_303_SEE_OTHER, HTTP_401_UNAUTHORIZED
from litestar.stores.base import Store
from litestar.stores.registry import StoreRegistry

from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.control import SetpointStore
from swarmq_dashboard.web.auth import (
    SqliteStore,
    csrf_guard,
    session_guard,
)
from swarmq_dashboard.web.cancel import Canceller
from swarmq_dashboard.web.ergebnis import Ergebnisleser
from swarmq_dashboard.web.routes.dlq import (
    alle_wiedereinreihen,
    leeren,
    sichten,
    wiedereinreihen,
)
from swarmq_dashboard.web.routes.dlq import (
    uebersicht as dlq_uebersicht,
)
from swarmq_dashboard.web.routes.login import login, login_form, logout
from swarmq_dashboard.web.routes.probe import formular as probe_formular
from swarmq_dashboard.web.routes.probe import senden as probe_senden
from swarmq_dashboard.web.routes.tasks import cancel, detail, liste
from swarmq_dashboard.web.routes.worker import (
    entfernen,
    sollwert_aufheben,
    sollwert_dialog,
    sollwert_setzen,
    uebersicht,
)
from swarmq_dashboard.web.routes.workflows import (
    workflow_detail,
    workflow_glieder,
    workflow_liste,
    workflow_stornieren,
)
from swarmq_dashboard.web.services import (
    DEFAULT_INGEST_S,
    DEFAULT_MAINTENANCE_S,
    PROBE_TIMEOUT_S,
    STOP_TIMEOUT_S,
    Services,
)
from swarmq_dashboard.web.stream import live
from swarmq_dashboard.web.templates import render, statische_dateien, template_config
from swarmq_dashboard.workflows import Workflowleser

#: Die Namen, die von hier aus importierbar bleiben (R4). ``Services`` und die
#: Takt-Konstanten wohnen in ``web/services.py`` und werden nur weitergereicht.
__all__ = [
    "DEFAULT_INGEST_S",
    "DEFAULT_MAINTENANCE_S",
    "PROBE_TIMEOUT_S",
    "SESSION_STORE_NAME",
    "STOP_TIMEOUT_S",
    "Services",
    "UnknownStore",
    "aus_der_umgebung",
    "build_store_registry",
    "create_app",
]

#: Name, unter dem der Sitzungsspeicher registriert wird.
SESSION_STORE_NAME = "sessions"


class UnknownStore(LookupError):
    """Ein Speicher wurde unter einem Namen verlangt, den niemand angemeldet hat.

    Litestars Registratur legt in diesem Fall **still** einen prozesslokalen
    Speicher an. Die Sitzungen laegen dann im Arbeitsspeicher statt in der
    Kontendatei -- und niemand saehe es, bis nach einem Neustart alle
    abgemeldet sind. Ein Tippfehler im Namen darf den Start scheitern lassen,
    nicht die Ablage austauschen.
    """


class _SitzungsWeiche(Store):
    """Reicht an den Sitzungsspeicher des Dienstes durch.

    Die Registratur wird **einmal** beim Bauen der Anwendung angelegt, der
    Speicher gehoert aber dem Lebenszyklus: er wird beim Hochfahren geoeffnet
    und beim Herunterfahren geschlossen. Ohne diese Weiche haelt die
    Registratur eine Verbindung fest, die nach dem ersten Herunterfahren tot
    ist -- ein zweiter Lebenszyklus derselben Anwendung faende sie geschlossen
    vor. Genau daran sind zwei Tests haengengeblieben.
    """

    def __init__(self, services: Services) -> None:
        self._services = services

    @property
    def _echt(self) -> SqliteStore | None:
        # Kein Wurf bei fehlendem Speicher: die Sitzungsschicht schreibt auf
        # *jeder* Antwort, und ein Wurf hier machte aus einer unbrauchbaren
        # Kontendatei einen Dienst, der auf jede Anfrage mit 500 antwortet --
        # auch auf die Anmeldeseite und die Kopfzeile, die den Grund nennen
        # soll. Ohne Speicher gibt es eben keine Sitzung; der Waechter weist
        # dann ab, und das ist die richtige Antwort (R33).
        return self._services._sitzungsspeicher

    async def set(self, key: str, value: str | bytes, expires_in: object = None) -> None:
        if (speicher := self._echt) is not None:
            await speicher.set(key, value, expires_in)  # type: ignore[arg-type]

    async def get(self, key: str, renew_for: object = None) -> bytes | None:
        speicher = self._echt
        return await speicher.get(key, renew_for) if speicher else None  # type: ignore[arg-type]

    async def delete(self, key: str) -> None:
        if (speicher := self._echt) is not None:
            await speicher.delete(key)

    async def delete_all(self) -> None:
        if (speicher := self._echt) is not None:
            await speicher.delete_all()

    async def exists(self, key: str) -> bool:
        speicher = self._echt
        return await speicher.exists(key) if speicher else False

    async def expires_in(self, key: str) -> int | None:
        speicher = self._echt
        return await speicher.expires_in(key) if speicher else None


def build_store_registry(store: Store) -> StoreRegistry:
    """Die Registratur mit genau einem angemeldeten Speicher.

    Legt **nichts** an: das tut ``Services._konten_oeffnen``, gekapselt. Hier
    stand einmal ein ``AccountStore(...)``, damit die Datei existiert -- und
    damit lag der einzige ungekapselte Zugriff ausgerechnet im Bauweg der
    Anwendung. Eine unbrauchbare Kontendatei liess ``create_app`` werfen; der
    Wurf war nur von ``startup()`` hierher gewandert.
    """

    def kein_ersatz(name: str) -> Store:
        raise UnknownStore(
            f"kein Speicher unter dem Namen {name!r} angemeldet -- ein stiller "
            "Rueckfall auf den Arbeitsspeicher wuerde die Sitzungen verlieren"
        )

    return StoreRegistry({SESSION_STORE_NAME: store}, default_factory=kein_ersatz)



def create_app(
    config: DashboardConfig,
    *,
    hasher: PasswordHasher | None = None,
    maintenance_every_s: float = DEFAULT_MAINTENANCE_S,
    ingest_every_s: float = DEFAULT_INGEST_S,
    throttle_scale: float = 1.0,
    canceller: Canceller | None = None,
    broker: Any | None = None,
    dlq_admin: Any | None = None,
    stream: Any | None = None,
    setpoint_store: SetpointStore | None = None,
    probe_sender: Any | None = None,
    ergebnisleser: Ergebnisleser | None = None,
    workflowleser: Workflowleser | None = None,
    secure_cookies: bool = True,
) -> Litestar:
    """Die Anwendung bauen. Der Lebenszyklus haelt die Aussenverbindungen.

    ``maintenance_every_s`` ist die Naht fuer die Testreihe: ohne sie prueft
    kein Test, dass der Lebenszyklus den Takt ueberhaupt **startet** -- und
    genau das war der Fehler bei ``refresh()``, das nur von Hand gerufen wurde.
    """
    services = Services(config, hasher=hasher, throttle_scale=throttle_scale,
                        canceller=canceller, broker=broker, dlq_admin=dlq_admin,
                        stream=stream, setpoint_store=setpoint_store,
                        probe_sender=probe_sender, ergebnisleser=ergebnisleser,
                        workflowleser=workflowleser)
    # Die Registratur zeigt auf den Speicher des Dienstes, statt einen
    # eigenen zu halten: sonst bliebe eine vierte Verbindung auf die
    # Kontendatei offen, die niemand schliesst und deren abgelaufene Zeilen
    # niemand wegraeumt.
    weiche = _SitzungsWeiche(services)

    @asynccontextmanager
    async def lebenszyklus(app: Litestar) -> AsyncGenerator[None]:
        # Die Anmelderoute setzt ihr Merkmalscookie selbst und braucht
        # dieselbe Einstellung wie die Sitzung -- sonst schickt der Klient
        # ueber ``http`` nur eines von beiden mit.
        app.state.secure_cookies = secure_cookies
        await services.startup()
        app.state.services = services
        # Der Einlese-Takt ist die Hauptarbeit: ohne ihn liest der Dienst den
        # Strom nie und das Verzeichnis bleibt leer.
        aufgaben = [services.start_ingest(every_s=ingest_every_s),
                    *services.start_maintenance(every_s=maintenance_every_s)]
        try:
            yield
        finally:
            # Erst anhalten, dann schliessen. Andersherum schliesst der
            # Dienst eine Verbindung, auf der noch geschrieben wird.
            await services.stop_tasks(aufgaben)
            await services.shutdown()

    @get("/api/status", sync_to_thread=False)
    def zustand() -> dict[str, Any]:
        """Die eine Stelle, die im Vorfall zuerst aufgerufen wird.

        Sie darf nicht die erste sein, die ausfaellt -- deshalb liest sie nur
        festgehaltene Zustaende und spricht mit keinem Dienst.
        """
        return services.status()

    # Jedes Merkmal ausdruecklich: der Rahmen setzt ``secure=False`` und eine
    # lockere Seitenbindung. Ohne ``secure`` geht das Sitzungscookie beim
    # ersten Aufruf ueber eine mitlesbare Leitung, und ueber diese Leitung
    # gingen auch sieben Tage Klartext-Argumente (R7, R7a). Der Dienst ist
    # ausschliesslich ueber TLS erreichbar -- direkt oder hinter einem
    # vorgelagerten Abschluss.
    sitzung = ServerSideSessionConfig(
        store=SESSION_STORE_NAME,
        max_age=int(config.session_idle_s),
        renew_on_access=True,
        # ``False`` **nur** fuer die oertliche Entwicklung ueber Klartext-HTTP,
        # und nur ueber ``serve --insecure-http``. Der Grund ist zwingend:
        # kein Klient schickt ein ``Secure``-Cookie ueber ``http`` -- auch
        # nicht an 127.0.0.1 --, und ohne Cookie kommt der POST ohne Sitzung
        # an. Die Anmeldung antwortet dann mit "Das Anmeldeformular ist
        # abgelaufen", und niemand kaeme je hinein.
        secure=secure_cookies,
        httponly=True,
        samesite="strict",
    )
    # **Kein** ``csrf_config``: die CSRF-Schicht des Rahmens prueft doppeltes
    # Absenden mit HMAC, also nur, dass Cookie und Kopfzeile dieselbe,
    # irgendwann von diesem Dienst signierte Zeichenfolge tragen. Ein Bezug
    # zur Sitzung besteht nicht -- nachgemessen wurde ein Merkmal, das sich
    # ein nie angemeldeter Klient abholt, in einer fremden angemeldeten
    # Sitzung angenommen. R23 verlangt aber ein **sitzungsgebundenes**
    # Merkmal. ``csrf_guard`` liefert das; es steht in der Sitzung selbst.
    def zur_anmeldung(request: Request[Any, Any, Any],
                      exc: NotAuthorizedException) -> Response[Any]:
        """Einen Menschen zur Anmeldung schicken, ein Programm nicht.

        Beim ersten Oeffnen im Browser kam
        ``{"status_code":401,"detail":"keine gueltige Sitzung"}`` -- eine
        Sackgasse. Die Anmeldeseite gibt es, nur fuehrte nichts zu ihr.

        Die Unterscheidung haengt an der Anfrage, nicht am Pfad:

        * **HTMX** tauscht die Antwort in die laufende Seite. Eine Umleitung
          morphte die Anmeldeseite *in* das Dashboard; die Kopfzeile
          ``HX-Redirect`` laesst den Klienten stattdessen wirklich
          navigieren.
        * **Ein Browser** (er verlangt ``text/html``) wird umgeleitet.
        * **Alles andere** -- ``/api/status``, der Live-Strom, ein Skript --
          bekommt weiter 401. Ein 303 auf eine HTML-Seite saehe fuer den
          Strom wie ein Erfolg aus.
        """
        will_html = "text/html" in request.headers.get("accept", "")
        if request.headers.get("hx-request"):
            return Response(content=None, status_code=HTTP_401_UNAUTHORIZED,
                            headers={"HX-Redirect": "/login"})
        if will_html:
            return Redirect("/login", status_code=HTTP_303_SEE_OTHER)
        return create_exception_response(request, exc)

    def fehlerseite(request: Request[Any, Any, Any],
                    exc: HTTPException) -> Response[Any]:
        """Ein Fehler, der wie das Dashboard aussieht -- fuer Menschen.

        Rohes JSON ist fuer ein Programm richtig und fuer einen Browser eine
        Sackgasse. Wer einer Kennung aus einem Ticket folgt, deren Task aus
        der Aufbewahrung gefallen ist, trifft den 404 im **Normalfall**.
        """
        if "text/html" not in request.headers.get("accept", ""):
            return create_exception_response(request, exc)
        titel = {404: "Nicht gefunden", 403: "Nicht erlaubt",
                 405: "So nicht erreichbar"}.get(exc.status_code, "Etwas ging schief")
        return Response(
            content=render("fehler.html", code=exc.status_code, titel=titel,
                           detail=exc.detail or titel),
            media_type="text/html", status_code=exc.status_code,
        )

    return Litestar(
        exception_handlers={NotAuthorizedException: zur_anmeldung,
                            HTTPException: fehlerseite},
        route_handlers=[zustand, login_form, login, logout, live, liste, detail,
                        cancel, uebersicht, entfernen, sollwert_dialog,
                        sollwert_setzen, sollwert_aufheben,
                        probe_formular, probe_senden,
                        dlq_uebersicht, sichten, workflow_liste, workflow_detail,
                        workflow_glieder, workflow_stornieren,
                        wiedereinreihen, alle_wiedereinreihen, leeren,
                        statische_dateien()],
        template_config=template_config(),
        lifespan=[lebenszyklus],
        stores=build_store_registry(weiche),
        middleware=[sitzung.middleware],
        guards=[session_guard, csrf_guard],
    )


def aus_der_umgebung() -> Litestar:
    """Die Anwendung aus den Umgebungsvariablen bauen.

    Die Naht zwischen Kommandozeile und Dienst. Granian bekommt einen
    **Modulpfad** und ruft ihn im Arbeitsprozess selbst auf; alles, was der
    Dienst braucht, muss darum aus der Umgebung kommen und nicht aus
    Argumenten, die diesen Weg nicht ueberleben.
    """
    # ``SQD_INSECURE_HTTP`` setzt ``serve --insecure-http``; der
    # Arbeitsprozess erfaehrt nur ueber die Umgebung davon.
    unsicher = os.environ.get("SQD_INSECURE_HTTP", "").strip() == "1"
    return create_app(DashboardConfig.from_env(), secure_cookies=not unsicher)
