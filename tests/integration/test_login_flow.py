"""Anmelden, arbeiten, abmelden -- gegen echte Dienste.

Test-Liste (AGENTS.md):

1. Der vollstaendige Ablauf aus Anmelden, Aufruf und Abmelden.
2. Die Sitzung ueberlebt einen Neustart des Dienstes -- sie liegt in der
   Datei, nicht im Arbeitsspeicher (KTD8).
3. Ein gesperrtes Konto verliert seine Sitzung sofort, ohne Neustart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from argon2 import PasswordHasher
from litestar.testing import AsyncTestClient

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.web.app import create_app

pytestmark = pytest.mark.integration

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
_PASSWORT = "ein-langes-passwort"
BASIS = "https://testserver.local"


def _config(tmp_path: Path, broker: str, backend: str, strom: str) -> DashboardConfig:
    c = DashboardConfig(
        broker_url=broker, backend_url=backend, stream_url=strom,
        retention_days=7, unconfirmed_after_s=15.0, disconnected_after_s=40.0,
        session_idle_s=1800.0, backlog_cap=300, stream_stale_after_s=120.0,
        data_dir=tmp_path / "sqd-data",
    )
    s = AccountStore(c.accounts_file, hasher=_BILLIG)
    s.create("ops", _PASSWORT)
    s.close()
    return c


async def _anmelden(c: AsyncTestClient) -> str:
    await c.get("/login")
    await c.post("/login", data={"account": "ops", "password": _PASSWORT},
                 follow_redirects=False)
    return str(c.cookies.get("csrftoken"))


async def test_anmelden_arbeiten_abmelden(
    tmp_path: Path, broker_url: str, backend_url: str, stream_url: str
) -> None:
    config = _config(tmp_path, broker_url, backend_url, stream_url)
    async with AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS) as c:
        merkmal = await _anmelden(c)
        bericht = await c.get("/api/status")
        assert bericht.status_code == 200
        assert bericht.json()["stream"]["ok"] is True

        assert (await c.post("/logout", headers={"x-csrftoken": merkmal},
                             follow_redirects=False)).status_code == 303
        assert (await c.get("/api/status", follow_redirects=False)).status_code in (302, 303, 401)


async def test_die_sitzung_ueberlebt_einen_neustart(
    tmp_path: Path, broker_url: str, backend_url: str, stream_url: str
) -> None:
    """KTD8: die Sitzungen liegen in der Datei. Laegen sie im Arbeitsspeicher,
    waere nach jedem Neustart das ganze Team abgemeldet -- und ein Neustart ist
    im Vorfall das Naheliegendste.
    """
    config = _config(tmp_path, broker_url, backend_url, stream_url)
    async with AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS) as c:
        await _anmelden(c)
        sitzung = str(c.cookies.get("session"))

    # Ein frisch gebauter Dienst auf denselben Dateien: derselbe Neustart.
    async with AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS) as neu:
        neu.cookies.set("session", sitzung, domain="testserver.local")
        assert (await neu.get("/api/status")).status_code == 200, (
            "die Sitzung hat den Neustart nicht ueberlebt"
        )


async def test_sperren_wirkt_sofort(
    tmp_path: Path, broker_url: str, backend_url: str, stream_url: str
) -> None:
    config = _config(tmp_path, broker_url, backend_url, stream_url)
    async with AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS) as c:
        await _anmelden(c)
        assert (await c.get("/api/status")).status_code == 200

        s = AccountStore(config.accounts_file, hasher=_BILLIG)
        s.lock("ops")
        s.close()
        assert (await c.get("/api/status", follow_redirects=False)).status_code in (302, 303, 401)
