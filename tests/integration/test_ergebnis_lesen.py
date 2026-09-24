"""Der Ergebnis-Leser gegen einen echten Valkey (U1).

Was hier geprueft wird, kann eine Attrappe nicht zeigen: dass die Frist aus
dem **eigenen** Klienten kommt. ``get_result`` reicht keine durch -- der
Aufruf ist ein blanker GET ohne Deadline --, und der Klient, den die
Bibliothek sich sonst selbst baut, ist fuer den Worker-Pfad eingestellt:
fuenf Versuche mit Backoff. Fuer einen Worker ist das richtig. Fuer die
Detailseite ist es falsch.

Test-Liste (AGENTS.md):

1. [x] Ein totes Backend gibt in unter einer Sekunde auf, nicht nach fuenf
       Versuchen mit Backoff.
2. [x] Ein echtes Ergebnis aus dem Backend wird gelesen und dargestellt.
"""

from __future__ import annotations

import time

import pytest

from swarmq_dashboard.web.ergebnis import SwarmQErgebnisleser

pytestmark = pytest.mark.timeout(60)

#: Laenger als ein Aufbau je dauern darf, kuerzer als die fuenf Versuche mit
#: Backoff, die der Kern fuer den Worker-Pfad einstellt (gemessen 3,2 s).
FRIST_S = 1.0


async def test_ein_totes_backend_gibt_schnell_auf(broker_url: str) -> None:
    """R5: Das Nachschlagen haelt die Seite nie auf.

    Der Port ist zu, nichts horcht. Die Antwort "das Backend ist weg" ist auf
    dieser Seite so viel wert wie jede andere -- und sie muss sofort kommen,
    nicht nach drei Sekunden Backoff.
    """
    leser = SwarmQErgebnisleser(broker_url, "valkey://localhost:6300/0")

    beginn = time.monotonic()
    bild = await leser.lesen("gibtesnicht")
    gebraucht = time.monotonic() - beginn

    assert bild.zustand == "nicht_erreichbar"
    assert bild.grund
    assert gebraucht < FRIST_S, f"der Aufbau dauerte {gebraucht:.1f}s"
    await leser.close()


async def test_ein_echtes_ergebnis_wird_gelesen_und_dargestellt(
    broker_url: str, backend_url: str,
) -> None:
    """AE1 gegen den echten Schluessel, nicht gegen eine Attrappe.

    Abgelegt wird ueber die Bibliothek selbst -- ``store_result`` mit dem
    Paket, das ``_unpack`` beim Lesen erwartet. Damit prueft dieser Test
    genau die Naht, die eine Attrappe nicht zeigen kann: dass das
    Packformat der Bibliothek ihre Sache bleibt (R1a) und das Dashboard es
    nirgends nachbaut.
    """
    import uuid

    import valkey.asyncio
    from swarmq.backend.valkey import ValkeyBackend
    from swarmq.serialization import encode_result

    task_id = f"sqd-itest-{uuid.uuid4().hex[:8]}"
    client = valkey.asyncio.Valkey.from_url(backend_url)
    backend = ValkeyBackend(client=client)
    try:
        await backend.store_result(
            task_id,
            encode_result({"is_error": False, "result": {"pid": 4711, "note": "grün"}}),
            ttl_ms=60_000,
        )

        leser = SwarmQErgebnisleser(broker_url, backend_url)
        try:
            bild = await leser.lesen(task_id)
        finally:
            await leser.close()
    finally:
        await backend.delete_result(task_id)
        await client.aclose()

    assert bild.zustand == "da"
    assert bild.art == "fertig"
    assert bild.text == '{"note": "grün", "pid": 4711}'
    assert bild.abgeschnitten is False
