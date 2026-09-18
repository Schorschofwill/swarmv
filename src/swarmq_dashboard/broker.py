"""Passive Beobachtung der Broker-Queues: Tiefe und Konsumenten.

Warum ueberhaupt eine eigene AMQP-Verbindung, wo R1a sonst auf oeffentlichen
Schnittstellen der Bibliothek besteht: die Zahl der **Konsumenten** gibt die
Bibliothek nicht heraus, und ihre Tiefenabfrage schneidet den Zuschnitt anders
als das Dashboard ihn braucht. Das ist der eine begruendete Ausbruch (KTD11).

Drei Eigenheiten von AMQP bestimmen den Aufbau, und jede hat einen Test:

1. **Eine fehlgeschlagene passive Deklaration schliesst den Kanal.** Also
   bekommt jede Abfrage einen eigenen, wegwerfbaren Kanal. Ein gemeinsamer
   waere nach der ersten fehlenden Queue tot, und alles danach meldete
   faelschlich null.
2. **Prioritaeten sind eigene Queues.** ``priority="high"`` schreibt nach
   ``{queue}.high``. Nur die Basis-Queue zu zaehlen untertreibt den Rueckstau
   (R14) und die Dead-Letter-Tiefe (R28).
3. **Ein Worker konsumiert aus allen drei Varianten.** Die Konsumenten zu
   summieren zaehlte ihn dreifach -- und R18 meldete "genug Worker da",
   waehrend keiner laeuft. Deshalb das Maximum.

Getrennt gehalten wird, was verschieden bedeutet: die verzoegerte Queue
``{queue}.DQ`` wartet auf ihre Zeit, nicht auf einen Worker, und die
Dead-Letter-Queue ``{queue}.XQ`` ist gescheiterte Arbeit, kein Rueckstau.
Beide in die Tiefe gerechnet beantworteten die Frage "staut es sich?" falsch.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass, field

import aio_pika
from aio_pika.exceptions import ChannelNotFoundEntity

#: Namenszusaetze der Prioritaetsvarianten. ``normal`` bleibt die Basis-Queue.
PRIORITY_SUFFIXES = ("high", "low")
#: Zusatz der verzoegerten Queue.
DELAY_SUFFIX = "DQ"
#: Zusatz der Dead-Letter-Queue.
DEAD_LETTER_SUFFIX = "XQ"
#: Zeitgrenze fuer den Verbindungsaufbau. Ohne sie haengt der Aufbau gegen
#: einen Server, der TCP annimmt und dann schweigt -- eine Firewall, die
#: verwirft statt abzulehnen -- unbegrenzt, und mit ihm der ganze Takt.
CONNECT_TIMEOUT_S = 10.0
#: Was AMQP als Queue-Namen zulaesst -- der Zeichenvorrat der Protokollschicht,
#: Zeichen fuer Zeichen gegen sie abgeglichen, nicht enger gefasst. Eine
#: strengere eigene Regel waere genau die stille Falschaussage, gegen die
#: dieses Modul sonst argumentiert: ``ablage/eingang`` und ``queue 1`` sind
#: zulaessige Namen, und "gibt es nicht" darueber waere schlicht falsch.
#: Der Zeichenvorrat wird **vor** dem Netz geprueft, darum ist sein Verstoss
#: kein Broker-Ausfall.
_NAME = re.compile(r"^[ #+,\-./0-9:@A-Z_a-z]{1,255}$")


class BrokerUnavailable(RuntimeError):
    """Der Broker war nicht erreichbar oder antwortete unbrauchbar.

    Bewusst von "Queue nicht vorhanden" getrennt: eine fehlende Queue ist
    eine Antwort, ein Netzfehler ist keine. Als Null durchgereicht laese sich
    ein Netzfehler als "kein Rueckstau" -- die gefaehrlichste aller stillen
    Falschaussagen in diesem Dashboard.
    """


@dataclass(frozen=True)
class QueueStats:
    """Was eine Abfrage ueber eine Queue und ihre Varianten sagt."""

    queue: str
    #: Existiert wenigstens eine der Varianten?
    exists: bool = False
    #: Wartende Nachrichten: Basis plus Prioritaetsvarianten (R14).
    depth: int = 0
    #: Nachrichten in der verzoegerten Queue -- warten auf die Zeit, nicht
    #: auf einen Worker.
    delayed: int = 0
    #: Gescheiterte Arbeit in ``{queue}.XQ`` (R28).
    dead_letter: int = 0
    #: Konsumenten: Maximum ueber die Varianten, nicht ihre Summe (R18).
    consumers: int = 0
    #: Warum diese Queue nicht gemessen werden konnte, oder ``None``. Von
    #: ``exists=False`` unterschieden: "gibt es nicht" ist eine Antwort,
    #: "keine Antwort" ist keine.
    error: str | None = None
    #: Je abgefragter AMQP-Queue die Nachrichtenzahl, ``None`` fuer
    #: "nicht vorhanden". Die Oberflaeche kann damit zwischen "leer" und
    #: "gibt es nicht" unterscheiden, statt beides als 0 zu zeigen.
    parts: dict[str, int | None] = field(default_factory=dict)


@dataclass(frozen=True)
class _Probe:
    """Das Ergebnis einer einzelnen passiven Deklaration."""

    messages: int | None
    consumers: int


class BrokerObserver:
    """Fragt Queues passiv ab. Aendert nie etwas am Broker."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None

    async def connect(self) -> None:
        """Verbindung aufbauen **und pruefen**. Idempotent.

        ``is_closed`` allein genuegt nicht: ``RobustConnection`` heilt sich
        selbst und meldet sich waehrend eines Wiederaufbaus nicht als
        geschlossen. Genau diese Luege hat hier schon einmal ein Kanalleck
        verursacht; hier verursachte sie eine gruene Kopfzeile ueber einem
        Broker, der nicht mehr da war -- waehrend in derselben Antwort jede
        Queue-Zeile einen Fehlgrund trug.

        Geprueft wird mit einem Kanal, nicht mit einem Feld: das ist der
        billigste Vorgang, der wirklich mit dem Broker spricht.
        """
        if self._connection is not None and not self._connection.is_closed:
            try:
                kanal = await asyncio.wait_for(self._connection.channel(),
                                               timeout=CONNECT_TIMEOUT_S)
            except Exception as exc:
                # Schliessen, nicht bloss vergessen: eine fallengelassene
                # Verbindung meldet sich beim Aufraeumen als "close was never
                # awaited" und haelt bis dahin ihren Socket.
                tot, self._connection = self._connection, None
                with contextlib.suppress(Exception):
                    await tot.close()
                raise BrokerUnavailable(str(exc)) from exc
            with contextlib.suppress(Exception):
                await kanal.close()
            return
        if self._connection is None or self._connection.is_closed:
            try:
                self._connection = await asyncio.wait_for(
                    aio_pika.connect_robust(self._url), timeout=CONNECT_TIMEOUT_S
                )
            except TimeoutError as exc:
                raise BrokerUnavailable(
                    f"keine Antwort binnen {CONNECT_TIMEOUT_S:.0f} s"
                ) from exc
            except Exception as exc:
                raise BrokerUnavailable(str(exc)) from exc

    async def close(self) -> None:
        """Verbindung schliessen. Idempotent."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    # -- Abfragen ---------------------------------------------------------

    async def stats(self, queue: str) -> QueueStats:
        """Tiefe, Verzoegerung, Dead-Letter und Konsumenten einer Queue.

        Eine unbekannte Queue ist kein Fehler: das Dashboard fragt Queues ab,
        die es aus den Stammdaten-Ereignissen kennt, und die kann es geben,
        bevor ein Worker sie angelegt hat.
        """
        namen = [queue, *(f"{queue}.{s}" for s in PRIORITY_SUFFIXES)]
        teile: dict[str, int | None] = {}

        tiefe = 0
        konsumenten = 0
        vorhanden = False
        for name in namen:
            probe = await self._probe(name)
            teile[name] = probe.messages
            if probe.messages is None:
                continue
            vorhanden = True
            tiefe += probe.messages
            konsumenten = max(konsumenten, probe.consumers)

        verzoegert = await self._probe(f"{queue}.{DELAY_SUFFIX}")
        teile[f"{queue}.{DELAY_SUFFIX}"] = verzoegert.messages
        tot = await self._probe(f"{queue}.{DEAD_LETTER_SUFFIX}")
        teile[f"{queue}.{DEAD_LETTER_SUFFIX}"] = tot.messages

        return QueueStats(
            queue=queue,
            exists=vorhanden,
            depth=tiefe,
            delayed=verzoegert.messages or 0,
            dead_letter=tot.messages or 0,
            consumers=konsumenten,
            parts=teile,
        )

    async def stats_many(self, queues: list[str]) -> dict[str, QueueStats]:
        """Mehrere Queues nacheinander abfragen.

        Nacheinander, nicht gleichzeitig: jede Abfrage oeffnet fuenf Kanaele,
        und ein Dashboard mit dreissig Queues brauchte sonst schlagartig
        hundertfuenfzig. Die Abfrage laeuft im Hintergrund, nicht im
        Anfrageweg -- Dauer ist hier billiger als eine Kanalflut.

        Je Queue gekapselt: faellt der Broker mitten im Lauf weg, sind die
        bereits gemessenen Queues trotzdem etwas wert. Frueher verwarf eine
        einzige Ausnahme den ganzen Lauf, und die Oberflaeche bekam nichts
        statt "diese acht sind gemessen, fuer den Rest fehlt die Antwort".
        """
        ergebnis: dict[str, QueueStats] = {}
        for q in queues:
            try:
                ergebnis[q] = await self.stats(q)
            except BrokerUnavailable as exc:
                ergebnis[q] = QueueStats(queue=q, error=str(exc))
        return ergebnis

    # -- Eine einzelne Deklaration ---------------------------------------

    async def _probe(self, name: str) -> _Probe:
        """Eine Queue passiv deklarieren. ``messages is None`` heisst
        "nicht vorhanden".

        Der Kanal ist ein Wegwerfkanal (KTD11): AMQP schliesst ihn, wenn die
        Deklaration fehlschlaegt, und ein geteilter Kanal waere danach fuer
        alle weiteren Abfragen tot.
        """
        if not _NAME.match(name):
            # Die Protokollschicht lehnte den Namen ohnehin lokal ab, und das
            # ``except Exception`` unten machte daraus einen Broker-Ausfall --
            # also genau die Aussage, die einem Netzfehler vorbehalten ist.
            # Ein einziger fauler Name aus einem Stammdaten-Ereignis blendete
            # so die Tiefen *aller* Queues aus. Schon ein Basisname ab 252
            # Zeichen genuegte, weil ``.high`` die Grenze von 255 sprengt.
            return _Probe(messages=None, consumers=0)
        if self._connection is None or self._connection.is_closed:
            raise BrokerUnavailable("der Broker ist nicht verbunden")

        kanal: aio_pika.abc.AbstractChannel | None = None
        try:
            kanal = await self._connection.channel()
            q = await kanal.declare_queue(name, passive=True)
            ergebnis = getattr(q, "declaration_result", None)
            if ergebnis is None:
                return _Probe(messages=0, consumers=0)
            return _Probe(
                messages=int(getattr(ergebnis, "message_count", 0)),
                consumers=int(getattr(ergebnis, "consumer_count", 0)),
            )
        except ChannelNotFoundEntity:
            return _Probe(messages=None, consumers=0)
        except Exception as exc:
            raise BrokerUnavailable(f"{name}: {exc}") from exc
        finally:
            if kanal is not None:
                # **Unbedingt** schliessen, auch wenn der Kanal sich selbst
                # schon als geschlossen meldet. Gemessen: nach einer
                # fehlgeschlagenen Deklaration steht ``is_closed`` auf True,
                # ein ``RobustChannel`` stellt sich danach aber im Hintergrund
                # selbst wieder her und hinterlaesst einen frischen, offenen
                # Kanal, den niemand mehr besitzt. Mit der frueheren Pruefung
                # blieben nach 50 Fehlproben 50 Kanaele auf dem Broker stehen;
                # bei dreissig Queues, von denen die Prioritaets- und
                # Verzoegerungsvarianten meist fehlen, war die ausgehandelte
                # Obergrenze von 2047 nach siebzehn Takten erreicht. Dann
                # verwirft RabbitMQ die **ganze** Verbindung, und die robuste
                # Wiederverbindung stellt die verwaisten Kanaele gleich mit
                # wieder her -- der Dienst kam ohne Neustart nicht zurueck.
                try:
                    await kanal.close()
                except Exception:
                    # Ein Kanal, der sich nicht schliessen laesst, ist fort.
                    # Daran darf keine Abfrage scheitern.
                    pass
