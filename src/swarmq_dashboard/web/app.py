"""Dienstgeruest und Zustandsanzeige.

Ein Grundsatz traegt diese Datei: **kein Teil des Hochfahrens darf den Start
verhindern** (R33). Ein Dashboard, das nicht startet, weil der Broker fehlt,
ist genau in dem Vorfall weg, fuer den es gebaut wurde. Jeder Schritt wird
darum einzeln versucht, sein Fehlgrund festgehalten und von der Kopfzeile
ausgewiesen -- benannt, nicht als leere Flaeche.

Dasselbe gilt fuer die Rueckkehr: kommt ein Dienst wieder, muss die Anzeige
umspringen, ohne dass jemand das Dashboard neu startet. Deshalb ist die
Erreichbarkeit nichts, was einmal beim Start ermittelt wird, sondern etwas,
das ``refresh()`` im Takt nachfuehrt.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from litestar import Litestar, get
from litestar.config.csrf import CSRFConfig
from litestar.middleware.session.server_side import ServerSideSessionConfig
from litestar.stores.base import Store
from litestar.stores.registry import StoreRegistry

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.broker import BrokerObserver, BrokerUnavailable
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.directory.reader import StreamReader
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import TASK_STREAM
from swarmq_dashboard.stream import StreamUnavailable, ValkeyStreamClient
from swarmq_dashboard.web.auth import LoginThrottle, SqliteStore, session_guard
from swarmq_dashboard.web.routes.login import login, login_form, logout

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


def build_store_registry(accounts_file: Path) -> StoreRegistry:
    """Die Registratur mit genau einem angemeldeten Speicher."""

    def kein_ersatz(name: str) -> Store:
        raise UnknownStore(
            f"kein Speicher unter dem Namen {name!r} angemeldet -- ein stiller "
            "Rueckfall auf den Arbeitsspeicher wuerde die Sitzungen verlieren"
        )

    return StoreRegistry({SESSION_STORE_NAME: SqliteStore(accounts_file)},
                         default_factory=kein_ersatz)


def _teil(ok: bool, reason: str | None = None) -> dict[str, Any]:
    """Ein Teil des Zustandsberichts. ``reason`` ist bei ``ok`` immer leer."""
    return {"ok": ok, "reason": None if ok else (reason or "nicht erreichbar")}


class Services:
    """Haelt die Aussenverbindungen des Dienstes und ihren Zustand.

    Bewusst kein Litestar-Plugin: der Lebenszyklus ist hier die einfachere
    Sache, und diese Klasse laesst sich ohne Anwendung pruefen.
    """

    def __init__(self, config: DashboardConfig, *, hasher: PasswordHasher | None = None) -> None:
        self._config = config
        self._hasher = hasher
        self.accounts: AccountStore | None = None
        self.throttle: LoginThrottle | None = None
        self._stream_url = config.stream_url
        self._backend_url = config.backend_url
        self._broker_url = config.broker_url

        self.reader: StreamReader | None = None
        self.services_directory_writer: DirectoryWriter | None = None
        self._stream: ValkeyStreamClient | None = None
        self._backend: ValkeyStreamClient | None = None
        self._broker: BrokerObserver | None = None

        self._gruende: dict[str, str | None] = {
            "directory": "noch nicht geoeffnet",
            "stream": "noch nicht verbunden",
            "backend": "noch nicht verbunden",
            "broker": "noch nicht verbunden",
        }

    # -- Lebenszyklus ----------------------------------------------------

    async def startup(self) -> None:
        """Alles hochfahren, was geht. Wirft nie.

        Idempotent: ein zweiter Aufruf baut nichts doppelt auf. Das ist keine
        Bequemlichkeit -- ein Lebenszyklus, der beim zweiten Durchlauf wirft,
        macht aus einem Neustartversuch einen endgueltigen Ausfall.
        """
        self._verzeichnis_oeffnen()
        self._konten_oeffnen()
        await self.refresh()

    def _konten_oeffnen(self) -> None:
        """Konten und Bremse. Beide in derselben eigenen Datei (KTD8) -- sie
        haengen an keinem Netzdienst und muessen darum auch dann stehen, wenn
        sonst nichts erreichbar ist.
        """
        if self.accounts is not None:
            return
        self.accounts = AccountStore(self._config.accounts_file, hasher=self._hasher)
        self.throttle = LoginThrottle(self._config.accounts_file)

    def _verzeichnis_oeffnen(self) -> None:
        if self.services_directory_writer is not None:
            return
        try:
            con = connect(self._config.directory_file)
            self.services_directory_writer = DirectoryWriter(con)
            self.reader = StreamReader(
                _NochNichtVerbunden(), self.services_directory_writer, stream_key=TASK_STREAM
            )
            self._gruende["directory"] = None
        except Exception as exc:
            # Ohne Verzeichnis ist das Dashboard fast nutzlos -- aber "fast"
            # ist nicht "ganz": die Kopfzeile kann immer noch sagen, was fehlt.
            self._gruende["directory"] = str(exc)

    async def refresh(self) -> None:
        """Erreichbarkeit neu ermitteln. Wirft nie.

        Im Takt gefahren, nicht nur beim Start: sonst bliebe die Anzeige nach
        der Rueckkehr eines Dienstes auf "nicht erreichbar" stehen, und der
        Betreiber startete das Dashboard neu, weil es faelschlich "weg" sagt.
        """
        self._stream, self._gruende["stream"] = await self._valkey(self._stream, self._stream_url)
        self._backend, self._gruende["backend"] = await self._valkey(
            self._backend, self._backend_url
        )
        if self.reader is not None and self._stream is not None:
            self.reader._source = self._stream
        await self._broker_pruefen()

    async def _valkey(
        self, vorhanden: ValkeyStreamClient | None, url: str
    ) -> tuple[ValkeyStreamClient | None, str | None]:
        client = vorhanden if vorhanden is not None else ValkeyStreamClient(url)
        try:
            await client.connect()
        except StreamUnavailable as exc:
            await _leise_schliessen(client)
            return None, str(exc)
        return client, None

    async def _broker_pruefen(self) -> None:
        if self._broker is None:
            self._broker = BrokerObserver(self._broker_url)
        try:
            await self._broker.connect()
            self._gruende["broker"] = None
        except BrokerUnavailable as exc:
            self._gruende["broker"] = str(exc)

    async def shutdown(self) -> None:
        """Alles schliessen, auch das, was nie hochkam. Wirft nie."""
        for teil in (self._stream, self._backend, self._broker):
            await _leise_schliessen(teil)
        self._stream = self._backend = None
        self._broker = None
        if self.services_directory_writer is not None:
            try:
                self.services_directory_writer.close()
            except Exception:
                pass
            self.services_directory_writer = None
            self.reader = None

    # -- Zustand ---------------------------------------------------------

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        """Der Bericht fuer die Kopfzeile (R9, R26, R33)."""
        jetzt = time.time() if now is None else now
        juengstes = (
            self.services_directory_writer.newest_entry()
            if self.services_directory_writer is not None
            else None
        )
        return {
            "broker": _teil(self._gruende["broker"] is None, self._gruende["broker"]),
            "backend": _teil(self._gruende["backend"] is None, self._gruende["backend"]),
            "stream": {
                **_teil(self._gruende["stream"] is None, self._gruende["stream"]),
                "last_event_at": juengstes,
                # Noch nie ein Signal gesehen heisst **nicht** "aktuell". Ein
                # stehender Strom laese sich sonst als ruhiges System lesen.
                "stale": juengstes is None or (jetzt - juengstes) > self._config.stream_stale_after_s,
                "stale_after_s": self._config.stream_stale_after_s,
                # Die angezeigte Grenze ist die wirksame (R9): eine Zahl aus
                # einer Einstellung, die niemand anwendet, waere schlimmer als
                # gar keine.
                "retention_days": self._config.retention_days,
            },
            "directory": self._verzeichnis_bericht(),
        }

    def _verzeichnis_bericht(self) -> dict[str, Any]:
        if self.reader is None:
            return {"ok": False, "reason": self._gruende["directory"], "rebuilding": True}
        bericht = self.reader.status()
        bericht["ok"] = True
        bericht["reason"] = None
        return bericht


class _NochNichtVerbunden:
    """Ein Strom, der nichts liefert, solange keine Verbindung steht.

    Damit laeuft der Leser auch ohne Valkey, statt dass der Dienst ihn gar
    nicht erst bekommt -- und die Kopfzeile kann sagen, dass der Strom fehlt,
    statt zu schweigen.
    """

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        raise StreamUnavailable("der Ereignisstrom ist nicht verbunden")

    async def min_id(self, key: str) -> str | None:
        raise StreamUnavailable("der Ereignisstrom ist nicht verbunden")

    async def trim_by_min_id(self, key: str, min_ms: int) -> int:
        raise StreamUnavailable("der Ereignisstrom ist nicht verbunden")


async def _leise_schliessen(teil: object) -> None:
    schliessen = getattr(teil, "close", None)
    if schliessen is None:
        return
    try:
        await schliessen()
    except Exception:
        # Ein Teil, das sich nicht schliessen laesst, ist bereits fort. Daran
        # darf kein Herunterfahren haengen.
        pass


def create_app(config: DashboardConfig, *, hasher: PasswordHasher | None = None) -> Litestar:
    """Die Anwendung bauen. Der Lebenszyklus haelt die Aussenverbindungen."""
    services = Services(config, hasher=hasher)

    @asynccontextmanager
    async def lebenszyklus(app: Litestar) -> AsyncGenerator[None]:
        await services.startup()
        app.state.services = services
        try:
            yield
        finally:
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
        secure=True,
        httponly=True,
        samesite="strict",
    )
    # Das CSRF-Cookie ist absichtlich **nicht** httponly: die Fragmentaufrufe
    # der Oberflaeche lesen es und schicken es als Kopfzeile zurueck. Sein
    # Schutz liegt in der Seitenbindung, nicht in der Unlesbarkeit.
    csrf = CSRFConfig(
        secret=_csrf_secret(config.accounts_file),
        cookie_secure=True,
        cookie_samesite="strict",
        safe_methods={"GET", "HEAD", "OPTIONS"},
    )
    return Litestar(
        route_handlers=[zustand, login_form, login, logout],
        lifespan=[lebenszyklus],
        stores=build_store_registry(config.accounts_file),
        middleware=[sitzung.middleware],
        csrf_config=csrf,
        guards=[session_guard],
    )


def _csrf_secret(accounts_file: Path) -> str:
    """Das CSRF-Geheimnis, dauerhaft in der Kontendatei.

    Ein bei jedem Start neu gewuerfeltes Geheimnis entwertet jedes offene
    Formular -- nach einem Neustart scheiterte die naechste Aktion jedes
    Teammitglieds mit "Merkmal ungueltig", und niemand wuesste warum.
    """
    store = AccountStore(accounts_file)
    try:
        con = store._con
        con.execute("create table if not exists meta (key text primary key, value text) without rowid")
        zeile = con.execute("select value from meta where key='csrf_secret'").fetchone()
        if zeile is not None:
            return str(zeile["value"])
        wert = secrets.token_urlsafe(32)
        con.execute("insert into meta(key, value) values ('csrf_secret', ?)", (wert,))
        con.commit()
        return wert
    finally:
        store.close()
