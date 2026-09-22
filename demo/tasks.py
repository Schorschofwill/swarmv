"""Ein paar Aufgaben zum Ansehen -- fuer den oertlichen Demo-Lauf.

Zwei Queues, damit die Worker-Uebersicht zwei Gruppen zeigt: ``io`` fuer
wartende Arbeit, ``cpu`` fuer rechnende.
"""

from __future__ import annotations

import asyncio
import hashlib
import random

from swarmq.task import Task, TaskInfo

# Der Probe-Task des Dashboards. Ein Import genuegt -- die Registrierung
# haengt an der Klassendefinition, nicht am Modulnamen in ``--module``.
# Ein zweites ``--module`` gaebe es nicht: das Argument ist einmalig.
from swarmq_dashboard.probe import ProbeTask  # noqa: F401


class BerichtHolen(Task):
    """Wartende Arbeit: tut so, als spraeche sie mit einem fremden Dienst."""

    task_info = TaskInfo(name="demo.bericht_holen", queues=["io"])

    async def run(self, kunde: str, tage: int = 30) -> dict[str, object]:
        await asyncio.sleep(random.uniform(0.3, 2.0))
        return {"kunde": kunde, "tage": tage, "zeilen": random.randint(10, 900)}


class RechnungPruefen(Task):
    """Wartende Arbeit, die manchmal scheitert -- damit die Dead-Letter-Seite etwas zeigt."""

    task_info = TaskInfo(name="demo.rechnung_pruefen", queues=["io"], max_retries=1)

    async def run(self, nummer: str) -> str:
        await asyncio.sleep(random.uniform(0.2, 1.0))
        if nummer.endswith("7"):
            raise ValueError(f"Rechnung {nummer}: Summe stimmt nicht mit den Posten ueberein")
        return f"{nummer} geprueft"


class PruefsummeBilden(Task):
    """Rechnende Arbeit fuer den CPU-Pool."""

    task_info = TaskInfo(name="demo.pruefsumme", queues=["cpu"])

    async def run(self, runden: int = 200_000) -> str:
        wert = b"swarmq"
        for _ in range(runden):
            wert = hashlib.sha256(wert).digest()
        return wert.hex()[:16]
