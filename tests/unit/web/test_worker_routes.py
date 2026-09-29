"""Worker-Uebersicht -- die Startseite, die auch ohne Kontrollkanal etwas
Wahres sagt (U14 Teil 2, R14, R14a, R18, R19, R33).

Zwei Dinge machen diese Seite heikel:

* **Sie ist die Startseite.** Wer das Dashboard oeffnet, sieht sie zuerst --
  auch und gerade im Vorfall. Eine leere Tabelle waere dann die schlechteste
  aller Antworten.
* **Der Kontrollkanal fehlt.** Er ist in swarmq gemergt, aber unveroeffentlicht.
  Spielraum, Slot-Kapazitaet und Sollwert lassen sich also nicht fuellen. An
  ihrer Stelle steht eine benannte Ursache, kein leerer Bereich (R33), und
  die Zahl wartender Nachrichten **bleibt** -- sie kommt aus der passiven
  Broker-Beobachtung und ist die einzige Antwort auf "staut es sich?".

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Worker und ihre Quelle ===
1. Covers AE8. Ein Worker ohne Lebenszeichen ueber die Frist erscheint als
   getrennt, mit Zeitpunkt, und die Quelle ist benannt.
2. Faellt das Lebenszeichen aus, steht die schwaechere Quelle da -- und die
   Seite sagt, dass sie eingeschraenkt ist (R14a).
3. Ein zurueckkehrender Worker gilt wieder als verbunden, ohne Zutun.
4. Worker erscheinen nach Gruppe gebuendelt.

=== ZYKLUS 2: Konsumenten ohne Hook (R18) ===
5. Covers AE7. Drei Konsumenten gegen zwei bekannte Worker ergeben den
   Hinweis.
6. Covers AE7a. Vier Konsumenten gegen keinen bekannten Worker nennen alle
   vier, statt eine leere Tabelle zu zeigen.
7. Eine Queue, die nicht abgefragt werden konnte, zeigt ihren Fehlgrund --
   nicht eine Null.

=== ZYKLUS 3: Ohne Kontrollkanal (R33, AE15) ===
8. Covers AE15. Die Seite zeigt Worker und nennt an **einer** Stelle, dass
   Steuerung nicht verfuegbar ist.
9. Die Zahl wartender Nachrichten steht trotzdem da.

=== ZYKLUS 4: Entfernen (R19, R27) ===
10. Ein entfernter Worker verschwindet und steht im Audit.
11. Entfernen braucht Sitzung und Merkmal.
12. Ein getrennter Worker zaehlt nicht als Konsument.
13. Ein werfender Broker ergibt je Queue einen Grund, keine leere Tabelle.
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
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin
from swarmq_dashboard.web.app import create_app

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
_PASSWORT = "ein-langes-passwort"
BASIS = "https://testserver.local"
JETZT = 1_700_000_000.0


class FakeBroker:
    """Die Broker-Beobachtung, wie sie sich seit der Pruefung von Stufe B
    verhaelt: je Queue **entweder** Zahlen **oder** ein Fehlgrund."""

    def __init__(self, stats: dict[str, QueueStats] | None = None) -> None:
        self.stats = stats or {}
        self.gefragt: list[str] = []
        self.verbunden = False

    # Der echte Beobachter hat beides, und der Lebenszyklus ruft es. Eine
    # Attrappe ohne diese Methoden liesse den Dienst gar nicht hochfahren --
    # und eine, die sie nur wegwirft, verdeckte, dass er sie ueberhaupt ruft.
    async def connect(self) -> None:
        self.verbunden = True

    async def close(self) -> None:
        self.verbunden = False

    async def stats_many(self, queues: list[str]) -> dict[str, QueueStats]:
        self.gefragt = list(queues)
        return {q: self.stats.get(q, QueueStats(queue=q, exists=True)) for q in queues}


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


def _worker(config: DashboardConfig, *ereignisse: Event) -> None:
    w = DirectoryWriter(connect(config.directory_file))
    w.apply([(f"{i}000-0", e) for i, e in enumerate(ereignisse, 1)])
    w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                   "on conflict(key) do nothing")
    w._con.commit()
    w.close()


def _meta(wid: str, ts: float, gruppe: str = "reports", queues: list[str] | None = None) -> Event:
    return Event(type="sqd_worker_meta", ts=ts, source=wid, origin=Origin.PICKUP,
                 payload={"worker_id": wid, "slot": 0, "group": gruppe,
                          "queues": queues or ["reports"],
                          "all_queues": (queues or ["reports"]) + ["reports.high"],
                          "concurrency": 4, "pool": "thread", "started_at": ts})


def _herzschlag(wid: str, ts: float) -> Event:
    return Event(type="worker_heartbeat", ts=ts, source=wid, origin=Origin.PICKUP,
                 payload={"worker_id": wid})


async def _angemeldet(config: DashboardConfig, *, broker: object | None = None,
                      mit_app: bool = False) -> Any:
    app = create_app(config, hasher=_BILLIG, broker=broker)
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="?_csrf_token"?\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return (c, app) if mit_app else c


# --- Zyklus 1 -----------------------------------------------------------


async def test_ein_getrennter_worker_mit_zeitpunkt_und_quelle(tmp_path: Path) -> None:
    """Covers AE8, R14a. "Getrennt" ohne Zeitpunkt und ohne Quelle ist eine
    Behauptung; mit beidem ist es eine Auskunft, die man nachpruefen kann.
    """
    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT), _herzschlag("phx-w1:0", JETZT))
    c = await _angemeldet(config, broker=FakeBroker())
    try:
        antwort = await c.get(f"/?jetzt={JETZT + 10_000}")
        assert antwort.status_code == 200
        assert "phx-w1:0" in antwort.text
        assert "getrennt" in antwort.text.lower()
        assert "Lebenszeichen" in antwort.text, "die Quelle wird nicht genannt"
        assert "2023-11-14" in antwort.text, "der Zeitpunkt des letzten Lebenszeichens fehlt"
    finally:
        await c.__aexit__(None, None, None)


async def test_ohne_lebenszeichen_steht_die_schwaechere_quelle_da(tmp_path: Path) -> None:
    """R14a. Das Lebenszeichen schweigt ohne Metrik-Port **ohne Fehlermeldung**
    -- die Seite muss sagen, dass ihre Aussage dann schwaecher ist.
    """
    config = _config(tmp_path)
    _worker(config, _meta("phx-w9:0", JETZT))       # nur Stammdaten, kein Lebenszeichen
    c = await _angemeldet(config, broker=FakeBroker())
    try:
        antwort = await c.get(f"/?jetzt={JETZT + 10}")
        assert "phx-w9:0" in antwort.text
        assert "eingeschr" in antwort.text.lower(), "die Seite meldet sich nicht als eingeschraenkt"
        assert "letzten Signal" in antwort.text or "letztes Signal" in antwort.text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_zurueckkehrender_worker(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT), _herzschlag("phx-w1:0", JETZT + 9_000))
    c = await _angemeldet(config, broker=FakeBroker())
    try:
        antwort = await c.get(f"/?jetzt={JETZT + 9_010}")
        assert "verbunden" in antwort.text.lower()
        assert "getrennt" not in antwort.text.lower()
    finally:
        await c.__aexit__(None, None, None)


async def test_worker_nach_gruppe(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT), _meta("phx-w2:0", JETZT),
            _meta("phx-b1:0", JETZT, gruppe="billing", queues=["billing"]))
    c = await _angemeldet(config, broker=FakeBroker())
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert text.index("billing") < text.index("reports"), "die Gruppen sind nicht geordnet"
        for wid in ("phx-w1:0", "phx-w2:0", "phx-b1:0"):
            assert wid in text
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 2 -----------------------------------------------------------


async def test_konsumenten_ohne_hook(tmp_path: Path) -> None:
    """Covers AE7, R18. Ein Ueberschuss heisst: da arbeitet jemand, dessen
    Ereignisse nie im Verzeichnis landen -- und dessen Tasks sich darum als
    "nicht gelaufen" lesen.
    """
    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT), _herzschlag("phx-w1:0", JETZT),
            _meta("phx-w2:0", JETZT), _herzschlag("phx-w2:0", JETZT))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, depth=17,
                                               consumers=3)})
    c = await _angemeldet(config, broker=broker)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "ohne Hook" in text, "der Ueberschuss wird nicht ausgewiesen"
        assert re.search(r"1\s*(Konsument|Konsumenten)\s*ohne Hook", text), text[:0] or "Zahl fehlt"
    finally:
        await c.__aexit__(None, None, None)


async def test_konsumenten_ohne_jeden_bekannten_worker(tmp_path: Path) -> None:
    """Covers AE7a. Der Fall ist ausdruecklich eingeschlossen (R18): kennt der
    Strom gar keinen Worker, sind **alle** Konsumenten ohne Hook -- und eine
    leere Tabelle waere die Falschaussage "es laeuft nichts".
    """
    config = _config(tmp_path)
    _worker(config, Event(type="task_enqueue", ts=JETZT, source="app",
                          origin=Origin.ENQUEUE,
                          payload={"task_id": "t1", "queue": "reports"}))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, depth=5,
                                               consumers=4)})
    c = await _angemeldet(config, broker=broker)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        # Die Zahl **im Hinweis**, nicht irgendwo: "4" steht auch in der
        # Spalte Konsumenten, und ``ohne_hook`` durfte um eins danebenliegen,
        # ohne dass diese Zusicherung kippte.
        assert re.search(r"4\s*(Konsument|Konsumenten)\s*ohne Hook", text), (
            "der Hinweis nennt nicht alle vier Konsumenten"
        )
        assert "keinen worker" in text.lower(), "der Fall wird nicht benannt"
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_queue_mit_fehlgrund(tmp_path: Path) -> None:
    """Seit der Pruefung von Stufe B liefert ``stats_many`` je Queue entweder
    Zahlen oder einen Fehlgrund. Eine Null an dieser Stelle laese sich als
    "kein Rueckstau" -- die gefaehrlichste stille Falschaussage der Seite.
    """
    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT), _herzschlag("phx-w1:0", JETZT))
    broker = FakeBroker({"reports": QueueStats(queue="reports", error="Verbindung abgelehnt")})
    c = await _angemeldet(config, broker=broker)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "Verbindung abgelehnt" in text, "der Fehlgrund wird verschwiegen"
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 3 -----------------------------------------------------------


async def test_ohne_kontrollkanal_sagt_die_seite_das(tmp_path: Path, monkeypatch: Any) -> None:
    """Covers AE15, R33. An **einer** Stelle, nicht an jeder Zelle.

    Seit U16 haengt der Satz an der Faehigkeitspruefung statt fest in der
    Vorlage: gegen eine Bibliothek **mit** Kanal waere er selbst die
    Falschaussage. Der Test stellt die alte Bibliothek her, statt sich auf
    die gerade installierte zu verlassen -- sonst prueft er die Umgebung und
    nicht den Code.
    """
    monkeypatch.setattr(
        "swarmq_dashboard.web.services.kontrollkanal_grund",
        lambda: ("Steuerung nicht verfügbar: die installierte swarmq-Fassung bringt den "
                 "Kontrollkanal nicht mit."),
    )
    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT), _herzschlag("phx-w1:0", JETZT))
    c = await _angemeldet(config, broker=FakeBroker())
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert text.count("Steuerung nicht verf") == 1, (
            "der Hinweis steht nicht genau einmal auf der Seite"
        )
        assert "phx-w1:0" in text, "die Seite zeigt keine Worker mehr"
    finally:
        await c.__aexit__(None, None, None)


async def test_die_wartenden_nachrichten_bleiben(tmp_path: Path) -> None:
    """Sie kommen aus der passiven Broker-Beobachtung und haengen **nicht** am
    Kontrollkanal -- in der ersten Auslieferung sind sie die einzige Antwort
    auf "staut es sich?".
    """
    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT), _herzschlag("phx-w1:0", JETZT))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, depth=42,
                                               consumers=1)})
    c = await _angemeldet(config, broker=broker)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "42" in text, "die Zahl wartender Nachrichten fehlt"
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 4 -----------------------------------------------------------


async def test_entfernen_und_audit(tmp_path: Path) -> None:
    """R19, R27."""
    from swarmq_dashboard.audit import AuditLog

    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT))
    c, app = await _angemeldet(config, broker=FakeBroker(), mit_app=True)
    try:
        class Speicher:
            def __init__(self) -> None:
                self.e: list[tuple[str, bytes]] = []

            async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
                self.e.extend((f"{i}-0", w) for i, w in enumerate(events, len(self.e) + 1))

            async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
                return self.e[:count]

        app.state.services.audit = AuditLog(Speicher())
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/worker/phx-w1:0/remove", headers={"x-csrftoken": merkmal},
                               follow_redirects=False)
        assert antwort.status_code == 303

        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "phx-w1:0" not in text, "der entfernte Worker steht noch da"

        eintraege = [e for e in await app.state.services.audit.read()
                     if e.action == "worker_remove"]
        assert eintraege and eintraege[0].target == "phx-w1:0"
        assert eintraege[0].account == "ops"
    finally:
        await c.__aexit__(None, None, None)


async def test_entfernen_braucht_sitzung_und_merkmal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT))
    async with AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS) as c:
        ohne = await c.post("/worker/phx-w1:0/remove", follow_redirects=False)
        assert ohne.status_code in (302, 303, 401)

    c = await _angemeldet(config, broker=FakeBroker())
    try:
        ohne_merkmal = await c.post("/worker/phx-w1:0/remove", follow_redirects=False)
        assert ohne_merkmal.status_code == 403
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_getrennter_worker_zaehlt_nicht_als_konsument(tmp_path: Path) -> None:
    """R18 im Kern: ein getrennter Worker konsumiert nicht mehr.

    Ihn mitzuzaehlen verdeckte genau den Ueberschuss, um den es geht -- die
    Seite meldete "alles abgedeckt", waehrend in Wahrheit jemand arbeitet,
    dessen Ereignisse nie im Verzeichnis landen.
    """
    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT), _herzschlag("phx-w1:0", JETZT))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, depth=3,
                                               consumers=1)})
    c = await _angemeldet(config, broker=broker)
    try:
        # Solange er verbunden ist, deckt er den einen Konsumenten ab.
        nah = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "ohne Hook" not in nah, nah[nah.find("Queues"):][:300]

        # Nach der Frist gilt er als getrennt -- und der Konsument ist
        # unabgedeckt, obwohl der Worker noch in der Liste steht.
        weit = (await c.get(f"/?jetzt={JETZT + 10_000}")).text
        assert "1 Konsumenten ohne Hook" in weit, (
            "ein getrennter Worker wird als Konsument mitgezaehlt"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_werfender_broker_ergibt_je_queue_einen_grund(tmp_path: Path) -> None:
    """Eine leere Auskunft liesse die Queue-Tabelle verschwinden -- und damit
    die einzige Antwort auf "staut es sich?".
    """
    class WerfenderBroker(FakeBroker):
        async def stats_many(self, queues: list[str]) -> dict[str, QueueStats]:
            raise RuntimeError("Verbindung waehrend der Abfrage verloren")

    config = _config(tmp_path)
    _worker(config, _meta("phx-w1:0", JETZT), _herzschlag("phx-w1:0", JETZT))
    c = await _angemeldet(config, broker=WerfenderBroker())
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "reports" in text, "die Queue verschwindet aus der Tabelle"
        assert "Verbindung waehrend der Abfrage verloren" in text, (
            "der Fehlgrund wird verschwiegen"
        )
    finally:
        await c.__aexit__(None, None, None)
