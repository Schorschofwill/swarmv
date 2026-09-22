"""Kontrollkanal gegen einen echten Valkey und einen echten Supervisor (U16).

Test-Liste (AGENTS.md):

1. [x] Ein toter Kanal gibt in unter einer Sekunde auf, nicht nach fuenf
       Versuchen mit Backoff.
2. [x] Geschriebener Sollwert und gelesener Sollwert sind derselbe
       (im Durchlauf unter 4 enthalten).
3. [x] Die Rueckmeldungen einer Gruppe werden ueber den Schluesselraum
       gefunden (ebenda -- die Kennung traegt die PID des Supervisors).
4. [x] Ein echter Supervisor bestaetigt einen geschriebenen Sollwert, die
       Ansicht folgt, und eine Ablehnung raeumt den Kanal (Verification
       aus U16, KTD13).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from swarmq_dashboard.control import Sollwert, Steuerung, SwarmQSetpointStore, Zustand
from swarmq_dashboard.stream import StreamUnavailable

pytestmark = pytest.mark.timeout(180)

WURZEL = Path(__file__).resolve().parents[2]
#: Macht die Supervisor-Kennung vorhersagbar (``<worker_id>:p<pid>``).
WORKER_ID = "sqd-ctl-itest"
#: Kurze Gnadenfrist -> kurzes Verfallsdatum der Rueckmeldung.
GNADENFRIST_S = 2

#: Laenger als ein Aufbau je dauern darf, kuerzer als die fuenf Versuche mit
#: Backoff, die der Kern fuer den Worker-Pfad einstellt (gemessen 3,2 s).
FRIST_S = 1.0


async def test_ein_toter_kanal_gibt_schnell_auf() -> None:
    """R33: die Worker-Uebersicht ist die Startseite -- auch mit totem Backend.

    Der Kern stellt fuer den Worker-Pfad fuenf Versuche mit Backoff ein; das
    ist dort richtig und hier falsch. Gemessen kostete ein Aufbau 3,2 s, und
    die Seite bezahlte ihn je Worker-Gruppe.
    """
    store = SwarmQSetpointStore("valkey://127.0.0.1:1/0")
    begonnen = time.monotonic()
    with pytest.raises(StreamUnavailable):
        await store.status_aller("orders-eu")
    gebraucht = time.monotonic() - begonnen
    await store.close()
    assert gebraucht < FRIST_S, f"der Aufbau brauchte {gebraucht:.2f} s"


def _supervisor(queue: str, gruppe: str, backend: str, broker: str) -> subprocess.Popen[str]:
    """Ein echter Supervisor mit Kontrollkanal, Spielraum 4, Start mit einem Prozess."""
    umgebung = dict(os.environ)
    umgebung["SWARMQ_BROKER_URL"] = broker
    umgebung["SWARMQ_BACKEND_URL"] = backend
    umgebung["SWARMQ_WORKER_ID"] = WORKER_ID
    umgebung["SWARMQ_SHUTDOWN_TIMEOUT"] = str(GNADENFRIST_S)
    umgebung["PYTHONUNBUFFERED"] = "1"
    umgebung["PYTHONPATH"] = str(WURZEL / "tests" / "integration")
    umgebung["SQD_ITEST_QUEUE"] = queue
    return subprocess.Popen(
        [sys.executable, "-m", "swarmq.cli", "worker",
         "--module", "_worker_tasks", "--queues", queue, "--concurrency", "1",
         "--processes", "1", "--max-processes", "4", "--group", gruppe,
         "--log-format", "json"],
        cwd=str(WURZEL), env=umgebung, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )


def _mitlesen(proc: subprocess.Popen[str], senke: list[str]) -> None:
    """stdout leeren -- sonst haengt der Supervisor an einer vollen Pipe."""

    def lauf() -> None:
        assert proc.stdout is not None
        for zeile in proc.stdout:
            senke.append(zeile)

    threading.Thread(target=lauf, daemon=True).start()


async def _warten(
    fn: Callable[[], Any], bedingung: Callable[[Any], bool], *, frist_s: float, ausgabe: list[str]
) -> Any:
    """Auf ein **Anzeichen** warten, nie eine feste Zeitspanne verschlafen.

    Der Supervisor tastet ab, das Dashboard auch; eine geschlafene Sekunde
    waere in einem von zehn Laeufen zu kurz.
    """
    ende = time.monotonic() + frist_s
    letzter = None
    while time.monotonic() < ende:
        letzter = await fn()
        if bedingung(letzter):
            return letzter
        await __import__("asyncio").sleep(0.2)
    pytest.fail(f"nicht erreicht in {frist_s} s; zuletzt {letzter!r}\n" + "".join(ausgabe[-30:]))


async def test_ein_echter_supervisor_bestaetigt_einen_geschriebenen_sollwert(
    backend_url: str, broker_url: str
) -> None:
    """Die Verifikation aus U16: der Rueckstau-Fluss gegen einen echten Worker.

    Und der zweite Teil von KTD13 am lebenden Objekt: ein Wert ueber dem
    Spielraum wird abgelehnt, die Ansicht fuehrt ihn als abgelehnt **und**
    raeumt ihn aus dem Kanal -- sonst wendete ihn ein spaeter startender
    Supervisor mit groesserem Spielraum an.
    """
    queue = f"sqdctl_{uuid.uuid4().hex[:8]}"
    gruppe = f"grp-{uuid.uuid4().hex[:8]}"
    proc = _supervisor(queue, gruppe, backend_url, broker_url)
    kennung = f"{WORKER_ID}:p{proc.pid}"
    ausgabe: list[str] = []
    _mitlesen(proc, ausgabe)
    steuerung = Steuerung(SwarmQSetpointStore(backend_url))
    try:
        # Der Supervisor meldet sich mit seinen Startargumenten.
        bild = await _warten(
            lambda: steuerung.bild(gruppe, now=time.time()),
            lambda b: b.gruppe.supervisoren == 1,
            frist_s=60, ausgabe=ausgabe,
        )
        assert bild.gruppe.meldungen[0]["supervisor"] == kennung
        assert bild.gruppe.spielraum == 4
        assert bild.gruppe.herkunft == "Startargumente"
        assert bild.sollwert is None

        # Setzen: die Pruefung laesst 3..3 durch, der Supervisor wendet es an.
        ergebnis = await steuerung.setzen(gruppe, Sollwert(3, 3), now=time.time())
        assert ergebnis.erlaubt is True and ergebnis.wirkungsunbekannt is False
        bestaetigt = await _warten(
            lambda: steuerung.bild(gruppe, now=time.time()),
            lambda b: b.stand is not None and b.stand.zustand is Zustand.BESTAETIGT,
            frist_s=40, ausgabe=ausgabe,
        )
        assert bestaetigt.sollwert == Sollwert(3, 3)
        assert bestaetigt.gruppe.herkunft == "Sollwert"
        assert bestaetigt.stand.bestaetigt_von == 1

        # Aufheben: zurueck zu den Startargumenten.
        await steuerung.aufheben(gruppe)
        zurueck = await _warten(
            lambda: steuerung.bild(gruppe, now=time.time()),
            lambda b: b.sollwert is None and b.gruppe.herkunft == "Startargumente",
            frist_s=40, ausgabe=ausgabe,
        )
        assert zurueck.stand is None

        # Zwei Ablehnungen, beide vor dem Schreiben (R15b). Dieser Supervisor
        # laeuft ohne --autoscale, also nimmt er nur die feste Form an ...
        bereich = await steuerung.setzen(gruppe, Sollwert(2, 4), now=time.time())
        assert bereich.erlaubt is False
        assert "Autoskalierung" in (bereich.grund or "")
        # ... und 12 liegt ueber seinem Spielraum von 4.
        zu_gross = await steuerung.setzen(gruppe, Sollwert(12, 12), now=time.time())
        assert zu_gross.erlaubt is False
        assert "4" in (zu_gross.grund or "") and "12" in (zu_gross.grund or "")

        # Am Kanal vorbei geschrieben -- so, wie ein Supervisor mit kleinerem
        # Spielraum einen fremden Sollwert vorfaende. Der Supervisor lehnt ab.
        await steuerung._store.schreiben(gruppe, Sollwert(12, 12))
        abgelehnt = await _warten(
            lambda: steuerung.bild(gruppe, now=time.time()),
            lambda b: b.stand is not None and b.stand.zustand is Zustand.ABGELEHNT,
            frist_s=40, ausgabe=ausgabe,
        )
        assert abgelehnt.stand.grund, "die Ablehnung kommt ohne Grund"
        # KTD13: der verworfene Wert liegt nicht mehr im Kanal.
        assert await steuerung._store.gelesen(gruppe) is None
    finally:
        await steuerung._store.close()
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.kill()
