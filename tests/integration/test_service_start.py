"""Der Dienst gegen echte Dienste -- und gegen ihr Fehlen.

Test-Liste (AGENTS.md):

1. Ein vollstaendiger Start meldet Broker, Backend und Strom als erreichbar.
2. Faellt ein Dienst weg und kommt zurueck, wechselt die Anzeige ohne
   Neustart des Dashboards.
3. Die Zustandsseite antwortet auch, waehrend ein Dienst fehlt.

Die zweite Zeile ist der Kern: ein Dashboard, das nach der Rueckkehr eines
Dienstes weiter "nicht erreichbar" sagt, treibt den Betreiber zu einem
Neustart -- im Vorfall die letzte Handlung, die man will.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from litestar.testing import AsyncTestClient

from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.web.app import Services, create_app

pytestmark = pytest.mark.integration


def _config(tmp_path: Path, broker: str, backend: str, strom: str) -> DashboardConfig:
    return DashboardConfig(
        broker_url=broker, backend_url=backend, stream_url=strom,
        retention_days=7, unconfirmed_after_s=15.0, disconnected_after_s=40.0,
        session_idle_s=1800.0, backlog_cap=300, stream_stale_after_s=120.0,
        data_dir=tmp_path / "sqd-data",
    )


async def test_vollstaendiger_start_meldet_dreimal_erreichbar(
    tmp_path: Path, broker_url: str, backend_url: str, stream_url: str
) -> None:
    s = Services(_config(tmp_path, broker_url, backend_url, stream_url))
    await s.startup()
    try:
        z = s.status()
        for teil in ("broker", "backend", "stream"):
            assert z[teil]["ok"] is True, f"{teil}: {z[teil]['reason']}"
        assert z["directory"]["ok"] is True
    finally:
        await s.shutdown()


async def test_rueckkehr_eines_dienstes_ohne_neustart(
    tmp_path: Path, broker_url: str, backend_url: str, stream_url: str
) -> None:
    """Erst gegen einen toten Port, dann gegen den echten -- ohne den Dienst
    dazwischen neu zu bauen.
    """
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        tot = sock.getsockname()[1]

    s = Services(_config(tmp_path, broker_url, backend_url, f"valkey://127.0.0.1:{tot}/1"))
    await s.startup()
    try:
        assert s.status()["stream"]["ok"] is False
        assert s.status()["stream"]["reason"]

        s._stream_url = stream_url
        await s.refresh()
        z = s.status()
        assert z["stream"]["ok"] is True, f"die Rueckkehr wurde nicht bemerkt: {z['stream']}"
        assert z["stream"]["reason"] is None, "der alte Grund steht noch"
    finally:
        await s.shutdown()


async def test_zustandsseite_waehrend_ein_dienst_fehlt(
    tmp_path: Path, broker_url: str, backend_url: str
) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        tot = sock.getsockname()[1]

    from argon2 import PasswordHasher

    from swarmq_dashboard.accounts import AccountStore

    billig = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
    config = _config(tmp_path, broker_url, backend_url, f"valkey://127.0.0.1:{tot}/1")
    konten = AccountStore(config.accounts_file, hasher=billig)
    konten.create("ops", "ein-langes-passwort")
    konten.close()

    app = create_app(config, hasher=billig)
    async with AsyncTestClient(app=app, base_url="https://testserver.local") as client:
        seite = await client.get("/login")
        merkmal = re.search(r'name="?_csrf_token"?\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
        await client.post("/login", data={"account": "ops", "password": "ein-langes-passwort",
                                          "_csrf_token": merkmal},
                          follow_redirects=False)
        antwort = await client.get("/api/status")
        assert antwort.status_code == 200
        k = antwort.json()
        assert k["broker"]["ok"] is True
        assert k["stream"]["ok"] is False and k["stream"]["reason"]
