"""Reiht ein paar Aufgaben ein -- mit dem Anwendungs-Hook, damit das
Dashboard sie ab der Einreihung kennt (mit Argumenten und Zeitpunkt)."""

from __future__ import annotations

import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))

from swarmq import SwarmQ                                   # noqa: E402
from swarmq.backend.valkey import ValkeyBackend             # noqa: E402
from swarmq.broker.rabbitmq import RabbitMQBroker           # noqa: E402
from swarmq_dashboard.hooks.app import install              # noqa: E402

import tasks                                                # noqa: E402, F401


async def main() -> None:
    broker = RabbitMQBroker(os.environ["SWARMQ_BROKER_URL"])
    backend = ValkeyBackend(os.environ["SWARMQ_BACKEND_URL"])
    await broker.connect()
    await backend.connect()
    app = SwarmQ(broker=broker, backend=backend)
    install(app)          # der Anwendungs-Hook -- eine Zeile (R3a)
    kunden = ["Fischer GmbH", "Novak & Partner", "Blau AG", "Steiner OG", "Wagner KG"]
    eingereiht = 0
    for i in range(18):
        await app.schedule("demo.bericht_holen", kunde=random.choice(kunden),
                           tage=random.choice([7, 30, 90]))
        eingereiht += 1
    for i in range(12):
        await app.schedule("demo.rechnung_pruefen", nummer=f"RG-2026-{1000 + i}")
        eingereiht += 1
    for _ in range(6):
        await app.schedule("demo.pruefsumme", runden=random.randint(120_000, 400_000))
        eingereiht += 1
    await broker.close()
    await backend.close()
    print(f"{eingereiht} Aufgaben eingereiht")


asyncio.run(main())
