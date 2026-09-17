"""Das Paket erklaert, was es importiert (R1).

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Eigene Abhaengigkeitsliste ===
1. Jedes fremde Top-Level-Paket, das der Quellcode importiert, steht in pyproject.toml.
"""

from __future__ import annotations

import ast
import importlib.metadata
import pathlib
import tomllib

STDLIB_UND_EIGEN = {"swarmq_dashboard", "__future__"}


def _importierte_pakete() -> set[str]:
    import sys

    treffer: set[str] = set()
    for datei in pathlib.Path("src/swarmq_dashboard").rglob("*.py"):
        baum = ast.parse(datei.read_text(encoding="utf-8"))
        for knoten in ast.walk(baum):
            if isinstance(knoten, ast.Import):
                treffer |= {a.name.split(".")[0] for a in knoten.names}
            elif isinstance(knoten, ast.ImportFrom) and knoten.level == 0 and knoten.module:
                treffer.add(knoten.module.split(".")[0])
    return {t for t in treffer if t not in sys.stdlib_module_names and t not in STDLIB_UND_EIGEN}


def test_jeder_import_steht_in_der_abhaengigkeitsliste() -> None:
    daten = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
    erklaert = {
        d.split("[")[0].split(">")[0].split("<")[0].split("=")[0].strip()
        for d in daten["project"]["dependencies"]
    }
    # Der Importname ist nicht der Paketname: ``import argon2`` kommt aus
    # ``argon2-cffi``, ``import valkey`` aus ``valkey``. Eine Regel mit
    # Bindestrichen raet das nur -- und raet bei argon2 falsch. Python kennt
    # die Zuordnung, also wird sie gefragt statt nachgebaut.
    zuordnung = importlib.metadata.packages_distributions()

    def erklaert_sich(modul: str) -> bool:
        namen = {modul, modul.replace("_", "-"), *zuordnung.get(modul, ())}
        return bool(namen & erklaert)

    fehlend = sorted(p for p in _importierte_pakete() if not erklaert_sich(p))
    assert not fehlend, (
        f"importiert, aber nicht erklaert: {fehlend}. "
        "Sie kommen heute nur ueber swarmq mit — ein Release, das sie tauscht, "
        "bricht den Dienst beim Start (R1, KTD14)."
    )
