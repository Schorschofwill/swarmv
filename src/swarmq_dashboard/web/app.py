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
from typing import Any

from argon2 import PasswordHasher
from litestar import Litestar, get
from litestar.channels import ChannelsPlugin
from litestar.channels.backends.memory import MemoryChannelsBackend
from litestar.middleware.session.server_side import ServerSideSessionConfig
from litestar.stores.base import Store
from litestar.stores.registry import StoreRegistry

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.audit import AuditLog
from swarmq_dashboard.broker import BrokerObserver, BrokerUnavailable
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.directory.reader import StreamReader
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.dlq import DlqService, SwarmQDlqAdmin
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
from swarmq_dashboard.web.cancel import Canceller, SwarmQCanceller
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
from swarmq_dashboard.web.routes.tasks import cancel, detail, liste
from swarmq_dashboard.web.routes.worker import entfernen, uebersicht
from swarmq_dashboard.web.stream import LIVE_CHANNEL, LivePublisher, live
from swarmq_dashboard.web.templates import statische_dateien, template_config

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
        canceller: Canceller | None = None,
        broker: Any | None = None,
        dlq_admin: Any | None = None,
    ) -> None:
        self._config = config
        self._hasher = hasher
        #: Massstab der Anmeldebremse. Nur fuer die Testreihe kleiner als 1 --
        #: sie prueft die Bremse ueber echte Anfragen und bezahlt die
        #: Wartezeit sonst in Sekunden Laufzeit.
        self._throttle_scale = throttle_scale
        #: Das Stornieren. Es haengt am **Backend** der Bibliothek (Valkey
        #: Index 0), nicht am Ereignisstrom des Dashboards -- der Merker
        #: gehoert swarmq, nicht uns.
        self.canceller: Canceller = canceller or SwarmQCanceller(
            config.broker_url, config.backend_url
        )
        #: Die Dead-Letter-Bedienung (U15). Ihre Verbindung entsteht **traege**
        #: -- erst bei der ersten Aktion. Das Oeffnen der Seite kommt ohne sie
        #: aus: die Tiefe liefert die passive Beobachtung, die gescheiterten
        #: Tasks das Verzeichnis. Wer nur hinsieht, bezahlt nichts (R28).
        self._dlq_admin: Any = dlq_admin or SwarmQDlqAdmin(config.broker_url)
        self.dlq = DlqService(self._dlq_admin)
        #: Der Audit-Strom (R27). Er haengt am Ereignisstrom, nicht an der
        #: Kontendatei -- und er darf fehlen, ohne eine Aktion aufzuhalten.
        self.audit = AuditLog(None)
        #: Die Kanal-Verteilung der Live-Schicht und ihr Sender. Der Deckel
        #: verhindert das unbegrenzte Wachsen einer stehengebliebenen Ansicht
        #: (KTD6); **bemerkt** wird ein Verlust ueber die Folgenummern, weil
        #: der Rahmen den Ueberlauf ohne Rueckmeldung verwirft.
        self.live = ChannelsPlugin(
            backend=MemoryChannelsBackend(history=0),
            channels=[LIVE_CHANNEL],
            subscriber_max_backlog=config.backlog_cap,
            # ``dropleft`` haelt die **juengsten** Nachrichten: eine Ansicht
            # will den aktuellen Stand, nicht den aeltesten. Und zusammen mit
            # dem Anfangswert der Folgenummer faellt der Verlust sofort auf,
            # nicht erst bei der naechsten Nachricht.
            subscriber_backlog_strategy="dropleft",
        )
        self.publisher = LivePublisher(self.live)
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
        self._broker: Any | None = broker

        self._gruende: dict[str, str | None] = {
            "directory": "noch nicht geoeffnet",
            "accounts": "noch nicht geoeffnet",
            "takt_refresh": None,
            "takt_purge": None,
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
        await self.live._on_startup()
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
        # Derselbe Strom, eigener Schluessel und eigener Deckel (R9, R27).
        self.audit = AuditLog(self._stream)
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

    async def queue_stats(self, queues: list[str]) -> dict[str, Any]:
        """Tiefe und Konsumenten je Queue -- oder je Queue ein Fehlgrund.

        Nie eine leere Antwort und nie eine Null, wo eine Auskunft fehlt: eine
        Null laese sich als "kein Rueckstau", und das ist die gefaehrlichste
        stille Falschaussage der Worker-Seite (R18, R14).
        """
        from swarmq_dashboard.broker import QueueStats

        if not queues:
            return {}
        if self._broker is None:
            grund = self._gruende["broker"] or "der Broker ist nicht verbunden"
            return {q: QueueStats(queue=q, error=grund) for q in queues}
        try:
            return dict(await self._broker.stats_many(queues))
        except Exception as exc:
            return {q: QueueStats(queue=q, error=str(exc)) for q in queues}

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
        for teil in (self._stream, self._backend, self._broker, self._dlq_admin):
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
        try:
            await self.live._on_shutdown()
        except Exception:
            pass
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
                # Jeder Schritt einzeln gekapselt: zusammen legte ein dauerhaft
                # fehlschlagendes ``refresh()`` das Aufraeumen still mit --
                # gemessen null Aufraeum-Durchlaeufe bei zwoelf Takten. Genau
                # der Ausfall, gegen den diese Aufgabe eingezogen wurde, kaeme
                # damit zurueck, und zwar unsichtbar.
                for schritt, name in ((self.refresh, "refresh"), (self._aufraeumen, "purge")):
                    try:
                        await schritt()
                        self._gruende[f"takt_{name}"] = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # Ein Fehler beendet den Takt nicht -- sonst steht die
                        # Anzeige nach der ersten Stoerung fuer immer. Er
                        # verschwindet aber auch nicht: der Bericht nennt ihn.
                        self._gruende[f"takt_{name}"] = str(exc)

        return asyncio.ensure_future(takt())

    async def _aufraeumen(self) -> None:
        """Abgelaufene Sitzungen und verfallene Bremszaehler entfernen."""
        if self.throttle is not None:
            await asyncio.to_thread(self.throttle.purge)
        if self._sitzungsspeicher is not None:
            # Wie der Bremsen-purge in einen Faden: gemessen 0,18 s bei
            # 200.000 abgelaufenen Zeilen, und Granian faehrt einen Arbeiter.
            await asyncio.to_thread(self._sitzungsspeicher.purge_sync)

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
            # Ein Takt, der still nicht mehr laeuft, waere genau die Art
            # Ausfall, die dieses Dashboard nicht machen darf.
            # Nicht geschriebene Audit-Eintraege. Sie entstehen, wenn Valkey
            # fehlt -- die Aktion laeuft dann trotzdem (KTD8), aber still
            # bleiben darf das nicht (R27).
            "audit": {"lost": self.audit.lost},
            "maintenance": {
                "refresh": self._gruende["takt_refresh"],
                "purge": self._gruende["takt_purge"],
            },
        }

    def _verzeichnis_bericht(self) -> dict[str, Any]:
        if self.reader is None:
            # ``gaps`` und ``behind`` auch hier: die Kopfzeile fragt danach,
            # und eine fehlende Angabe waere in einer strengen Vorlage ein
            # Absturz -- ausgerechnet in dem Fall, in dem ohnehin schon etwas
            # fehlt.
            return {"ok": False, "reason": self._gruende["directory"],
                    "rebuilding": True, "behind": False, "gaps": 0}
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
    canceller: Canceller | None = None,
    broker: Any | None = None,
    dlq_admin: Any | None = None,
) -> Litestar:
    """Die Anwendung bauen. Der Lebenszyklus haelt die Aussenverbindungen.

    ``maintenance_every_s`` ist die Naht fuer die Testreihe: ohne sie prueft
    kein Test, dass der Lebenszyklus den Takt ueberhaupt **startet** -- und
    genau das war der Fehler bei ``refresh()``, das nur von Hand gerufen wurde.
    """
    services = Services(config, hasher=hasher, throttle_scale=throttle_scale,
                        canceller=canceller, broker=broker, dlq_admin=dlq_admin)
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
        route_handlers=[zustand, login_form, login, logout, live, liste, detail,
                        cancel, uebersicht, entfernen, dlq_uebersicht, sichten,
                        wiedereinreihen, alle_wiedereinreihen, leeren,
                        statische_dateien()],
        template_config=template_config(),
        lifespan=[lebenszyklus],
        stores=build_store_registry(weiche),
        middleware=[sitzung.middleware],
        guards=[session_guard, csrf_guard],
    )
