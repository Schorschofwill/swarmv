"""Die Workflow-Ansicht gegen echte Dienste (U8).

Der Durchstich, den keine Attrappe zeigen kann: ein echter Chord, ueber
swarmq eingereiht mit dem Anwendungs-Hook, abgearbeitet von einem echten
Worker mit dem Worker-Hook, eingelesen vom laufenden Dienst, abgeglichen
gegen den Zustand, den die Bibliothek selbst schreibt -- und am Ende als
Seite ueber HTTP abgefragt. Jeder Zwischenschritt hat eigene Tests; ob die
Teile zusammen die Aussage tragen, prueft nur dieser Weg.

Nichts wird hier in Valkey geschrieben, wo die Bibliothek selbst schreibt:
Zustand, Merker und Ergebnisse entstehen ueber ``apply``, die Route und den
Worker. Gewartet wird auf die Seite, nicht auf eine Zeitspanne -- Hook,
Einlesen und Abgleich laufen je in eigenem Takt, und eine geschlafene
Sekunde waere in einem von zehn Laeufen zu kurz.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Vom Chord bis zur Liste (Covers F1) ===
1. [x] Ein Chord aus drei Downloads und einem Body steht nach dem Abgleich
       in der Liste als "läuft" mit 0/4, solange kein Worker laeuft;
       danach mit 4/4 als "fertig", und die Detailseite bietet kein
       Stornieren mehr an.

=== ZYKLUS 2: Stornieren, solange alles wartet (Covers AE11) ===
2. [x] Ein Chord, dessen Glieder in der Queue warten, wird ueber die Route
       storniert; die Ansicht zeigt "Stornierung angefordert". Nach dem
       Start eines Workers bricht er alle Glieder beim Aufgreifen ab, und
       die Ansicht zeigt "Stornierung angefordert, nichts mehr aktiv" --
       keinen Endstatus, kein erneutes Stornieren. Der Zustand in Valkey
       steht dabei wirklich noch auf "running" (R6).

=== ZYKLUS 3: Der echte Chord als Diagramm (Workflow-Diagramm U5, Covers F1, R1, R2) ===
3. [x] Ohne Angabe und ohne Cookie zeigt die Detailseite das Diagramm,
       nicht die Liste: drei wartende Downloads untereinander, vom
       Startpunkt aufgefaechert, jeder mit einer Linie zum Body, der
       "wartet auf 3 Glieder" nennt. Nach dem Worker steht der Body auf
       "fertig", die Downloads sind zum Sammelknoten "3 fertig" gefaltet,
       der auf die Seite der fertigen Glieder verweist.
       (Das Verhalten stand aus U3/U4; rot war der Fall zuerst an der
       eigenen Erwartung -- die Glieder stehen je nach Ankunft des
       Hook-Ereignisses auf "eingereiht" oder "wartet seit", beides
       neutral. Per Mutation belegt: mit der Liste als Vorgabe scheitert
       er am fehlenden Diagramm.)

=== ZYKLUS 4: Umschalter und gemerkte Wahl (Workflow-Diagramm U5, Covers AE4, R7, R8) ===
4. [x] "Liste" in der Adresse zeigt den Baum und setzt das Cookie
       (Pfad /workflows, SameSite=Strict, HttpOnly, Secure); ein anderer
       Workflow ohne Angabe zeigt danach die Liste. "Diagramm" in der
       Adresse schaltet zurueck und merkt auch das. Ein unbekannter Wert
       zeigt das Diagramm und laesst die gemerkte Wahl stehen.
       (Zuerst rot am eigenen Test: jede Antwort erneuert auch das
       Sitzungs-Cookie, geprueft wird nur das Ansicht-Cookie. Per Mutation
       belegt: liest die Route das Cookie nicht, zeigt der zweite Workflow
       das Diagramm statt der Liste.)
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aio_pika
import pytest
from litestar.testing import AsyncTestClient

from swarmq_dashboard.audit import AuditLog
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.events import AUDIT_STREAM, TASK_STREAM
from swarmq_dashboard.stream import ValkeyStreamClient
from swarmq_dashboard.web.app import create_app
from swarmq_dashboard.workflows import SwarmQWorkflowleser

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

#: Takt des Abgleichs im Test. Im Betrieb 60 s; hier kurz, damit die Seite
#: auch ohne Anstoss aus einem Ereignis bald nachzieht.
WORKFLOW_POLL_S = 0.5
#: Wie lange auf ein Anzeichen gewartet wird. Hook (1 s), Einlesen (0,2 s)
#: und die zweite Lesung nach einem Ereignis (2 s) addieren sich.
FRIST_S = 30.0
PASSWORT = "ein-langes-passwort"


# -- Tasks ------------------------------------------------------------------


def _tasks(queue: str) -> list[type[Any]]:
    """Download und Zusammenfassung, **ohne** globale Registrierung.

    Wie in ``test_workflow_zustand.py``: ``task_info`` erst nach der Klasse,
    damit keine spaeter gebaute App dieser Testreihe die Tasks erbt.
    """
    from swarmq.task import Task, TaskInfo

    class Lade(Task):
        async def run(self, n: int) -> int:
            return n

    class Fasse(Task):
        async def run(self, teile: list[int]) -> int:
            return sum(teile)

    Lade.task_info = TaskInfo(name=f"{queue}_lade", queues=[queue])
    Fasse.task_info = TaskInfo(name=f"{queue}_fasse", queues=[queue])
    return [Lade, Fasse]


def _swarmq(broker_url: str, backend_url: str, queue: str, **cfg: Any) -> Any:
    from swarmq import RabbitMQBroker, SwarmQ, ValkeyBackend

    app = SwarmQ(broker=RabbitMQBroker(broker_url), backend=ValkeyBackend(backend_url),
                 queues=[queue], **cfg)
    for task in _tasks(queue):
        app.registry.register(task)
    return app


def _chord(queue: str) -> Any:
    from swarmq import chord, group, sig

    return chord(
        group(*(sig(f"{queue}_lade", n) for n in (1, 2, 3))),
        sig(f"{queue}_fasse"),
    )


# -- Vorrichtung --------------------------------------------------------------


@dataclass
class Lauf:
    """Alles, was ein Fall braucht: Queue, Einreiher, Worker, Dashboard."""

    queue: str
    einreiher: Any
    web: AsyncTestClient[Any]
    broker_url: str
    backend_url: str
    workflow_ids: list[str] = field(default_factory=list)
    audit: AuditLog | None = None
    worker_app: Any | None = None
    worker_task: asyncio.Task[None] | None = None

    async def worker_starten(self) -> None:
        """Ein Worker im Prozess, mit dem Worker-Hook -- wie in den
        Worker-Tests der Bibliothek als Hintergrundaufgabe."""
        from swarmq_dashboard.hooks.worker import setup

        self.worker_app = _swarmq(self.broker_url, self.backend_url, self.queue,
                                  concurrency=4)
        setup(self.worker_app)
        self.worker_task = asyncio.ensure_future(self.worker_app.start_worker())

    async def worker_stoppen(self) -> None:
        if self.worker_task is None:
            return
        self.worker_task.cancel()
        try:
            await self.worker_task
        except (asyncio.CancelledError, Exception):
            pass
        self.worker_task = None


async def _anmelden(client: AsyncTestClient[Any]) -> None:
    seite = await client.get("/login")
    merkmal = re.search(r'name="?_csrf_token"?\s+value="([^"]*)"', seite.text)
    assert merkmal, seite.text[:300]
    antwort = await client.post(
        "/login", data={"account": "ops", "password": PASSWORT,
                        "_csrf_token": merkmal.group(1)},
        follow_redirects=False,
    )
    assert antwort.status_code in (302, 303), antwort.text[:300]


async def _aufraeumen_valkey(backend_url: str, workflow_ids: list[str]) -> None:
    """Die Schluessel der eigenen Workflows: Zustand, Merker, Ergebnisse.

    Ueber das Muster, weil die Bibliothek die Schluessel selbst benennt --
    Ergebnisse unter den Kennungen ``<wf>#<pfad>``, der Chord-Zaehler unter
    einem eigenen Namen. Wer sie einzeln aufzaehlte, uebersaehe den naechsten.
    """
    import valkey.asyncio

    client = valkey.asyncio.Valkey.from_url(backend_url)
    try:
        for wf in workflow_ids:
            async for key in client.scan_iter(match=f"*{wf}*", count=500):
                await client.delete(key)
    finally:
        await client.aclose()


async def _aufraeumen_broker(broker_url: str, queue: str) -> None:
    """Die Queues dieses Tests samt Prioritaets-, Verzoegerungs- und DLQ."""
    con = await aio_pika.connect_robust(broker_url)
    try:
        for basis in (queue, f"{queue}.high", f"{queue}.low"):
            for endung in ("", ".DQ", ".XQ"):
                kanal = await con.channel()
                try:
                    await kanal.queue_delete(f"{basis}{endung}")
                except Exception:
                    pass
                finally:
                    if not kanal.is_closed:
                        await kanal.close()
    finally:
        await con.close()


async def _letzte_kennung(strom: ValkeyStreamClient, key: str) -> str:
    letzte = await strom.client.xrevrange(key, count=1)
    return letzte[0][0].decode() if letzte else "0-0"


async def _eintraege_nach_loeschen(strom: ValkeyStreamClient, key: str, nach: str) -> None:
    eigene = await strom.client.xrange(key, min=f"({nach}")
    if eigene:
        await strom.client.xdel(key, *(k for k, _ in eigene))


@pytest.fixture
async def lauf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    broker_url: str, backend_url: str, stream_url: str,
) -> AsyncIterator[Lauf]:
    from argon2 import PasswordHasher

    from swarmq_dashboard.accounts import AccountStore
    from swarmq_dashboard.hooks.app import install, shutdown

    # Die Hooks lesen ihr Ziel aus der Umgebung (``HookConfig.from_env``).
    monkeypatch.setenv("SWARMQ_BROKER_URL", broker_url)
    monkeypatch.setenv("SWARMQ_BACKEND_URL", backend_url)
    monkeypatch.setenv("SQD_STREAM_URL", stream_url)

    strom = ValkeyStreamClient(stream_url)
    await strom.connect()
    # Rueckstand eines Vorgaengers wegraeumen, wie ``test_app_hook.py``: der
    # Dienst liest den gemeinsamen Strom von vorn.
    await strom.delete(TASK_STREAM)
    # Der Audit-Strom ist geteilt: nur die eigenen Eintraege gehen am Ende,
    # also alles nach dem jetzt letzten.
    audit_vorher = await _letzte_kennung(strom, AUDIT_STREAM)

    queue = f"sqd_itest_wfa_{uuid.uuid4().hex[:8]}"
    billig = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
    config = DashboardConfig(
        broker_url=broker_url, backend_url=backend_url, stream_url=stream_url,
        retention_days=7, unconfirmed_after_s=15.0, disconnected_after_s=40.0,
        session_idle_s=1800.0, backlog_cap=300, stream_stale_after_s=120.0,
        data_dir=tmp_path / "sqd-data", workflow_poll_s=WORKFLOW_POLL_S,
    )
    konten = AccountStore(config.accounts_file, hasher=billig)
    konten.create("ops", PASSWORT)
    konten.close()

    einreiher = _swarmq(broker_url, backend_url, queue)
    install(einreiher)
    workflow_ids: list[str] = []
    try:
        async with einreiher:
            # Vor dem Einreihen deklarieren: ohne Queue gingen die Nachrichten
            # der Anfangsfront im Standard-Exchange ins Leere, und der Worker
            # faende spaeter nichts vor.
            await einreiher.broker.declare_queue(queue)
            app = create_app(config, hasher=billig, ingest_every_s=0.2)
            async with AsyncTestClient(app=app, base_url="https://testserver.local") as web:
                await _anmelden(web)
                vorrichtung = Lauf(queue=queue, einreiher=einreiher, web=web,
                                   broker_url=broker_url, backend_url=backend_url,
                                   workflow_ids=workflow_ids, audit=AuditLog(strom))
                try:
                    yield vorrichtung
                finally:
                    await vorrichtung.worker_stoppen()
                    # Erst den geteilten Puffer schliessen, dann den Strom
                    # leeren -- sonst schriebe er noch in einen geleerten Strom.
                    await shutdown(einreiher)
    finally:
        await _aufraeumen_valkey(backend_url, workflow_ids)
        await _aufraeumen_broker(broker_url, queue)
        await strom.delete(TASK_STREAM)
        await _eintraege_nach_loeschen(strom, AUDIT_STREAM, audit_vorher)
        await strom.close()


async def _starten(lauf: Lauf) -> str:
    handle = await lauf.einreiher.apply(_chord(lauf.queue))
    wf = str(handle.workflow_id)
    lauf.workflow_ids.append(wf)
    return wf


# -- Lesen der Seiten ----------------------------------------------------------


def _zeile(html: str, workflow_id: str) -> str | None:
    """Die Tabellenzeile eines Workflows aus der Liste, ohne Markup."""
    treffer = re.search(rf'<tr id="workflow-{re.escape(workflow_id)}">(.*?)</tr>', html, re.S)
    if treffer is None:
        return None
    return " ".join(re.sub(r"<[^>]+>", " ", treffer.group(1)).split())


async def _warten(
    lesen: Callable[[], Awaitable[str | None]], bedingung: Callable[[str], bool],
    *, was: str,
) -> str:
    """Auf ein Anzeichen warten, nie eine feste Zeitspanne verschlafen.

    Das Muster der Bestandstests (``test_control_roundtrip._warten``): kurz
    nachsehen, bis die Frist ablaeuft, und dann mit dem letzten Stand
    scheitern -- der sagt, woran es hing.
    """
    ende = time.monotonic() + FRIST_S
    letzter: str | None = None
    while time.monotonic() < ende:
        letzter = await lesen()
        if letzter is not None and bedingung(letzter):
            return letzter
        await asyncio.sleep(0.2)
    pytest.fail(f"{was}: nicht erreicht in {FRIST_S} s; zuletzt {letzter!r}")


def _liste(lauf: Lauf, workflow_id: str) -> Callable[[], Awaitable[str | None]]:
    async def lesen() -> str | None:
        antwort = await lauf.web.get("/workflows", headers={"accept": "text/html"})
        assert antwort.status_code == 200, antwort.status_code
        return _zeile(antwort.text, workflow_id)
    return lesen


def _detail(lauf: Lauf, workflow_id: str) -> Callable[[], Awaitable[str | None]]:
    async def lesen() -> str | None:
        antwort = await lauf.web.get(f"/workflows/{workflow_id}", headers={"accept": "text/html"})
        return antwort.text if antwort.status_code == 200 else None
    return lesen


def _status(detail_html: str) -> str:
    """Der Text des Status-Kennzeichens auf der Detailseite."""
    treffer = re.search(r'<span class="tag [^"]*">\s*<span class="tag-dot"></span>\s*([^<]+?)\s*</span>',
                        detail_html)
    assert treffer, detail_html[:500]
    return treffer.group(1)


@dataclass(frozen=True)
class Diagrammknoten:
    """Ein Knoten des Diagramms, wie die Seite ihn liefert."""

    kennung: str
    klasse: str
    titel: str
    text: str
    link: str | None


def _diagramm(detail_html: str) -> tuple[dict[str, Diagrammknoten], set[str]] | None:
    """Knoten und Linienkennungen des Diagramms, ``None`` ohne Diagramm.

    Aus dem Markup, nicht aus ``anordnen``: geprueft wird, was ueber HTTP
    ankommt. Die Linien bleiben Kennungen -- ``dk-kante-<von>-<nach>`` laesst
    sich nicht eindeutig zerlegen, die Pfade enthalten selbst Striche.
    """
    svg = re.search(r'<svg id="workflow-diagramm".*?</svg>', detail_html, re.S)
    if svg is None:
        return None
    knoten: dict[str, Diagrammknoten] = {}
    for m in re.finditer(r'<(a|g) id="(dk-[^"]+)" class="dk-knoten ([^"]+)"([^>]*)>(.*?)</\1>',
                         svg.group(0), re.S):
        link = re.search(r'href="([^"]*)"', m.group(4))
        titel = re.search(r"<title>(.*?)</title>", m.group(5), re.S)
        text = " ".join(re.sub(r"<title>.*?</title>|<[^>]+>", " ", m.group(5), flags=re.S).split())
        knoten[m.group(2)] = Diagrammknoten(
            kennung=m.group(2), klasse=m.group(3).strip(),
            titel=titel.group(1).strip() if titel else "", text=text,
            link=link.group(1) if link else None,
        )
    linien = set(re.findall(r'<path id="(dk-kante-[^"]+)" class="dk-linie', svg.group(0)))
    return knoten, linien


def _name(k: Diagrammknoten) -> str:
    """Der Teil einer Knotenkennung, der in eine Linienkennung eingeht (KTD4)."""
    return k.kennung.removeprefix("dk-")


def _schritte(knoten: dict[str, Diagrammknoten], task_name: str) -> list[Diagrammknoten]:
    return [k for k in knoten.values() if k.titel == task_name]


# -- Faelle --------------------------------------------------------------------


async def test_ein_echter_chord_erscheint_zeigt_fortschritt_und_wird_fertig(lauf: Lauf) -> None:
    """Covers F1. Der Hauptfluss: Workflows oeffnen, Status und Fortschritt
    sehen -- vom Einreihen bis zum Ende, ohne Valkey-Schluessel zu lesen."""
    wf = await _starten(lauf)

    # Kein Worker: die Glieder warten. Die Zeile entsteht aus der Einreihung
    # des Anwendungs-Hooks, Topologie und Zaehler aus dem Abgleich.
    wartend = await _warten(_liste(lauf, wf), lambda z: "0/4" in z, was="Zeile mit 0/4")
    assert "läuft" in wartend, wartend
    assert "Chord" in wartend, wartend

    await lauf.worker_starten()

    # Auf beides warten, nicht nacheinander: der Endstatus kommt aus der
    # Lesung des Zustands, der Zaehler aus den Ereignissen der Worker. Die
    # Lesung kann schneller sein als der Hook-Puffer (1 s Takt) -- die
    # Zeile stand im ersten Lauf kurz auf "fertig" mit 0/4.
    fertig = await _warten(_liste(lauf, wf), lambda z: "fertig" in z and "4/4" in z,
                           was="Zeile fertig mit 4/4")
    assert "läuft" not in fertig, fertig

    detail = await _detail(lauf, wf)()
    assert detail is not None
    assert _status(detail) == "fertig"
    assert "Workflow stornieren" not in detail, "AE9: ein fertiger Workflow ist nicht stornierbar"


async def test_ein_wartender_chord_wird_storniert_und_nichts_ist_mehr_aktiv(lauf: Lauf) -> None:
    """Covers AE11. Stornieren, solange alle Glieder in der Queue warten.

    Die Bibliothek bricht jedes Glied beim Aufgreifen ab und schreibt dabei
    **keinen** Endstatus: ohne Advance bleibt der Zustand "running". Die
    Ansicht darf deshalb nicht "storniert" behaupten (R6) -- und auch nicht
    weiter "Stornierung angefordert" mit Knopf, als liefe noch etwas.
    """
    wf = await _starten(lauf)
    await _warten(_liste(lauf, wf), lambda z: "läuft" in z and "0/4" in z, was="Zeile läuft")

    detail = await _detail(lauf, wf)()
    assert detail is not None and "Workflow stornieren" in detail, "R15: laufend stornierbar"
    merkmal = re.search(r'name="_csrf_token" value="([^"]*)"', detail)
    assert merkmal, "das Storno-Formular traegt kein Merkmal"

    antwort = await lauf.web.post(
        f"/workflows/{wf}/cancel",
        data={"bestaetigt": "stornieren", "_csrf_token": merkmal.group(1)},
        follow_redirects=False,
    )
    assert antwort.status_code == 303, antwort.text[:300]
    assert antwort.headers["location"] == f"/workflows/{wf}?hinweis=angefordert"

    # Die Glieder sind noch eingereiht, also noch aktiv.
    angefordert = await _warten(_detail(lauf, wf),
                                lambda d: _status(d) == "Stornierung angefordert",
                                was="Stornierung angefordert")
    assert "Workflow stornieren" in angefordert, "AE8: eine bestehende Anforderung bleibt anklickbar"

    await lauf.worker_starten()

    ende = await _warten(
        _detail(lauf, wf),
        lambda d: _status(d) == "Stornierung angefordert, nichts mehr aktiv",
        was="nichts mehr aktiv",
    )
    assert "Workflow stornieren" not in ende, "AE11: kein erneutes Stornieren"
    zeile = await _liste(lauf, wf)()
    assert zeile is not None
    assert "Stornierung angefordert, nichts mehr aktiv" in zeile, zeile
    for endstatus in ("fertig", "gescheitert", "storniert"):
        assert endstatus not in zeile, zeile

    # Die Gegenprobe an der Quelle: kein Endstatus, weil keiner geschrieben
    # wurde -- nicht, weil die Ansicht ihn verschluckt.
    leser = SwarmQWorkflowleser(lauf.broker_url, lauf.backend_url)
    try:
        bild = await leser.lesen(wf)
    finally:
        await leser.close()
    assert bild.zustand == "gelesen", bild.grund
    assert bild.status == "running"

    # Und die Handlung steht im Audit, genau einmal, als gelungen.
    assert lauf.audit is not None
    eintraege = [e for e in await lauf.audit.read(action="workflow_cancel") if e.target == wf]
    assert [e.outcome for e in eintraege] == ["ok"], eintraege


async def test_ein_echter_chord_steht_als_diagramm_da_und_wird_fertig(lauf: Lauf) -> None:
    """Covers F1, R1, R2. Der Chord aus dem Durchstich, diesmal im Diagramm:
    ohne Wahl ist es die Vorgabe, und Knoten wie Linien entstehen aus dem
    Zustand, den Bibliothek und Worker schreiben."""
    wf = await _starten(lauf)
    lade, fasse = f"{lauf.queue}_lade", f"{lauf.queue}_fasse"

    def body_wartet(d: str) -> bool:
        bild = _diagramm(d)
        return bild is not None and any(
            "wartet auf 3 Glieder" in k.text for k in _schritte(bild[0], fasse))

    wartend = await _warten(_detail(lauf, wf), body_wartet, was="Body wartet auf 3")
    assert 'id="workflow-baum"' not in wartend, "R8: ohne Wahl zeigt die Seite das Diagramm"
    bild = _diagramm(wartend)
    assert bild is not None
    knoten, linien = bild
    kopf = _schritte(knoten, lade)
    [body] = _schritte(knoten, fasse)
    assert len(kopf) == 3, knoten
    # Jeder Schritt ist Zeichen, Wort und Name -- Farbe allein nie (R1).
    for glied in kopf:
        # "eingereiht" aus der Topologie oder "wartet seit" aus dem Ereignis
        # des Anwendungs-Hooks -- je nachdem, was zuerst ankam; neutral beides.
        assert glied.klasse == "is-idle" and glied.text.startswith("○ "), glied
        assert "eingereiht" in glied.text or "wartet seit" in glied.text, glied
        assert glied.link is not None and glied.link.startswith("/tasks/"), glied
    assert body.link is None, "R5: ein nicht eingereihter Body hat keine Task-Ansicht"
    # Der Kopf faechert vom Startpunkt auf und fuehrt in den Body (R2).
    for glied in kopf:
        assert f"dk-kante-start-{_name(glied)}" in linien, (glied.kennung, linien)
        assert f"dk-kante-{_name(glied)}-{_name(body)}" in linien, (glied.kennung, linien)

    await lauf.worker_starten()

    def body_fertig(d: str) -> bool:
        bild = _diagramm(d)
        return bild is not None and any(
            k.klasse == "is-ok" and "fertig" in k.text for k in _schritte(bild[0], fasse))

    fertig = await _warten(_detail(lauf, wf), body_fertig, was="Body fertig")
    bild = _diagramm(fertig)
    assert bild is not None
    knoten, linien = bild
    [body_nachher] = _schritte(knoten, fasse)
    # Dieselbe Kennung wie vorher: idiomorph gleicht den Knoten ab (R9).
    assert body_nachher.kennung == body.kennung
    assert body_nachher.link is not None and body_nachher.link.startswith("/tasks/")
    # Die fertigen Downloads sind gefaltet (R4) und verweisen auf ihre Seite.
    assert _schritte(knoten, lade) == [], knoten
    [sammel] = [k for k in knoten.values() if k.kennung.endswith("-fertig")]
    assert sammel.titel == "3 fertig", sammel
    assert sammel.link is not None and sammel.link.startswith(f"/workflows/{wf}/glieder?gruppe="), sammel
    assert f"dk-kante-{_name(sammel)}-{_name(body)}" in linien, linien


def _ansicht_der_seite(detail_html: str) -> str:
    """Welche Ansicht die Seite zeigt -- genau eine, nie beide (R7)."""
    diagramm = _diagramm(detail_html) is not None
    liste = 'id="workflow-baum"' in detail_html
    assert diagramm != liste, "die Seite zeigt beide Ansichten oder keine"
    return "diagramm" if diagramm else "liste"


def _ansicht_cookie(antwort: Any) -> str | None:
    """Das gesetzte Ansicht-Cookie einer Antwort; die Sitzung erneuert ihr
    eigenes Cookie bei jeder Antwort und zaehlt hier nicht."""
    eigene = [c for c in antwort.headers.get_list("set-cookie")
              if c.startswith("sqd_workflow_ansicht=")]
    assert len(eigene) <= 1, eigene
    return eigene[0] if eigene else None


async def test_die_gewaehlte_ansicht_steht_in_der_adresse_und_wird_gemerkt(lauf: Lauf) -> None:
    """Covers AE4, R7, R8. Der Umschalter gegen den echten Dienst, mit dem
    Cookie, wie der Client es zuruecksendet -- nicht wie eine Attrappe es
    sich vorstellt."""
    erster = await _starten(lauf)
    zweiter = await _starten(lauf)

    def mit_body(d: str) -> bool:
        bild = _diagramm(d)
        return bild is not None and any(k.kennung == "dk-b" for k in bild[0].values())

    for wf in (erster, zweiter):
        await _warten(_detail(lauf, wf), mit_body, was=f"Diagramm von {wf} mit Body")

    liste = await lauf.web.get(f"/workflows/{erster}?ansicht=liste", headers={"accept": "text/html"})
    assert liste.status_code == 200
    assert _ansicht_der_seite(liste.text) == "liste"
    assert 'id="knoten-b"' in liste.text, "die Liste zeigt den Baum mit dem Body"
    assert re.search(r'href="\?ansicht=liste"\s+aria-current="page"', liste.text), \
        "der Umschalter markiert die gewaehlte Ansicht"
    cookie = _ansicht_cookie(liste)
    assert cookie is not None and cookie.startswith("sqd_workflow_ansicht=liste;"), cookie
    for merkmal in ("Path=/workflows", "SameSite=Strict", "HttpOnly", "Secure"):
        assert merkmal.lower() in cookie.lower(), (merkmal, cookie)

    # Ein anderer Workflow ohne Angabe: die gemerkte Wahl gilt (AE4).
    anderer = await _detail(lauf, zweiter)()
    assert anderer is not None and _ansicht_der_seite(anderer) == "liste"
    # Dieselben Kopfangaben in beiden Ansichten (R7).
    assert _status(anderer) == "läuft" and "Workflow stornieren" in anderer

    # Ein verstuemmelter Wert zeigt das Diagramm, merkt aber nichts.
    quatsch = await lauf.web.get(f"/workflows/{zweiter}?ansicht=quatsch",
                                 headers={"accept": "text/html"})
    assert _ansicht_der_seite(quatsch.text) == "diagramm"
    assert _ansicht_cookie(quatsch) is None
    nach_quatsch = await _detail(lauf, zweiter)()
    assert nach_quatsch is not None and _ansicht_der_seite(nach_quatsch) == "liste"

    # Zurueck zum Diagramm -- auch das wird gemerkt.
    zurueck = await lauf.web.get(f"/workflows/{zweiter}?ansicht=diagramm",
                                 headers={"accept": "text/html"})
    assert _ansicht_der_seite(zurueck.text) == "diagramm"
    gemerkt = _ansicht_cookie(zurueck)
    assert gemerkt is not None and gemerkt.startswith("sqd_workflow_ansicht=diagramm;"), gemerkt
    wieder = await _detail(lauf, erster)()
    assert wieder is not None and _ansicht_der_seite(wieder) == "diagramm"
