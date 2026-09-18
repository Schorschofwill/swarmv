"""Die Naehte zwischen den Teilen und dem Dienst (Nachlauf Stufe D).

Aus der Pruefung von Stufe D. Sechs Teile waren gebaut, getestet und **nicht
angeschlossen**: der Strom-Leser, die Pflege, der Live-Sender, das
Abonnement im Browser, das Stornieren und der CSRF-Weg der Formulare. Alle
vier Tore standen dabei auf gruen, weil jeder Test das Teil **direkt**
aufruft. Damit pruefen sie, dass es funktioniert -- nie, dass es benutzt
wird.

Genau diese Luecke schliessen die Tests hier. Sie fassen kein Teil an,
sondern nur den Dienst: sie fahren ihn hoch und sehen nach, ob er von sich
aus tut, was er verspricht. Was hier nicht gruen ist, ist im Betrieb tot,
egal wie viele Tests das Teil selbst hat.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Der Dienst liest und raeumt von sich aus ===
1. Nach dem Hochfahren steht ein Task aus dem Strom im Verzeichnis, ohne
   dass die Testreihe ``poll_once`` gerufen hat.
2. Der Neuaufbau gilt danach als beendet -- sonst nennt jede Seite ewig
   "wird gerade neu aufgebaut" als Ursache.
3. Der Dienst wendet die Aufbewahrungsfrist von sich aus an: eine zu alte
   Zeile verschwindet, und der Strom wird getrimmt (R5, R7a, R9).

=== ZYKLUS 2: Was eingearbeitet wurde, erreicht die Live-Schicht ===
4. Ein eingearbeitetes Ereignis hebt die Folgenummer, ohne dass die
   Testreihe ``publish`` gerufen hat.

=== ZYKLUS 3: Das Stornieren ist verbunden ===
5. Eine Stornierung erreicht das Backend, statt einen Backend-Ausfall zu
   behaupten, den es nicht gibt (R12).
5a. Der Canceller verbindet sich dafuer beim ersten Gebrauch selbst --
    ``connect()`` hatte im ganzen Dienst keinen Aufrufer.

=== ZYKLUS 4: Die Formulare der Oberflaeche sind benutzbar ===
6. Jede schreibende Aktion laeuft so, wie ein Browser sie absendet:
   Merkmal im Koerper, **keine** Kopfzeile (R23).
7. Ein falsches Merkmal im Koerper wird abgewiesen.
8. Die Sitzungsbindung bleibt: das Merkmal einer fremden Sitzung traegt
   nicht.
8a. Geprueft wird gegen die **Sitzung**, nicht gegen das Cookie -- ein
    selbst gesetztes Merkmal traegt nicht.

=== ZYKLUS 5: Die Seite haengt am Live-Kanal ===
9. Das gerenderte Geruest abonniert ``/live``.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from litestar.testing import AsyncTestClient

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin, encode
from swarmq_dashboard.web.app import create_app

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
_PASSWORT = "ein-langes-passwort"
BASIS = "https://testserver.local"
JETZT = 1_700_000_000.0


class FakeStream:
    """Ein Strom im Speicher, mit Kennungen wie Valkey sie vergibt."""

    def __init__(self) -> None:
        self.eintraege: list[tuple[str, bytes]] = []
        self.getrimmt: list[int] = []

    def add(self, ev: Event) -> str:
        sid = f"{int(ev.ts * 1000)}-0"
        self.eintraege.append((sid, encode(ev)))
        return sid

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        def schluessel(s: str) -> tuple[int, int]:
            a, b = s.split("-")
            return (int(a), int(b))

        grenze = schluessel(last_id) if last_id else (0, 0)
        return [(i, r) for i, r in self.eintraege if schluessel(i) > grenze][:count]

    async def min_id(self, key: str) -> str | None:
        return self.eintraege[0][0] if self.eintraege else None

    async def trim_by_min_id(self, key: str, min_ms: int) -> int:
        vorher = len(self.eintraege)
        self.eintraege = [(i, r) for i, r in self.eintraege if int(i.split("-")[0]) >= min_ms]
        self.getrimmt.append(min_ms)
        return vorher - len(self.eintraege)

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        self.eintraege.extend((f"{9_000_000 + i}-0", w) for i, w in enumerate(events))


class FakeBroker:
    """Ohne sie haengt das Hochfahren Sekunden am toten Broker-Port."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def stats_many(self, queues: list[str]) -> dict[str, Any]:
        from swarmq_dashboard.broker import QueueStats
        return {q: QueueStats(queue=q) for q in queues}


class FakeCanceller:
    """Bildet den echten Canceller nach, samt der Naht: ``cancel`` verbindet
    sich selbst."""

    def __init__(self) -> None:
        self.verbunden = False
        self.storniert: list[str] = []

    async def connect(self) -> None:
        self.verbunden = True

    async def close(self) -> None:
        self.verbunden = False

    async def cancel(self, task_id: str) -> bool:
        # Wie der echte: er verbindet sich beim ersten Gebrauch selbst.
        await self.connect()
        self.storniert.append(task_id)
        return True


def _ev(tid: str, ts: float) -> Event:
    return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
                 payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                          "args": [], "kwargs": {}})


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


async def _warte_bis(bedingung: Any, frist: float = 3.0) -> bool:
    """Auf eine Bedingung warten, statt eine feste Zeit zu schlafen."""
    ende = asyncio.get_running_loop().time() + frist
    while asyncio.get_running_loop().time() < ende:
        if bedingung():
            return True
        await asyncio.sleep(0.01)
    return bedingung()


def _dienst(config: DashboardConfig, **kw: Any) -> Any:
    kw.setdefault("broker", FakeBroker())
    kw.setdefault("canceller", FakeCanceller())
    return create_app(config, hasher=_BILLIG, maintenance_every_s=0.05,
                      ingest_every_s=0.01, **kw)


# --- Zyklus 1 -----------------------------------------------------------


async def test_der_dienst_liest_den_strom_von_sich_aus(tmp_path: Path) -> None:
    """Die wichtigste Zusicherung des ganzen Projekts.

    Ohne sie fuellt sich das Verzeichnis nie, und jede Seite bleibt dauerhaft
    leer -- waehrend die Kopfzeile "Strom: aktuell" meldet.
    """
    config = _config(tmp_path)
    strom = FakeStream()
    strom.add(_ev("a41f", JETZT))
    app = _dienst(config, stream=strom)
    async with AsyncTestClient(app=app, base_url=BASIS):
        w = app.state.services.services_directory_writer
        assert await _warte_bis(lambda: w.row("a41f") is not None), (
            "der Dienst liest den Strom nicht -- das Verzeichnis bleibt leer"
        )


async def test_der_neuaufbau_gilt_danach_als_beendet(tmp_path: Path) -> None:
    """Sonst nennt jeder Leerzustand ewig "wird gerade neu aufgebaut" -- die
    eine Ursache, die zum Abwarten bewegt."""
    config = _config(tmp_path)
    app = _dienst(config, stream=FakeStream())
    async with AsyncTestClient(app=app, base_url=BASIS):
        services = app.state.services
        assert await _warte_bis(
            lambda: services.status()["directory"]["rebuilding"] is False
        ), "der Neuaufbau endet nie"


async def test_der_dienst_wendet_die_aufbewahrung_von_sich_aus_an(tmp_path: Path) -> None:
    """R5, R7a, R9. Die Frist ist die einzige technische Begrenzung, unter der
    die Klartext-Entscheidung aus KD11 ueberhaupt vertretbar ist.

    Ohne diese Naht wird nie geloescht -- und die Kopfzeile nennt trotzdem
    sieben Tage.
    """
    config = _config(tmp_path, retention_days=7)
    strom = FakeStream()
    app = _dienst(config, stream=strom)
    # Eine Zeile, die deutlich aelter ist als die Frist, samt Klartext.
    w = DirectoryWriter(connect(config.directory_file))
    import time as _t
    w.apply([(None, _ev("alt", _t.time() - 30 * 86400))])
    w._con.execute("update task set last_seen_at=?, args_text=? where task_id='alt'",
                   (_t.time() - 30 * 86400, '{"args": ["kunde3@example.com"]}'))
    w._con.commit()
    w.close()

    async with AsyncTestClient(app=app, base_url=BASIS):
        schreiber = app.state.services.services_directory_writer
        assert await _warte_bis(lambda: schreiber.row("alt") is None), (
            "die Aufbewahrungsfrist wird nie angewendet -- der Klartext bleibt liegen"
        )
        assert await _warte_bis(lambda: bool(strom.getrimmt)), (
            "der Strom wird nie nach Alter getrimmt"
        )


# --- Zyklus 2 -----------------------------------------------------------


async def test_ein_eingearbeitetes_ereignis_erreicht_die_live_schicht(tmp_path: Path) -> None:
    """Der Sender war gebaut und hatte keinen Aufrufer -- die Live-Schicht
    lieferte darum nur Herzschlaege."""
    config = _config(tmp_path)
    strom = FakeStream()
    strom.add(_ev("a41f", JETZT))
    app = _dienst(config, stream=strom)
    async with AsyncTestClient(app=app, base_url=BASIS):
        publisher = app.state.services.publisher
        assert await _warte_bis(lambda: publisher.current_seq > 0), (
            "kein Ereignis erreicht die Live-Schicht"
        )


# --- Zyklus 3 -----------------------------------------------------------


async def _angemeldet(app: Any) -> Any:
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="_csrf_token"\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return c


def _feld(text: str) -> str:
    treffer = re.search(r'name="_csrf_token"\s+value="([^"]*)"', text)
    assert treffer, "die Seite traegt kein Merkmal"
    return treffer.group(1)



async def test_die_stornierung_erreicht_das_backend(tmp_path: Path) -> None:
    """R12. ``connect()`` hatte keinen Aufrufer, also warf ``cancel()`` immer
    -- und die Ansicht behauptete einen Backend-Ausfall, den es nicht gab.

    Geprueft wird der **Weg** ueber die Route, nicht ein Zustandsfeld: der
    echte Canceller verbindet sich beim ersten Gebrauch, nicht beim
    Hochfahren, und genau das soll hier ankommen.
    """
    config = _config(tmp_path)
    strom = FakeStream()
    strom.add(_ev("a41f", JETZT))
    storno = FakeCanceller()
    app = _dienst(config, stream=strom, canceller=storno)
    c = await _angemeldet(app)
    try:
        w = app.state.services.services_directory_writer
        assert await _warte_bis(lambda: w.row("a41f") is not None)
        seite = await c.get("/tasks/a41f")
        antwort = await c.post("/tasks/a41f/cancel",
                               data={"_csrf_token": _feld(seite.text)},
                               follow_redirects=False)
        assert antwort.status_code == 303
        assert storno.storniert == ["a41f"], (
            "die Stornierung erreicht das Backend nicht"
        )
        assert "hinweis=backend" not in str(antwort.headers.get("location", "")), (
            "die Ansicht behauptet einen Backend-Ausfall, den es nicht gibt"
        )
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 4 -----------------------------------------------------------


async def test_die_formulare_der_oberflaeche_sind_benutzbar(tmp_path: Path) -> None:
    """R23, und mit ihr R12, R19, R27, R29, R30.

    Der Waechter las das Merkmal **nur** aus der Kopfzeile, die Vorlagen
    schicken es als Formularfeld, und kein Skript setzt die Kopfzeile. Damit
    antwortete jede schreibende Aktion im Browser mit 403 -- Stornieren,
    Worker entfernen, Sichten, Wiedereinreihen, Leeren und selbst Abmelden.
    Die Testreihe sah es nicht, weil sie durchgehend die Kopfzeile schickte,
    also genau den Weg, den kein Formular geht.
    """
    config = _config(tmp_path)
    app = _dienst(config, stream=FakeStream(), canceller=FakeCanceller())
    c = await _angemeldet(app)
    try:
        seite = await c.get("/")
        merkmal = _feld(seite.text)
        # So, wie ein Browser ein Formular absendet: Feld im Koerper, keine
        # Kopfzeile.
        antwort = await c.post("/logout", data={"_csrf_token": merkmal},
                               follow_redirects=False)
        assert antwort.status_code != 403, (
            "das Abmelde-Formular der Oberflaeche wird abgewiesen"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_falsches_merkmal_im_koerper_wird_abgewiesen(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app = _dienst(config, stream=FakeStream())
    c = await _angemeldet(app)
    try:
        antwort = await c.post("/logout", data={"_csrf_token": "x" * 43},
                               follow_redirects=False)
        assert antwort.status_code == 403
    finally:
        await c.__aexit__(None, None, None)


async def test_das_merkmal_bleibt_an_die_sitzung_gebunden(tmp_path: Path) -> None:
    """Die naheliegende Reparatur -- "dann lies eben auch den Koerper" --
    darf die Sitzungsbindung nicht aufweichen.

    Geprueft wird mit **zwei aufeinanderfolgenden** Sitzungen desselben
    Kontos statt mit zwei gleichzeitigen Klienten: zwei Klienten auf
    derselben Anwendung fahren deren Lebenszyklus doppelt und verklemmen
    sich. Die Aussage ist dieselbe -- ein Merkmal, das nicht zu **dieser**
    Sitzung gehoert, traegt nicht.
    """
    config = _config(tmp_path)
    app = _dienst(config, stream=FakeStream())
    c = await _angemeldet(app)
    try:
        erstes = _feld((await c.get("/")).text)
        await c.post("/logout", data={"_csrf_token": erstes}, follow_redirects=False)

        seite = await c.get("/login")
        await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                     "_csrf_token": _feld(seite.text)},
                     follow_redirects=False)
        zweites = _feld((await c.get("/")).text)
        assert erstes != zweites, "beide Sitzungen tragen dasselbe Merkmal"

        antwort = await c.post("/logout", data={"_csrf_token": erstes},
                               follow_redirects=False)
        assert antwort.status_code == 403, "ein fremdes Merkmal wurde angenommen"
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 5 -----------------------------------------------------------


async def test_die_seite_abonniert_den_live_kanal(tmp_path: Path) -> None:
    """R13, R13a, R32. Die Live-Schicht war serverseitig vollstaendig und im
    Browser an nichts angeschlossen: kein ``EventSource``, kein
    ``sse-connect``. Eine offene Seite fror ein und bescheinigte sich dabei
    in der Kopfzeile "Strom: aktuell".
    """
    config = _config(tmp_path)
    app = _dienst(config, stream=FakeStream())
    c = await _angemeldet(app)
    try:
        text = (await c.get("/")).text
        assert "/live" in text, "keine Seite abonniert den Live-Kanal"
    finally:
        await c.__aexit__(None, None, None)


async def test_der_canceller_verbindet_sich_beim_ersten_gebrauch() -> None:
    """Die Naht selbst, am echten Objekt.

    ``connect()`` hatte im ganzen Dienst keinen Aufrufer; ``cancel()`` warf
    darum jedes Mal "das Backend ist nicht verbunden", und die Ansicht gab
    dem Backend die Schuld. Jetzt haengt die Naht in ``cancel()`` -- traege,
    damit niemand beim Hochfahren eine Verbindung bezahlt, die die meisten
    Sitzungen nie brauchen.
    """
    from swarmq_dashboard.web.cancel import SwarmQCanceller

    c = SwarmQCanceller("amqp://guest:guest@127.0.0.1:1/", "valkey://127.0.0.1:1/0")
    rufe: list[int] = []

    class App:
        async def cancel(self, task_id: str) -> bool:
            return True

    async def statt_connect() -> None:
        rufe.append(1)
        c._app = App()

    c.connect = statt_connect  # type: ignore[method-assign]
    assert await c.cancel("t-1") is True
    assert rufe == [1], "cancel() verbindet sich nicht"


async def test_das_merkmal_wird_gegen_die_sitzung_geprueft_nicht_gegen_das_cookie(
    tmp_path: Path,
) -> None:
    """Die eine Mutation, die alle anderen Tests ueberlebt hat.

    Cookie und Sitzung tragen nach der Anmeldung **denselben** Wert -- jede
    Zusicherung, die beide nicht auseinanderzieht, ist gegen die Verwechslung
    blind. Und die Verwechslung ist der ganze Unterschied: das Cookie schickt
    der Browser von selbst mit, und wer es setzen kann, setzt auch das
    Formularfeld. Verglichen wuerde dann eine Zeichenfolge mit einer Kopie
    ihrer selbst.

    Hier wird das Cookie ueberschrieben und derselbe Wert im Feld gesendet.
    Gegen die **Sitzung** geprueft scheitert das; gegen das Cookie geprueft
    ginge es durch.
    """
    config = _config(tmp_path)
    app = _dienst(config, stream=FakeStream())
    c = await _angemeldet(app)
    try:
        erfunden = "z" * 43
        c.cookies.set("csrftoken", erfunden, domain="testserver.local")
        antwort = await c.post("/logout", data={"_csrf_token": erfunden},
                               follow_redirects=False)
        assert antwort.status_code == 403, (
            "ein selbst gesetztes Merkmal wurde angenommen -- geprueft wird "
            "gegen das Cookie statt gegen die Sitzung"
        )
    finally:
        await c.__aexit__(None, None, None)
