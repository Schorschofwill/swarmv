"""Sitzungen und Anmeldebremse -- beide in der Kontendatei.

**Warum nicht in Valkey** (KTD8): Die Stack-Entscheidung sah zunaechst den
Valkey-Sitzungsspeicher vor, weil er Ablaufzeit und Aufraeumen geschenkt
mitbringt. Dagegen steht der Zweck des Werkzeugs -- liegt Valkey, kaeme
niemand mehr herein, und das ist genau der Vorfall, fuer den das Dashboard
existiert. Dasselbe gilt fuer die Bremszaehler: laegen sie dort, verschwaende
die Bremse bei demselben Ausfall, fuer den die Anmeldung erreichbar bleiben
soll.

**Warum die Bremse nie sperrt** (R22 ausdruecklich): Eine Sperre nach n
Fehlversuchen ist eine Waffe, die der Angreifer fuehrt -- er meldet fremde
Konten falsch an und sperrt das Team aus. Die Verzoegerung waechst deshalb
bis zu einer Obergrenze und bleibt dort.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException
from litestar.handlers.base import BaseRouteHandler
from litestar.stores.base import Store
from litestar.types import Empty

from swarmq_dashboard.accounts import BUSY_TIMEOUT_MS, MAX_NAME_LEN

#: Ab dem wievielten Fehlversuch ueberhaupt gebremst wird. Ein Vertipper ist
#: keine Verdachtslage, und eine Bremse ab dem ersten Versuch traefe fast nur
#: Unschuldige.
FREE_ATTEMPTS = 2
#: Faktor, um den die Verzoegerung je weiterem Fehlversuch waechst.
DELAY_BASE_S = 0.25
#: Obergrenze. Sie ist der Unterschied zwischen einer Bremse und einer Sperre.
MAX_DELAY_S = 8.0
#: Nach dieser Ruhezeit verfaellt ein Zaehler.
ATTEMPT_TTL_S = 900.0
#: Wie oft eine Sitzung hoechstens verlaengert wird. Ohne diese Frist ist jede
#: Anfrage ein Schreibvorgang, und bei einer offenen Live-Verbindung waere das
#: jede Sekunde einer.
DEFAULT_RENEW_EVERY_S = 60.0

#: Unter welchem Schluessel der Kontoname in der Sitzung steht.
SESSION_ACCOUNT_KEY = "account"
#: Unter welchem Schluessel das CSRF-Merkmal in der Sitzung steht.
SESSION_CSRF_KEY = "csrf"
#: Name des Cookies, ueber das die Oberflaeche das Merkmal liest.
CSRF_COOKIE = "csrftoken"
#: Kopfzeile, ueber die Fragmentaufrufe es zurueckschicken.
CSRF_HEADER = "x-csrftoken"
#: Formularfeld fuer gewoehnliche Absendungen.
CSRF_FIELD = "_csrf_token"
#: Methoden ohne Schreibwirkung -- sie brauchen kein Merkmal.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

Clock = Callable[[], float]


#: Wie eine vom Rahmen erzeugte Sitzungskennung aussieht: 32 Bytes als Hex.
_KENNUNG = re.compile(r"^[0-9a-f]{64}$")
#: Serialisierte leere Sitzungen -- msgspec und json schreiben dasselbe.
_LEERE_SITZUNG = (b"{}", b"")


def _sekunden(wert: int | timedelta | None) -> float | None:
    if wert is None:
        return None
    return wert.total_seconds() if isinstance(wert, timedelta) else float(wert)


def _connect(pfad: Path) -> sqlite3.Connection:
    """Dieselbe Datei wie die Konten, eigene Verbindung.

    Eigene Verbindung, nicht die des Kontenspeichers: die beiden werden aus
    verschiedenen Faden benutzt, und eine geteilte Verbindung waere die
    Stelle, an der sie sich in die Quere kommen.
    """
    con = sqlite3.connect(pfad, isolation_level=None, check_same_thread=False)
    con.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
    con.row_factory = sqlite3.Row
    return con


class SqliteStore(Store):
    """Litestar-Sitzungsspeicher auf der Kontendatei."""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock = time.time,
        renew_every_s: float = DEFAULT_RENEW_EVERY_S,
    ) -> None:
        self._con = _connect(Path(path))
        self._clock = clock
        self._renew_every_s = renew_every_s
        # Wie im Kontenspeicher: die Verbindung ist fadenoffen, also braucht
        # sie einen Riegel. Ohne ihn brachen unter sechzehn Faden fuenfzehn
        # mit "bad parameter or other API misuse" ab und der Zaehlerstand war
        # danach falsch -- eine Bremse, die still nicht zaehlt.
        self._riegel = threading.Lock()

    # -- Litestars Vertrag ------------------------------------------------

    async def set(self, key: str, value: str | bytes, expires_in: int | timedelta | None = None) -> None:
        roh_wert = value.encode() if isinstance(value, str) else value
        if roh_wert in _LEERE_SITZUNG:
            # Eine leere Sitzung ist keine Zeile wert. Der Rahmen legt bei
            # **jeder** Anfrage ``scope["session"] = {}`` an, und ``{}`` ist
            # nicht ``Empty`` -- also schrieb er auch auf der 401 des
            # Waechters. Gemessen: 25 unangemeldete Anfragen, 25 Zeilen, in
            # der einen Datei, die laut KTD8 nicht geloescht werden darf.
            return
        if not _KENNUNG.match(key):
            # Nur selbst erzeugte Kennungen. Der Schluessel kommt aus dem
            # Cookie des Klienten und war damit fremdbestimmt -- gemessen
            # landeten Schluessel mit zweitausend Zeichen in der Kontendatei,
            # und zwar von *unangemeldeten* Anfragen. Betroffen ist die eine
            # Datei, die laut KTD8 nicht geloescht werden darf.
            return
        roh = roh_wert
        frist = _sekunden(expires_in)
        jetzt = self._clock()
        with self._riegel:
            vorher = self._con.execute(
                "select value, renewed_at from session_store where key=?", (key,)
            ).fetchone()
            if (
                vorher is not None
                and bytes(vorher["value"]) == roh
                and (jetzt - float(vorher["renewed_at"])) < self._renew_every_s
            ):
                # Unveraendert und eben erst verlaengert: nichts tun.
                #
                # Die Drossel gehoert **hierher**, nicht in ``get``: der Rahmen
                # ruft auf jeder Antwort ``set`` auf, und diese Zeile schrieb
                # bedingungslos. Gemessen wanderte ``renewed_at`` in 0,4 s um
                # 0,42 s -- jede Anfrage war ein Schreibvorgang, entgegen der
                # ausdruecklichen Zusage in U17 ("hoechstens einmal je Minute,
                # sonst reihen sich die Schreiber auf").
                return
            self._con.execute(
                "insert into session_store(key, value, expires_at, renewed_at) values (?,?,?,?) "
                "on conflict(key) do update set value=excluded.value, "
                "expires_at=excluded.expires_at, renewed_at=excluded.renewed_at",
                (key, roh, None if frist is None else jetzt + frist, jetzt),
            )
            self._con.commit()

    async def get(self, key: str, renew_for: int | timedelta | None = None) -> bytes | None:
        with self._riegel:
            zeile = self._con.execute(
                "select value, expires_at, renewed_at from session_store where key=?", (key,)
            ).fetchone()
        if zeile is None:
            return None
        jetzt = self._clock()
        if zeile["expires_at"] is not None and float(zeile["expires_at"]) <= jetzt:
            # Abgelaufen. Nicht ausliefern und gleich wegraeumen -- eine
            # abgelaufene Sitzung auszuliefern hiesse, die Inaktivitaetsfrist
            # aus R21 unsichtbar nicht einzuhalten.
            with self._riegel:
                self._con.execute("delete from session_store where key=?", (key,))
                self._con.commit()
            return None

        frist = _sekunden(renew_for)
        if frist is not None and (jetzt - float(zeile["renewed_at"])) >= self._renew_every_s:
            # Verlaengern, aber hoechstens einmal je Frist: sonst ist jede
            # Anfrage ein Schreibvorgang und die Schreiber reihen sich auf.
            with self._riegel:
                self._con.execute(
                    "update session_store set expires_at=?, renewed_at=? where key=?",
                    (jetzt + frist, jetzt, key),
                )
                self._con.commit()
        return bytes(zeile["value"])

    async def delete(self, key: str) -> None:
        with self._riegel:
            self._con.execute("delete from session_store where key=?", (key,))
            self._con.commit()

    async def delete_all(self) -> None:
        with self._riegel:
            self._con.execute("delete from session_store")
            self._con.commit()

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    async def expires_in(self, key: str) -> int | None:
        with self._riegel:
            zeile = self._con.execute(
                "select expires_at from session_store where key=?", (key,)
            ).fetchone()
        if zeile is None or zeile["expires_at"] is None:
            return None
        return max(0, int(float(zeile["expires_at"]) - self._clock()))

    # -- Pflege ------------------------------------------------------------

    async def purge(self) -> int:
        """Abgelaufene Zeilen entfernen. Gibt die Zahl zurueck.

        Noetig, weil eine Sitzung, die nie wieder abgerufen wird, sonst fuer
        immer stehen bleibt -- der Ablauf allein raeumt nur auf, was jemand
        noch einmal anfasst.
        """
        with self._riegel:
            cur = self._con.execute(
                "delete from session_store where expires_at is not null and expires_at <= ?",
                (self._clock(),),
            )
            self._con.commit()
        return int(cur.rowcount)

    def close(self) -> None:
        """Verbindung schliessen."""
        self._con.close()


class LoginThrottle:
    """Wachsende Verzoegerung je Konto **und** je Herkunft (R22).

    Beide Zaehler, nicht einer: ohne die Herkunft probiert derselbe Rechner
    einfach das naechste Konto durch, ohne das Konto kommt derselbe Angriff
    von der naechsten Adresse. Die groessere der beiden Verzoegerungen gilt.
    """

    def __init__(self, path: str | Path, *, clock: Clock = time.time) -> None:
        self._con = _connect(Path(path))
        self._clock = clock

    def close(self) -> None:
        self._con.close()

    # -- Lesen -----------------------------------------------------------

    def delay_for(self, account: str, origin: str) -> float:
        """Wie lange dieser Versuch warten muss, in Sekunden."""
        return max(
            self._delay("account", account),
            self._delay("origin", origin),
        )

    def _delay(self, scope: str, key: str) -> float:
        zeile = self._con.execute(
            "select count, last_at from login_attempt where scope=? and key=?", (scope, key)
        ).fetchone()
        if zeile is None:
            return 0.0
        if (self._clock() - float(zeile["last_at"])) > ATTEMPT_TTL_S:
            return 0.0
        ueber = int(zeile["count"]) - FREE_ATTEMPTS
        if ueber <= 0:
            return 0.0
        # Verdopplung je Fehlversuch, gedeckelt. Der Deckel ist der
        # Unterschied zwischen einer Bremse und einer Sperre (R22).
        #
        # Der **Exponent** wird zuerst gedeckelt, nicht erst das Ergebnis:
        # ``2 ** 1024`` passt nicht mehr in ein ``float``, und der
        # ``OverflowError`` traf den Handler vor der Passwortpruefung. Ab 1027
        # Fehlversuchen war die Anmeldung damit ein HTTP 500 -- auch mit dem
        # richtigen Passwort, und fuer die ganze Herkunft. Ausgerechnet die
        # Zeile, die "nie eine Sperre" sichern soll, war die Sperre.
        return float(min(DELAY_BASE_S * (2 ** min(ueber - 1, 40)), MAX_DELAY_S))

    # -- Schreiben -------------------------------------------------------

    def record_failure(self, account: str, origin: str) -> None:
        """Einen Fehlversuch zaehlen -- fuer Konto und Herkunft.

        Auch fuer ein Konto, das es gar nicht gibt: braemste nur ein
        bekanntes, verriete die Bremse, welche Konten es gibt -- dieselbe
        Aufzaehlung, die die Pruefung in ``accounts`` verhindert (AE9a).
        """
        jetzt = self._clock()
        for scope, key in (("account", account), ("origin", origin)):
            self._con.execute(
                "insert into login_attempt(scope, key, count, last_at) values (?,?,1,?) "
                "on conflict(scope, key) do update set "
                # Ein verfallener Zaehler beginnt von vorn statt weiterzulaufen.
                "count = case when ? - login_attempt.last_at > ? then 1 "
                "else login_attempt.count + 1 end, last_at = ?",
                (scope, key, jetzt, jetzt, ATTEMPT_TTL_S, jetzt),
            )
        self._con.commit()

    def record_success(self, account: str, origin: str) -> None:
        """Nach gelungener Anmeldung beide Zaehler loeschen."""
        self._con.execute(
            "delete from login_attempt where (scope='account' and key=?) "
            "or (scope='origin' and key=?)",
            (account, origin),
        )
        self._con.commit()

    def purge(self) -> int:
        """Verfallene Zaehler entfernen."""
        cur = self._con.execute(
            "delete from login_attempt where ? - last_at > ?", (self._clock(), ATTEMPT_TTL_S)
        )
        self._con.commit()
        return int(cur.rowcount)


def session_guard(connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler) -> None:
    """Ohne gueltige Sitzung ist nichts erreichbar ausser der Anmeldung (R23).

    Der Kontozustand wird bei **jeder** Anfrage nachgesehen, nicht nur beim
    Anmelden. Das ist die Zusage aus R21, dass das Sperren eines Kontos seine
    laufenden Sitzungen beendet: eine Sperre, die erst bei der naechsten
    Anmeldung wirkt, ist keine -- wer schon drin ist, bliebe drin. Der Preis
    ist eine Abfrage auf einen Primaerschluessel je Anfrage.
    """
    if connection.route_handler.opt.get("public"):
        return
    konto = connection.session.get(SESSION_ACCOUNT_KEY) if connection.session else None
    if not isinstance(konto, str) or not konto:
        raise NotAuthorizedException(detail="keine gueltige Sitzung")

    services = getattr(connection.app.state, "services", None)
    speicher = getattr(services, "accounts", None)
    if speicher is None:
        # Wer den Kontozustand nicht nachsehen kann, darf ihn nicht raten.
        # Frueher stand hier ``return`` -- die Pruefung fiel dann ersatzlos
        # weg, und ein gesperrtes Konto kam durch.
        raise NotAuthorizedException(detail="der Kontenspeicher ist nicht verfuegbar")
    zeile = speicher.state_of(konto)
    if zeile is None or zeile.locked:
        # Konto geloescht oder gesperrt: die Sitzung endet hier, nicht erst
        # beim naechsten Anmelden.
        connection.clear_session()
        raise NotAuthorizedException(detail="das Konto ist gesperrt oder unbekannt")


async def rotate_session_id(connection: ASGIConnection[Any, Any, Any, Any]) -> None:
    """Der Anmeldung eine **neue** Sitzungskennung geben.

    Gegen Sitzungsuebernahme: wer dem Browser vorher ein ``session``-Cookie
    unterschiebt -- ueber eine Nachbardomaene oder eine frueher ausgenutzte
    Luecke --, kennt die Kennung, unter der das Opfer danach angemeldet ist.
    Nachgemessen: ohne diese Erneuerung blieb eine untergeschobene Kennung
    nach dem Anmelden unveraendert gueltig.

    Der Rahmen bietet in 2.24 keinen Weg dafuer an. Seine Kennung kommt in
    dieser Reihenfolge zustande: Cookie, dann Verbindungszustand, dann neu
    gewuerfelt (``ServerSideSessionBackend.get_session_id``). Beide ersten
    Quellen werden hier geleert, damit die dritte greift. Das greift in
    Interna -- deshalb gibt es einen Test, der anschlaegt, sobald der Rahmen
    das anders macht, statt dass die Erneuerung still ausfaellt.
    """
    # Aus der **rohen** Kopfzeile, nicht aus dem Woerterbuch: bei zwei
    # gleichnamigen Cookies -- ein Wirt-Cookie und eines einer Nachbardomaene
    # sind zwei verschiedene -- behaelt das Woerterbuch nur das letzte, und
    # die Erneuerung entwertete dann nur eines von beiden.
    speicher = connection.app.stores.get("sessions")
    for kennung in _alle_sitzungskennungen(connection):
        await speicher.delete(kennung)
    # ``cookies`` und der Verbindungszustand teilen sich dasselbe Woerterbuch
    # -- die Aenderung erreicht darum auch die Verbindung, die die
    # Sitzungs-Middleware haelt.
    connection.cookies.pop("session", None)
    connection._connection_state.session_id = Empty


def _alle_sitzungskennungen(connection: ASGIConnection[Any, Any, Any, Any]) -> set[str]:
    """Jede ``session``-Kennung aus der rohen Cookie-Kopfzeile."""
    roh = connection.headers.get("cookie", "")
    gefunden = set()
    for stueck in roh.split(";"):
        name, _, wert = stueck.strip().partition("=")
        if name == "session" and wert:
            gefunden.add(unquote(unquote(wert.strip('"'))))
    return gefunden


def normalisierter_kontoname(roh: str) -> str:
    """Kontoname, wie ihn die Kontenpruefung sieht -- und gekuerzt.

    Zwei Gruende. Erstens normalisiert ``AccountStore.verify`` nach NFC, die
    Bremse tat es nicht: dieselbe Anmeldung bekam je Schreibweise einen
    eigenen Zaehler, und der Kontoteil der Bremse halbierte sich. Zweitens
    wurde der rohe Formularwert als Zeilenschluessel gespeichert -- gemessen
    fuellte ein Angreifer damit 0,72 MiB je Sekunde in die Kontendatei, die
    laut KTD8 nicht geloescht werden darf.
    """
    return unicodedata.normalize("NFC", roh).strip()[:MAX_NAME_LEN]


def new_csrf_token() -> str:
    """Ein frisches CSRF-Merkmal."""
    return secrets.token_urlsafe(32)


def csrf_guard(connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler) -> None:
    """Schreibende Anfragen brauchen ein **sitzungsgebundenes** Merkmal (R23).

    Warum nicht die CSRF-Schicht des Rahmens: sie prueft doppeltes Absenden
    mit HMAC -- also nur, dass Cookie und Kopfzeile dieselbe, irgendwann von
    diesem Dienst signierte Zeichenfolge tragen. Ein Bezug zur Sitzung besteht
    nicht. Nachgemessen wurde ein Merkmal, das sich ein **nie angemeldeter**
    Klient mit einem einfachen ``GET /login`` abholt, in einer fremden,
    angemeldeten Sitzung angenommen. R23 verlangt aber ausdruecklich ein
    sitzungsgebundenes Merkmal.

    Hier steht das Merkmal **in der Sitzung**. Der Vergleich ist damit
    zugleich doppeltes Absenden und Bindung: wer ein Cookie unterschiebt,
    kann den Wert in der fremden Sitzung nicht treffen.
    """
    # ``scope["method"]`` statt ``connection.method``: die Verbindung kann
    # auch eine Websocket-Verbindung sein, die keine Methode kennt.
    methode = str(connection.scope.get("method", "GET")).upper()
    if methode in SAFE_METHODS or connection.route_handler.opt.get("no_csrf"):
        return
    erwartet = connection.session.get(SESSION_CSRF_KEY) if connection.session else None
    gesendet = connection.headers.get(CSRF_HEADER) or connection.cookies.get(CSRF_COOKIE)
    if (
        not isinstance(erwartet, str)
        or not gesendet
        or not secrets.compare_digest(erwartet, gesendet)
    ):
        raise PermissionDeniedException(detail="CSRF-Merkmal fehlt oder passt nicht zur Sitzung")
