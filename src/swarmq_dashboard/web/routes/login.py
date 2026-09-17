"""Anmelden und Abmelden.

Der Handler ist die Stelle, an der die Anmeldebremse sitzt (R22) -- nicht die
eingebaute Ratenbegrenzung des Rahmens: deren Schluesselfunktion bekommt den
Anfragekoerper nicht zu sehen und kann darum gar nicht wissen, um welches
Konto es geht. Sie koennte nur die Herkunft bremsen, und das ist die Haelfte.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from litestar import Request, Response, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect
from litestar.utils.scope.state import ScopeState

from swarmq_dashboard.web.auth import SESSION_ACCOUNT_KEY, rotate_session_id

#: Was die Anmeldung bei einem Fehlschlag sagt. **Ein** Satz fuer alle
#: Faelle -- unbekanntes Konto, falsches Passwort, gesperrtes Konto: jede
#: Unterscheidung waere dieselbe Aufzaehlung, die die Pruefung in
#: ``accounts`` mit gleichen Antwortzeiten verhindert (R22).
LOGIN_FAILED = "Konto oder Passwort stimmt nicht."


def _herkunft(request: Request[Any, Any, Any]) -> str:
    """Die Herkunft eines Versuchs, fuer die zweite Haelfte der Bremse.

    Der Klient der Verbindung, nicht eine Kopfzeile: ``X-Forwarded-For`` waere
    frei waehlbar und die Bremse damit wirkungslos. Steht ein Abschluss davor,
    muss er die Adresse selbst durchreichen -- das gehoert in die
    Betriebsanleitung, nicht in eine geratene Kopfzeile.
    """
    return request.client.host if request.client else "unbekannt"


@get("/login", opt={"public": True}, sync_to_thread=False)
def login_form(request: Request[Any, Any, Any]) -> Response[str]:
    """Die Anmeldeseite. Schlicht, bis U11 das Seitengeruest bringt."""
    # Der Rahmen legt das Merkmal in den Verbindungszustand, nicht in den
    # Scope-Schluessel gleichen Namens -- und in den Cookies steht es beim
    # ersten Aufruf noch nicht, weil es erst beim Senden gesetzt wird.
    merkmal = ScopeState.from_scope(request.scope).csrf_token or ""
    return Response(
        content=(
            "<!doctype html><meta charset=utf-8><title>Anmelden</title>"
            "<form method=post action=/login>"
            f'<input type=hidden name=_csrf_token value="{merkmal}">'
            "<label>Konto <input name=account autofocus></label>"
            "<label>Passwort <input name=password type=password></label>"
            "<button>Anmelden</button></form>"
        ),
        media_type="text/html",
    )


@post("/login", opt={"public": True, "exclude_from_csrf": True})
async def login(
    request: Request[Any, Any, Any],
    data: Annotated[dict[str, str], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response[str] | Redirect:
    """Anmelden. Bremst vor der Pruefung, nicht danach.

    Vor der Pruefung, weil die Verzoegerung sonst nur den Fall traefe, den die
    Pruefung ohnehin ablehnt -- und ein Angreifer koennte die Bremse durch
    Abbrechen der Verbindung umgehen, waehrend der Server noch wartet.
    """
    services = request.app.state.services
    konto = (data.get("account") or "").strip()
    passwort = data.get("password") or ""
    herkunft = _herkunft(request)

    verzoegerung = services.throttle.delay_for(konto, herkunft)
    if verzoegerung:
        await asyncio.sleep(verzoegerung)

    if konto and services.accounts.verify(konto, passwort):
        services.throttle.record_success(konto, herkunft)
        # Neue Sitzungskennung nach der Anmeldung: sonst behaelt eine vor dem
        # Anmelden untergeschobene Kennung ihre Gueltigkeit (Sitzungsuebernahme).
        await rotate_session_id(request)
        request.set_session({SESSION_ACCOUNT_KEY: konto})
        return Redirect("/", status_code=303)

    services.throttle.record_failure(konto, herkunft)
    return Response(content=LOGIN_FAILED, media_type="text/plain", status_code=401)


@post("/logout")
async def logout(request: Request[Any, Any, Any]) -> Redirect:
    """Abmelden. Die Sitzung gilt danach sofort nicht mehr."""
    request.clear_session()
    return Redirect("/login", status_code=303)
