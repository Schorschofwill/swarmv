"""Was die Oberflaeche behauptet, ohne es zu wissen (Nachlauf Stufe D).

Sechs Stellen aus der Pruefung, alle derselben Sorte: die Seite sagt etwas,
das im Augenblick der Aussage nicht gedeckt ist. Keine davon ist ein
Absturz, und genau darum sind sie gefaehrlich -- ein Absturz faellt auf.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Reichweite (R8) ===
0. Sie geht nie vor den Beginn des Verzeichnisses zurueck (Zyklus 6).
1. Ein Dauerlaeufer zieht die gemeldete Reichweite nicht hinter die
   Aufbewahrungsgrenze zurueck.
2. Ohne Dauerlaeufer bleibt die Reichweite der aelteste Eintrag.

=== ZYKLUS 2: Der Wegfall eines Dienstes (R26) ===
3. Faellt der Strom nach dem Hochfahren weg, sagt die Kopfzeile das im
   naechsten Takt.
4. Dasselbe fuer den Broker.

=== ZYKLUS 3: Verlorene Eintraege (R4a) ===
5. Nicht lesbare Strom-Eintraege stehen in der Kopfzeile.
6. Von der Datenbank abgewiesene Ereignisse ebenso.

=== ZYKLUS 4: Der Riegel deckt alle drei Handlungen (R28a, R30) ===
7. Leeren waehrend einer Sichtung wird abgewiesen.
8. Zwei Sammel-Wiedereinreihungen verschraenken sich nicht.
9. Eine andere Queue bleibt dabei frei.

=== ZYKLUS 5: Was der Loesch-Knopf benennt (R30, AE12) ===
10. Covers AE12. Der Knopf nennt die Dead-Letter-Queue, nicht die
    Arbeits-Queue.
11. Ist die Tiefe unbekannt, steht dort keine Null.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from litestar.testing import AsyncTestClient

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.broker import QueueStats
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.directory.queries import DirectoryQueries
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin
from swarmq_dashboard.web.app import create_app

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
_PASSWORT = "ein-langes-passwort"
BASIS = "https://testserver.local"


def _config(tmp_path: Path, *, retention_days: int = 7) -> DashboardConfig:
    c = DashboardConfig(
        broker_url="amqp://guest:guest@127.0.0.1:1/", backend_url="valkey://127.0.0.1:1/0",
        stream_url="valkey://127.0.0.1:1/1", retention_days=retention_days,
        unconfirmed_after_s=15.0, disconnected_after_s=40.0, session_idle_s=1800.0,
        backlog_cap=300, stream_stale_after_s=120.0, data_dir=tmp_path / "sqd-data",
    )
    s = AccountStore(c.accounts_file, hasher=_BILLIG)
    s.create("ops", _PASSWORT)
    s.close()
    return c


def _enq(tid: str, ts: float) -> Event:
    return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
                 payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                          "args": [], "kwargs": {}})


# --- Zyklus 1: Reichweite -----------------------------------------------


def test_ein_dauerlaeufer_zieht_die_reichweite_nicht_zurueck(tmp_path: Path) -> None:
    """R8 nennt diesen Fehler woertlich vorweg.

    Die Reichweite war ``min(first_seen_at)``, geloescht wird aber nach
    ``last_seen_at``. Eine einzige langlebige Zeile -- ein Dauerlaeufer, ein
    Workflow, eine Wiederholungskette -- zog die gemeldete Reichweite
    beliebig weit zurueck, waehrend alles dazwischen laengst geraeumt war.
    Die Seite sagte dann "die Suche reicht zurueck bis vor 30 Tagen" und
    antwortete auf eine Suche in Tag 15 mit "keine Treffer".
    """
    con = connect(tmp_path / "v.sqlite3")
    w, q = DirectoryWriter(con), DirectoryQueries(con)
    jetzt = time.time()
    w.apply([(None, _enq("dauerlaeufer", jetzt - 30 * 86400)),
             (None, _enq("frisch", jetzt - 86400))])
    w._con.execute("update task set last_seen_at=? where task_id='dauerlaeufer'", (jetzt,))
    w._con.commit()

    grenze = jetzt - 7 * 86400
    reichweite = q.reach(retention_start=grenze)
    assert reichweite is not None
    assert reichweite >= grenze, (
        f"die gemeldete Reichweite liegt {(grenze - reichweite) / 86400:.0f} Tage "
        "hinter der Aufbewahrungsgrenze -- dort wurde laengst geloescht"
    )


def test_ohne_dauerlaeufer_bleibt_der_aelteste_eintrag(tmp_path: Path) -> None:
    """Die Grenze darf die Reichweite nicht kuenstlich nach hinten schieben:
    ein junges Verzeichnis reicht so weit, wie es reicht."""
    con = connect(tmp_path / "v.sqlite3")
    w, q = DirectoryWriter(con), DirectoryQueries(con)
    jetzt = time.time()
    w.apply([(None, _enq("a", jetzt - 2 * 86400))])
    reichweite = q.reach(retention_start=jetzt - 7 * 86400)
    assert reichweite is not None
    assert abs(reichweite - (jetzt - 2 * 86400)) < 1.0, (
        "die Grenze verdeckt den aeltesten Eintrag"
    )


# --- Zyklus 2: Der Wegfall eines Dienstes -------------------------------


class FallenderClient:
    """Ein Valkey-Klient, der nach dem Hochfahren aufhoert zu antworten."""

    def __init__(self) -> None:
        self.da = True
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        if not self.da:
            raise ConnectionError("Verbindung abgebrochen")
        return True

    async def aclose(self) -> None: ...


async def _warte_bis(bedingung: Any, frist: float = 3.0) -> bool:
    ende = asyncio.get_running_loop().time() + frist
    while asyncio.get_running_loop().time() < ende:
        if bedingung():
            return True
        await asyncio.sleep(0.01)
    return bedingung()


async def test_ein_weggefallener_strom_wird_bemerkt() -> None:
    """R26. ``connect()`` kehrte sofort zurueck, sobald ein Klient stand:
    einmal erreichbar hiess fuer immer erreichbar.

    Geprueft wird der **Klient**, nicht der Dienst. Der Dienst reicht seinen
    vorhandenen Klienten in jedem Takt an ``connect()`` weiter; ob der
    Wegfall auffaellt, entscheidet sich also genau hier. Eine Attrappe auf
    Dienstebene, die bei jedem Aufruf wirft, waere strenger als das Original
    und bewiese nichts.
    """
    from swarmq_dashboard.stream import StreamUnavailable, ValkeyStreamClient

    client = FallenderClient()

    class Pruefbar(ValkeyStreamClient):
        def _bauen(self) -> Any:
            return client

    c = Pruefbar("valkey://127.0.0.1:1/1")
    await c.connect()
    assert client.pings == 1, "der erste Aufbau prueft nicht"

    client.da = False
    try:
        await c.connect()
    except StreamUnavailable as exc:
        assert "abgebrochen" in str(exc)
    else:
        raise AssertionError(
            "ein zweiter connect() auf einen toten Klienten kehrt still zurueck -- "
            "die Kopfzeile bliebe fuer immer gruen"
        )
    assert client.pings == 2, "die Pruefung wurde uebersprungen"


async def test_ein_weggefallener_broker_wird_bemerkt() -> None:
    """Dasselbe fuer den Broker. Sonst widerspricht sich die Seite selbst:
    die Kopfzeile sagt "erreichbar", und in derselben Antwort traegt jede
    Queue-Zeile einen Fehlgrund."""
    from swarmq_dashboard.broker import BrokerObserver, BrokerUnavailable

    class ToteVerbindung:
        is_closed = False

        async def close(self) -> None: ...

        async def channel(self) -> Any:
            raise ConnectionError("Verbindung abgebrochen")

    o = BrokerObserver("amqp://guest:guest@127.0.0.1:1/")
    o._connection = ToteVerbindung()          # type: ignore[assignment]
    try:
        await o.connect()
    except BrokerUnavailable as exc:
        assert "abgebrochen" in str(exc)
    else:
        raise AssertionError(
            "connect() haelt eine tote Verbindung fuer gut, weil sie sich nicht "
            "als geschlossen meldet -- genau so verhaelt sich RobustConnection"
        )




# --- Zyklus 3: Verlorene Eintraege --------------------------------------


def test_verworfene_und_abgewiesene_stehen_in_der_kopfzeile() -> None:
    """R4a. Der Leser zaehlt beides, und sein eigener Docstring sagt "beides
    gehoert in die Kopfzeile: still bleiben hiesse, eine Luecke als
    Vollstaendigkeit auszugeben". Gerendert wurde keins von beiden.

    ``dropped`` sind Strom-Eintraege, die nicht gelesen werden konnten -- ein
    Hook einer neueren Fassung mit unbekanntem Herkunftswert reicht dafuer.
    ``rejected`` sind Ereignisse, die die Datenbank abwies. In beiden Faellen
    fehlt ein Task im Verzeichnis und liest sich als "nie gelaufen".
    """
    from swarmq_dashboard.web.templates import render

    verzeichnis = {"ok": True, "reason": None, "rebuilding": False, "behind": False,
                   "gaps": 0, "rejected": 3, "dropped": 5, "erasure_pending": False,
                   "oldest_entry": 1_700_000_000.0}
    html = render("base.html", status={
        "broker": {"ok": True, "reason": None},
        "backend": {"ok": True, "reason": None},
        "stream": {"ok": True, "reason": None, "stale": False,
                   "last_event_at": 1_700_000_000.0, "retention_days": 7},
        "directory": verzeichnis,
        "accounts": {"ok": True, "reason": None},
        "audit": {"lost": 0},
        "maintenance": {"refresh": None, "purge": None},
    }, account="ops", aktiv="tasks")

    # Die Aufschriften aller Wartemarken, jede fuer sich -- nicht der
    # Seitentext: eine Ziffer findet sich sonst auch im Datum.
    marken = [" ".join(m.split()) for m in re.findall(
        r'<span class="tag is-wait"[^>]*>\s*<span class="tag-dot"></span>([^<]*)', html)]
    assert marken, f"die Kopfzeile traegt keine Wartemarke: {marken}"
    for zahl, wort in ((5, "nicht lesbar"), (3, "abgewiesen")):
        passend = [m for m in marken if wort in m]
        assert passend, f"die Kopfzeile schweigt ueber {wort!r}: {marken}"
        assert passend[0].startswith(str(zahl)), (
            f"die Zahl fehlt oder ist falsch: {passend[0]!r}"
        )


# --- Zyklus 5: Was der Loesch-Knopf benennt -----------------------------


async def _angemeldet(config: DashboardConfig, **kw: Any) -> Any:
    app = create_app(config, hasher=_BILLIG, **kw)
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="_csrf_token"\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return c


def _knopf(html: str, queue: str) -> str:
    stelle = html[html.find(f'action="/dlq/{queue}/purge"'):]
    anfang = stelle.find("<button")
    return " ".join(stelle[stelle.find(">", anfang) + 1:stelle.find("</button>")].split())


class StummerBroker:
    def __init__(self, stats: dict[str, QueueStats]) -> None:
        self.stats = stats

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def stats_many(self, queues: list[str]) -> dict[str, QueueStats]:
        return {q: self.stats.get(q, QueueStats(queue=q)) for q in queues}


def _verzeichnis(config: DashboardConfig) -> None:
    w = DirectoryWriter(connect(config.directory_file))
    w.apply([("1000-0", Event(type="task_dlq", ts=1_700_000_000.0, source="phx-w1:0",
                              origin=Origin.PICKUP,
                              payload={"task_id": "t-1", "task_name": "reports.build",
                                       "queue": "reports", "error": "kaputt",
                                       "duration_ms": 1.0}))])
    w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                   "on conflict(key) do nothing")
    w._con.commit()
    w.close()


async def test_der_knopf_nennt_die_dead_letter_queue(tmp_path: Path) -> None:
    """Covers AE12, woertlich: "eine Bestaetigung nennt ``reports.XQ`` und 37
    Nachrichten".

    Der Knopf sagte "37 Nachrichten in reports endgueltig loeschen" --
    geleert wird aber ``reports.XQ``, und die Tiefe misst ebenfalls ``.XQ``.
    Wer den Satz liest, muss glauben, dass gleich 37 **wartende** Nachrichten
    aus der Arbeits-Queue verschwinden. Bei der einzigen Handlung des
    Dashboards, die Daten endgueltig vernichtet, ist das der falsche Satz.
    """
    config = _config(tmp_path)
    _verzeichnis(config)
    broker = StummerBroker({"reports": QueueStats(queue="reports", exists=True,
                                                  dead_letter=37)})
    c = await _angemeldet(config, broker=broker, stream=None)
    try:
        aufschrift = _knopf((await c.get("/dlq")).text, "reports")
        assert "reports.XQ" in aufschrift, (
            f"der Knopf nennt die Arbeits-Queue statt der Dead-Letter-Queue: "
            f"{aufschrift!r}"
        )
        assert "37" in aufschrift, aufschrift
    finally:
        await c.__aexit__(None, None, None)


async def test_bei_unbekannter_tiefe_steht_dort_keine_null(tmp_path: Path) -> None:
    """R28. Die Null neben der Zerstoer-Handlung ist die beruhigendste
    Falschaussage der Seite: sie sagt "es geht um nichts", waehrend in
    Wahrheit niemand weiss, wie viel dort liegt.

    Die Spalte daneben faengt den Fall korrekt ab -- der Knopf nicht.
    """
    config = _config(tmp_path)
    _verzeichnis(config)
    broker = StummerBroker({"reports": QueueStats(queue="reports",
                                                  error="Verbindung weg")})
    c = await _angemeldet(config, broker=broker, stream=None)
    try:
        text = (await c.get("/dlq")).text
        aufschrift = _knopf(text, "reports")
        assert "0" not in aufschrift, (
            f"der Knopf behauptet eine Zahl, die niemand kennt: {aufschrift!r}"
        )
        assert "unbekannt" in aufschrift.lower(), (
            f"der Knopf sagt nicht, dass die Zahl fehlt: {aufschrift!r}"
        )
        # Und er ist gesperrt: eine Bestaetigung, deren Zahl niemand kennt,
        # traegt nicht. Ohne diese Zusicherung genuegte es, dem Formular ein
        # verstecktes ``erwartet=0`` mitzugeben -- die Aufschrift bliebe
        # ehrlich, und geloescht wuerde trotzdem auf gut Glueck.
        form = text[text.find('action="/dlq/reports/purge"'):]
        knopf = form[form.find("<button"):form.find(">", form.find("<button"))]
        assert "disabled" in knopf, f"der Knopf ist nicht gesperrt: {knopf!r}"
        assert 'name="erwartet"' not in form[:form.find("<button")], (
            "das Formular traegt eine erwartete Zahl, obwohl keine bekannt ist"
        )
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 6: Die Reichweite hat einen dritten Boden -------------------


def test_die_reichweite_geht_nicht_vor_den_beginn_des_verzeichnisses(
    tmp_path: Path,
) -> None:
    """R8, zweiter Anlauf.

    Der erste Anlauf nahm ``max(aeltester Eintrag, Aufbewahrungsgrenze)``. Das
    hob die Zahl gegenueber vorher an, aber nicht weit genug: laeuft der
    Dienst erst seit zwei Tagen und zieht **eine** noch lebende Zeile den
    aeltesten Eintrag auf dreissig Tage zurueck, meldete die Seite weiter
    sieben Tage -- abgedeckt sind zwei. Der Fehler war von dreissig auf
    sieben Tage geschrumpft, nicht beseitigt, und der Docstring behauptete
    dabei ausdruecklich das Gegenteil.

    Es fehlte der dritte Boden: der Punkt, ab dem das Verzeichnis ueberhaupt
    liest. Vorher kann es nichts wissen, egal wie grosszuegig die
    Aufbewahrung ist.
    """
    con = connect(tmp_path / "v.sqlite3")
    w, q = DirectoryWriter(con), DirectoryQueries(con)
    jetzt = time.time()
    beginn = jetzt - 2 * 86400
    w.set_coverage_start(beginn)
    # Ein Dauerlaeufer von vor 30 Tagen, sonst nichts Aelteres als 2 Tage.
    w.apply([(None, _enq("dauerlaeufer", jetzt - 30 * 86400)),
             (None, _enq("frisch", jetzt - 86400))])
    w._con.execute("update task set last_seen_at=? where task_id='dauerlaeufer'", (jetzt,))
    w._con.commit()

    reichweite = q.reach(retention_start=jetzt - 7 * 86400)
    assert reichweite is not None
    assert reichweite >= beginn - 1.0, (
        f"die gemeldete Reichweite liegt {(beginn - reichweite) / 86400:.1f} Tage "
        "vor dem Beginn des Verzeichnisses -- dort hat es nie gelesen"
    )


def test_ohne_bekannten_beginn_bleibt_es_bei_der_grenze(tmp_path: Path) -> None:
    """Ein Verzeichnis aus der Zeit vor diesem Merker kennt seinen Beginn
    nicht. Dann ist die Aufbewahrungsgrenze die beste Auskunft -- und sie zu
    verschweigen waere schlechter, als sie vorsichtig zu nennen."""
    con = connect(tmp_path / "v.sqlite3")
    w, q = DirectoryWriter(con), DirectoryQueries(con)
    jetzt = time.time()
    w.apply([(None, _enq("alt", jetzt - 30 * 86400))])
    w._con.execute("update task set last_seen_at=? where task_id='alt'", (jetzt,))
    w._con.commit()
    grenze = jetzt - 7 * 86400
    assert q.reach(retention_start=grenze) == grenze


def test_der_beginn_wird_nur_einmal_gesetzt(tmp_path: Path) -> None:
    """Sonst wanderte er mit jedem Durchlauf nach vorn, und die Reichweite
    schrumpfte auf den letzten Takt zusammen."""
    con = connect(tmp_path / "v.sqlite3")
    w = DirectoryWriter(con)
    w.set_coverage_start(1000.0)
    w.set_coverage_start(9999.0)
    assert w.coverage_start() == 1000.0
