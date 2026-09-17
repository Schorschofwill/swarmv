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

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from litestar import Litestar, get
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
from swarmq_dashboard.web.auth import (
    DELAY_BASE_S,
    MAX_DELAY_S,
    LoginThrottle,
    SqliteStore,
    csrf_guard,
    session_guard,
)
from swarmq_dashboard.web.routes.login import login, login_form, logout

#: Name, unter dem der Sitzungsspeicher registriert wird.
SESSION_STORE_NAME = "sessions"
#: Takt der Pflege: Erreichbarkeit nachfuehren, Abgelaufenes wegraeumen.
#: Grosszuegig -- die Rueckkehr eines Dienstes darf ein paar Sekunden spaeter
#: sichtbar werden, aber nicht erst nach einem Neustart.
DEFAULT_MAINTENANCE_S = 10.0


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
    def _echt(self) -> SqliteStore:
        speicher = self._services._sitzungsspeicher
        if speicher is None:
            raise RuntimeError("der Sitzungsspeicher ist nicht geoeffnet")
        return speicher

    async def set(self, key: str, value: str | bytes, expires_in: object = None) -> None:
        await self._echt.set(key, value, expires_in)  # type: ignore[arg-type]

    async def get(self, key: str, renew_for: object = None) -> bytes | None:
        return await self._echt.get(key, renew_for)  # type: ignore[arg-type]

    async def delete(self, key: str) -> None:
        await self._echt.delete(key)

    async def delete_all(self) -> None:
        await self._echt.delete_all()

    async def exists(self, key: str) -> bool:
        return await self._echt.exists(key)

    async def expires_in(self, key: str) -> int | None:
        return await self._echt.expires_in(key)


def build_store_registry(accounts_file: Path, store: Store | None = None) -> StoreRegistry:
    """Die Registratur mit genau einem angemeldeten Speicher.

    Legt die Kontendatei zuvor an: ``auth._connect`` oeffnet nur, es stellt
    kein Schema. Frueher ging das nur gut, weil zufaellig eine Zeile darueber
    den Kontenspeicher anfasste -- wer die beiden Zeilen tauscht, braeche jede
    Neuinstallation. Die Abhaengigkeit steht jetzt da, wo sie gilt.
    """
    AccountStore(accounts_file).close()

    def kein_ersatz(name: str) -> Store:
        raise UnknownStore(
            f"kein Speicher unter dem Namen {name!r} angemeldet -- ein stiller "
            "Rueckfall auf den Arbeitsspeicher wuerde die Sitzungen verlieren"
        )

    return StoreRegistry({SESSION_STORE_NAME: store or SqliteStore(accounts_file)},
                         default_factory=kein_ersatz)


def _teil(ok: bool, reason: str | None = None) -> dict[str, Any]:
    """Ein Teil des Zustandsberichts. ``reason`` ist bei ``ok`` immer leer."""
    return {"ok": ok, "reason": None if ok else (reason or "nicht erreichbar")}


class Services:
    """Haelt die Aussenverbindungen des Dienstes und ihren Zustand.

    Bewusst kein Litestar-Plugin: der Lebenszyklus ist hier die einfachere
    Sache, und diese Klasse laesst sich ohne Anwendung pruefen.
    """

    def __init__(
        self,
        config: DashboardConfig,
        *,
        hasher: PasswordHasher | None = None,
        throttle_scale: float = 1.0,
    ) -> None:
        self._config = config
        self._hasher = hasher
        #: Massstab der Anmeldebremse. Nur fuer die Testreihe kleiner als 1 --
        #: sie prueft die Bremse ueber echte Anfragen und bezahlt die
        #: Wartezeit sonst in Sekunden Laufzeit.
        self._throttle_scale = throttle_scale
        self.accounts: AccountStore | None = None
        self.throttle: LoginThrottle | None = None
        #: Der Speicher aus der Registratur -- eine vierte Verbindung auf die
        #: Kontendatei, die niemand schloss.
        self._sitzungsspeicher: SqliteStore | None = None
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
            "accounts": "noch nicht geoeffnet",
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

        Gekapselt wie jeder andere Startschritt: das war der einzige, der es
        nicht war, und eine unlesbare Kontendatei -- aus einer Ruecksicherung,
        von einem ``ExecStartPre`` -- liess ``startup()`` werfen. Der Dienst
        kam dann gar nicht hoch und konnte auch nicht sagen, woran es liegt.
        Ohne Konten meldet sich niemand an; die Kopfzeile sagt aber, warum.
        """
        if self.accounts is not None:
            return
        try:
            self.accounts = AccountStore(self._config.accounts_file, hasher=self._hasher)
            self.throttle = LoginThrottle(
                self._config.accounts_file,
                base_delay_s=DELAY_BASE_S * self._throttle_scale,
                max_delay_s=MAX_DELAY_S * self._throttle_scale,
            )
            self._sitzungsspeicher = SqliteStore(self._config.accounts_file)
            self._gruende["accounts"] = None
        except Exception as exc:
            self._gruende["accounts"] = str(exc)

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
        # Auch die drei Verbindungen auf die Kontendatei. Gemessen blieben
        # ohne das sechs Dateizeiger offen, und ueber fuenf Lebenszyklen
        # wuchsen sie linear auf dreissig -- und das Herunterfahren ist
        # ohnehin nicht abgeschlossen, solange die letzten Schreibvorgaenge
        # nicht zusammengefuehrt sind.
        for lokal in (self.accounts, self.throttle, self._sitzungsspeicher):
            if lokal is not None:
                try:
                    lokal.close()
                except Exception:
                    pass
        self.accounts = None
        self.throttle = None
        self._sitzungsspeicher = None

    def start_maintenance(self, *, every_s: float = DEFAULT_MAINTENANCE_S) -> asyncio.Task[None]:
        """Die Taktaufgabe starten: Erreichbarkeit nachfuehren und aufraeumen.

        Zwei Dinge in einem Takt, weil beide dieselbe Begruendung haben --
        etwas, das der Dienst von sich aus tun muss, weil es sonst niemand tut:

        * ``refresh()`` rief ausser ``startup()`` **niemand**. Der Modulkopf
          verspricht das Gegenteil, und ohne den Takt bleibt die Anzeige nach
          der Rueckkehr eines Dienstes dauerhaft auf "nicht erreichbar" --
          worauf der Betreiber das Dashboard neu startet, also genau das tut,
          was der Absatz verhindern wollte.
        * ``purge()`` hatte ueberhaupt keinen Aufrufer. Hochgerechnet 673 MiB
          in einer Woche, in der Datei, die laut KTD8 nicht geloescht werden
          darf.
        """

        async def takt() -> None:
            while True:
                await asyncio.sleep(every_s)
                try:
                    await self.refresh()
                    await self._aufraeumen()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Ein Fehler im Takt darf den Takt nicht beenden -- sonst
                    # steht die Anzeige nach der ersten Stoerung fuer immer.
                    continue

        return asyncio.ensure_future(takt())

    async def _aufraeumen(self) -> None:
        """Abgelaufene Sitzungen und verfallene Bremszaehler entfernen."""
        if self.throttle is not None:
            await asyncio.to_thread(self.throttle.purge)
        if self._sitzungsspeicher is not None:
            await self._sitzungsspeicher.purge()

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
            "accounts": _teil(self._gruende["accounts"] is None, self._gruende["accounts"]),
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


def create_app(
    config: DashboardConfig,
    *,
    hasher: PasswordHasher | None = None,
    maintenance_every_s: float = DEFAULT_MAINTENANCE_S,
    throttle_scale: float = 1.0,
) -> Litestar:
    """Die Anwendung bauen. Der Lebenszyklus haelt die Aussenverbindungen.

    ``maintenance_every_s`` ist die Naht fuer die Testreihe: ohne sie prueft
    kein Test, dass der Lebenszyklus den Takt ueberhaupt **startet** -- und
    genau das war der Fehler bei ``refresh()``, das nur von Hand gerufen wurde.
    """
    services = Services(config, hasher=hasher, throttle_scale=throttle_scale)
    # Die Registratur zeigt auf den Speicher des Dienstes, statt einen
    # eigenen zu halten: sonst bliebe eine vierte Verbindung auf die
    # Kontendatei offen, die niemand schliesst und deren abgelaufene Zeilen
    # niemand wegraeumt.
    weiche = _SitzungsWeiche(services)

    @asynccontextmanager
    async def lebenszyklus(app: Litestar) -> AsyncGenerator[None]:
        await services.startup()
        app.state.services = services
        takt = services.start_maintenance(every_s=maintenance_every_s)
        try:
            yield
        finally:
            takt.cancel()
            with suppress(asyncio.CancelledError):
                await takt
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
    # **Kein** ``csrf_config``: die CSRF-Schicht des Rahmens prueft doppeltes
    # Absenden mit HMAC, also nur, dass Cookie und Kopfzeile dieselbe,
    # irgendwann von diesem Dienst signierte Zeichenfolge tragen. Ein Bezug
    # zur Sitzung besteht nicht -- nachgemessen wurde ein Merkmal, das sich
    # ein nie angemeldeter Klient abholt, in einer fremden angemeldeten
    # Sitzung angenommen. R23 verlangt aber ein **sitzungsgebundenes**
    # Merkmal. ``csrf_guard`` liefert das; es steht in der Sitzung selbst.
    return Litestar(
        route_handlers=[zustand, login_form, login, logout],
        lifespan=[lebenszyklus],
        stores=build_store_registry(config.accounts_file, weiche),
        middleware=[sitzung.middleware],
        guards=[session_guard, csrf_guard],
    )
