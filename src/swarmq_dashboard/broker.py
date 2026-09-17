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

from dataclasses import dataclass, field

import aio_pika
from aio_pika.exceptions import ChannelNotFoundEntity

#: Namenszusaetze der Prioritaetsvarianten. ``normal`` bleibt die Basis-Queue.
PRIORITY_SUFFIXES = ("high", "low")
#: Zusatz der verzoegerten Queue.
DELAY_SUFFIX = "DQ"
#: Zusatz der Dead-Letter-Queue.
DEAD_LETTER_SUFFIX = "XQ"


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
        """Verbindung aufbauen. Idempotent."""
        if self._connection is None or self._connection.is_closed:
            try:
                self._connection = await aio_pika.connect_robust(self._url)
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
        """
        return {q: await self.stats(q) for q in queues}

    # -- Eine einzelne Deklaration ---------------------------------------

    async def _probe(self, name: str) -> _Probe:
        """Eine Queue passiv deklarieren. ``messages is None`` heisst
        "nicht vorhanden".

        Der Kanal ist ein Wegwerfkanal (KTD11): AMQP schliesst ihn, wenn die
        Deklaration fehlschlaegt, und ein geteilter Kanal waere danach fuer
        alle weiteren Abfragen tot.
        """
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
            if kanal is not None and not kanal.is_closed:
                try:
                    await kanal.close()
                except Exception:
                    # Ein Kanal, der sich nicht schliessen laesst, ist bereits
                    # fort. Daran darf keine Abfrage scheitern.
                    pass
