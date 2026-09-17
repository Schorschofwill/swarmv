"""Integrations-Ebene: echte Verbindungen zu RabbitMQ und Valkey.

Der Valkey-Datenbankindex ist bewusst **nicht** 0 (KTD9): die
Integrationsvorrichtung von swarmq fuehrt dort bei jedem Testlauf ein FLUSHDB
aus, und zwar automatisch. Ein Dashboard-Strom auf Index 0 waere bei jedem
Lauf der Bibliothekstests weg.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator

import pytest

RABBIT_HOST = os.environ.get("SQD_TEST_RABBITMQ_HOST", "localhost")
RABBIT_PORT = int(os.environ.get("SQD_TEST_RABBITMQ_PORT", "5699"))
VALKEY_HOST = os.environ.get("SQD_TEST_VALKEY_HOST", "localhost")
VALKEY_PORT = int(os.environ.get("SQD_TEST_VALKEY_PORT", "6399"))
#: Index des Backends, wie ihn die Bibliothek benutzt -- hier nur zum Vergleich.
BACKEND_DB = 0
#: Eigener Index des Dashboards.
STREAM_DB = int(os.environ.get("SQD_TEST_STREAM_DB", "1"))


def _reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session", autouse=True)
def check_services() -> None:
    """Die ganze Integrationsstufe ueberspringen, wenn die Dienste fehlen."""
    missing = [
        name
        for name, host, port in (
            ("RabbitMQ", RABBIT_HOST, RABBIT_PORT),
            ("Valkey", VALKEY_HOST, VALKEY_PORT),
        )
        if not _reachable(host, port)
    ]
    if missing:
        pytest.skip(f"nicht erreichbar: {', '.join(missing)} — docker compose up", allow_module_level=True)


@pytest.fixture(scope="session")
def broker_url() -> str:
    return f"amqp://guest:guest@{RABBIT_HOST}:{RABBIT_PORT}/"


@pytest.fixture(scope="session")
def backend_url() -> str:
    return f"valkey://{VALKEY_HOST}:{VALKEY_PORT}/{BACKEND_DB}"


@pytest.fixture(scope="session")
def stream_url() -> str:
    return f"valkey://{VALKEY_HOST}:{VALKEY_PORT}/{STREAM_DB}"


@pytest.fixture
def stream_env(
    monkeypatch: pytest.MonkeyPatch, broker_url: str, backend_url: str, stream_url: str
) -> Iterator[None]:
    monkeypatch.setenv("SWARMQ_BROKER_URL", broker_url)
    monkeypatch.setenv("SWARMQ_BACKEND_URL", backend_url)
    monkeypatch.setenv("SQD_STREAM_URL", stream_url)
    yield
