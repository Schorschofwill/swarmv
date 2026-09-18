"""Was R14 je Worker und je Gruppe verlangt (Nachlauf Stufe D).

Aus der Pruefung: die Tabelle liess drei der sieben geforderten Angaben weg
-- belegte Slots, verarbeitete Tasks und Uptime -- und der Gruppenkopf die
wartenden Nachrichten. Damit ist die Frage, die Fluss F2 ausloest ("sind alle
Slots belegt und staut es sich in dieser Gruppe?"), auf der Startseite nicht
beantwortbar.

Keine der drei Angaben kommt aus dem Kontrollkanal; alle drei stehen schon im
Verzeichnis:

* **Belegt** ist die Zahl der Tasks dieses Workers, die laufen.
* **Verarbeitet** ist die Zahl derer mit einem Ausgang -- und zwar nur, so
  weit das Verzeichnis reicht. Das ist keine Lebenszeit-Zahl, und die Seite
  darf sie auch nicht als eine ausgeben.
* **Uptime** folgt aus ``started_at`` der Stammdaten.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Belegte Slots und verarbeitete Tasks ===
1. Ein Worker mit zwei laufenden von vier Slots zeigt "2 von 4".
2. Erledigte Tasks werden gezaehlt, laufende nicht.
3. Die Zahlen bleiben je Worker getrennt.
4. Die Spalte sagt, dass sie nur so weit reicht wie das Verzeichnis.

=== ZYKLUS 2: Uptime ===
5. Ein Worker mit bekanntem Startzeitpunkt zeigt seine Laufzeit.
6. Ohne Startzeitpunkt steht dort kein geratener Wert.

=== ZYKLUS 3: Rueckstau je Gruppe (R14 Satz 2) ===
7. Die Gruppe nennt die Summe der wartenden Nachrichten ihrer Queues.
8. Laesst sich eine Queue nicht abfragen, steht dort keine Summe, die so
   tut, als waere sie vollstaendig.
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
    def __init__(self, stats: dict[str, QueueStats] | None = None) -> None:
        self.stats = stats or {}

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def stats_many(self, queues: list[str]) -> dict[str, QueueStats]:
        return {q: self.stats.get(q, QueueStats(queue=q)) for q in queues}


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


def _meta(wid: str, *, gruppe: str = "reports", queues: list[str] | None = None,
          concurrency: int = 4, started_at: float | None = JETZT) -> Event:
    nutzlast: dict[str, Any] = {
        "worker_id": wid, "slot": 0, "group": gruppe,
        "queues": queues or ["reports"], "all_queues": (queues or ["reports"]),
        "concurrency": concurrency, "pool": "thread",
    }
    if started_at is not None:
        nutzlast["started_at"] = started_at
    return Event(type="sqd_worker_meta", ts=JETZT, source=wid, origin=Origin.PICKUP,
                 payload=nutzlast)


def _herzschlag(wid: str, ts: float) -> Event:
    return Event(type="worker_heartbeat", ts=ts, source=wid, origin=Origin.PICKUP,
                 payload={"worker_id": wid})


def _laufend(tid: str, wid: str, ts: float) -> list[Event]:
    return [Event(type="task_started", ts=ts, source=wid, origin=Origin.PICKUP,
                  payload={"task_id": tid, "task_name": "render_pdf",
                           "queue": "reports", "worker_id": wid})]


def _gescheitert(tid: str, wid: str, ts: float) -> list[Event]:
    return [*_laufend(tid, wid, ts),
            Event(type="task_failed", ts=ts + 1, source=wid, origin=Origin.PICKUP,
                  payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                           "worker_id": wid, "error": "kaputt", "duration_ms": 3.0})]


def _verworfen(tid: str, wid: str, ts: float) -> list[Event]:
    return [*_laufend(tid, wid, ts),
            Event(type="task_dlq", ts=ts + 1, source=wid, origin=Origin.PICKUP,
                  payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                           "worker_id": wid, "error": "weg", "duration_ms": 2.0})]


def _erledigt(tid: str, wid: str, ts: float) -> list[Event]:
    return [*_laufend(tid, wid, ts),
            Event(type="task_completed", ts=ts + 1, source=wid, origin=Origin.PICKUP,
                  payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                           "worker_id": wid, "duration_ms": 12.0})]


def _fuellen(config: DashboardConfig, *ereignisse: Event) -> None:
    w = DirectoryWriter(connect(config.directory_file))
    w.apply([(f"{i}000-0", e) for i, e in enumerate(ereignisse, 1)])
    w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                   "on conflict(key) do nothing")
    w._con.commit()
    w.close()


async def _angemeldet(config: DashboardConfig, **kw: Any) -> Any:
    app = create_app(config, hasher=_BILLIG, **kw)
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="_csrf_token"\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return c


def _zeile(html: str, wid: str) -> str:
    """Die Tabellenzeile eines Workers -- nicht die ganze Seite."""
    anfang = html.find(f'id="worker-{wid}"')
    assert anfang >= 0, f"keine Zeile fuer {wid}"
    return " ".join(html[anfang:html.find("</tr>", anfang)].split())


# --- Zyklus 1 -----------------------------------------------------------


async def test_belegte_slots_und_erledigte_tasks(tmp_path: Path) -> None:
    """R14. "Nebenlaeufig" ist die Kapazitaet, nicht die Belegung -- die
    Frage "sind alle Slots belegt?" beantwortete die Seite gar nicht."""
    config = _config(tmp_path)
    # Zwei laufende, **drei** erledigte: gleiche Zahlen liessen sich nicht
    # auseinanderhalten, und die Mutation "melde die laufenden als erledigt"
    # ueberlebte. Unter den erledigten je ein anderer Ausgang -- die Liste in
    # ``load()`` kannte sechs, geprueft war einer.
    _fuellen(config, _meta("phx-w1:0"), _herzschlag("phx-w1:0", JETZT),
             *_laufend("a", "phx-w1:0", JETZT),
             *_laufend("b", "phx-w1:0", JETZT),
             *_erledigt("c", "phx-w1:0", JETZT),
             *_gescheitert("d", "phx-w1:0", JETZT),
             *_verworfen("e", "phx-w1:0", JETZT))
    c = await _angemeldet(config, broker=FakeBroker(), stream=None)
    try:
        zeile = _zeile((await c.get(f"/?jetzt={JETZT + 10}")).text, "phx-w1:0")
        assert "2 von 4" in zeile, f"die Belegung fehlt oder ist falsch: {zeile}"
        # Die **eigene** Zelle, und eine andere Zahl als die Belegung.
        nach = zeile.split("2 von 4</td>")[1]
        assert re.match(r'\s*<td class="mono num right">3</td>', nach), (
            f"die Zahl erledigter Tasks fehlt oder ist falsch: {nach[:80]}"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_die_zahlen_bleiben_je_worker_getrennt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fuellen(config, _meta("phx-w1:0"), _herzschlag("phx-w1:0", JETZT),
             _meta("phx-w2:0"), _herzschlag("phx-w2:0", JETZT),
             *_laufend("a", "phx-w1:0", JETZT),
             *_erledigt("c", "phx-w2:0", JETZT))
    c = await _angemeldet(config, broker=FakeBroker(), stream=None)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "1 von 4" in _zeile(text, "phx-w1:0")
        assert "0 von 4" in _zeile(text, "phx-w2:0"), (
            f"die Belegung des zweiten Workers ist falsch: {_zeile(text, 'phx-w2:0')}"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_die_spalte_nennt_ihre_reichweite(tmp_path: Path) -> None:
    """Die Zahl ist **keine** Lebenszeit-Zahl: sie reicht nur so weit wie das
    Verzeichnis. Ohne diesen Hinweis liest sich "12" als "dieser Worker hat
    zwoelf Tasks gemacht", und nach einem Ablauf faellt sie grundlos."""
    config = _config(tmp_path)
    _fuellen(config, _meta("phx-w1:0"), _herzschlag("phx-w1:0", JETZT))
    c = await _angemeldet(config, broker=FakeBroker(), stream=None)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        # Der Zusatz steht in der Meta-Zeile der Gruppe, nicht im
        # Tabellenkopf: "Tasks (im Verzeichnis)" quetschte dort die ganze
        # Tabelle, und die Aussage ist neben der Erklaerung ohnehin besser
        # aufgehoben als in einer Versalien-Spalte.
        teil = text[text.find('id="gruppe-'):]
        meta = " ".join(teil[:teil.find("<table")].split())
        assert "Verzeichnis" in meta or "Aufbewahrung" in meta, (
            f"die Zahl gibt sich als vollstaendig aus: {meta}"
        )
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 2 -----------------------------------------------------------


async def test_die_laufzeit_steht_da(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _fuellen(config, _meta("phx-w1:0", started_at=JETZT - 3 * 86400 - 4 * 3600),
             _herzschlag("phx-w1:0", JETZT))
    c = await _angemeldet(config, broker=FakeBroker(), stream=None)
    try:
        zeile = _zeile((await c.get(f"/?jetzt={JETZT + 10}")).text, "phx-w1:0")
        # Die Laufzeit-Zelle, nicht "irgendeine mono-Zelle": die
        # Spaltenreihenfolge folgt jetzt dem Mockup, und ein Test, der von
        # hinten zaehlt, prueft je nach Umbau etwas anderes.
        zelle = re.search(r'<td class="mono right">([^<]*)</td>', zeile)
        assert zelle and zelle.group(1).strip() == "3 d 4 h", (
            f"die Laufzeit fehlt oder die Einheiten stehen verkehrt: {zeile}"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_ohne_startzeitpunkt_kein_geratener_wert(tmp_path: Path) -> None:
    """Ein Worker, dessen Stammdaten fehlen -- nur ueber ein gewoehnliches
    Signal bekannt -- hat keinen Startzeitpunkt. Eine gerechnete Laufzeit
    waere dann erfunden."""
    config = _config(tmp_path)
    _fuellen(config, *_laufend("a", "phx-w9:0", JETZT))
    c = await _angemeldet(config, broker=FakeBroker(), stream=None)
    try:
        zeile = _zeile((await c.get(f"/?jetzt={JETZT + 10}")).text, "phx-w9:0")
        # Die **Laufzeit-Zelle**, nicht die Zeile: der Strich stand auch in
        # der Slot-Spalte, und "0 min" waere durchgegangen.
        zelle = re.search(r'<td class="mono right">([^<]*)</td>', zeile)
        assert zelle and zelle.group(1).strip() == "--", (
            f"eine Laufzeit wurde geraten: {zeile}"
        )
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 3 -----------------------------------------------------------


async def test_die_gruppe_nennt_ihren_rueckstau(tmp_path: Path) -> None:
    """R14 Satz 2, und U14 nennt sie ausdruecklich als das, was ohne
    Kontrollkanal bleibt: "die einzige Antwort auf 'staut es sich?'".

    Sie stand nur in einer Tabelle am Seitenende -- also nicht dort, wo die
    Frage entsteht.
    """
    config = _config(tmp_path)
    _fuellen(config, _meta("phx-w1:0", gruppe="reports", queues=["reports", "mails"]),
             _herzschlag("phx-w1:0", JETZT))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, depth=1200),
                         "mails": QueueStats(queue="mails", exists=True, depth=84)})
    c = await _angemeldet(config, broker=broker, stream=None)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        kopf = text[text.find("gruppe-reports"):]
        kopf = " ".join(kopf[:kopf.find("<table")].split())
        assert "1284" in kopf.replace(".", "").replace(" ", ""), (
            f"die Gruppe nennt ihren Rueckstau nicht: {kopf}"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_unabfragbare_queue_verdirbt_die_summe_nicht_still(
    tmp_path: Path,
) -> None:
    """Eine Summe, der eine Queue fehlt, ist keine Summe -- und sie liest
    sich als eine. Also muss dastehen, dass sie unvollstaendig ist."""
    config = _config(tmp_path)
    _fuellen(config, _meta("phx-w1:0", gruppe="reports", queues=["reports", "mails"]),
             _herzschlag("phx-w1:0", JETZT))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, depth=1200),
                         "mails": QueueStats(queue="mails", error="Broker weg")})
    c = await _angemeldet(config, broker=broker, stream=None)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        kopf = text[text.find("gruppe-reports"):]
        kopf = " ".join(kopf[:kopf.find("<table")].split())
        assert "unvollst" in kopf.lower() or "mindestens" in kopf.lower(), (
            f"die lueckenhafte Summe gibt sich als vollstaendig aus: {kopf}"
        )
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 4: Was die Korrektur selbst falsch behauptete ----------------


async def test_ein_getrennter_worker_belegt_keinen_slot(tmp_path: Path) -> None:
    """"5 von 2" ist arithmetisch unmoeglich -- und stand so auf der Seite.

    ``belegt()`` zaehlt Zeilen mit Status "laufend". Bleibt ein Abschluss
    aus -- Luecke im Strom, verworfener Eintrag, abgestuerzter Worker --,
    bleibt die Zeile fuer immer laufend. Fuer einen Worker, den dieselbe
    Zeile als **getrennt** ausweist, meldet die Seite dann Ueberlast: im
    Vorfall die teuerste Fehldeutung, weil sie in die falsche Richtung
    schickt.
    """
    config = _config(tmp_path)
    _fuellen(config, _meta("phx-w1:0", concurrency=2), _herzschlag("phx-w1:0", JETZT),
             *_laufend("a", "phx-w1:0", JETZT), *_laufend("b", "phx-w1:0", JETZT),
             *_laufend("c", "phx-w1:0", JETZT), *_laufend("d", "phx-w1:0", JETZT),
             *_laufend("e", "phx-w1:0", JETZT))
    c = await _angemeldet(config, broker=FakeBroker(), stream=None)
    try:
        # Lange nach der Frist: der Worker gilt als getrennt.
        zeile = _zeile((await c.get(f"/?jetzt={JETZT + 10_000}")).text, "phx-w1:0")
        assert "getrennt" in zeile
        assert "5 von 2" not in zeile, f"unmoegliche Belegung: {zeile}"
        assert re.search(r">\s*--\s*<|0 von 2", zeile), (
            f"ein getrennter Worker meldet weiter belegte Slots: {zeile}"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_queue_die_der_broker_nicht_kennt_gilt_als_ungemessen(
    tmp_path: Path,
) -> None:
    """Der Rueckstau zaehlte alles, was keinen **Fehler** trug.

    ``QueueStats(exists=False, depth=0, error=None)`` ist aber keine Messung,
    sondern ihr Ausbleiben: die Queue gibt es am Broker nicht, oder ihr Name
    wurde oertlich abgelehnt. Als Null in die Summe zu nehmen und die
    Unvollstaendigkeits-Marke zu unterdruecken ist genau die Null, wo eine
    Auskunft fehlt.
    """
    config = _config(tmp_path)
    _fuellen(config, _meta("phx-w1:0", gruppe="reports", queues=["reports", "mails"]),
             _herzschlag("phx-w1:0", JETZT))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, depth=12),
                         "mails": QueueStats(queue="mails", exists=False)})
    c = await _angemeldet(config, broker=broker, stream=None)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        kopf = text[text.find("gruppe-reports"):]
        kopf = " ".join(kopf[:kopf.find("<table")].split())
        assert "unvollst" in kopf.lower(), (
            f"eine nie gemessene Queue geht still als Null in die Summe: {kopf}"
        )
        assert "mails" in kopf, f"die fehlende Queue wird nicht benannt: {kopf}"
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_gruppe_ohne_bekannte_queues_meldet_keine_null(
    tmp_path: Path,
) -> None:
    """Ein Worker ohne Stammdaten hat keine Queues. Ueber die leere Menge zu
    summieren ergibt 0 -- und "0 wartende Nachrichten" ist die beruhigendste
    Antwort auf "staut es sich?", die es gibt. Gemessen wurde nichts."""
    config = _config(tmp_path)
    _fuellen(config, *_laufend("a", "phx-w9:0", JETZT))     # nur ein Signal
    c = await _angemeldet(config, broker=FakeBroker(), stream=None)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        kopf = text[text.find("gruppe-"):]
        kopf = " ".join(kopf[:kopf.find("<table")].split())
        assert "0 wartende" not in kopf, (
            f"eine Gruppe ohne bekannte Queue meldet einen Rueckstau von null: {kopf}"
        )
        assert "unbekannt" in kopf.lower() or "keine Queue" in kopf, (
            f"der Grund fehlt: {kopf}"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_jede_gruppe_zaehlt_nur_ihre_eigenen_queues(tmp_path: Path) -> None:
    """"Je Gruppe" war nicht beobachtbar: beide Tests bauten genau eine
    Gruppe, deren Queues alle Queues der Seite waren. Die Summe ueber
    **alle** Queues statt ueber die eigenen fiel damit nicht auf."""
    config = _config(tmp_path)
    _fuellen(config,
             _meta("phx-a:0", gruppe="reports", queues=["reports"]),
             _herzschlag("phx-a:0", JETZT),
             _meta("phx-b:0", gruppe="mails", queues=["mails"]),
             _herzschlag("phx-b:0", JETZT))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, depth=100),
                         "mails": QueueStats(queue="mails", exists=True, depth=7)})
    c = await _angemeldet(config, broker=broker, stream=None)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text

        def kopf(name: str) -> str:
            teil = text[text.find(f'id="gruppe-{name}"'):]
            return " ".join(teil[:teil.find("<table")].split())

        assert "100 wartende" in kopf("reports"), kopf("reports")
        assert "7 wartende" in kopf("mails"), kopf("mails")
        assert "107" not in kopf("reports"), (
            f"die Gruppe zaehlt fremde Queues mit: {kopf('reports')}"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_ohne_luecke_keine_unvollstaendigkeits_marke(tmp_path: Path) -> None:
    """Die Gegenprobe. Eine Marke, die immer dasteht, sagt nichts -- und
    kein Test sicherte bisher zu, dass sie **fehlt**, wenn alles antwortet."""
    config = _config(tmp_path)
    _fuellen(config, _meta("phx-a:0", gruppe="reports", queues=["reports"]),
             _herzschlag("phx-a:0", JETZT))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, depth=5)})
    c = await _angemeldet(config, broker=broker, stream=None)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        teil = text[text.find('id="gruppe-reports"'):]
        kopf = " ".join(teil[:teil.find("<table")].split())
        assert "unvollst" not in kopf.lower() and "keine Queue" not in kopf, (
            f"die Marke steht da, obwohl alle Queues geantwortet haben: {kopf}"
        )
    finally:
        await c.__aexit__(None, None, None)
