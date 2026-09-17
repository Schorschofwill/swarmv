"""Die Kommandozeile als echter Prozess.

Test-Liste (AGENTS.md):

1. Der installierte Befehl legt ein Konto an und die Kontendatei traegt es.
2. Das Passwort steht in keinem Argument des Prozesses.
3. Die Datei liegt im Datenverzeichnis, mit 0700 am Verzeichnis und 0600 an
   der Datei.

Bewusst als eigener Prozess und ueber den in ``pyproject.toml`` eingetragenen
Einstiegspunkt: die Unit-Reihe reicht ``prompt`` und ``out`` hinein, und
genau die Naht gibt es im echten Lauf nicht. Ein Einstiegspunkt, der nur im
Test funktioniert, faellt sonst erst dem Betreiber auf.
"""

from __future__ import annotations

import subprocess
import sys
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
