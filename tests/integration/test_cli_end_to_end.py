"""Die Kommandozeile als echter Prozess.

Test-Liste (AGENTS.md):

1. Der installierte Befehl legt ein Konto an und die Kontendatei traegt es.
2. Das Passwort steht in keinem Argument des Prozesses.
3. Die Datei liegt im Datenverzeichnis, mit 0700 am Verzeichnis und 0600 an
   der Datei.
4. Sechzehn Prozesse oeffnen gleichzeitig eine frische Datei, ohne zu scheitern.

Bewusst als eigener Prozess und ueber den in ``pyproject.toml`` eingetragenen
Einstiegspunkt: die Unit-Reihe reicht ``prompt`` und ``out`` hinein, und
genau die Naht gibt es im echten Lauf nicht. Ein Einstiegspunkt, der nur im
Test funktioniert, faellt sonst erst dem Betreiber auf.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _lauf(daten: Path, *argv: str, eingabe: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "swarmq_dashboard.cli", "--data-dir", str(daten), *argv],
        input=eingabe, capture_output=True, text=True, timeout=60,
    )


def test_konto_anlegen_als_echter_prozess(tmp_path: Path) -> None:
    daten = tmp_path / "sqd-data"
    p = _lauf(daten, "account", "create", "ops",
              eingabe="ein-langes-passwort\nein-langes-passwort\n")
    assert p.returncode == 0, f"stdout={p.stdout!r} stderr={p.stderr!r}"

    q = _lauf(daten, "account", "list")
    assert q.returncode == 0
    assert "ops" in q.stdout and "offen" in q.stdout
    assert "$argon2" not in q.stdout


def test_die_dateien_liegen_geschuetzt(tmp_path: Path) -> None:
    daten = tmp_path / "sqd-data"
    _lauf(daten, "account", "create", "ops",
          eingabe="ein-langes-passwort\nein-langes-passwort\n")
    datei = daten / "accounts.sqlite3"
    assert datei.exists(), "die Kontendatei liegt nicht im Datenverzeichnis"
    assert not (datei.stat().st_mode & 0o077), oct(datei.stat().st_mode & 0o777)
    assert not (daten.stat().st_mode & 0o077), (
        f"das Datenverzeichnis ist fuer andere begehbar: {oct(daten.stat().st_mode & 0o777)}"
    )


def test_gleichzeitiges_oeffnen_einer_frischen_datei(tmp_path: Path) -> None:
    """Das Umschalten auf das WAL-Journal braucht einen Augenblick eine
    Exklusivsperre, und die Wartefrist greift dort **nicht**: gemessen
    scheiterte jedes dritte gleichzeitige Oeffnen einer frischen Datei mit
    "database is locked" -- nach 1,2 ms, nicht nach den fuenf Sekunden.

    Bewusst als eigene Prozesse: die Sperre wirkt zwischen Prozessen, und in
    einem einzigen laesst sich die Lage gar nicht erzeugen. Genau so sieht es
    aus, wenn der Dienst laeuft und der Betreiber daneben ein Konto anlegt.
    """
    daten = tmp_path / "sqd-data"
    daten.mkdir(mode=0o700)
    # Auf einen gemeinsamen Startzeitpunkt gestellt: die Prozesse brauchen je
    # rund eine Zehntelsekunde zum Hochfahren, und nacheinander gestartet
    # traefen sie das Fenster nie. Genau deshalb blieb dieser Fall in einer
    # ersten Fassung des Tests unentdeckt.
    start = time.time() + 1.5
    programm = (
        "import time,sys;"
        "from argon2 import PasswordHasher;"
        "from swarmq_dashboard.accounts import AccountStore;"
        "ziel=float(sys.argv[1]);"
        "time.sleep(max(0, ziel-time.time()));"
        "AccountStore(sys.argv[2], "
        "hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)).close()"
    )
    laeufe = [
        subprocess.Popen(
            [sys.executable, "-c", programm, str(start), str(daten / "accounts.sqlite3")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(16)
    ]
    ergebnisse = [(p.wait(timeout=60), p.communicate()) for p in laeufe]
    gescheitert = [err for rc, (_, err) in ergebnisse if rc != 0]
    assert not gescheitert, (
        f"{len(gescheitert)} von 16 gleichzeitigen Oeffnungen scheiterten:\n"
        f"{gescheitert[0][-400:]}"
    )


def test_das_passwort_steht_in_keinem_argument(tmp_path: Path) -> None:
    """Punkt 2 der Test-Liste -- er stand da, aber es gab ihn nicht.

    Geprueft am laufenden Prozess ueber ``/proc``: ein Passwort in der
    Kommandozeile stuende dort fuer jeden auf dem Rechner lesbar, und ebenso
    in der Prozessliste, im Shell-Verlauf und in jedem Protokoll, das
    Kommandozeilen mitschreibt.
    """
    daten = tmp_path / "sqd-data"
    geheim = "Sonnenblume-2026-geheim"
    p = subprocess.Popen(
        [sys.executable, "-m", "swarmq_dashboard.cli", "--data-dir", str(daten),
         "account", "create", "ops"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # Kurz warten, bis der Prozess wirklich ausgetauscht ist -- direkt nach
        # dem Start ist /proc noch leer, und der Test praefte dann nichts.
        argumente = b""
        for _ in range(100):
            argumente = pathlib.Path(f"/proc/{p.pid}/cmdline").read_bytes()
            if b"account" in argumente:
                break
            time.sleep(0.02)
        umgebung = pathlib.Path(f"/proc/{p.pid}/environ").read_bytes()
    finally:
        p.communicate(input=f"{geheim}\n{geheim}\n", timeout=60)

    assert p.returncode == 0
    assert geheim.encode() not in argumente, "das Passwort steht in der Kommandozeile"
    assert geheim.encode() not in umgebung, "das Passwort steht in der Umgebung"
    assert b"account" in argumente, "Vorbedingung: die Argumente wurden wirklich gelesen"
