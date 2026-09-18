"""Die Dead-Letter-Seite (U15 Teil 2, R6, R27, R28, R28a, R29, R30).

Diese Seite ist die einzige im Dashboard, deren **Ansehen** Schaden anrichten
kann. ``list_dead_letters`` heisst "non-destructive", und im Wortsinn stimmt
das -- aber jede gelesene Nachricht wird *neu* eingereiht: die Reihenfolge
aendert sich, der Zustellzaehler beginnt von vorn, und ein Absturz mitten im
Vorgang verliert die schon gelesenen.

Daraus folgt der Aufbau: der **Dauerzustand** der Seite ist die Tiefe aus der
passiven Broker-Beobachtung, und daneben stehen die Dead-Letter-Tasks aus dem
Verzeichnis. Beides kostet keine einzige Umreihung. Das Sichten ist die
Ausnahme, verlangt eine ausdrueckliche Bestaetigung und laeuft je Queue nur
einmal gleichzeitig.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Ansehen kostet nichts (R28, AE12a) ===
1. Covers AE12a. Das Oeffnen zeigt die Tiefe und ruehrt die Queue nicht an.
2. Nach einem Fehlerdeploy steht der Fehlergrund da -- ohne eine Umreihung.
3. Was das Verzeichnis nicht kennt, fehlt dort, und die Seite sagt das,
   statt Vollstaendigkeit zu suggerieren.
4. Eine Queue ohne Broker-Auskunft zeigt ihren Grund, keine Null.

=== ZYKLUS 2: Sichten ist eine Handlung (R28a) ===
5. Die Seite nennt die Wirkung des Sichtens, bevor jemand sichtet.
6. Ein Sichten ohne Bestaetigung wird abgewiesen und ruehrt nichts an.
7. Ein zweiter gleichzeitiger Versuch auf derselben Queue wird abgewiesen
   und erklaert -- und die Uebersicht sagt es schon vor dem Versuch.
7a. Eine andere Queue bleibt dabei frei.

=== ZYKLUS 3: Wiedereinreihen (R29) ===
8. Eine Sammel-Wiedereinreihung meldet beide Zahlen getrennt.
9. Eine einzelne Nachricht wandert zurueck.

=== ZYKLUS 4: Leeren (R30, AE12) ===
10. Covers AE12. Die Seite nennt Queue und Zahl, und der Knopf wiederholt sie.
11. Eine falsche Zahl loescht nicht.

=== ZYKLUS 5: Nachweis und Merkmal (R27) ===
12. Alle drei Aktionen erscheinen im Audit mit Konto und Ziel.
13. Eine Aktion ohne gueltiges Merkmal wird abgewiesen und ruehrt nichts an.
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
    """Die passive Beobachtung. Sie liefert die Tiefe -- ohne die Queue
    anzufassen."""

    def __init__(self, stats: dict[str, QueueStats] | None = None) -> None:
        self.stats = stats or {}
        self.gefragt: list[str] = []

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def stats_many(self, queues: list[str]) -> dict[str, QueueStats]:
        self.gefragt = list(queues)
        return {q: self.stats.get(q, QueueStats(queue=q)) for q in queues}


class FakeAdmin:
    """Die **wirkende** Seite. Jeder Aufruf wird mitgeschrieben -- die Tests
    zu Zyklus 1 pruefen, dass die Liste leer **bleibt**."""

    def __init__(self, nachrichten: int = 3) -> None:
        self.nachrichten = nachrichten
        self.aufrufe: list[str] = []

    async def list_dead_letters(self, queue: str, limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        self.aufrufe.append(f"list:{queue}")
        eintraege = [{"task_id": f"t{i}", "task_name": "reports.build", "queue": queue,
                      "headers": {"x-death": [{"count": 1}]}, "body_size": 120}
                     for i in range(self.nachrichten)]
        return eintraege, len(eintraege)

    async def retry_dead_letter(self, queue: str, task_id: str,
                                scan_limit: int = 1000) -> tuple[bool, int]:
        self.aufrufe.append(f"retry:{queue}:{task_id}")
        return True, 4

    async def retry_all_dead_letters(self, queue: str, limit: int = 10_000) -> tuple[int, int]:
        self.aufrufe.append(f"retry_all:{queue}")
        return 2, 3

    async def purge_dead_letters(self, queue: str) -> int:
        self.aufrufe.append(f"purge:{queue}")
        return self.nachrichten

    async def dead_letter_depth(self, queue: str) -> int:
        return self.nachrichten


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


def _verzeichnis(config: DashboardConfig, *ereignisse: Event) -> None:
    w = DirectoryWriter(connect(config.directory_file))
    w.apply([(f"{i}000-0", e) for i, e in enumerate(ereignisse, 1)])
    w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                   "on conflict(key) do nothing")
    w._con.commit()
    w.close()


def _gescheitert(task_id: str, fehler: str, queue: str = "reports") -> list[Event]:
    """Ein Task, der in der DLQ gelandet ist -- so, wie ihn das Verzeichnis
    aus dem Strom kennt."""
    return [
        Event(type="task_enqueue", ts=JETZT, source="api", origin=Origin.ENQUEUE,
              payload={"task_id": task_id, "task_name": "reports.build", "queue": queue,
                       "args": ["kunde-42"], "kwargs": {}}),
        Event(type="task_dlq", ts=JETZT + 5, source="phx-w1:0", origin=Origin.PICKUP,
              payload={"task_id": task_id, "task_name": "reports.build", "queue": queue,
                       "error": fehler, "duration_ms": 12.0}),
    ]


async def _angemeldet(config: DashboardConfig, *, broker: object | None = None,
                      admin: object | None = None, mit_app: bool = False) -> Any:
    app = create_app(config, hasher=_BILLIG, broker=broker, dlq_admin=admin)
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="?_csrf_token"?\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return (c, app) if mit_app else c


def _merkmal(c: Any) -> str:
    return str(c.cookies.get("csrftoken") or "")


# --- Zyklus 1: Ansehen kostet nichts ------------------------------------


async def test_oeffnen_zeigt_die_tiefe_und_ruehrt_nichts_an(tmp_path: Path) -> None:
    """Covers AE12a. Die Tiefe kommt aus der passiven Beobachtung. Wuerde die
    Seite sie ueber ``list_dead_letters`` holen, kostete jeder Aufruf eine
    komplette Umreihung -- und das Dashboard waere selbst die Stoerung.
    """
    config = _config(tmp_path)
    _verzeichnis(config, *_gescheitert("t-1", "ZeroDivisionError"))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, dead_letter=17)})
    admin = FakeAdmin()
    c = await _angemeldet(config, broker=broker, admin=admin)
    try:
        for _ in range(10):
            antwort = await c.get("/dlq")
            assert antwort.status_code == 200
        assert "17" in antwort.text, "die Tiefe fehlt"
        assert admin.aufrufe == [], f"das Ansehen hat die Queue angefasst: {admin.aufrufe}"
    finally:
        await c.__aexit__(None, None, None)


async def test_der_fehlergrund_steht_ohne_umreihung_da(tmp_path: Path) -> None:
    """Der Alltagsfall nach einem Fehlerdeploy: Warum sind sie gescheitert?

    Das Verzeichnis kennt die Antwort schon aus dem Strom. Sie dort zu holen
    kostet nichts -- das Sichten waere fuer dieselbe Auskunft eine komplette
    Umreihung.
    """
    config = _config(tmp_path)
    _verzeichnis(config, *_gescheitert("t-1", "ZeroDivisionError: division by zero"))
    admin = FakeAdmin()
    c = await _angemeldet(config, broker=FakeBroker(), admin=admin)
    try:
        text = (await c.get("/dlq")).text
        assert "t-1" in text
        assert "ZeroDivisionError" in text, "der Fehlergrund fehlt"
        assert "kunde-42" in text, "die Argumente fehlen"
        assert admin.aufrufe == [], "fuer den Fehlergrund wurde umgereiht"
    finally:
        await c.__aexit__(None, None, None)


async def test_die_verzeichnisliste_sagt_dass_sie_unvollstaendig_sein_kann(tmp_path: Path) -> None:
    """Die maßgebliche Zahl ist die Tiefe, nicht die Laenge der Liste.

    Eine Nachricht, deren Ereignisse nie ankamen -- Worker ohne Hook, Luecke
    im Strom -- fehlt in der Liste. Ohne diesen Hinweis liest sich "drei
    Zeilen" als "drei Nachrichten", und wer danach leert, vernichtet vier.
    """
    config = _config(tmp_path)
    _verzeichnis(config, *_gescheitert("t-1", "ZeroDivisionError"))
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, dead_letter=4)})
    c = await _angemeldet(config, broker=broker, admin=FakeAdmin())
    try:
        text = (await c.get("/dlq")).text
        assert "unvollst" in text.lower(), "die Liste behauptet Vollstaendigkeit"
        assert "Verzeichnis" in text, "die Herkunft der Liste ist nicht benannt"
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_queue_ohne_auskunft_zeigt_ihren_grund(tmp_path: Path) -> None:
    """Eine Null hiesse "keine gescheiterte Arbeit" -- die gefaehrlichste
    stille Falschaussage dieser Seite."""
    config = _config(tmp_path)
    _verzeichnis(config, *_gescheitert("t-1", "ZeroDivisionError"))
    broker = FakeBroker({"reports": QueueStats(queue="reports", error="Broker weg")})
    c = await _angemeldet(config, broker=broker, admin=FakeAdmin())
    try:
        text = (await c.get("/dlq")).text
        assert "Broker weg" in text, "der Grund fehlt"
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 2: Sichten ist eine Handlung --------------------------------


async def test_die_wirkung_des_sichtens_steht_vorher_da(tmp_path: Path) -> None:
    """R28a. "Non-destructive" ist die Zusage der Bibliothek -- und sie gilt
    nur im Wortsinn. Wer das nicht vorher liest, sichtet arglos."""
    config = _config(tmp_path)
    _verzeichnis(config, *_gescheitert("t-1", "ZeroDivisionError"))
    c = await _angemeldet(config, broker=FakeBroker(), admin=FakeAdmin())
    try:
        text = (await c.get("/dlq")).text
        niedrig = text.lower()
        assert "umgereiht" in niedrig or "neu eingereiht" in niedrig, "die Umreihung wird verschwiegen"
        assert "zustellz" in niedrig, "der zurueckgesetzte Zustellzaehler wird verschwiegen"
        assert "absturz" in niedrig or "verloren" in niedrig, "der moegliche Verlust wird verschwiegen"
    finally:
        await c.__aexit__(None, None, None)


async def test_sichten_ohne_bestaetigung_ruehrt_nichts_an(tmp_path: Path) -> None:
    """Ein Knopf allein genuegt nicht: die Wirkung ist zu gross fuer einen
    Fehlklick."""
    config = _config(tmp_path)
    admin = FakeAdmin()
    c = await _angemeldet(config, broker=FakeBroker(), admin=admin)
    try:
        antwort = await c.post("/dlq/reports/peek", headers={"x-csrftoken": _merkmal(c)},
                               follow_redirects=False)
        assert antwort.status_code == 200
        assert "Bestaetigung" in antwort.text, "die Abweisung wird nicht benannt"
        assert admin.aufrufe == [], "ohne Bestaetigung wurde gesichtet"
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_zweiter_gleichzeitiger_versuch_wird_erklaert(tmp_path: Path) -> None:
    """Zwei Durchlaeufe reihen dieselben Nachrichten doppelt um. Der Riegel
    traegt, weil der Dienst in einem Prozess laeuft (KTD5).

    Hier wird der Riegel von Hand gehalten statt ueber zwei gleichzeitige
    Anfragen: der Testklient faehrt die Anwendung in einem **eigenen Faden**
    mit eigener Ereignisschleife, und jeder Versuch, ueber die HTTP-Naht
    echte Gleichzeitigkeit zu erzwingen, endete in einer Zeitsperre statt in
    einer Aussage. Dass zwei gleichzeitige Durchlaeufe sich wirklich
    ausschliessen -- und zwei verschiedene Queues sich nicht --, prueft
    ``tests/unit/test_dlq.py`` am Dienst. Was **hier** zu pruefen ist: dass
    die Route den Riegel ueberhaupt befragt, statt an ihm vorbeizugehen, und
    dass sie den Grund nennt statt eines Absturzes.
    """
    config = _config(tmp_path)
    _verzeichnis(config, *_gescheitert("t-1", "ZeroDivisionError"))
    admin = FakeAdmin()
    c, app = await _angemeldet(config, broker=FakeBroker(), admin=admin, mit_app=True)
    riegel = app.state.services.dlq._lock("reports")
    await riegel.acquire()
    try:
        antwort = await c.post("/dlq/reports/peek", data={"bestaetigt": "sichten"},
                               headers={"x-csrftoken": _merkmal(c)}, follow_redirects=True)
        assert antwort.status_code == 200, "der zweite Versuch stuerzte ab"
        assert "wird gerade gesichtet" in antwort.text, "der zweite Versuch lief mit"
        assert admin.aufrufe == [], f"doppelt gesichtet: {admin.aufrufe}"

        # Und die Uebersicht sagt es auch ohne Versuch -- sonst drueckt das
        # zweite Teammitglied erst und erfaehrt es danach.
        assert "wird gerade gesichtet" in (await c.get("/dlq")).text
    finally:
        riegel.release()
        await c.__aexit__(None, None, None)


async def test_eine_andere_queue_bleibt_frei(tmp_path: Path) -> None:
    """Ein Riegel je Queue, nicht einer fuer alles: zwei Teams duerfen sich
    nicht gegenseitig blockieren."""
    config = _config(tmp_path)
    _verzeichnis(config, *_gescheitert("t-1", "ZeroDivisionError", queue="mails"))
    admin = FakeAdmin()
    c, app = await _angemeldet(config, broker=FakeBroker(), admin=admin, mit_app=True)
    riegel = app.state.services.dlq._lock("reports")
    await riegel.acquire()
    try:
        antwort = await c.post("/dlq/mails/peek", data={"bestaetigt": "sichten"},
                               headers={"x-csrftoken": _merkmal(c)}, follow_redirects=True)
        assert "wird gerade gesichtet" not in antwort.text
        assert admin.aufrufe == ["list:mails"], "die fremde Queue wurde mitgesperrt"
    finally:
        riegel.release()
        await c.__aexit__(None, None, None)


# --- Zyklus 3: Wiedereinreihen ------------------------------------------


async def test_sammel_wiedereinreihung_meldet_beide_zahlen(tmp_path: Path) -> None:
    """R29. "Zwei" allein liest sich als "alles erledigt", wo eine Nachricht
    liegen blieb."""
    config = _config(tmp_path)
    admin = FakeAdmin()          # retry_all: 2 von 3
    c = await _angemeldet(config, broker=FakeBroker(), admin=admin)
    try:
        antwort = await c.post("/dlq/reports/requeue-all", headers={"x-csrftoken": _merkmal(c)},
                               follow_redirects=True)
        assert "2" in antwort.text and "1" in antwort.text
        assert "unverarbeitet" in antwort.text.lower(), "die nicht verarbeitete Zahl fehlt"
        assert admin.aufrufe == ["retry_all:reports"]
    finally:
        await c.__aexit__(None, None, None)


async def test_einzelne_nachricht_wandert_zurueck(tmp_path: Path) -> None:
    config = _config(tmp_path)
    admin = FakeAdmin()
    c = await _angemeldet(config, broker=FakeBroker(), admin=admin)
    try:
        antwort = await c.post("/dlq/reports/requeue", data={"task_id": "t-1"},
                               headers={"x-csrftoken": _merkmal(c)}, follow_redirects=True)
        assert antwort.status_code == 200
        assert admin.aufrufe == ["retry:reports:t-1"]
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 4: Leeren ---------------------------------------------------


async def test_leeren_nennt_queue_und_zahl(tmp_path: Path) -> None:
    """Covers AE12. Die einzige Handlung im Dashboard, die Daten endgueltig
    vernichtet -- der Knopf wiederholt darum beides."""
    config = _config(tmp_path)
    broker = FakeBroker({"reports": QueueStats(queue="reports", exists=True, dead_letter=3)})
    _verzeichnis(config, *_gescheitert("t-1", "ZeroDivisionError"))
    admin = FakeAdmin(nachrichten=3)
    c = await _angemeldet(config, broker=broker, admin=admin)
    try:
        seite = (await c.get("/dlq")).text
        # Ausdruecklich die **Aufschrift** des Knopfes, nicht das Formular
        # drumherum: die versteckten Felder tragen Queue und Zahl ohnehin,
        # und gegen sie zu pruefen hiesse, eine Beschriftung "Loeschen" fuer
        # ausreichend zu halten. R30 verlangt, dass da steht, was passiert.
        stelle = seite[seite.find('action="/dlq/reports/purge"'):]
        anfang = stelle.find("<button")
        aufschrift = " ".join(stelle[stelle.find(">", anfang) + 1:stelle.find("</button>")].split())
        assert "reports" in aufschrift, f"der Knopf nennt die Queue nicht: {aufschrift!r}"
        assert "3" in aufschrift, f"der Knopf nennt die Zahl nicht: {aufschrift!r}"
        assert admin.aufrufe == [], "die Seite hat schon geloescht"

        antwort = await c.post("/dlq/reports/purge",
                               data={"bestaetigung": "reports", "erwartet": "3"},
                               headers={"x-csrftoken": _merkmal(c)}, follow_redirects=True)
        assert antwort.status_code == 200
        assert admin.aufrufe == ["purge:reports"]
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_falsche_zahl_loescht_nicht(tmp_path: Path) -> None:
    """Stimmt die Zahl nicht mehr, ist inzwischen etwas dazugekommen -- und
    die Bestaetigung ist nicht mehr die, die gegeben wurde."""
    config = _config(tmp_path)
    admin = FakeAdmin(nachrichten=7)
    c = await _angemeldet(config, broker=FakeBroker(), admin=admin)
    try:
        antwort = await c.post("/dlq/reports/purge",
                               data={"bestaetigung": "reports", "erwartet": "3"},
                               headers={"x-csrftoken": _merkmal(c)}, follow_redirects=True)
        assert "7" in antwort.text, "die neue Zahl wird nicht genannt"
        assert admin.aufrufe == [], "es wurde trotzdem geloescht"
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 5: Nachweis und Merkmal -------------------------------------


async def test_alle_drei_aktionen_stehen_im_audit(tmp_path: Path) -> None:
    """R27. Wer hinterher fragt, was mit den Nachrichten geschah, bekommt
    eine Antwort -- mit Konto und Ziel."""
    from swarmq_dashboard.audit import AuditLog

    config = _config(tmp_path)
    admin = FakeAdmin(nachrichten=3)
    c, app = await _angemeldet(config, broker=FakeBroker(), admin=admin, mit_app=True)
    try:
        class Speicher:
            def __init__(self) -> None:
                self.e: list[tuple[str, bytes]] = []

            async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
                self.e.extend((f"{i}-0", w) for i, w in enumerate(events, len(self.e) + 1))

            async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
                return self.e[:count]

        app.state.services.audit = AuditLog(Speicher())
        kopf = {"x-csrftoken": _merkmal(c)}
        await c.post("/dlq/reports/peek", data={"bestaetigt": "sichten"}, headers=kopf,
                     follow_redirects=True)
        await c.post("/dlq/reports/requeue-all", headers=kopf, follow_redirects=True)
        await c.post("/dlq/reports/purge", data={"bestaetigung": "reports", "erwartet": "3"},
                     headers=kopf, follow_redirects=True)

        eintraege = [e for e in await app.state.services.audit.read() if e.action == "dlq_action"]
        assert len(eintraege) == 3, f"nicht alle drei stehen im Audit: {eintraege}"
        assert all(e.account == "ops" and e.target == "reports" for e in eintraege)
        gesagt = " ".join(str(e.detail or "") for e in eintraege)
        for wort in ("sicht", "einreih", "leer"):
            assert wort in gesagt.lower(), f"{wort!r} fehlt im Audit: {gesagt}"
    finally:
        await c.__aexit__(None, None, None)


async def test_ohne_merkmal_geschieht_nichts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    admin = FakeAdmin()
    c = await _angemeldet(config, broker=FakeBroker(), admin=admin)
    try:
        for pfad, daten in (("/dlq/reports/peek", {"bestaetigt": "sichten"}),
                            ("/dlq/reports/requeue-all", {}),
                            ("/dlq/reports/purge", {"bestaetigung": "reports", "erwartet": "3"})):
            antwort = await c.post(pfad, data=daten, follow_redirects=False)
            assert antwort.status_code == 403, f"{pfad} lief ohne Merkmal"
        assert admin.aufrufe == [], "ohne Merkmal wurde gehandelt"
    finally:
        await c.__aexit__(None, None, None)


async def test_ohne_bekannte_queue_steht_die_ursache_da(tmp_path: Path) -> None:
    """R33. "Keine Queue" heisst nicht "keine gescheiterte Arbeit".

    Die Namen stammen aus dem Verzeichnis. Kennt es keine, weiss das
    Dashboard nicht, wo es nachsehen soll -- und genau das muss dastehen,
    statt einer leeren Tabelle, die sich wie Entwarnung liest.
    """
    config = _config(tmp_path)
    _verzeichnis(config)          # fertig aufgebaut, aber leer
    admin = FakeAdmin()
    c = await _angemeldet(config, broker=FakeBroker(), admin=admin)
    try:
        text = (await c.get("/dlq")).text
        assert "Keine Queue bekannt" in text
        assert "nicht beantwortet" in text, (
            "die Seite laesst die leere Tabelle als Entwarnung stehen")
        assert admin.aufrufe == []
    finally:
        await c.__aexit__(None, None, None)
