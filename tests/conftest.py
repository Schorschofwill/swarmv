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


@pytest.fixture(autouse=True)
def frischer_puffer_registry() -> Iterator[None]:
    """Prozessweiten Puffer-Registry zwischen Tests leeren.

    ``buffer`` haelt einen Puffer je Strom-Adresse im Modul, damit zwei
    swarmq-Instanzen im selben Prozess sich einen teilen. Ohne Zuruecksetzen
    saehe ein Test die Ereignisse seines Vorgaengers.
    """
    from swarmq_dashboard.buffer import reset_shared

    reset_shared()
    yield
    reset_shared()
