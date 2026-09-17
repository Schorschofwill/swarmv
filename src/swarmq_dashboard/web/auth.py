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

import sqlite3
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler
from litestar.stores.base import Store
from litestar.types import Empty

from swarmq_dashboard.accounts import BUSY_TIMEOUT_MS

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

Clock = Callable[[], float]


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

    # -- Litestars Vertrag ------------------------------------------------

    async def set(self, key: str, value: str | bytes, expires_in: int | timedelta | None = None) -> None:
        roh = value.encode() if isinstance(value, str) else value
        frist = _sekunden(expires_in)
        jetzt = self._clock()
        self._con.execute(
            "insert into session_store(key, value, expires_at, renewed_at) values (?,?,?,?) "
            "on conflict(key) do update set value=excluded.value, "
            "expires_at=excluded.expires_at, renewed_at=excluded.renewed_at",
            (key, roh, None if frist is None else jetzt + frist, jetzt),
        )
        self._con.commit()

    async def get(self, key: str, renew_for: int | timedelta | None = None) -> bytes | None:
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
            self._con.execute("delete from session_store where key=?", (key,))
            self._con.commit()
            return None

        frist = _sekunden(renew_for)
        if frist is not None and (jetzt - float(zeile["renewed_at"])) >= self._renew_every_s:
            # Verlaengern, aber hoechstens einmal je Frist: sonst ist jede
            # Anfrage ein Schreibvorgang und die Schreiber reihen sich auf.
            self._con.execute(
                "update session_store set expires_at=?, renewed_at=? where key=?",
                (jetzt + frist, jetzt, key),
            )
            self._con.commit()
        return bytes(zeile["value"])

    async def delete(self, key: str) -> None:
        self._con.execute("delete from session_store where key=?", (key,))
        self._con.commit()

    async def delete_all(self) -> None:
        self._con.execute("delete from session_store")
        self._con.commit()

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    async def expires_in(self, key: str) -> int | None:
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
        return float(min(DELAY_BASE_S * (2 ** (ueber - 1)), MAX_DELAY_S))

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
        return
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
    alt = connection.cookies.get("session")
    if alt:
        speicher = connection.app.stores.get("sessions")
        await speicher.delete(alt)
        # ``cookies`` und der Verbindungszustand teilen sich dasselbe
        # Woerterbuch -- die Aenderung erreicht darum auch die Verbindung,
        # die die Sitzungs-Middleware haelt.
        connection.cookies.pop("session", None)
    connection._connection_state.session_id = Empty
