"""Das Ergebnis eines Tasks lesen -- als Auskunft, nicht als Ausnahme (U1).

``SwarmQ.get_result`` wirft bei Storno und bei Fehlschlag. Fuer einen
Aufrufer, der das Ergebnis *verarbeiten* will, ist das richtig. Fuer eine
Seite, die nur **zeigen** soll, was herauskam, ist es der Normalfall: ein
gescheiterter Task ist keine Fehlerseite, sondern seine eigene Auskunft
(KTD2).

Der Leser wandelt darum jede Ausnahme in einen Zustand.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from swarmq_dashboard.stream import StreamUnavailable
from swarmq_dashboard.zugang import Zugang

#: Anzeigegrenze, gemessen an der UTF-8-Laenge der **Darstellung** (KTD3).
#: Die Zahl ist nicht gemessen, sondern gewaehlt: eine Karte fasst ungefaehr
#: so viel, bevor Scrollen die Seite unbrauchbar macht. Ausdruecklich nicht
#: die Groesse des im Backend abgelegten Pakets -- an dessen Rohbytes kommt
#: das Dashboard nach KD2 nicht heran.
GRENZE_B = 8 * 1024

#: Wie lange nach einem Fehlversuch nicht erneut gewaehlt wird, wie beim
#: Kontrollkanal. Kuerzer waere wirkungslos, viel laenger liesse die
#: Rueckkehr des Backends zu lange unbemerkt.
SPERRE_S = 30.0

#: Wie viele beendete Ergebnisse der Leser behaelt (KTD3). Ein fertiges oder
#: storniertes Ergebnis aendert sich nicht mehr; die Grenze haelt nur den
#: Speicher eines langlebigen Dienstes klein. 4096 deckt die Kopf-Glieder
#: einiger grosser Workflows, die gleichzeitig offen sind.
SPEICHER_N = 4096

#: Die Arten, die sich nicht mehr aendern und darum gespeichert werden.
#: "gescheitert" gehoert nicht dazu: ein Requeue aus der Dead-Letter-Queue
#: reiht dieselbe Kennung neu ein, und ein erfolgreicher Lauf ueberschreibt
#: das Fehlerergebnis.
ENDGUELTIG = frozenset({"fertig", "storniert"})

#: Frist fuer den Aufbau und fuer jede einzelne Abfrage (KTD4). Kurz: Die
#: Detailseite ist kein Ort zum Warten.
CONNECT_TIMEOUT_S = 2.0


def _kuerzen(text: str) -> tuple[str, bool, int]:
    """``(Text, abgeschnitten, volle UTF-8-Laenge)``.

    Geschnitten wird auf Bytes, nicht auf Zeichen -- und danach auf das
    naechste ganze Zeichen zurueck, damit kein halbes Mehrbyte-Zeichen
    stehen bleibt.
    """
    roh = text.encode("utf-8")
    if len(roh) <= GRENZE_B:
        return text, False, len(roh)
    return roh[:GRENZE_B].decode("utf-8", "ignore"), True, len(roh)


def _darstellen(wert: Any) -> str:
    """Den Rueckgabewert lesbar machen -- wie die Argumente darueber.

    ``directory/writer.py`` legt die Argumente als JSON ab. Dieselbe Form
    hier, damit auf einer Seite nicht zweimal dieselbe Art Daten in zwei
    Schreibweisen steht.

    Was JSON nicht kennt -- die Ext-Typen der Bibliothek etwa --, faellt auf
    ``repr`` zurueck. Eine unschoene Zeile ist besser als eine leere Karte.
    """
    try:
        return json.dumps(wert, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        pass
    try:
        return repr(wert)
    except Exception:
        # Auch das kann werfen. Dann bleibt der Typname -- eine Auskunft, und
        # keine Fehlerseite anstelle der Seite, die gefragt war.
        return f"<{type(wert).__name__}, nicht darstellbar>"


@dataclass(frozen=True)
class Ergebnisbild:
    """Was die Seite ueber das Ergebnis eines Tasks weiss."""

    zustand: str
    art: str | None
    text: str
    abgeschnitten: bool = False
    groesse: int | None = None
    grund: str | None = None


class Ergebnisleser(Protocol):
    """Was die Detailansicht vom Ergebnis-Lesen braucht."""

    async def lesen(self, task_id: str) -> Ergebnisbild: ...


class SwarmQErgebnisleser(Zugang):
    """Der Leser ueber die oeffentliche API der Bibliothek (KD2).

    Verbindungsaufbau, Riegel, Sperrfrist und Schliessen kommen aus
    :class:`~swarmq_dashboard.zugang.Zugang`.
    """

    SPERRE_S = SPERRE_S
    CONNECT_TIMEOUT_S = CONNECT_TIMEOUT_S

    def __init__(
        self,
        broker_url: str,
        backend_url: str,
        *,
        app: Any | None = None,
        verbinden: Callable[[], Awaitable[Any]] | None = None,
        uhr: Callable[[], float] | None = None,
        speicher_n: int = SPEICHER_N,
    ) -> None:
        super().__init__(backend_url, kern=app, verbinden=verbinden, uhr=uhr)
        self._broker_url = broker_url
        #: Beendete Ergebnisse je Task-Kennung, der zuletzt genutzte hinten.
        self._gespeichert: OrderedDict[str, Ergebnisbild] = OrderedDict()
        self._speicher_n = speicher_n

    async def _kern_auf(self, client: Any) -> Any:
        """Die Bibliothek auf dem **eigenen** Valkey-Klienten.

        Hier entsteht KTD4s Frist: ``get_result`` reicht keine durch -- mit
        ``timeout=0`` ist es ein blanker GET ohne Deadline. Der ``ping()``
        davor ist Pflicht, sonst kaeme der Fehlschlag erst beim Lesen.
        """
        from swarmq import SwarmQ
        from swarmq.backend.valkey import ValkeyBackend
        from swarmq.broker.rabbitmq import RabbitMQBroker

        await client.ping()
        backend = ValkeyBackend(client=client)
        # Der Broker wird zum Lesen nicht gebraucht -- das Ergebnis liegt im
        # Backend. Das Objekt muss es trotzdem geben, weil die Bibliothek
        # beides zusammen haelt; verbunden wird es nie.
        return SwarmQ(broker=RabbitMQBroker(self._broker_url), backend=backend)

    async def connect(self) -> None:
        """Verbindung aufbauen. Idempotent. Wirft benannt.

        Traege wie beim Stornieren und beim Kontrollkanal: Wer die Liste
        ansieht, bezahlt keine Backend-Verbindung.
        """
        await super().connect()

    async def lesen(self, task_id: str) -> Ergebnisbild:
        """Das Ergebnis holen und als Zustand zurueckgeben.

        Ein fertiges oder storniertes Ergebnis kommt nach der ersten Lesung
        aus dem Speicher -- **vor** dem Verbindungsaufbau: ein Treffer liest
        nicht und verbindet nicht, auch nicht waehrend der Sperre. Verfaellt
        es danach in Valkey, bleibt es trotzdem sichtbar, bis der Dienst neu
        startet (KTD3). Alles andere wird bei jeder Anfrage neu gelesen.
        """
        gespeichert = self._gespeichert.get(task_id)
        if gespeichert is not None:
            self._gespeichert.move_to_end(task_id)
            return gespeichert
        bild = await self._frisch_lesen(task_id)
        if bild.zustand == "da" and bild.art in ENDGUELTIG:
            self._gespeichert[task_id] = bild
            if len(self._gespeichert) > self._speicher_n:
                self._gespeichert.popitem(last=False)
        return bild

    async def _frisch_lesen(self, task_id: str) -> Ergebnisbild:
        """Das Ergebnis aus dem Backend holen, ohne Zwischenspeicher."""
        try:
            # ``connect()`` liefert hier nichts (R2); die App kommt aus der Basis.
            app = await super().connect()
        except StreamUnavailable as exc:
            return Ergebnisbild(
                zustand="nicht_erreichbar", art=None, text="", grund=str(exc),
            )
        from swarmq.exceptions import (
            ResultNotReadyError,
            ResultSerializationError,
            TaskCancelledError,
            TaskError,
        )

        try:
            wert = await app.get_result(task_id, timeout=0)
        except ResultNotReadyError:
            return Ergebnisbild(zustand="fehlt", art=None, text="")
        except ResultSerializationError as exc:
            # Weder Task- noch Verbindungsfehler: Das Ergebnis liegt vor, nur
            # diese Fassung kann es nicht lesen. Faellt es in einen der
            # Sammelfaenge, behauptet die Seite etwas ueber die Anwendung, was
            # in Wahrheit ueber die Ablage gilt.
            return Ergebnisbild(
                zustand="nicht_lesbar", art=None, text="",
                grund=f"mit dieser swarmq-Fassung nicht lesbar: {exc}",
            )
        except TaskCancelledError:
            # **Vor** ``TaskError``: Storno ist dessen Unterklasse, und in der
            # umgekehrten Reihenfolge verschwaende der Zustand stillschweigend
            # im Fehlerzweig. Die Bibliothek gibt dem Storno denselben Vorrang.
            return Ergebnisbild(
                zustand="da", art="storniert",
                text="Der Task wurde storniert.",
            )
        except TaskError as exc:
            text, abgeschnitten, groesse = _kuerzen(str(exc))
            return Ergebnisbild(
                zustand="da", art="gescheitert", text=text,
                abgeschnitten=abgeschnitten, groesse=groesse,
            )
        except Exception as exc:
            # Alles, was keine Auskunft ueber den Task ist: die Leitung. Hier
            # -- und nicht nur beim Verbindungsaufbau -- muss die Sperre
            # greifen. Stirbt das Backend, nachdem der Leser verbunden war,
            # kehrt ``connect()`` weiterhin sofort zurueck; ohne diesen Zweig
            # laeuft jeder Seitenaufruf erneut in die volle Frist.
            return Ergebnisbild(
                zustand="nicht_erreichbar", art=None, text="",
                grund=await self._ausfall(exc),
            )
        text, abgeschnitten, groesse = _kuerzen(_darstellen(wert))
        return Ergebnisbild(
            zustand="da", art="fertig", text=text,
            abgeschnitten=abgeschnitten, groesse=groesse,
        )
