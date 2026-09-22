"""Ein Task zum Ausprobieren -- der einzige, den dieses Paket mitbringt.

Das Dashboard kann einen Task **einreihen**; laufen lassen muss ihn ein
Worker, der seinen Code kennt. Ein Knopf "Testtask senden" ist darum nur so
viel wert wie der Task, den er losschickt: Er muss in fremden Workern
registrierbar sein, ohne deren eigene Tasks zu beruehren, und er muss auf
Ansage langsam sein, scheitern oder rechnen -- sonst prueft er nur, dass
etwas ankam, nicht was dabei passiert.

Im Worker anmelden -- durch **Import** im eigenen Task-Modul, nicht durch ein
zweites ``--module``; das Argument ist einmalig, ein zweites ueberschriebe das
erste::

    # app/tasks.py
    from swarmq_dashboard.probe import ProbeTask  # noqa: F401

Ein Worker ohne eigene Tasks kann auch direkt ``--module
swarmq_dashboard.probe`` fahren.

Was hier **nicht** steht, steht in ``TaskInfo`` und gilt fuer jede
Einreihung gleich: Fristen, Wiederholungen, Ratenbegrenzung, Sperren,
Verfall, Priorität. Die Bibliothek nimmt sie beim Einreihen nicht entgegen,
sie gehoeren zur Registrierung. Was der Absender je Einreihung bestimmen
kann, sind die Argumente hier und die vier Schalter von ``schedule()``:
Queue, Verzoegerung oder Zeitpunkt, und die Entdoppelung.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import time
from typing import Any

from swarmq.task import Task, TaskInfo

#: Obergrenze fuer Schlafen und Rechnen. Ein Tippfehler in einem Formular
#: darf keinen Slot fuer Stunden belegen -- und wer laenger messen will,
#: schickt mehrere Tasks.
MAX_DAUER_S = 300.0
#: Queues, auf denen der Task angemeldet wird. Beide, damit sich derselbe
#: Task an einen wartenden und an einen rechnenden Worker schicken laesst;
#: welcher ihn bekommt, entscheidet die Queue beim Einreihen.
PROBE_QUEUES = ["io", "cpu"]
#: Name, unter dem das Dashboard ihn einreiht.
PROBE_NAME = "sqd.probe"


def _zahl(wert: Any, name: str) -> float:
    """Eine Zeitangabe deuten -- oder benannt abweisen.

    Kein stilles Deuten: "0,5" mit Komma, "abc" oder ein negativer Wert
    sind Eingabefehler, und ein Task, der sie als 0 liest, verschweigt sie.
    """
    try:
        zahl = float(wert)
    except (TypeError, ValueError):
        raise ValueError(f"{name}={wert!r} ist keine Zahl") from None
    if zahl != zahl or zahl in (float("inf"), float("-inf")):
        raise ValueError(f"{name}={wert!r} ist keine brauchbare Zahl")
    if zahl < 0:
        raise ValueError(f"{name}={zahl} ist negativ")
    if zahl > MAX_DAUER_S:
        raise ValueError(f"{name}={zahl} überschreitet die Grenze von {MAX_DAUER_S:g} s")
    return zahl


class ProbeTask(Task):
    """Schlaeft, rechnet oder scheitert -- genau so, wie der Absender es sagt."""

    task_info = TaskInfo(
        name=PROBE_NAME,
        queues=PROBE_QUEUES,
        # Wiederholungen sind eingeschaltet, sonst liesse sich der Weg
        # "scheitert, wird wiederholt, landet im Dead-Letter" gar nicht
        # ausprobieren.
        max_retries=3,
        retry_base_delay=1.0,
        # Eine harte Frist oberhalb der eigenen Obergrenze: der Task soll an
        # seiner Grenze scheitern, nicht an der des Workers.
        soft_timeout=int(MAX_DAUER_S) + 30,
        hard_timeout=int(MAX_DAUER_S) + 60,
    )

    async def run(
        self,
        sleep_s: float = 0.0,
        burn_s: float = 0.0,
        fail: bool = False,
        fail_times: int = 0,
        note: str = "",
    ) -> dict[str, Any]:
        """Die Probe ausfuehren.

        ``sleep_s`` wartet (belegt einen Slot, ohne die CPU zu belasten),
        ``burn_s`` rechnet (belastet sie). Beides zusammen ist erlaubt --
        erst gewartet, dann gerechnet.
        """
        dauer = _zahl(sleep_s, "sleep_s")
        rechnen = _zahl(burn_s, "burn_s")
        begonnen = time.perf_counter()

        if dauer:
            await asyncio.sleep(dauer)
        if rechnen:
            _rechnen(rechnen)

        if fail:
            raise ProbeFehler(f"auf Ansage gescheitert{f': {note}' if note else ''}")

        return {
            "sleep_s": dauer,
            "burn_s": rechnen,
            "note": note,
            "worker": os.environ.get("SWARMQ_WORKER_ID") or socket.gethostname(),
            "pid": os.getpid(),
            "laufzeit_s": round(time.perf_counter() - begonnen, 3),
        }


class ProbeFehler(RuntimeError):
    """Der Fehler, den der Probe-Task auf Ansage wirft."""


def _rechnen(sekunden: float) -> None:
    """Die CPU beschaeftigen, bis die Zeit um ist.

    Bewusst eine Schleife mit Zeitpruefung und keine feste Rundenzahl: die
    haengt sonst an der Geschwindigkeit des Rechners, und "zwei Sekunden
    Rechenlast" waeren auf dem einen zehn und auf dem anderen eine.
    """
    ende = time.perf_counter() + sekunden
    wert = b"swarmq-probe"
    while time.perf_counter() < ende:
        for _ in range(500):
            wert = hashlib.sha256(wert).digest()
