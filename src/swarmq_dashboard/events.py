"""Ereignishuelle und Stromschluessel.

Ein Format fuer alle drei Aufnahmestellen (KTD2): den Signal-Handler und die
``pre_execute``-Middleware im Worker, und die ``post_enqueue``-Middleware in der
einreihenden Anwendung. Die Nutzlast bleibt unangetastet -- R3 verlangt sie
vollstaendig, und die tatsaechlich verschickten Signal-Nutzlasten weichen an
mehreren Stellen von der Dokumentation der Bibliothek ab. Wer hier Felder
auswaehlt, verliert stillschweigend die, die morgen dazukommen.

Eigener Schluesselpraefix ``sqd:`` (KTD9): die Bibliothek benutzt ``sq:``, und
ihre Testvorrichtung leert den Datenbankindex 0 vollstaendig.
"""

from __future__ import annotations

import enum
from typing import Any

import msgspec

#: Strom der Task- und Worker-Ereignisse.
TASK_STREAM = "sqd:events"
#: Strom der Nutzeraktionen (R27). Eigener Schluessel, eigene Trimmung -- das
#: Budget des Task-Stroms begrenzt ihn nicht (R9).
AUDIT_STREAM = "sqd:audit"


class Origin(enum.Enum):
    """Woher das Verzeichnis einen Task kennt (R31).

    Scheduler-Ausloesungen und verzoegerte Einreihungen passieren den
    Anwendungs-Hook nie; von ihnen erfaehrt das Verzeichnis erst beim
    Aufgreifen, und ohne Einreihungszeitpunkt. Die Unterscheidung muss bis in
    die Oberflaeche durchschlagen, sonst liest jemand einen geratenen
    Zeitpunkt als Tatsache.
    """

    ENQUEUE = "einreihung"
    PICKUP = "aufgreifen"


class Event(msgspec.Struct, frozen=True, omit_defaults=True):
    """Ein Ereignis im Strom."""

    type: str
    ts: float
    source: str
    origin: Origin
    payload: dict[str, Any] = msgspec.field(default_factory=dict)


def _tolerant(obj: Any) -> str:
    """Rueckfall fuer alles, was der Kodierer nicht kennt.

    Ein Signal traegt gelegentlich ein Objekt, das kein JSON kennt -- etwa den
    Traceback eines Fehlers. Daran darf das Schreiben nicht scheitern: lieber
    eine lesbare Darstellung als ein verlorenes Ereignis (R4).
    """
    return repr(obj)


_encoder = msgspec.json.Encoder(enc_hook=_tolerant)
_decoder = msgspec.json.Decoder(Event)


def encode(event: Event) -> bytes:
    """Ereignis in Bytes. Wirft nicht an einer fremden Nutzlast."""
    return _encoder.encode(event)


def decode(raw: bytes) -> Event:
    """Bytes zurueck in ein Ereignis."""
    return _decoder.decode(raw)
