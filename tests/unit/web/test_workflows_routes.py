"""Workflow-Liste, Baum und fertige Glieder (Workflow-Ansicht, U6).

Der Hauptfluss (F1, F2): ein Teammitglied kommt von der Navigation zur
Liste, sieht Status und Fortschritt, oeffnet den Workflow und findet im Baum
das schuldige Glied -- ohne Logs oder Valkey-Schluessel zu lesen.

Die Seiten rechnen nichts selbst: Status, Zaehler und Gruende kommen aus dem
Baum (U4), Zeilen und Seiten aus den Abfragen (U5). Geprueft wird hier, dass
sie **ankommen** -- und dass keine Seite einen Zustand behauptet, den keine
Lesung geliefert hat (R6).

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Die Liste ===
1. Covers AE1. Die Liste zeigt eine Zeile mit Kennung, Wurzelform, erstem
   Schritt, Status, Fortschritt, Start und letzter Aktivitaet; ein
   laufender Workflow traegt ``is-run``.
2. Der stehende Hinweis (R4) steht auf der Seite, und eine leere Liste nennt
   ihre wahrscheinliche Ursache (R33).
3. Status- und Kennungsfilter wirken ueber die Adresszeile (R3), auch mit
   der Kennung eines Schritts.
4. Covers AE6. Ein nie gesehener Workflow zeigt "Zustand nie gesehen" in
   der Liste.
5. Ein verfallener Workflow zeigt "Zustand verfallen, zuletzt: läuft" mit
   dem Zeitpunkt der letzten Lesung (R17).
6. Mehr als eine Seite: der Verweis "Weitere" traegt Filter und Marke.

=== ZYKLUS 2: Der Baum ===
7. Covers AE3. Ein Chord mit laufendem Kopf: der Body steht als "nicht
   eingereiht — wartet auf Vorgänger" im Baum und nennt, auf wie viele
   Glieder er wartet (R10, R11).
8. Ein Body mit "Vorgänger fertig, nicht eingereiht" traegt ``is-wait``.
9. Covers AE2. 498 fertige, 1 laufendes, 1 gescheitertes Glied: zwei
   Gliedzeilen und eine aufklappbare Zeile "498 fertig".
10. Covers AE4. Kette a -> b -> c, b gescheitert: oben Fehlertext und b mit
    Sprung in den Baum, c als "nie gestartet, Workflow gescheitert".
11. Covers AE5. Fertig, Zustand verfallen: Baum und "fertig" aus der Kopie.
12. Covers AE6. Nie gesehen: die bekannten Schritte und "Zustand nie
    gesehen", kein Endstatus.
13. Eine Schrittkennung mit ``#`` ist im Baum anklickbar und fuehrt zur
    Task-Ansicht; jeder Knoten hat eine stabile Element-ID.
14. Ein unbekannter oder unbestaetigter Workflow ergibt eine benannte 404.

=== ZYKLUS 3: Ergebnisse der Kopf-Glieder (R13, R18, KTD8) ===
15. Ein beendetes Kopf-Glied im Baum wird gelesen; ein abgelaufenes
    Ergebnis heisst "Ergebnis abgelaufen". Ein noch laufendes Glied loest
    keinen Leseraufruf aus.
16. Ein Kopf-Glied, das eine Gruppe ist, zeigt "nicht einzeln anzeigbar",
    und der Ergebnisleser wird dafuer nicht aufgerufen (R18).
17. Ein Kopf-Glied, das eine Kette ist, zeigt das Ergebnis ihres letzten
    Schritts (R18).

=== ZYKLUS 4: Fertige Glieder (R9, KTD13) ===
18. Der Link der gefalteten Zeile oeffnet die Seite mit den fertigen
    Gliedern, seitenweise mit "Weitere". Covers AE7: fertige Kopf-Glieder
    mit abgelaufenem Ergebnis zeigen dort "Ergebnis abgelaufen".
19. Eine unbekannte Gruppe nennt den Grund statt einer leeren Tabelle.

=== ZYKLUS 5: Zugang ===
20. Ohne Sitzung liefern Liste, Detail und Glieder keine Daten.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from litestar.testing import AsyncTestClient

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin
from swarmq_dashboard.web.app import create_app
from swarmq_dashboard.web.ergebnis import Ergebnisbild
from swarmq_dashboard.workflows import Stornobild, Workflowbild

pytestmark = pytest.mark.timeout(5)

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
_PASSWORT = "ein-langes-passwort"
BASIS = "https://testserver.local"


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


def _wf(i: int) -> str:
    """Eine Workflow-Kennung in der UUID-Form der Bibliothek (KTD5)."""
    return f"{i:08x}-0000-4000-8000-000000000000"


def _sig_top(name: str) -> dict[str, object]:
    return {"kind": "signature", "task": name, "args": [], "kwargs": {}, "immutable": False}


def _gruppe(*glieder: Mapping[str, object]) -> dict[str, object]:
    return {"kind": "group", "members": list(glieder)}


def _kette(*schritte: Mapping[str, object]) -> dict[str, object]:
    return {"kind": "chain", "steps": list(schritte)}


def _chord(kopf: Mapping[str, object], body: Mapping[str, object]) -> dict[str, object]:
    return {"kind": "chord", "header": kopf, "body": body}


class _Verzeichnis:
    """Schreibt Schritte und Lesungen ins Verzeichnis, wie Strom und Abgleich."""

    def __init__(self, config: DashboardConfig) -> None:
        self.w = DirectoryWriter(connect(config.directory_file))
        self._n = 0
        # Der Leser gilt als eingeholt -- sonst meldet der Leerzustand einen
        # Neuaufbau, und das ist nicht, was diese Tests pruefen.
        self.w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                            "on conflict(key) do nothing")
        self.w._con.commit()

    def _apply(self, ereignis: Event) -> None:
        self._n += 1
        self.w.apply([(f"{self._n}-0", ereignis)])

    def schritt(self, wf: str, pfad: str, ts: float, *signale: str,
                name: str = "download", fehler: str | None = None) -> str:
        """Einen Schritt einreihen und die *signale* nacheinander melden."""
        tid = f"{wf}#{pfad}"
        self._apply(Event(
            type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
            payload={"task_id": tid, "task_name": name, "queue": "wf", "args": [],
                     "kwargs": {}, "workflow_id": wf, "step_path": pfad}))
        for i, typ in enumerate(signale, start=1):
            nutzlast: dict[str, object] = {"task_id": tid, "task_name": name, "queue": "wf"}
            if typ == "task_completed":
                nutzlast["duration_ms"] = 1500.0
            if typ == "task_failed" and fehler:
                nutzlast["error"] = fehler
            self._apply(Event(type=typ, ts=ts + i, source="phx-w1:0", origin=Origin.PICKUP,
                              payload=nutzlast))
        return tid

    def lesung(self, wf: str, topologie: Mapping[str, object] | None, *,
               status: str = "running", step_tasks: Mapping[str, str] | None = None,
               fehler: Mapping[str, object] | None = None, gelesen_am: float = 1_700_000_500.0,
               ) -> None:
        wurzel = None if topologie is None else str(topologie["kind"])
        assert self.w.schreibe_lesung(
            wf, status=status, wurzel=wurzel, topologie=topologie,
            step_tasks=dict(step_tasks or {}), fehler=fehler, gelesen_am=gelesen_am)

    def close(self) -> None:
        self.w.close()


class _StillerLeser:
    """Workflowleser, der nie etwas liest: der Abgleich beim Start laesst die
    Kopie dann, wie der Test sie geschrieben hat."""

    async def lesen(self, workflow_id: str) -> Workflowbild:
        return Workflowbild(zustand="nicht_erreichbar", grund="Testreihe")

    async def stornieren(self, workflow_id: str) -> Stornobild:
        return Stornobild(ausgang="nicht_erreichbar", grund="Testreihe")

    async def close(self) -> None:
        return None


class _Zaehlleser:
    """Ergebnisleser, der jeden Aufruf festhaelt."""

    def __init__(self, bild: Ergebnisbild | None = None) -> None:
        self.bild = bild or Ergebnisbild(zustand="fehlt", art=None, text="")
        self.aufrufe: list[str] = []

    async def lesen(self, task_id: str) -> Ergebnisbild:
        self.aufrufe.append(task_id)
        return self.bild

    async def close(self) -> None:
        return None


async def _angemeldet(config: DashboardConfig, *, ergebnisleser: object | None = None,
                      ) -> Any:
    app = create_app(config, hasher=_BILLIG, ergebnisleser=ergebnisleser,  # type: ignore[arg-type]
                     workflowleser=_StillerLeser())
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="?_csrf_token"?\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return c


def _zeile(html: str, kennung: str) -> str:
    """Die Tabellenzeile mit ``id="<kennung>"``."""
    treffer = re.search(rf'<tr id="{re.escape(kennung)}".*?</tr>', html, re.S)
    assert treffer, f"keine Zeile {kennung!r} in der Seite"
    return treffer.group(0)


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


# --- Zyklus 1 -----------------------------------------------------------


async def test_die_liste_zeigt_kennung_form_status_und_fortschritt(tmp_path: Path) -> None:
    """Covers AE1. Ein Chord mit drei Downloads im Kopf, zwei fertig: "2/4"
    (fertige Schritte zu Schritten laut Topologie) und "läuft" als ``is-run``."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    top = _chord(_gruppe(*[_sig_top("download")] * 3), _sig_top("verarbeiten"))
    v.schritt(wf, "/h/g0", 1_700_000_000.0, "task_started", "task_completed")
    v.schritt(wf, "/h/g1", 1_700_000_010.0, "task_started", "task_completed")
    v.schritt(wf, "/h/g2", 1_700_000_020.0, "task_started")
    v.lesung(wf, top)
    v.close()
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/workflows")
        assert antwort.status_code == 200
        zeile = _zeile(antwort.text, f"workflow-{wf}")
        assert f'href="/workflows/{wf}"' in zeile
        text = _text(zeile)
        assert "Chord" in text and "download" in text and "2/4" in text
        assert "läuft" in text
        assert 'class="tag is-run"' in zeile
        assert "2023-11-14 22:13:20" in text, "der Start fehlt"
        assert "2023-11-14 22:13:41" in text, "die letzte Aktivitaet fehlt"
    finally:
        await c.__aexit__(None, None, None)


async def test_hinweis_und_leerzustand_stehen_da(tmp_path: Path) -> None:
    """R4 und R33: eine leere Liste liest sich sonst wie "nichts gelaufen"."""
    from swarmq_dashboard.directory.queries import WORKFLOW_HINWEIS

    config = _config(tmp_path)
    _Verzeichnis(config).close()
    c = await _angemeldet(config)
    try:
        leer = await c.get("/workflows")
        assert WORKFLOW_HINWEIS in leer.text
        assert "Noch kein Workflow im Verzeichnis" in leer.text
        gesucht = await c.get(f"/workflows?q={_wf(9)}")
        assert "Kein Workflow passt zu dieser Suche" in gesucht.text
    finally:
        await c.__aexit__(None, None, None)


async def test_status_und_kennung_filtern(tmp_path: Path) -> None:
    """R3: nach Status, und ueber die Kennung eines Schritts zum Workflow."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    laeuft, fertig = _wf(1), _wf(2)
    v.schritt(laeuft, "", 1_700_000_000.0, "task_started")
    v.lesung(laeuft, _sig_top("download"))
    v.schritt(fertig, "/0", 1_700_000_100.0, "task_started", "task_completed", name="a")
    v.schritt(fertig, "/1", 1_700_000_200.0, "task_started", "task_completed", name="b")
    v.lesung(fertig, _kette(_sig_top("a"), _sig_top("b")), status="completed")
    v.close()
    c = await _angemeldet(config)
    try:
        nur_fertig = await c.get("/workflows?status=fertig")
        assert fertig in nur_fertig.text and laeuft not in nur_fertig.text
        assert '<option value="fertig" selected>' in nur_fertig.text

        nach_schritt = await c.get("/workflows", params={"q": f"{fertig}#/1"})
        assert f"workflow-{fertig}" in nach_schritt.text
        assert f"workflow-{laeuft}" not in nach_schritt.text
    finally:
        await c.__aexit__(None, None, None)


async def test_nie_gesehen_steht_in_der_liste(tmp_path: Path) -> None:
    """Covers AE6. Kein Status geraten: "Zustand nie gesehen", und der
    Fortschritt zaehlt nur die bekannten Schritte (R7)."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(wf, "/1", 1_700_000_010.0, "task_started", name="b")
    assert v.w.vermerke_nie_gesehen(wf)
    v.w.bestaetige_kandidat(wf)
    v.close()
    c = await _angemeldet(config)
    try:
        zeile = _zeile((await c.get("/workflows")).text, f"workflow-{wf}")
        assert "Zustand nie gesehen" in _text(zeile) and "1/2" in _text(zeile)
        assert 'class="tag is-idle"' in zeile
        for endstatus in ("fertig", "gescheitert", "storniert"):
            assert f"> {endstatus}" not in zeile
    finally:
        await c.__aexit__(None, None, None)


async def test_verfallen_nennt_den_zeitpunkt_der_letzten_lesung(tmp_path: Path) -> None:
    """R17: "Zustand verfallen, zuletzt: läuft" -- mit dem Zeitpunkt, sonst
    liest es sich wie "gerade eben"."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "", 1_700_000_000.0, "task_started")
    v.lesung(wf, _sig_top("download"), gelesen_am=1_700_003_600.0)
    assert v.w.vermerke_anzeige(wf, "laeuft")
    assert v.w.vermerke_verfallen(wf)
    v.close()
    c = await _angemeldet(config)
    try:
        text = _text(_zeile((await c.get("/workflows")).text, f"workflow-{wf}"))
        assert "Zustand verfallen, zuletzt: läuft" in text
        assert "2023-11-14 23:13:20" in text, text
        gefiltert = await c.get("/workflows?status=verfallen")
        assert f"workflow-{wf}" in gefiltert.text
    finally:
        await c.__aexit__(None, None, None)


async def test_weitere_traegt_filter_und_marke(tmp_path: Path) -> None:
    """Wer blaettert, bleibt im gewaehlten Status und Zeitraum."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    for i in range(51):
        v.schritt(_wf(i), "", 1_700_000_000.0 + i, "task_started")
        v.lesung(_wf(i), _sig_top("download"))
    v.close()
    c = await _angemeldet(config)
    try:
        erste = await c.get("/workflows?status=laeuft&until=1800000000")
        verweis = re.search(r'href="(/workflows\?[^"]*)">Weitere', erste.text)
        assert verweis, "kein Verweis auf die naechste Seite"
        ziel = verweis.group(1).replace("&amp;", "&")
        assert "status=laeuft" in ziel and "until=1800000000" in ziel and "cursor=" in ziel
        zweite = await c.get(ziel)
        assert f"workflow-{_wf(0)}" in zweite.text
        assert f"workflow-{_wf(50)}" not in zweite.text
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 2 -----------------------------------------------------------


async def test_der_body_wartet_auf_den_laufenden_kopf(tmp_path: Path) -> None:
    """Covers AE3. Den Body hat noch kein Ereignis gemeldet -- er steht
    trotzdem im Baum, mit Grund und der Zahl offener Glieder (R10, R11)."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    top = _chord(_gruppe(*[_sig_top("download")] * 3), _sig_top("verarbeiten"))
    v.schritt(wf, "/h/g0", 1_700_000_000.0, "task_started", "task_completed")
    v.schritt(wf, "/h/g1", 1_700_000_010.0, "task_started")
    v.schritt(wf, "/h/g2", 1_700_000_020.0)
    v.lesung(wf, top)
    v.close()
    c = await _angemeldet(config)
    try:
        antwort = await c.get(f"/workflows/{wf}")
        assert antwort.status_code == 200
        body = _zeile(antwort.text, "knoten-b")
        assert "nicht eingereiht — wartet auf Vorgänger" in _text(body)
        assert "wartet auf 2 Glieder" in _text(body)
        assert 'class="tag is-wait"' in body
        kopf = _text(_zeile(antwort.text, "knoten-h"))
        assert "1 fertig" in kopf and "1 laufend" in kopf and "1 wartend" in kopf
    finally:
        await c.__aexit__(None, None, None)


async def test_vorgaenger_fertig_ist_ein_wartezustand(tmp_path: Path) -> None:
    """KTD7: alle Kopf-Glieder fertig, der Body nicht eingereiht -- das ist
    ein Warten (``is-wait``), kein "wartet auf 0 Glieder"."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    top = _chord(_gruppe(*[_sig_top("download")] * 2), _sig_top("verarbeiten"))
    v.schritt(wf, "/h/g0", 1_700_000_000.0, "task_started", "task_completed")
    v.schritt(wf, "/h/g1", 1_700_000_010.0, "task_started", "task_completed")
    v.lesung(wf, top)
    v.close()
    c = await _angemeldet(config)
    try:
        body = _zeile((await c.get(f"/workflows/{wf}")).text, "knoten-b")
        assert "nicht eingereiht — Vorgänger fertig, nicht eingereiht" in _text(body)
        assert 'class="tag is-wait"' in body
        assert "wartet auf 0" not in _text(body)
    finally:
        await c.__aexit__(None, None, None)


async def test_fertige_glieder_sind_eine_aufklappbare_zeile(tmp_path: Path) -> None:
    """Covers AE2. 498 fertig, 1 laeuft, 1 gescheitert: zwei Gliedzeilen und
    eine Zeile "498 fertig" (R9)."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    for i in range(500):
        signale: tuple[str, ...] = ("task_started", "task_completed")
        if i == 7:
            signale = ("task_started",)
        elif i == 300:
            signale = ("task_started", "task_failed")
        v.schritt(wf, f"/g{i}", 1_700_000_000.0 + i, *signale)
    v.lesung(wf, _gruppe(*[_sig_top("download")] * 500))
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}")).text
        baum = re.search(r'<tbody id="workflow-baum">(.*?)</tbody>', html, re.S)
        assert baum
        zeilen = re.findall(r'<tr id="([^"]+)"', baum.group(1))
        assert zeilen == ["knoten", "knoten-g7", "knoten-g300", "knoten-fertig"], zeilen
        assert "498 fertig" in _text(_zeile(html, "knoten-fertig"))
    finally:
        await c.__aexit__(None, None, None)


async def test_gescheiterte_kette_nennt_fehler_und_schuldigen(tmp_path: Path) -> None:
    """Covers AE4. Status aus dem Zustand, oben ``b`` mit Fehlertext und
    Sprung in den Baum, ``c`` als "nie gestartet, Workflow gescheitert"."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(wf, "/1", 1_700_000_010.0, "task_started", "task_failed", name="b",
              fehler="ValueError: kaputt")
    v.lesung(wf, _kette(_sig_top("a"), _sig_top("b"), _sig_top("c")), status="failed",
             fehler={"type": "ValueError", "message": "kaputt im Zustand"})
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}")).text
        oben = html[:html.index('id="workflow-baum"')]
        assert "ValueError: kaputt im Zustand" in oben
        assert 'href="#knoten-1"' in oben, "kein Sprung zum gescheiterten Schritt"
        assert "ValueError: kaputt" in _text(oben)
        assert 'class="tag is-fail"' in oben
        c_zeile = _zeile(html, "knoten-2")
        assert "nicht eingereiht — nie gestartet, Workflow gescheitert" in _text(c_zeile)
        assert 'class="tag is-idle"' in c_zeile
    finally:
        await c.__aexit__(None, None, None)


async def test_fertig_aus_der_kopie_nach_dem_verfall(tmp_path: Path) -> None:
    """Covers AE5. Der Zustand ist verfallen, die Kopie traegt "fertig"."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(wf, "/1", 1_700_000_010.0, "task_started", "task_completed", name="b")
    v.lesung(wf, _kette(_sig_top("a"), _sig_top("b")), status="completed")
    v.w.vermerke_verfallen(wf)      # greift bei einem Endstatus nicht -- gut so
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}")).text
        oben = html[:html.index('id="workflow-baum"')]
        assert 'class="tag is-ok"' in oben and "fertig" in _text(oben)
        assert "verfallen" not in _text(oben)
        for kennung in ("knoten", "knoten-0", "knoten-1"):
            _zeile(html, kennung)
    finally:
        await c.__aexit__(None, None, None)


async def test_nie_gesehen_zeigt_die_bekannten_schritte(tmp_path: Path) -> None:
    """Covers AE6. Schritte aus dem Verzeichnis, "Zustand nie gesehen", kein
    Endstatus."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(wf, "/1", 1_700_000_010.0, "task_started", name="b")
    assert v.w.vermerke_nie_gesehen(wf)
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}")).text
        oben = _text(html[:html.index('id="workflow-baum"')])
        assert "Zustand nie gesehen" in oben
        assert "nie lesen können" in oben
        assert "a" in _text(_zeile(html, "knoten-0")) and "b" in _text(_zeile(html, "knoten-1"))
    finally:
        await c.__aexit__(None, None, None)


async def test_schrittkennung_mit_raute_fuehrt_zur_task_ansicht(tmp_path: Path) -> None:
    """``<wf>#<pfad>`` roh in der Adresse trennte das Fragment ab, und die
    Task-Ansicht antwortete mit 404."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    tid = v.schritt(wf, "", 1_700_000_000.0, "task_started", name="einzeln")
    v.lesung(wf, _sig_top("einzeln"))
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}")).text
        verweis = re.search(r'<a class="trunc" href="(/tasks/[^"]+)">einzeln</a>',
                            _zeile(html, "knoten"))
        assert verweis and verweis.group(1) == f"/tasks/{wf}%23"
        task = await c.get(verweis.group(1))
        assert task.status_code == 200 and tid in task.text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_unbekannter_workflow_ist_eine_benannte_404(tmp_path: Path) -> None:
    """Auch ein unbestaetigter Kandidat aus dem Muster (KTD5)."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    kandidat = _wf(2)
    v._apply(Event(type="task_enqueue", ts=1_700_000_000.0, source="app",
                   origin=Origin.ENQUEUE,
                   payload={"task_id": f"{kandidat}#", "task_name": "x", "queue": "q",
                            "args": [], "kwargs": {}}))
    v.close()
    c = await _angemeldet(config)
    try:
        for wf in (_wf(1), kandidat):
            antwort = await c.get(f"/workflows/{wf}", headers={"accept": "text/html"})
            assert antwort.status_code == 404
            assert "Aufbewahrung" in antwort.text, antwort.text[-400:]
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 3 -----------------------------------------------------------


async def test_ein_beendetes_kopfglied_zeigt_sein_ergebnis(tmp_path: Path) -> None:
    """Covers AE7 (im Baum). Gelesen wird nur, was beendet ist -- ein
    laufendes Glied hat noch kein Ergebnis, der Aufruf waere umsonst."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    top = _chord(_gruppe(*[_sig_top("download")] * 2), _sig_top("verarbeiten"))
    v.schritt(wf, "/h/g0", 1_700_000_000.0, "task_started", "task_failed", fehler="kaputt")
    v.schritt(wf, "/h/g1", 1_700_000_010.0, "task_started")
    v.lesung(wf, top, status="failed", fehler={"type": "E", "message": "kaputt"})
    v.close()
    leser = _Zaehlleser(Ergebnisbild(zustand="fehlt", art=None, text=""))
    c = await _angemeldet(config, ergebnisleser=leser)
    try:
        html = (await c.get(f"/workflows/{wf}")).text
        assert "Ergebnis abgelaufen" in _text(_zeile(html, "knoten-h-g0"))
        assert "Ergebnis abgelaufen" not in _text(_zeile(html, "knoten-h-g1"))
        assert leser.aufrufe == [f"{wf}#/h/g0"]
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_gruppen_kopfglied_ist_nicht_einzeln_anzeigbar(tmp_path: Path) -> None:
    """R18, KD9: das Sammelergebnis gibt die Bibliothek nicht heraus -- also
    kein Leseraufruf, auch nicht fuer die fertigen Blaetter darin."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    top = _chord(_gruppe(_gruppe(_sig_top("a"), _sig_top("b")), _sig_top("c")),
                 _sig_top("verarbeiten"))
    v.schritt(wf, "/h/g0/g0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(wf, "/h/g0/g1", 1_700_000_001.0, "task_started", name="b")
    v.schritt(wf, "/h/g1", 1_700_000_002.0, "task_started", name="c")
    v.lesung(wf, top)
    v.close()
    leser = _Zaehlleser(Ergebnisbild(zustand="da", art="fertig", text="{}"))
    c = await _angemeldet(config, ergebnisleser=leser)
    try:
        html = (await c.get(f"/workflows/{wf}")).text
        assert "nicht einzeln anzeigbar" in _text(_zeile(html, "knoten-h-g0"))
        assert leser.aufrufe == []
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_ketten_kopfglied_zeigt_das_ergebnis_des_letzten_schritts(
        tmp_path: Path) -> None:
    """R18, KTD8: eine Kette folgt ihrem letzten Element."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    top = _chord(_gruppe(_kette(_sig_top("a"), _sig_top("b")), _sig_top("c")),
                 _sig_top("verarbeiten"))
    v.schritt(wf, "/h/g0/0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(wf, "/h/g0/1", 1_700_000_001.0, "task_started", "task_failed", name="b")
    v.schritt(wf, "/h/g1", 1_700_000_002.0, "task_started", name="c")
    v.lesung(wf, top, status="failed")
    v.close()
    leser = _Zaehlleser(Ergebnisbild(zustand="da", art="gescheitert", text="TypeError: b"))
    c = await _angemeldet(config, ergebnisleser=leser)
    try:
        html = (await c.get(f"/workflows/{wf}")).text
        assert "gescheitert: TypeError: b" in _text(_zeile(html, "knoten-h-g0"))
        assert leser.aufrufe == [f"{wf}#/h/g0/1"]
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 4 -----------------------------------------------------------


async def test_die_gefaltete_zeile_oeffnet_die_fertigen_glieder(tmp_path: Path) -> None:
    """KTD13, Covers AE7: die fertigen Kopf-Glieder seitenweise zu 100, jedes
    mit "Ergebnis abgelaufen", und ein Rueckweg in den Baum."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    top = _chord(_gruppe(*[_sig_top("download")] * 150), _sig_top("verarbeiten"))
    for i in range(150):
        v.schritt(wf, f"/h/g{i}", 1_700_000_000.0 + i, "task_started", "task_completed")
    v.lesung(wf, top)
    v.close()
    leser = _Zaehlleser(Ergebnisbild(zustand="fehlt", art=None, text=""))
    c = await _angemeldet(config, ergebnisleser=leser)
    try:
        baum = (await c.get(f"/workflows/{wf}")).text
        assert leser.aufrufe == [], "gefaltete Glieder wurden im Baum gelesen"
        verweis = re.search(r'<a href="([^"]+)">\s*150 fertig</a>',
                            _zeile(baum, "knoten-h-fertig"))
        assert verweis, "die gefaltete Zeile verlinkt nicht"
        erste = await c.get(verweis.group(1).replace("&amp;", "&"))
        assert erste.status_code == 200
        zeilen = re.findall(r'<tr id="(knoten[^"]*)"', erste.text)
        assert len(zeilen) == 100 and zeilen[0] == "knoten-h-g0"
        assert _text(erste.text).count("Ergebnis abgelaufen") == 100
        assert len(leser.aufrufe) == 100
        assert f'href="/workflows/{wf}"' in erste.text, "kein Rueckweg in den Baum"

        weiter = re.search(r'href="([^"]+)">Weitere', erste.text)
        assert weiter
        zweite = await c.get(weiter.group(1).replace("&amp;", "&"))
        zeilen = re.findall(r'<tr id="(knoten[^"]*)"', zweite.text)
        assert len(zeilen) == 50 and zeilen[0] == "knoten-h-g100"
        assert "Weitere" not in zweite.text
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_leere_gliederseite_nennt_den_grund(tmp_path: Path) -> None:
    """R33: eine unbekannte Gruppe, ein unbekannter Workflow und eine Gruppe
    ohne fertiges Glied -- keine leere Tabelle ohne Satz."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/g0", 1_700_000_000.0, "task_started")
    v.lesung(wf, _gruppe(_sig_top("a"), _sig_top("b")))
    v.close()
    c = await _angemeldet(config)
    try:
        falsche_gruppe = await c.get(f"/workflows/{wf}/glieder", params={"gruppe": "/g9"})
        assert "keine Gruppe" in _text(falsche_gruppe.text)
        unbekannt = await c.get(f"/workflows/{_wf(5)}/glieder", params={"gruppe": ""})
        assert "kennt das Verzeichnis nicht" in _text(unbekannt.text)
        ohne_fertige = await c.get(f"/workflows/{wf}/glieder", params={"gruppe": ""})
        assert "noch kein fertiges Glied" in _text(ohne_fertige.text)
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 5 -----------------------------------------------------------


async def test_ohne_sitzung_keine_daten(tmp_path: Path) -> None:
    """Die Topologie traegt Argumente im Klartext (KTD4) -- sie gehoert
    hinter die Anmeldung wie die Task-Seiten."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "", 1_700_000_000.0, "task_started")
    v.lesung(wf, _sig_top("geheim_task"))
    v.close()
    app = create_app(config, hasher=_BILLIG, workflowleser=_StillerLeser())
    async with AsyncTestClient(app=app, base_url=BASIS) as c:
        for pfad in ("/workflows", f"/workflows/{wf}", f"/workflows/{wf}/glieder"):
            antwort = await c.get(pfad, follow_redirects=False)
            assert antwort.status_code in (401, 303), (pfad, antwort.status_code)
            assert wf not in antwort.text and "geheim_task" not in antwort.text
