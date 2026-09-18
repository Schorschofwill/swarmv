"""Anmelden, arbeiten, abmelden -- gegen echte Dienste.

Test-Liste (AGENTS.md):

1. Der vollstaendige Ablauf aus Anmelden, Aufruf und Abmelden.
2. Die Sitzung ueberlebt einen Neustart des Dienstes -- sie liegt in der
   Datei, nicht im Arbeitsspeicher (KTD8).
3. Ein gesperrtes Konto verliert seine Sitzung sofort, ohne Neustart.
4. An- und Abmeldung stehen im Audit, Fehlanmeldungen auch (R27, R22).
5. Das Audit traegt nie das Passwort.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from litestar.testing import AsyncTestClient

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.audit import AuditEntry, AuditLog
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.events import AUDIT_STREAM
from swarmq_dashboard.stream import ValkeyStreamClient
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
    seite = await c.get("/login")
    merkmal = re.search(r'name=_csrf_token value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal},
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


async def _audit_leeren(stream_url: str) -> None:
    """Den Audit-Strom vor dem Lauf raeumen -- mit einer **eigenen**
    Verbindung aus dem Ereignis-Loop des Tests.

    Der Testklient faehrt die Anwendung in einem eigenen Loop; ein Zugriff auf
    ``services.audit`` von hier aus trifft eine Valkey-Verbindung aus einem
    fremden Loop und scheitert mit "attached to a different loop".
    """
    c = ValkeyStreamClient(stream_url)
    await c.connect()
    await c.delete(AUDIT_STREAM)
    await c.close()


async def _audit_lesen(stream_url: str) -> list[AuditEntry]:
    """Den Audit-Strom lesen, ebenfalls mit eigener Verbindung."""
    c = ValkeyStreamClient(stream_url)
    await c.connect()
    try:
        return await AuditLog(c).read()
    finally:
        await c.close()


async def test_an_und_abmeldung_stehen_im_audit(
    tmp_path: Path, broker_url: str, backend_url: str, stream_url: str
) -> None:
    """Covers R27, und R22 fuer die Fehlanmeldung.

    Gegen echtes Valkey: der Audit liegt dort. Ohne Strom ist er leer, und
    jede Zusicherung darauf hielte trivial -- genau daran ist eine erste
    Fassung dieses Tests gescheitert.
    """
    await _audit_leeren(stream_url)
    config = _config(tmp_path, broker_url, backend_url, stream_url)
    async with AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS) as c:
        seite = await c.get("/login")
        merkmal = re.search(r'name=_csrf_token value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
        await c.post("/login", data={"account": "ops", "password": "falsch",
                                     "_csrf_token": merkmal}, follow_redirects=False)
        gueltig = await _anmelden(c)
        await c.post("/logout", headers={"x-csrftoken": gueltig}, follow_redirects=False)

    eintraege = await _audit_lesen(stream_url)
    arten = [(e.action, e.outcome) for e in eintraege]
    assert ("login", "abgewiesen") in arten, f"die Fehlanmeldung fehlt: {arten}"
    assert ("login", "ok") in arten, f"die Anmeldung fehlt: {arten}"
    assert ("logout", "ok") in arten, f"die Abmeldung fehlt: {arten}"
    assert all(e.account == "ops" for e in eintraege), [e.account for e in eintraege]
    assert all(e.target for e in eintraege), "die Herkunft fehlt"


async def test_das_audit_traegt_nie_das_passwort(
    tmp_path: Path, broker_url: str, backend_url: str, stream_url: str
) -> None:
    """Der Audit-Strom hat weder die Rechtezusage aus R7a noch die Trimmung
    des Verzeichnisses -- was hier hineingeraet, steht dauerhaft und offen.
    """
    await _audit_leeren(stream_url)
    config = _config(tmp_path, broker_url, backend_url, stream_url)
    geheim = "Sonnenblume-2026-geheim"
    async with AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS) as c:
        for passwort in (geheim, _PASSWORT):
            seite = await c.get("/login")
            merkmal = re.search(r'name=_csrf_token value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
            await c.post("/login", data={"account": "ops", "password": passwort,
                                         "_csrf_token": merkmal}, follow_redirects=False)

    eintraege = await _audit_lesen(stream_url)
    assert len(eintraege) >= 2, "Vorbedingung: es muessen Eintraege entstanden sein"
    roh = " ".join(f"{e.account} {e.target} {e.detail}" for e in eintraege)
    assert geheim not in roh, f"das Passwort steht im Audit: {roh}"
