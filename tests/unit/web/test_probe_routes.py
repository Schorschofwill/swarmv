"""Die Seite, von der aus eine Probe losgeht (R27, und neu ausserhalb des Plans).

Test-Liste (AGENTS.md):

1. [ ] Die Seite steht in der Navigation und zeigt ein Formular.
2. [ ] Abschicken reiht ein und nennt die Zahl der Proben.
3. [ ] Jede Einreihung steht im Audit-Strom, mit Konto und Ziel.
4. [ ] Eine unsinnige Eingabe wird begruendet abgewiesen, ohne zu senden.
5. [ ] Die Aktion braucht Sitzung und Merkmal.
6. [ ] Ein nicht erreichbarer Broker wird benannt, nicht als Traceback.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from litestar.testing import AsyncTestClient

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.broker import QueueStats
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.web.app import create_app
from swarmq_dashboard.web.probe import ProbeAuftrag

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
_PASSWORT = "ein-langes-passwort"
BASIS = "https://testserver.local"


class FakeBroker:
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def stats_many(self, queues: list[str]) -> dict[str, QueueStats]:
        return {q: QueueStats(queue=q, exists=True) for q in queues}


class FakeSender:
    """Statt des echten Einreihens: schreibt die Auftraege mit."""

    def __init__(self, fehler: Exception | None = None) -> None:
        self.auftraege: list[ProbeAuftrag] = []
        self.fehler = fehler

    async def senden(self, auftrag: ProbeAuftrag) -> list[str]:
        if self.fehler is not None:
            raise self.fehler
        self.auftraege.append(auftrag)
        return [f"probe-{i}" for i in range(auftrag.anzahl)]

    async def close(self) -> None: ...


def _rueckmeldung(html: str) -> str:
    """Nur den Hinweis nach dem Abschicken, ohne den Rest der Seite."""
    treffer = re.search(r'role="(?:status|alert)">(.*?)</p>', html, re.S)
    return re.sub(r"<[^>]+>", " ", treffer.group(1)).strip() if treffer else ""


def _config(tmp_path: Path) -> DashboardConfig:
    c = DashboardConfig(
        broker_url="amqp://guest:guest@127.0.0.1:1/", backend_url="valkey://127.0.0.1:1/0",
        stream_url="valkey://127.0.0.1:1/1", retention_days=7, unconfirmed_after_s=15.0,
        disconnected_after_s=40.0, session_idle_s=1800.0, backlog_cap=300,
        stream_stale_after_s=120.0, data_dir=tmp_path / "sqd-data",
    )
    s = AccountStore(c.accounts_file, hasher=_BILLIG)
    s.create("ops", _PASSWORT)
    s.close()
    return c


async def _angemeldet(config: DashboardConfig, *, sender: Any | None = None) -> Any:
    app = create_app(config, hasher=_BILLIG, broker=FakeBroker(), probe_sender=sender)
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="_csrf_token"\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return c, app


async def test_die_seite_zeigt_ein_formular_und_steht_in_der_navigation(tmp_path: Path) -> None:
    """Was man nicht findet, benutzt man nicht."""
    c, _ = await _angemeldet(_config(tmp_path), sender=FakeSender())
    try:
        antwort = await c.get("/probe")
        assert antwort.status_code == 200
        text = antwort.text
        assert 'action="/probe"' in text, "kein Formular zum Abschicken"
        assert "sleep_s" in text and "anzahl" in text
        assert 'href="/probe"' in text, "die Seite steht nicht in der Navigation"
    finally:
        await c.__aexit__(None, None, None)


class AuditSpeicher:
    def __init__(self) -> None:
        self.e: list[tuple[str, bytes]] = []

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        self.e.extend((f"{i}-0", w) for i, w in enumerate(events, len(self.e) + 1))

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        return self.e[:count]


async def test_abschicken_reiht_ein_und_steht_im_audit(tmp_path: Path) -> None:
    """R27: die Aktion erzeugt Arbeit auf fremden Workern -- wer sie ausloeste, bleibt nachlesbar."""
    from swarmq_dashboard.audit import AuditLog

    sender = FakeSender()
    c, app = await _angemeldet(_config(tmp_path), sender=sender)
    try:
        app.state.services.audit = AuditLog(AuditSpeicher())
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/probe", data={
            "queue": "cpu", "anzahl": "5", "sleep_s": "0,5", "burn_s": "1.5",
            "note": "Lasttest", "fail": "ja",
        }, headers={"x-csrftoken": merkmal}, follow_redirects=False)
        assert antwort.status_code == 303

        assert len(sender.auftraege) == 1
        auftrag = sender.auftraege[0]
        assert auftrag.queue == "cpu"
        assert auftrag.anzahl == 5
        # Komma als Dezimaltrennzeichen: ein deutsches Formular bekommt
        # "0,5" haeufiger als "0.5", und daraus 0 zu machen waere die
        # schlechteste aller Deutungen.
        assert auftrag.sleep_s == 0.5
        assert auftrag.burn_s == 1.5
        assert auftrag.fail is True
        assert auftrag.note == "Lasttest"

        eintraege = [e for e in await app.state.services.audit.read() if e.action == "probe_send"]
        assert eintraege and eintraege[0].account == "ops"
        assert eintraege[0].target == "cpu"
        assert "5x" in (eintraege[0].detail or "")
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_unsinnige_eingabe_wird_begruendet_abgewiesen(tmp_path: Path) -> None:
    """Abgewiesen wird vor dem Senden -- sonst laegen die ersten Proben schon in der Queue."""
    from swarmq_dashboard.web.probe import ProbeFehleingabe

    sender = FakeSender(fehler=ProbeFehleingabe("Die Anzahl muss zwischen 1 und 200 liegen."))
    c, _ = await _angemeldet(_config(tmp_path), sender=sender)
    try:
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/probe", data={"queue": "io", "anzahl": "9999"},
                               headers={"x-csrftoken": merkmal}, follow_redirects=True)
        assert "Anzahl" in antwort.text
        assert sender.auftraege == []
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_unerreichbarer_broker_wird_benannt(tmp_path: Path) -> None:
    """R33: eine benannte Ursache, kein Traceback -- auch hier."""
    from swarmq_dashboard.stream import StreamUnavailable

    sender = FakeSender(fehler=StreamUnavailable("der Broker ist nicht erreichbar: Error 111"))
    c, _ = await _angemeldet(_config(tmp_path), sender=sender)
    try:
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/probe", data={"queue": "io", "anzahl": "1"},
                               headers={"x-csrftoken": merkmal}, follow_redirects=True)
        assert antwort.status_code == 200
        assert "111" in antwort.text
    finally:
        await c.__aexit__(None, None, None)


async def test_einreihen_braucht_sitzung_und_merkmal(tmp_path: Path) -> None:
    """Eine Aktion, die Arbeit auf fremden Workern erzeugt, braucht beides."""
    sender = FakeSender()
    config = _config(tmp_path)
    c, _ = await _angemeldet(config, sender=sender)
    try:
        ohne = await c.post("/probe", data={"queue": "io"}, follow_redirects=False)
        assert ohne.status_code >= 400
        assert sender.auftraege == []
    finally:
        await c.__aexit__(None, None, None)

    app = create_app(config, hasher=_BILLIG, broker=FakeBroker(), probe_sender=sender)
    fremd = AsyncTestClient(app=app, base_url=BASIS)
    await fremd.__aenter__()
    try:
        antwort = await fremd.get("/probe", follow_redirects=False)
        assert antwort.status_code in (302, 303, 401)
    finally:
        await fremd.__aexit__(None, None, None)


async def test_bei_verzoegerung_sagt_die_rueckmeldung_was_sie_nicht_weiss(tmp_path: Path) -> None:
    """Der Scheduler vergibt beim Feuern eine **neue** Kennung.

    ``schedule(delay=...)`` gibt die des geplanten Eintrags zurueck; der
    spaeter laufende Task bekommt eine andere (``_fire_one`` in swarmq).
    Wer die Probe unter der gemeldeten Kennung sucht, findet nichts -- im
    Betrieb genau so passiert, und die Suche ging erst zum Worker, dann zum
    Scheduler, bevor der Grund gefunden war.

    Die Ansicht sagt das jetzt, und sie nennt den Weg, der traegt: die Notiz.
    """
    sender = FakeSender()
    c, _ = await _angemeldet(_config(tmp_path), sender=sender)
    try:
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/probe", data={
            "queue": "io", "anzahl": "1", "delay_s": "30", "note": "spaeter",
        }, headers={"x-csrftoken": merkmal}, follow_redirects=True)
        # Nur die Rueckmeldung, nicht die ganze Seite: das Formular nennt
        # "Verzögerung" und "Notiz" ohnehin als Feldbeschriftung, und ein
        # Test, der darauf trifft, misst nichts.
        hinweis = _rueckmeldung(antwort.text)
        assert hinweis, "es gibt gar keine Rueckmeldung"
        assert "andere" in hinweis and "Kennung" in hinweis, (
            f"die neue Kennung wird nicht erklaert: {hinweis!r}"
        )
        assert "Notiz" in hinweis, f"der Weg zum Wiederfinden fehlt: {hinweis!r}"
    finally:
        await c.__aexit__(None, None, None)


async def test_rechenlast_im_io_pool_wird_benannt(tmp_path: Path) -> None:
    """Eine rechnende Probe im io-Pool legt den Worker still, solange sie laeuft.

    Der Probe-Task rechnet in einer Schleife; im Rechen-Pool laeuft die in
    einem eigenen Prozess, im io-Pool im Ereignis-Loop des Workers. Zwanzig
    Sekunden Rechenlast heissen dort zwanzig Sekunden, in denen der Worker
    nichts anderes tut -- auch keine Lebenszeichen sendet. Das gehoert
    dazugesagt, bevor jemand es an einem stehenden Dashboard bemerkt.
    """
    sender = FakeSender()
    c, _ = await _angemeldet(_config(tmp_path), sender=sender)
    try:
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/probe", data={
            "queue": "io", "anzahl": "1", "burn_s": "5",
        }, headers={"x-csrftoken": merkmal}, follow_redirects=True)
        hinweis = _rueckmeldung(antwort.text)
        assert hinweis and "blockiert" in hinweis, (
            f"die Rechenlast im io-Pool wird nicht benannt: {hinweis!r}"
        )
    finally:
        await c.__aexit__(None, None, None)
