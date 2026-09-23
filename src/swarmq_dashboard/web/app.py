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
import os
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from argon2 import PasswordHasher
from litestar import Litestar, Request, get
from litestar.channels import ChannelsPlugin
from litestar.channels.backends.memory import MemoryChannelsBackend
from litestar.exceptions import HTTPException, NotAuthorizedException
from litestar.exceptions.responses import create_exception_response
from litestar.middleware.session.server_side import ServerSideSessionConfig
from litestar.response import Redirect, Response
from litestar.status_codes import HTTP_303_SEE_OTHER, HTTP_401_UNAUTHORIZED
from litestar.stores.base import Store
from litestar.stores.registry import StoreRegistry

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.audit import AuditLog
from swarmq_dashboard.broker import BrokerObserver, BrokerUnavailable
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.control import (
    SetpointStore,
    Steuerung,
    SwarmQSetpointStore,
    kontrollkanal_grund,
)
from swarmq_dashboard.directory.reader import DEFAULT_BATCH, StreamReader
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
from swarmq_dashboard.web.probe import ProbeSender
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
from swarmq_dashboard.web.stream import LIVE_CHANNEL, LivePublisher, live
from swarmq_dashboard.web.templates import render, statische_dateien, template_config

#: Name, unter dem der Sitzungsspeicher registriert wird.
SESSION_STORE_NAME = "sessions"
#: Takt der Pflege: Erreichbarkeit nachfuehren, Abgelaufenes wegraeumen.
#: Grosszuegig -- die Rueckkehr eines Dienstes darf ein paar Sekunden spaeter
#: sichtbar werden, aber nicht erst nach einem Neustart.
DEFAULT_MAINTENANCE_S = 10.0
#: Wartezeit zwischen zwei Durchlaeufen, **wenn nichts mehr zu holen war**.
#: Ein voller Stapel fuehrt sofort zum naechsten Durchlauf -- sonst braeuchte
#: ein Rueckstand von zehntausend Ereignissen bei Stapelgroesse 500 zwanzig
#: Sekunden, in denen die Ansicht alt ist und nicht sagt, warum.
DEFAULT_INGEST_S = 0.5
#: Notbremse fuer eine einzelne Erreichbarkeitspruefung -- **laenger** als
#: jede Frist, die sie umschliesst (``stream.DEFAULT_CONNECT_TIMEOUT_S``,
#: ``broker.CONNECT_TIMEOUT_S``). Andersherum war sie schaedlich: sie brach
#: die innere Frist immer zuerst ab, mit ``CancelledError``, und das lief an
#: jedem ``except Exception`` vorbei -- der halb aufgebaute Klient blieb
#: liegen. Gemessen 25 Takte, 25 offene Dateizeiger. Sie greift jetzt nur
#: noch, wenn eine innere Frist selbst versagt.
#:
#: Ohne sie haelt ein
#: Dienst, der die Verbindung annimmt und dann schweigt, den ganzen Takt auf
#: -- gemessen sechs Sekunden je Durchlauf gegen ein totes Backend, und in
#: dieser Zeit laeuft weder die Aufbewahrung noch die Anzeige nach. Eine
#: ueberschrittene Frist ist kein Fehler des Dienstes, sondern eine Auskunft
#: ueber ihn, und sie steht so im Bericht.
PROBE_TIMEOUT_S = 6.0
#: Wie lange das Herunterfahren auf einen auslaufenden Takt wartet.
STOP_TIMEOUT_S = 5.0


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
        stream: Any | None = None,
        setpoint_store: SetpointStore | None = None,
        probe_sender: Any | None = None,
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
        #: Die Sollwert-Steuerung (U16). Die Faehigkeitspruefung laeuft
        #: **beim Bauen** und nicht bei der ersten Aktion: ein Bereich, der
        #: erst beim Klicken zugibt, dass es ihn nicht gibt, ist schlechter
        #: als einer, der gar nicht erst anbietet (R33, AE15). Ein von aussen
        #: eingesetzter Kanal ueberspringt sie -- er ist der Beweis, dass es
        #: ihn gibt.
        grund = None if setpoint_store is not None else kontrollkanal_grund()
        self._setpoint_store: SetpointStore | None = setpoint_store
        if self._setpoint_store is None and grund is None:
            self._setpoint_store = SwarmQSetpointStore(config.backend_url)
        self.steuerung = Steuerung(self._setpoint_store, grund=grund)
        #: Das Einreihen von Proben -- die einzige Stelle, an der der
        #: Dienst selbst Arbeit erzeugt. Traege verbunden wie das
        #: Stornieren: wer nur hinsieht, bezahlt keine Broker-Verbindung.
        self.probe_sender: Any = probe_sender or ProbeSender(
            broker_url=config.broker_url, backend_url=config.backend_url,
            stream_url=config.stream_url,
        )
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

        #: Gesetzt, sobald der Dienst herunterfaehrt. Die Takte sehen darauf
        #: und kehren zurueck, **statt** abgebrochen zu werden: ein Abbruch
        #: beendet die Aufgabe, nicht den Arbeitsfaden, in dem gerade eine
        #: SQLite-Anweisung laeuft. Wird die Verbindung darunter geschlossen,
        #: stuerzt der Prozess ab -- nachgemessen, ein Speicherzugriffsfehler
        #: mitten im Einlesen.
        self._halt = False
        #: Weckt die schlafenden Takte beim Herunterfahren. Ein blosses
        #: ``asyncio.sleep(every_s)`` laesst sie bis zu einem vollen Takt --
        #: im Betrieb zehn Sekunden -- weiterschlafen, und das Herunterfahren
        #: wartet so lange. Gemessen kostete das jeden Test fuenf Sekunden.
        self._wecker = asyncio.Event()
        self.reader: StreamReader | None = None
        self.services_directory_writer: DirectoryWriter | None = None
        #: Von aussen gesetzte Aussenverbindungen. Sie ueberleben das
        #: Herunterfahren, die laufenden nicht: ``startup()`` ist idempotent
        #: zugesagt, und ein zweiter Lebenszyklus muss dieselben Vorgaben
        #: wiederfinden -- sonst faehrt er gegen die echten Adressen, obwohl
        #: jemand ausdruecklich etwas anderes eingesetzt hat.
        self._stream_vorgabe = stream
        self._broker_vorgabe = broker
        self._stream: ValkeyStreamClient | None = stream
        self._backend: ValkeyStreamClient | None = None
        self._broker: Any | None = broker

        self._gruende: dict[str, str | None] = {
            "directory": "noch nicht geoeffnet",
            "accounts": "noch nicht geoeffnet",
            "takt_refresh": None,
            "takt_purge": None,
            "takt_ingest": None,
            # Die drei Teile der Pflege einzeln: faellt einer aus, sollen die
            # anderen weiterlaufen **und** der Ausfall benannt sein.
            "pflege_retention": None,
            "pflege_throttle": None,
            "pflege_sessions": None,
            "stream": "noch nicht verbunden",
            "backend": "noch nicht verbunden",
            "broker": "noch nicht verbunden",
        }

    # -- Lebenszyklus ----------------------------------------------------

    async def startup(self) -> None:
        """Alles hochfahren, was geht. Wirft nie.

        Setzt den Halt-Merker zurueck. Ohne das war die zugesagte Idempotenz
        eine Luege: nach einem ``shutdown()`` blieb ``_halt`` stehen, alle
        drei Takte pruefen ihn als Erstes, und der zweite Lebenszyklus
        derselben Anwendung las nichts, raeumte nichts auf und fuehrte nichts
        nach -- waehrend der Bericht alles in Ordnung meldete.
        """
        self._halt = False
        self._wecker.clear()
        return await self._startup()

    async def _startup(self) -> None:
        """

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
            # Die Naht zur Live-Schicht. Sie wird hier gesetzt und nicht im
            # Leser gebaut: der Leser soll nichts von der Weboberflaeche
            # wissen.
            self.reader = StreamReader(
                _NochNichtVerbunden(), self.services_directory_writer, stream_key=TASK_STREAM
            )
            self.reader.melder = self._live_melden
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
            await asyncio.wait_for(client.connect(), timeout=PROBE_TIMEOUT_S)
        except TimeoutError:
            await _leise_schliessen(client)
            return None, f"antwortet nicht innerhalb von {PROBE_TIMEOUT_S:.0f} s"
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
            await asyncio.wait_for(self._broker.connect(), timeout=PROBE_TIMEOUT_S)
            self._gruende["broker"] = None
        except TimeoutError:
            # Eine ueberschrittene Frist ist kein Fehler des Dienstes, sondern
            # eine Auskunft ueber ihn -- und sie muss dastehen. Ohne die Frist
            # hielt ein Broker, der die Verbindung annimmt und dann schweigt,
            # den ganzen Takt auf: gemessen sechs Sekunden je Durchlauf, in
            # denen weder die Anzeige nachlief noch die Aufbewahrung ablief.
            self._gruende["broker"] = (
                f"der Broker antwortet nicht innerhalb von {PROBE_TIMEOUT_S:.0f} s"
            )
        except BrokerUnavailable as exc:
            self._gruende["broker"] = str(exc)

    async def _schlafen(self, sekunden: float) -> None:
        """Warten -- aber nicht laenger, als der Dienst da ist."""
        with suppress(TimeoutError):
            await asyncio.wait_for(self._wecker.wait(), timeout=sekunden)

    async def stop_tasks(self, aufgaben: list[asyncio.Task[None]]) -> None:
        """Die Takte anhalten, **bevor** etwas geschlossen wird.

        Erst freundlich: ``_halt`` setzen und die Aufgaben auslaufen lassen.
        Ein Abbruch beendet nur die Aufgabe, nicht den Arbeitsfaden, in dem
        gerade eine SQLite-Anweisung laeuft -- und die Verbindung darunter zu
        schliessen war ein Speicherzugriffsfehler, kein Fehlerbild mit
        Meldung. Erst wenn eine Aufgabe die Frist ueberzieht, wird sie
        abgebrochen; dann ist der Absturz immer noch moeglich, aber er ist
        nicht mehr der Normalfall.
        """
        self._halt = True
        self._wecker.set()
        if aufgaben:
            await asyncio.wait(aufgaben, timeout=STOP_TIMEOUT_S)
        for aufgabe in aufgaben:
            if not aufgabe.done():
                aufgabe.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await aufgabe

    async def shutdown(self) -> None:
        """Alles schliessen, auch das, was nie hochkam. Wirft nie."""
        self._halt = True
        self._wecker.set()
        # Der Canceller gehoert dazu: er haelt eine eigene Valkey-Verbindung,
        # und ohne ihn leckte je Lebenszyklus eine.
        for teil in (self._stream, self._backend, self._broker, self._dlq_admin,
                     self.canceller, self._setpoint_store, self.probe_sender):
            await _leise_schliessen(teil)
        self._stream = self._stream_vorgabe
        self._backend = None
        self._broker = self._broker_vorgabe
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

    def start_maintenance(self, *, every_s: float = DEFAULT_MAINTENANCE_S
                          ) -> list[asyncio.Task[None]]:
        """Die Takte starten: Erreichbarkeit nachfuehren und aufraeumen.

        Beide haben dieselbe Begruendung -- etwas, das der Dienst von sich aus
        tun muss, weil es sonst niemand tut:

        * ``refresh()`` rief ausser ``startup()`` **niemand**. Ohne den Takt
          bleibt die Anzeige nach der Rueckkehr eines Dienstes dauerhaft auf
          "nicht erreichbar" -- worauf der Betreiber das Dashboard neu
          startet, also genau das tut, was der Absatz verhindern wollte.
        * ``purge()`` und ``reader.maintain()`` hatten ueberhaupt keinen
          Aufrufer. Ohne das erste waechst die Kontendatei (hochgerechnet
          673 MiB in einer Woche), ohne das zweite laeuft die
          Aufbewahrungsfrist nie ab -- und damit bleibt der Klartext der
          Kundenargumente liegen, entgegen R7a.

        **Zwei getrennte Aufgaben**, nicht zwei Schritte in einer. Vorher
        liefen sie nacheinander; jeder Schritt war zwar einzeln gegen
        Ausnahmen gekapselt, aber nicht gegen **Dauer**. Gemessen: gegen einen
        toten Broker braucht ``refresh()` sekundenlang, und das Aufraeumen kam
        in keinem einzigen Takt an die Reihe. Damit haette die Trennung genau
        den Ausfall zurueckgeholt, gegen den sie eingezogen wurde -- nur
        unsichtbar, weil kein Fehler entsteht. Getrennte Aufgaben koennen sich
        nicht gegenseitig aushungern.
        """
        return [
            self._takt(self.refresh, "refresh", every_s),
            self._takt(self._aufraeumen, "purge", every_s),
        ]

    def _takt(self, schritt: Callable[[], Awaitable[None]], name: str,
              every_s: float) -> asyncio.Task[None]:
        """Eine wiederkehrende Aufgabe, die nie an einem Fehler endet."""

        async def lauf() -> None:
            while not self._halt:
                await self._schlafen(every_s)
                if self._halt:
                    return
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

        return asyncio.ensure_future(lauf())

    def start_ingest(self, *, every_s: float = DEFAULT_INGEST_S) -> asyncio.Task[None]:
        """Den Strom-Leser antreiben -- die Naht, ohne die nichts geschieht.

        Aus der Pruefung von Stufe D: ``poll_once`` hatte im ganzen Dienst
        **keinen Aufrufer**. Der Leser wurde gebaut, bekam seine Quelle
        eingehaengt, und las nie. Das Verzeichnis blieb leer, jede Seite
        dauerhaft ohne Zeile, der Neuaufbau ewig "laeuft" -- und die
        Kopfzeile meldete dabei "Strom: aktuell". Alle Tests waren gruen,
        weil jeder von ihnen ``poll_once`` selbst rief.

        Ein eigener Takt und nicht im Pflegetakt: das Einlesen ist die
        Hauptarbeit und braucht einen halben Takt, das Aufraeumen kommt alle
        zehn Sekunden. In einen Topf geworfen waere die Ansicht entweder
        zehn Sekunden alt oder das Aufraeumen liefe zwanzigmal zu oft.
        """

        async def takt() -> None:
            while not self._halt:
                try:
                    gelesen = await self._einlesen()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._gruende["takt_ingest"] = str(exc)
                    gelesen = 0
                else:
                    self._gruende["takt_ingest"] = None
                # Voller Stapel heisst: da ist noch mehr. Dann sofort weiter,
                # statt den Rueckstand im Halbsekundentakt abzutragen.
                if gelesen < DEFAULT_BATCH:
                    await self._schlafen(every_s)

        return asyncio.ensure_future(takt())

    async def _einlesen(self) -> int:
        if self.reader is None:
            return 0
        return await self.reader.poll_once()

    async def _live_melden(self, ereignisse: list[tuple[str | None, Any]]) -> None:
        """Was eingearbeitet wurde, an die offenen Ansichten weitergeben (R13).

        Der Sender war gebaut und hatte ebenfalls keinen Aufrufer; die
        Live-Schicht lieferte darum nur Herzschlaege. Gemeldet wird je
        Ereignis Art und Task -- mehr braucht die Ansicht nicht, um die
        betroffene Zeile nachzuladen, und weniger waere ein Wecken ohne
        Auskunft.
        """
        for _sid, ev in ereignisse:
            await self.publisher.publish(
                ev.type, {"task_id": str(ev.payload.get("task_id") or ""),
                          "worker_id": str(ev.payload.get("worker_id") or ""),
                          "ts": ev.ts}
            )

    async def _aufraeumen(self) -> None:
        """Die Frist anwenden, abgelaufene Sitzungen und Bremszaehler entfernen.

        Die Aufbewahrung stand vorher in keinem Takt: ``reader.maintain`` und
        damit ``writer.expire`` hatten keinen Aufrufer. Gemessen ueberlebte
        eine dreissig Tage alte Zeile samt Klartext vierzig Pflegelaeufe.
        Damit war R7a gebrochen -- und die Kopfzeile nannte trotzdem sieben
        Tage. Die Frist ist die einzige technische Begrenzung, unter der die
        Klartext-Entscheidung aus KD11 vertretbar ist.

        **Jeder Schritt einzeln gekapselt**, aus demselben Grund, aus dem die
        Takte getrennt laufen: zusammen legte ein scheiterndes ``maintain``
        -- etwa weil der Strom nicht verbunden ist -- das Aufraeumen der
        Sitzungen still mit. Der erste Entwurf dieser Methode tat genau das,
        und die Testreihe zeigte es sofort.
        """
        for schritt, name in (
            (self._frist_anwenden, "retention"),
            (self._bremse_aufraeumen, "throttle"),
            (self._sitzungen_aufraeumen, "sessions"),
        ):
            try:
                await schritt()
                self._gruende[f"pflege_{name}"] = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._gruende[f"pflege_{name}"] = str(exc)

    async def _frist_anwenden(self) -> None:
        if self.reader is not None:
            await self.reader.maintain(retention_s=self._config.retention_days * 86400.0)

    async def _bremse_aufraeumen(self) -> None:
        if self.throttle is not None:
            await asyncio.to_thread(self.throttle.purge)

    async def _sitzungen_aufraeumen(self) -> None:
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
            # Alle Takte, nicht nur zwei. Ein Takt, der still nicht mehr
            # arbeitet, ist genau die Art Ausfall, die dieses Dashboard nicht
            # machen darf -- und die drei neuen Gruende standen in einem
            # privaten Woerterbuch und sonst nirgends.
            "maintenance": {
                "refresh": self._gruende["takt_refresh"],
                "purge": self._gruende["takt_purge"],
                "ingest": self._gruende["takt_ingest"],
                "retention": self._gruende["pflege_retention"],
                "throttle": self._gruende["pflege_throttle"],
                "sessions": self._gruende["pflege_sessions"],
            },
        }

    def _verzeichnis_bericht(self) -> dict[str, Any]:
        if self.reader is None:
            # ``gaps`` und ``behind`` auch hier: die Kopfzeile fragt danach,
            # und eine fehlende Angabe waere in einer strengen Vorlage ein
            # Absturz -- ausgerechnet in dem Fall, in dem ohnehin schon etwas
            # fehlt.
            return {"ok": False, "reason": self._gruende["directory"],
                    "rebuilding": True, "behind": False, "gaps": 0,
                    "dropped": 0, "rejected": 0}
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
    ingest_every_s: float = DEFAULT_INGEST_S,
    throttle_scale: float = 1.0,
    canceller: Canceller | None = None,
    broker: Any | None = None,
    dlq_admin: Any | None = None,
    stream: Any | None = None,
    setpoint_store: SetpointStore | None = None,
    probe_sender: Any | None = None,
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
                        probe_sender=probe_sender)
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
                        dlq_uebersicht, sichten,
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
