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
from litestar.datastructures import Cookie
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect

from swarmq_dashboard.web.auth import (
    CSRF_COOKIE,
    CSRF_FIELD,
    SESSION_ACCOUNT_KEY,
    SESSION_CSRF_KEY,
    gleich,
    new_csrf_token,
    normalisierter_kontoname,
    rotate_session_id,
)
from swarmq_dashboard.web.templates import render

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


@get("/login", opt={"public": True, "no_csrf": True}, sync_to_thread=False)
def login_form(request: Request[Any, Any, Any]) -> Response[str]:
    """Die Anmeldeseite. Schlicht, bis U11 das Seitengeruest bringt.

    Das Merkmal kommt aus der **Sitzung**, nicht aus dem Cookie des Klienten.
    Frueher stand hier der Wert aus dem Verbindungszustand -- und der ist bei
    sicheren Methoden der rohe, ungeprueft uebernommene Cookie-Wert des
    Klienten. Unmaskiert in ein Attribut geschrieben ergab das eine
    Skript-Einschleusung auf der einzigen Seite ohne Sitzung, in einem
    Formular mit Passwortfeld.

    Die Maskierung bleibt trotzdem stehen, obwohl sie nach dieser Korrektur
    von keiner Mutationsprobe mehr gekippt werden kann: das Merkmal ist jetzt
    selbst erzeugt und traegt nie ein Anfuehrungszeichen. Sie steht hier fuer
    den Tag, an dem jemand die Quelle wieder aendert -- so wie
    ``synchronous=full`` im Kontenspeicher.
    """
    # **Ohne** Sitzung: die Anmeldeseite bekommt jeder Unangemeldete zu sehen,
    # und jede angelegte Sitzung waere eine Zeile in der Kontendatei -- ein
    # kostenloser Weg, sie zu fuellen. Hier genuegt doppeltes Absenden:
    # Merkmal in Cookie **und** Formularfeld, beim Abschicken verglichen. Das
    # schliesst die Anmeldung von einer Fremdseite, ohne etwas zu speichern.
    # Sitzungsgebunden wird das Merkmal erst nach der Anmeldung (R23).
    # Ein bereits gueltiges Merkmal **nicht** ersetzen: die Seite ist
    # oeffentlich, und ein zweiter Tab, ein Lesezeichen oder die Zurueck-Taste
    # genuegte, damit das Cookie nicht mehr zur Sitzung passt. Danach scheitert
    # jede Schreibaktion mit 403, bis sich das Teammitglied neu anmeldet.
    vorhanden = request.session.get(SESSION_CSRF_KEY) if request.session else None
    merkmal = vorhanden if isinstance(vorhanden, str) and vorhanden else new_csrf_token()
    return Response(
        # Die Vorlage maskiert von sich aus (Jinja mit autoescape). Die
        # fruehere Fassung setzte die Zeichenkette von Hand zusammen -- und
        # genau dort ist einmal fremdes Skript hineingeraten.
        content=render("anmeldung.html", csrf_token=merkmal,
                       fehler=request.query_params.get("fehler")),
        media_type="text/html",
        cookies=[_merkmal_cookie(merkmal, secure=_sicher(request))],
    )


def _sicher(request: Request[Any, Any, Any]) -> bool:
    """Ob die Cookies ``Secure`` tragen -- dieselbe Einstellung wie die
    Sitzung. Vorgabe ist ``True``; nur ``serve --insecure-http`` senkt sie."""
    return bool(getattr(request.app.state, "secure_cookies", True))


def _merkmal_cookie(wert: str, *, secure: bool = True) -> Cookie:
    """Das Merkmal fuer die Oberflaeche lesbar hinterlegen.

    Absichtlich **nicht** ``httponly``: die Fragmentaufrufe holen es und
    schicken es als Kopfzeile zurueck. Sein Schutz liegt in der Bindung an die
    Sitzung, nicht in der Unlesbarkeit -- ein untergeschobenes Cookie trifft
    den Wert in der fremden Sitzung nicht.

    ``secure`` folgt derselben Einstellung wie das Sitzungscookie. Beim ersten
    echten Start fiel auf, warum das zusammengehoert: blieb dieses Cookie
    ``Secure``, waehrend das Sitzungscookie es nicht mehr war, schickte der
    Klient ueber ``http`` nur eines von beiden -- und die Anmeldung scheiterte
    weiter mit "Das Anmeldeformular ist abgelaufen", nur aus dem anderen
    Grund.
    """
    return Cookie(key=CSRF_COOKIE, value=wert, path="/", secure=secure,
                  httponly=False, samesite="strict")


@post("/login", opt={"public": True, "no_csrf": True})
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
    # Derselbe normalisierte Name, den die Kontenpruefung benutzt: sonst
    # bekaeme dieselbe Anmeldung je Unicode-Schreibweise einen eigenen
    # Zaehler, und der Kontoteil der Bremse halbierte sich. Und gekuerzt,
    # damit ein Name mit einer Million Zeichen nicht als Zeilenschluessel in
    # der Kontendatei landet -- gemessen 0,72 MiB je Sekunde.
    # Doppeltes Absenden fuer die Anmeldung selbst: ohne Sitzung gibt es
    # nichts, woran ein Merkmal gebunden werden koennte, aber der Vergleich
    # von Cookie und Formularfeld schliesst die Anmeldung von einer
    # Fremdseite -- sonst landet das Opfer unbemerkt im Konto des Angreifers.
    aus_dem_feld = data.get(CSRF_FIELD) or ""
    aus_dem_cookie = request.cookies.get(CSRF_COOKIE) or ""
    if not aus_dem_feld or not gleich(aus_dem_feld, aus_dem_cookie):
        return Response(content="Das Anmeldeformular ist abgelaufen. Bitte neu laden.",
                        media_type="text/plain", status_code=403)

    konto = normalisierter_kontoname(data.get("account") or "")
    passwort = data.get("password") or ""
    herkunft = _herkunft(request)

    # **Zuerst zaehlen, dann warten.** Frueher stand das Zaehlen am Ende, und
    # alle gleichzeitig laufenden Versuche lasen denselben Stand: zehn
    # gleichzeitige Versuche kosteten 1,02 s statt 10 s. Die Verzoegerung wirkte
    # je Anfrage statt je Angreifer. Ein Erfolg loescht die Zaehler ohnehin.
    # Alles Rechnende in einen Arbeitsfaden. argon2 kostet mit den Vorgaben
    # rund fuenfzig Millisekunden, und die Bremse schreibt dabei zweimal mit
    # echtem fsync; im Ereignis-Loop stand bei fuenfzig gleichzeitigen
    # Versuchen der ganze Dienst zwei Sekunden still -- auch ``/api/status``,
    # die Seite des Vorfalls. Granian faehrt einen Arbeiter.
    await asyncio.to_thread(services.throttle.record_failure, konto, herkunft)
    verzoegerung = await asyncio.to_thread(services.throttle.delay_for, konto, herkunft)
    if verzoegerung:
        await asyncio.sleep(verzoegerung)

    # Kein ``konto and``: die Abkuerzung war messbar billiger (4,8 ms gegen
    # 48 ms) und damit genau die fruehe Rueckkehr, die ``verify`` sonst
    # ueberall vermeidet.
    if await asyncio.to_thread(services.accounts.verify, konto, passwort):
        await asyncio.to_thread(services.throttle.record_success, konto, herkunft)
        # Neue Sitzungskennung nach der Anmeldung: sonst behaelt eine vor dem
        # Anmelden untergeschobene Kennung ihre Gueltigkeit (Sitzungsuebernahme).
        await rotate_session_id(request)
        merkmal = new_csrf_token()
        request.set_session({SESSION_ACCOUNT_KEY: konto, SESSION_CSRF_KEY: merkmal})
        # Die Herkunft als Ziel, nie das Passwort und nie der Kontoname als
        # Detailtext: der Audit-Strom hat weder die Rechtezusage aus R7a noch
        # die Trimmung des Verzeichnisses -- was hier hineingeraet, steht
        # dauerhaft und offen.
        await services.audit.record("login", account=konto, target=herkunft)
        antwort = Redirect("/", status_code=303)
        antwort.set_cookie(_merkmal_cookie(merkmal, secure=_sicher(request)))
        return antwort

    # R22: "der Betreiber sieht Fehlanmeldungen im Audit". Festgehalten wird
    # der **versuchte** Kontoname -- er ist die Spur, die den Angriff zeigt.
    await services.audit.record("login", account=konto, target=herkunft, outcome="abgewiesen")
    return Response(content=LOGIN_FAILED, media_type="text/plain", status_code=401)


@post("/logout")
async def logout(request: Request[Any, Any, Any]) -> Redirect:
    """Abmelden. Die Sitzung gilt danach sofort nicht mehr."""
    konto = request.session.get(SESSION_ACCOUNT_KEY) if request.session else None
    await request.app.state.services.audit.record(
        "logout", account=str(konto or "unbekannt"), target=_herkunft(request)
    )
    request.clear_session()
    antwort = Redirect("/login", status_code=303)
    # Das Merkmal mit fortnehmen: es gehoerte zur Sitzung und hat ohne sie
    # keinen Sinn -- auf einem geteilten Arbeitsplatz waere es sonst das
    # Merkmal des Vorgaengers.
    antwort.delete_cookie(CSRF_COOKIE, path="/")
    return antwort
