"""Gemeinsame Fixtures. Ebene 1 von dreien (AGENTS.md, Dateistruktur)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Alle SWARMQ_*- und SQD_*-Variablen entfernen.

    Ohne das faerbt die Umgebung des Entwicklers auf die Tests ab: wer lokal
    SWARMQ_BACKEND_URL gesetzt hat, sieht einen anderen Testausgang als die CI.
    """
    import os

    for key in list(os.environ):
        if key.startswith(("SWARMQ_", "SQD_")):
            monkeypatch.delenv(key, raising=False)
    yield
