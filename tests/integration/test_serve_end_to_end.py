"""Der Dienst laeuft wirklich (U18).

Die eine Beweisart, die diesem Projekt bis hierher vollstaendig fehlte. Alle
bisherigen Belege -- eigene wie die zweier Pruefrunden -- stammten aus
gelesenem Code, Unit-Tests mit Attrappen und kleinen Messskripten. Genau
deshalb konnten sechs Teile gebaut, getestet und **nicht angeschlossen**
bleiben: jeder Test rief das Teil direkt auf, und niemand hat den Dienst je
gestartet.

Dieser Test startet ihn als eigenen Prozess, so wie ein Betreiber es tut,
und spricht ueber das Netz mit ihm.

Test-Liste (AGENTS.md):

1. Der Prozess kommt hoch und beantwortet eine Anfrage.
2. Ohne Sitzung fuehrt jede Seite zur Anmeldung -- und jede
   Schnittstelle antwortet mit 401 (R21).
3. Die Anmeldeseite steht ohne Sitzung -- sonst kaeme niemand hinein.
4. Das Stylesheet wird ausgeliefert, und zwar aus dem Paket (KTD7).
5. Der Prozess laesst sich wieder beenden.
6. Eine echte Anmeldung ueber das Netz gelingt -- mit den Cookie-Regeln
   eines richtigen Klienten.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def dienst(tmp_path: Path, broker_url: str, backend_url: str,
           stream_url: str) -> Iterator[str]:
    """Der Dienst als eigener Prozess -- ueber die echte Kommandozeile."""
    port = _freier_port()
    umgebung = {
        **os.environ,
        "SWARMQ_BROKER_URL": broker_url,
        "SWARMQ_BACKEND_URL": backend_url,
        "SQD_STREAM_URL": stream_url,
        "SQD_DATA_DIR": str(tmp_path / "sqd-data"),
    }
    prozess = subprocess.Popen(
        # ``--insecure-http`` ist noetig und zugleich Teil der Aussage: ueber
        # Klartext-HTTP traegt das Dashboard nur mit diesem Schalter, weil
        # die Cookies sonst ``Secure`` tragen und kein Klient sie schickt.
        [sys.executable, "-m", "swarmq_dashboard.cli", "serve",
         "--host", "127.0.0.1", "--port", str(port), "--insecure-http"],
        env=umgebung, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    basis = f"http://127.0.0.1:{port}"
    try:
        ende = time.time() + 30.0
        while time.time() < ende:
            if prozess.poll() is not None:
                raise AssertionError(
                    f"der Dienst ist beim Start gestorben:\n{prozess.communicate()[0]}"
                )
            try:
                httpx.get(f"{basis}/login", timeout=1.0)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            prozess.kill()
            raise AssertionError("der Dienst kam binnen 30 s nicht hoch")
        yield basis
    finally:
        prozess.terminate()
        try:
            prozess.wait(timeout=10)
        except subprocess.TimeoutExpired:
            prozess.kill()
            prozess.wait(timeout=5)


def test_der_dienst_kommt_hoch_und_antwortet(dienst: str) -> None:
    """Der Satz, den dieses Projekt bis heute nicht belegen konnte."""
    antwort = httpx.get(f"{dienst}/login", timeout=5.0)
    assert antwort.status_code == 200
    assert "swarmq" in antwort.text.lower()


def test_ohne_sitzung_ist_alles_geschuetzt(dienst: str) -> None:
    """R21. Ueber das Netz geprueft, nicht ueber den Testklienten.

    Je Pfad **eine** erwartete Antwort. Die erste Fassung liess
    ``in (401, 302, 303)`` gelten -- und hat damit genau den Fehler
    durchgewinkt, ueber den ein Mensch dann stolperte: die Startseite
    antwortete mit einem nackten JSON-401, obwohl es eine Anmeldeseite gibt.
    Eine Zusicherung, die drei Antworten zugleich fuer richtig haelt, prueft
    keine davon.
    """
    # Seiten, die ein Mensch aufmacht: ab zur Anmeldung.
    for pfad in ("/", "/tasks", "/dlq"):
        antwort = httpx.get(f"{dienst}{pfad}", timeout=5.0,
                            headers={"accept": "text/html"},
                            follow_redirects=False)
        assert antwort.status_code == 303, (
            f"{pfad} antwortet mit {antwort.status_code} statt zur Anmeldung "
            "zu schicken"
        )
        assert antwort.headers["location"] == "/login"

    # Schnittstellen: 401, und keine Umleitung.
    for pfad, art in (("/api/status", "application/json"),
                      ("/live", "text/event-stream")):
        antwort = httpx.get(f"{dienst}{pfad}", timeout=5.0,
                            headers={"accept": art}, follow_redirects=False)
        assert antwort.status_code == 401, (
            f"{pfad} antwortet mit {antwort.status_code} statt 401"
        )


def test_das_stylesheet_kommt_aus_dem_paket(dienst: str) -> None:
    """KTD7. Kein fremdes Netz -- und die Anmeldeseite braucht es, bevor
    ueberhaupt jemand angemeldet ist."""
    antwort = httpx.get(f"{dienst}/static/app.css", timeout=5.0)
    assert antwort.status_code == 200
    assert "--brand" in antwort.text or "@layer" in antwort.text
    assert "http://" not in antwort.text and "https://" not in antwort.text


def test_das_live_skript_wird_ausgeliefert(dienst: str) -> None:
    """Es war die Naht, die am laengsten unbemerkt fehlte."""
    antwort = httpx.get(f"{dienst}/static/live.js", timeout=5.0)
    assert antwort.status_code == 200
    assert 'EventSource("/live")' in antwort.text


def test_eine_echte_anmeldung_gelingt(dienst: str, tmp_path: Path) -> None:
    """Der Satz, an dem der erste echte Start scheiterte.

    Ueber Klartext-HTTP schickt kein Klient ein ``Secure``-Cookie -- auch
    nicht an 127.0.0.1. Der POST kam ohne Sitzung an, und die Anmeldung
    antwortete "Das Anmeldeformular ist abgelaufen". Keine Attrappe der
    Testreihe konnte das zeigen: der Testklient spricht die Anwendung
    unmittelbar an und kennt keine Cookie-Regeln.
    """
    import re

    from argon2 import PasswordHasher

    from swarmq_dashboard.accounts import AccountStore
    from swarmq_dashboard.config import accounts_path

    store = AccountStore(accounts_path(tmp_path / "sqd-data"),
                         hasher=PasswordHasher(time_cost=2, memory_cost=1024,
                                               parallelism=1))
    store.create("ops", "ein-langes-passwort")
    store.close()

    with httpx.Client(base_url=dienst, timeout=10.0, follow_redirects=False) as c:
        seite = c.get("/login")
        merkmal = re.search(r'name="_csrf_token"\s+value="([^"]*)"', seite.text)
        assert merkmal, seite.text[:300]
        antwort = c.post("/login", data={"account": "ops",
                                         "password": "ein-langes-passwort",
                                         "_csrf_token": merkmal.group(1)})
        assert antwort.status_code in (302, 303), (
            f"die Anmeldung scheitert ueber das Netz: {antwort.status_code} "
            f"{antwort.text[:200]}"
        )
        seite = c.get("/tasks")
        assert seite.status_code == 200, seite.status_code
        assert "Dead Letter" in seite.text, "die Navigation fehlt"
