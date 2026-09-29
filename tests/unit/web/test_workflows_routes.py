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

=== ZYKLUS 6: Workflow stornieren (U7, R15, R16, KTD9) ===
21. Ein laufender Workflow bietet "Workflow stornieren" mit
    Bestaetigungsformular und dem Hinweis zur Wirkung (R16).
22. Covers AE9. Ein fertiger Workflow bietet den Knopf nicht an.
23. Covers F3. Bestaetigt: frische Lesung vor dem Aufruf, Vermerk gesetzt,
    Audit "ok", die Ansicht zeigt "Stornierung angefordert" und die Wirkung.
24. Covers AE8. Ein zweites Stornieren meldet die bestehende Anforderung,
    Audit "lief bereits", kein Fehler.
25. Covers AE10. Die frische Lesung ergibt "completed": "nicht mehr
    stornierbar", kein Aufruf von cancel, kein Audit-Eintrag.
26. Der Zustand ist verschwunden (Lesung "fehlt" oder cancel "nicht
    gefunden"): "nicht mehr stornierbar", kein Audit-Eintrag.
27. Backend nicht erreichbar (bei der Lesung oder beim cancel): Hinweis,
    Audit "fehlgeschlagen", kein Vermerk.
28. Ohne Bestaetigungsfeld wird nichts storniert.
29. Ohne CSRF-Merkmal wird die Anfrage abgewiesen.
30. Ein unbekannter Workflow ergibt eine 404 ohne Aufruf von cancel.
31. Nach dem Vermerk stoesst die Route eine Lesung des Workflows an, damit
    die Ansicht schnell nachzieht.
32. Covers F3. Nach dem Stornieren und dem Abbruch aller wartenden Glieder
    zeigt die Ansicht "Stornierung angefordert, nichts mehr aktiv" und
    keinen Knopf.

=== ZYKLUS 7: Kaputte Topologie ===
33. Eine Kopie, deren Topologie erst unter der Wurzel kaputt ist: die Liste
    zeigt sie und die anderen, das Detail antwortet 200 mit den bekannten
    Schritten und dem Hinweis "Topologie nicht lesbar", die Gliederseite
    nennt ihren Grund -- keine Seite antwortet 500.

=== ZYKLUS 8: Die Dauer-Spalte (Aufraeumen, U1, R6) ===
Charakterisierung: die Tests halten bestehendes Verhalten fest und waren
beim ersten Lauf gruen; eine Gegenprobe (Zweig "--" der Vorlage vorueber-
gehend geaendert) machte sie rot.
34. Ein Glied, das vor dem Start storniert wurde (``task_cancelled`` ohne
    ``task_started``), zeigt in der Dauer-Spalte "--".
35. Ein Glied mit Start und Ende zeigt seine Dauer, eins nur mit Start
    "läuft seit".

=== ZYKLUS 9: Zwischenspeicher der Ergebnisse (Aufraeumen, U4, R3) ===
Ueber einen **echten** ``SwarmQErgebnisleser`` auf einer zaehlenden
App-Attrappe -- ``_Zaehlleser`` ersetzte den ganzen Leser und saehe den
Speicher nicht. Fertige Kopf-Glieder stehen im Baum gefaltet und werden erst
auf der Seite der fertigen Glieder gelesen; im Baum selbst stehen die
stornierten und gescheiterten.
36. Die Detailseite eines Chords liest beim zweiten Abruf das stornierte
    Kopf-Glied nicht mehr, das gescheiterte erneut (DLQ-Requeue).
37. Die Seite der fertigen Glieder liest beim zweiten Abruf nichts mehr aus
    dem Backend.

=== ZYKLUS 10: Das Diagramm (Workflow-Diagramm, U3, R1, R5, R6, R9) ===
38. Covers AE1. Ein Chord mit 50 Downloads (47 fertig, 2 laufend, 1 in
    Wiederholung), Body nicht eingereiht: ein SVG mit dem Sammelknoten
    ``dk-h-fertig`` "47 fertig", drei einzelnen Knoten und dem Body ``dk-b``
    mit "wartet auf 3 Glieder"; von jedem der vier Knoten eine Linie zum Body.
39. Jeder Schritt-Knoten ist ein Link zur Task-Ansicht (auch mit ``#`` in
    der Kennung), der Sammelknoten zur Seite der fertigen Glieder; ein nicht
    eingereihter Schritt hat keinen Link.
40. Covers AE2. Ein gescheiterter Chord: der Body nennt "nie gestartet,
    Workflow gescheitert", die Linien zu ihm sind gestrichelt.
41. Das SVG traegt ``role="group"`` und eine Kurzbeschreibung in
    ``<title>``, kein ``role="img"``; Knoten sind ``<a>`` mit ``aria-label``
    und nennen Zustandszeichen und -wort (R1); ein abgeschnittener Name
    steht voll im ``<title>`` des Knotens.
41a. Die zweite Zeile nennt Wort und Dauer, ohne das Wort zu doppeln:
    "fertig · 1,0 s", aber "läuft seit ..." statt "läuft · läuft seit ...".
42. R6: das SVG hat seine echte Breite und steht in einem Behaelter, der
    waagerecht scrollt, statt zu schrumpfen. (sofort gruen -- die Breite
    kam mit Fall 38, der Behaelter mit dem Stylesheet; per Mutation belegt:
    ohne ``overflow-x`` in ``.dk-rahmen`` oder mit ``width="100%"`` rot.)
43. Covers AE5. Zweimal gerendert ergibt dieselben Kennungen in derselben
    Reihenfolge, auch wenn ein sichtbares Glied dazwischen den Zustand
    wechselt. (sofort gruen: die Kennungen kommen aus U1; der Test haelt
    fest, dass die Vorlage keine eigenen, etwa laufenden Nummern, vergibt.)
43a. Startpunkt und Knotenpunkt stehen als Kreise im Diagramm, mit ihren
    Kennungen ``dk-start`` und ``dk-<pfad>-ein``; der Knotenpunkt traegt
    die Zustandsart seiner Linien.

=== ZYKLUS 11: Umschalter und gemerkte Wahl (Workflow-Diagramm, U4, R7, R8) ===
44. Ohne Angabe und ohne Cookie zeigt die Seite das Diagramm, nicht die Liste.
45. Covers AE4. ``?ansicht=liste`` zeigt die Liste und setzt das Cookie
    (Pfad ``/workflows``, ``SameSite=Strict``, ``Secure``); ein folgender
    Aufruf eines anderen Workflows ohne Angabe zeigt die Liste.
46. ``?ansicht=diagramm`` schlaegt ein Cookie "liste" und setzt es um.
    (sofort gruen: faellt aus Fall 45 ohne neuen Code.)
47. Ein unbekannter Wert zeigt das Diagramm und setzt kein Cookie.
48. In der Ansicht "diagramm" wird der Ergebnisleser nicht aufgerufen, in
    der Liste wie bisher.
49. Storno-Knopf und Fehlerkopf stehen in beiden Ansichten; der Sprung aus
    dem Fehlerkopf zeigt auf den Knoten der gezeigten Ansicht.
50. Covers AE5. Der Umschalter-Link der aktiven Ansicht traegt
    ``aria-current``, beide Links tragen ``ansicht``.
"""

from __future__ import annotations

import asyncio
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
from swarmq_dashboard.web.ergebnis import Ergebnisbild, SwarmQErgebnisleser
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
                      workflowleser: object | None = None, mit_app: bool = False) -> Any:
    app = create_app(config, hasher=_BILLIG, ergebnisleser=ergebnisleser,  # type: ignore[arg-type]
                     workflowleser=workflowleser or _StillerLeser())  # type: ignore[arg-type]
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="?_csrf_token"?\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return (c, app) if mit_app else c


#: Die Liste statt des Diagramms (U4). Die Faelle bis Zyklus 9 pruefen die
#: Liste; seit dem Diagramm ist sie nicht mehr die Vorgabe.
_LISTE = {"ansicht": "liste"}


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
    assert v.w.vermerke_fehlend(wf)[2]
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
    assert v.w.vermerke_fehlend(wf)[1]
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
        antwort = await c.get(f"/workflows/{wf}", params=_LISTE)
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
        body = _zeile((await c.get(f"/workflows/{wf}", params=_LISTE)).text, "knoten-b")
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
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
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
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
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
    v.w.vermerke_fehlend(wf)        # greift bei einem Endstatus nicht -- gut so
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
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
    assert v.w.vermerke_fehlend(wf)[2]
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
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
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
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
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
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
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
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
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
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
        baum = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
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


# --- Zyklus 6 -----------------------------------------------------------


class _StornoLeser:
    """Workflowleser, der sich beim Stornieren wie die Bibliothek verhaelt.

    ``lesen`` liefert ``bild`` -- die Tests setzen es passend zur Kopie, damit
    der Abgleich beim Start nichts umschreibt. ``stornieren`` setzt den
    Merker beim ersten Mal (``neu_gesetzt``) und findet ihn danach gesetzt
    (``schon_gesetzt``); ohne Zustand gibt es ihn nicht (``nicht_gefunden``).
    """

    def __init__(self, bild: Workflowbild, *, storno_erreichbar: bool = True) -> None:
        self.bild = bild
        self.storno_erreichbar = storno_erreichbar
        #: Verfaellt der Zustand zwischen Lesung und cancel, findet die
        #: Bibliothek ihn nicht mehr.
        self.verfaellt_vor_storno = False
        self.merker = False
        self.aufrufe: list[tuple[str, str]] = []
        #: Gesetzt, sobald nach einem cancel noch einmal gelesen wird.
        self.nachgelesen = asyncio.Event()

    async def lesen(self, workflow_id: str) -> Workflowbild:
        if ("stornieren", workflow_id) in self.aufrufe:
            self.nachgelesen.set()
        self.aufrufe.append(("lesen", workflow_id))
        return self.bild

    async def stornieren(self, workflow_id: str) -> Stornobild:
        self.aufrufe.append(("stornieren", workflow_id))
        if not self.storno_erreichbar:
            return Stornobild(ausgang="nicht_erreichbar", grund="Valkey ist weg")
        if self.bild.zustand == "fehlt" or self.verfaellt_vor_storno:
            return Stornobild(ausgang="nicht_gefunden")
        if self.merker:
            return Stornobild(ausgang="schon_gesetzt")
        self.merker = True
        return Stornobild(ausgang="neu_gesetzt")

    @property
    def stornos(self) -> list[str]:
        return [wf for art, wf in self.aufrufe if art == "stornieren"]

    async def close(self) -> None:
        return None


class _Auditspeicher:
    """Ein Audit-Strom im Arbeitsspeicher, fuer Zusicherungen ohne Valkey."""

    def __init__(self) -> None:
        self.eintraege: list[tuple[str, bytes]] = []

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        self.eintraege.extend((f"{i}-0", w) for i, w in enumerate(events, len(self.eintraege) + 1))

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        return self.eintraege[:count]

    async def trim_by_min_id(self, key: str, min_ms: int) -> int:
        return 0


_DOWNLOADS = _gruppe(_sig_top("download"), _sig_top("download"))


def _laufender_workflow(config: DashboardConfig, wf: str) -> Workflowbild:
    """Eine Gruppe mit zwei Downloads, einer laeuft, einer wartet in der
    Queue. Gibt das Bild zurueck, das eine Lesung jetzt lieferte."""
    v = _Verzeichnis(config)
    step_tasks = {"/g0": f"{wf}#/g0", "/g1": f"{wf}#/g1"}
    v.schritt(wf, "/g0", 1_700_000_000.0, "task_started")
    v.schritt(wf, "/g1", 1_700_000_010.0)
    v.lesung(wf, _DOWNLOADS, step_tasks=step_tasks)
    v.close()
    return Workflowbild(zustand="gelesen", status="running", form="gruppe",
                        topologie=_DOWNLOADS, step_tasks=step_tasks)


async def _mit_audit(config: DashboardConfig, leser: _StornoLeser) -> tuple[Any, Any]:
    from swarmq_dashboard.audit import AuditLog

    c, app = await _angemeldet(config, workflowleser=leser, mit_app=True)
    # Die Einstellung zeigt auf tote Ports; ohne Strom waere das Audit leer,
    # und jede Zusicherung darauf hielte trivial.
    app.state.services.audit = AuditLog(_Auditspeicher())
    return c, app


async def _stornieren(c: Any, wf: str, *, bestaetigt: bool = True) -> Any:
    daten = {"bestaetigt": "stornieren"} if bestaetigt else {}
    return await c.post(f"/workflows/{wf}/cancel", data=daten,
                        headers={"x-csrftoken": str(c.cookies.get("csrftoken"))},
                        follow_redirects=False)


async def _workflow_audit(app: Any) -> list[Any]:
    return [e for e in await app.state.services.audit.read() if e.action == "workflow_cancel"]


def _vermerk(app: Any, wf: str) -> float | None:
    zeile = app.state.services.services_directory_writer.workflow(wf)
    return None if zeile is None else zeile["cancel_requested_at"]


WIRKUNG = "noch nicht aufgegriffene Schritte werden beim Aufgreifen abgebrochen"


async def test_ein_laufender_workflow_bietet_stornieren_mit_bestaetigung(tmp_path: Path) -> None:
    """R15, R16: der Knopf, eine Bestaetigung und vorher der Satz zur Wirkung."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    c = await _angemeldet(config, workflowleser=leser)
    try:
        seite = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
        formular = re.search(rf'<form method="post" action="/workflows/{wf}/cancel">.*?</form>',
                             seite, re.S)
        assert formular, "kein Storno-Formular"
        assert 'name="bestaetigt" value="stornieren"' in formular.group(0)
        assert 'name="_csrf_token"' in formular.group(0)
        assert "Workflow stornieren" in seite
        text = _text(seite)
        assert WIRKUNG in text
        assert "laufende Schritte laufen zu Ende" in text
        assert "danach wird nichts mehr eingereiht" in text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_fertiger_workflow_bietet_keinen_knopf(tmp_path: Path) -> None:
    """Covers AE9. Ein Endstatus aus der Lesung: nichts mehr zu stornieren."""
    config = _config(tmp_path)
    wf = _wf(1)
    v = _Verzeichnis(config)
    step_tasks = {"/g0": f"{wf}#/g0", "/g1": f"{wf}#/g1"}
    v.schritt(wf, "/g0", 1_700_000_000.0, "task_started", "task_completed")
    v.schritt(wf, "/g1", 1_700_000_010.0, "task_started", "task_completed")
    v.lesung(wf, _DOWNLOADS, status="completed", step_tasks=step_tasks)
    v.close()
    leser = _StornoLeser(Workflowbild(zustand="gelesen", status="completed", form="gruppe",
                                      topologie=_DOWNLOADS, step_tasks=step_tasks))
    c = await _angemeldet(config, workflowleser=leser)
    try:
        seite = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
        assert "fertig" in _text(seite)
        assert "Workflow stornieren" not in seite
        assert f"/workflows/{wf}/cancel" not in seite
    finally:
        await c.__aexit__(None, None, None)


async def test_stornieren_setzt_vermerk_und_audit_ok(tmp_path: Path) -> None:
    """Covers F3. Frische Lesung, dann cancel, dann der Vermerk vor dem Audit
    (KTD9); die Ansicht zeigt "Stornierung angefordert" und die Wirkung."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    c, app = await _mit_audit(config, leser)
    try:
        leser.aufrufe.clear()
        antwort = await _stornieren(c, wf)
        assert antwort.status_code == 303
        assert antwort.headers["location"] == f"/workflows/{wf}?hinweis=angefordert"
        assert leser.aufrufe[:2] == [("lesen", wf), ("stornieren", wf)], \
            "vor dem cancel wird nicht frisch gelesen"
        assert _vermerk(app, wf) is not None
        (eintrag,) = await _workflow_audit(app)
        assert (eintrag.account, eintrag.target, eintrag.outcome) == ("ops", wf, "ok")

        seite = await c.get(antwort.headers["location"])
        text = _text(seite.text)
        assert "Stornierung angefordert" in text
        assert WIRKUNG in text
        assert "notice is-fail" not in seite.text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_zweites_stornieren_meldet_die_bestehende_anforderung(tmp_path: Path) -> None:
    """Covers AE8. Jemand war schneller: eine Auskunft, kein Fehler. Der
    Zeitpunkt der ersten Anforderung bleibt."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    c, app = await _mit_audit(config, leser)
    try:
        await _stornieren(c, wf)
        erster = _vermerk(app, wf)
        zweite = await _stornieren(c, wf)
        assert zweite.status_code == 303
        assert zweite.headers["location"] == f"/workflows/{wf}?hinweis=bestehend"
        assert leser.stornos == [wf, wf]
        assert _vermerk(app, wf) == erster
        assert [e.outcome for e in await _workflow_audit(app)] == ["ok", "lief bereits"]

        seite = await c.get(zweite.headers["location"])
        text = _text(seite.text)
        assert "bereits angefordert" in text
        assert "Stornierung angefordert" in text
        assert "notice is-fail" not in seite.text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_inzwischen_fertiger_workflow_ist_nicht_mehr_stornierbar(
        tmp_path: Path) -> None:
    """Covers AE10. Der Knopf stand noch da; die frische Lesung vor dem
    Aufruf ergibt "completed". Kein cancel, kein Audit-Eintrag (R15)."""
    config = _config(tmp_path)
    wf = _wf(1)
    laeuft = _laufender_workflow(config, wf)
    leser = _StornoLeser(laeuft)
    c, app = await _mit_audit(config, leser)
    try:
        assert "Workflow stornieren" in (await c.get(f"/workflows/{wf}", params=_LISTE)).text
        leser.bild = Workflowbild(zustand="gelesen", status="completed", form="gruppe",
                                  topologie=laeuft.topologie, step_tasks=laeuft.step_tasks)
        antwort = await _stornieren(c, wf)
        assert antwort.status_code == 303
        assert antwort.headers["location"] == f"/workflows/{wf}?hinweis=nicht_stornierbar"
        assert leser.stornos == []
        assert await _workflow_audit(app) == []
        assert _vermerk(app, wf) is None

        seite = await c.get(antwort.headers["location"])
        assert "nicht mehr stornierbar" in _text(seite.text)
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_verschwundener_zustand_ist_nicht_mehr_stornierbar(tmp_path: Path) -> None:
    """Der Zustand ist bei der frischen Lesung fort: nichts zu stornieren,
    kein Audit-Eintrag (R15)."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    c, app = await _mit_audit(config, leser)
    try:
        leser.bild = Workflowbild(zustand="fehlt")
        antwort = await _stornieren(c, wf)
        assert antwort.headers["location"] == f"/workflows/{wf}?hinweis=nicht_stornierbar"
        assert leser.stornos == []
        assert await _workflow_audit(app) == []
        assert _vermerk(app, wf) is None
    finally:
        await c.__aexit__(None, None, None)


async def test_verfaellt_der_zustand_vor_dem_cancel_ist_nichts_zu_stornieren(
        tmp_path: Path) -> None:
    """Die Lesung sah ihn noch, der Aufruf findet ihn nicht mehr: wie ein
    Endstatus -- kein Vermerk, kein Audit-Eintrag (R15)."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    leser.verfaellt_vor_storno = True
    c, app = await _mit_audit(config, leser)
    try:
        antwort = await _stornieren(c, wf)
        assert antwort.headers["location"] == f"/workflows/{wf}?hinweis=nicht_stornierbar"
        assert leser.stornos == [wf]
        assert await _workflow_audit(app) == []
        assert _vermerk(app, wf) is None
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_nicht_erreichbares_backend_bei_der_lesung_wird_benannt(
        tmp_path: Path) -> None:
    """Ohne Lesung weiss niemand, ob der Workflow noch laeuft: kein cancel,
    kein Vermerk, aber ein Audit-Eintrag "fehlgeschlagen" -- der Versuch
    fand statt."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    c, app = await _mit_audit(config, leser)
    try:
        leser.bild = Workflowbild(zustand="nicht_erreichbar", grund="Valkey ist weg")
        antwort = await _stornieren(c, wf)
        assert antwort.headers["location"] == f"/workflows/{wf}?hinweis=backend"
        assert leser.stornos == []
        assert _vermerk(app, wf) is None
        (eintrag,) = await _workflow_audit(app)
        assert eintrag.outcome == "fehlgeschlagen"
        assert eintrag.detail == "Valkey ist weg"

        seite = await c.get(antwort.headers["location"])
        assert "notice is-fail" in seite.text
        assert "nicht angefordert" in _text(seite.text)
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_ausfall_beim_cancel_setzt_keinen_vermerk(tmp_path: Path) -> None:
    """Die Lesung gelang, der Aufruf nicht: "fehlgeschlagen", kein Vermerk --
    sonst zeigte die Ansicht eine Anforderung, die nie ankam."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf), storno_erreichbar=False)
    c, app = await _mit_audit(config, leser)
    try:
        antwort = await _stornieren(c, wf)
        assert antwort.headers["location"] == f"/workflows/{wf}?hinweis=backend"
        assert leser.stornos == [wf]
        assert _vermerk(app, wf) is None
        (eintrag,) = await _workflow_audit(app)
        assert (eintrag.outcome, eintrag.detail) == ("fehlgeschlagen", "Valkey ist weg")
    finally:
        await c.__aexit__(None, None, None)


async def test_ohne_bestaetigung_wird_nichts_storniert(tmp_path: Path) -> None:
    """Ein Knopf allein ist ein Fehlklick: ohne Bestaetigungsfeld weder
    Lesung noch cancel, kein Vermerk, kein Audit-Eintrag."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    c, app = await _mit_audit(config, leser)
    try:
        antwort = await _stornieren(c, wf, bestaetigt=False)
        assert antwort.status_code == 303
        assert antwort.headers["location"] == f"/workflows/{wf}?hinweis=unbestaetigt"
        assert leser.stornos == []
        assert _vermerk(app, wf) is None
        assert await _workflow_audit(app) == []
        seite = await c.get(antwort.headers["location"])
        assert "Bestätigung fehlte" in _text(seite.text)
    finally:
        await c.__aexit__(None, None, None)


async def test_ohne_csrf_merkmal_wird_abgewiesen(tmp_path: Path) -> None:
    """Der globale CSRF-Schutz gilt auch hier: ohne Merkmal kein cancel."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    c, app = await _mit_audit(config, leser)
    try:
        antwort = await c.post(f"/workflows/{wf}/cancel", data={"bestaetigt": "stornieren"},
                               follow_redirects=False)
        assert antwort.status_code == 403
        assert leser.stornos == []
        assert _vermerk(app, wf) is None
        assert await _workflow_audit(app) == []
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_unbekannter_workflow_wird_nicht_storniert(tmp_path: Path) -> None:
    """Nur was die Ansicht zeigt, laesst sich stornieren: ein unbekannter
    oder unbestaetigter Workflow ergibt eine 404, ohne Aufruf."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    c, app = await _mit_audit(config, leser)
    try:
        antwort = await _stornieren(c, _wf(7))
        assert antwort.status_code == 404
        assert leser.stornos == []
        assert await _workflow_audit(app) == []
    finally:
        await c.__aexit__(None, None, None)


async def test_nach_dem_vermerk_wird_der_workflow_nachgelesen(tmp_path: Path) -> None:
    """Die Ansicht soll nicht bis zum naechsten Takt auf dem alten Stand
    stehen: nach dem cancel stoesst die Route eine Lesung an (KTD3)."""
    config = _config(tmp_path)
    wf = _wf(1)
    leser = _StornoLeser(_laufender_workflow(config, wf))
    c, _app = await _mit_audit(config, leser)
    try:
        await _stornieren(c, wf)
        await asyncio.wait_for(leser.nachgelesen.wait(), timeout=2.0)
    finally:
        await c.__aexit__(None, None, None)


async def test_nach_dem_abbruch_aller_wartenden_ist_nichts_mehr_aktiv(tmp_path: Path) -> None:
    """Covers F3. Alle Downloads warteten noch in der Queue; nach dem
    Stornieren brechen die Worker sie beim Aufgreifen ab. Die Bibliothek
    schreibt dann nie "cancelled" -- die Ansicht zeigt "Stornierung
    angefordert, nichts mehr aktiv", keinen Endstatus und keinen Knopf (R16)."""
    config = _config(tmp_path)
    wf = _wf(1)
    v = _Verzeichnis(config)
    step_tasks = {"/g0": f"{wf}#/g0", "/g1": f"{wf}#/g1"}
    v.schritt(wf, "/g0", 1_700_000_000.0)
    v.schritt(wf, "/g1", 1_700_000_010.0)
    v.lesung(wf, _DOWNLOADS, step_tasks=step_tasks)
    v.close()
    leser = _StornoLeser(Workflowbild(zustand="gelesen", status="running", form="gruppe",
                                      topologie=_DOWNLOADS, step_tasks=step_tasks))
    c, app = await _mit_audit(config, leser)
    try:
        await _stornieren(c, wf)
        worker = _Verzeichnis(config)
        worker._n = 100
        for pfad in ("/g0", "/g1"):
            worker._apply(Event(type="task_cancelled", ts=1_700_000_100.0, source="phx-w1:0",
                                origin=Origin.PICKUP,
                                payload={"task_id": f"{wf}#{pfad}", "task_name": "download",
                                         "queue": "wf"}))
        worker.close()

        seite = await c.get(f"/workflows/{wf}", params=_LISTE)
        text = _text(seite.text)
        assert "Stornierung angefordert, nichts mehr aktiv" in text
        # Die Glieder tragen "storniert" zu Recht; der Workflow nicht (R6).
        status = re.search(r"<dt>Status</dt><dd>(.*?)</dd>", seite.text, re.S)
        assert status
        assert _text(status.group(1)) == "Stornierung angefordert, nichts mehr aktiv"
        assert f"/workflows/{wf}/cancel" not in seite.text
        assert [e.outcome for e in await _workflow_audit(app)] == ["ok"]
    finally:
        await c.__aexit__(None, None, None)


def _dauerzelle(html: str, kennung: str) -> str:
    """Der Text der letzten Zelle (Dauer) der Zeile ``kennung``."""
    zellen = re.findall(r"<td[^>]*>(.*?)</td>", _zeile(html, kennung), re.S)
    return _text(zellen[-1])


# --- Zyklus 8 -----------------------------------------------------------


async def test_ein_vor_dem_start_storniertes_glied_hat_keine_dauer(tmp_path: Path) -> None:
    """R6. Ein Glied endete, ohne je zu starten: es gibt keine Dauer, und
    die Spalte behauptet auch keine Wartezeit."""
    config = _config(tmp_path)
    wf = _wf(1)
    v = _Verzeichnis(config)
    step_tasks = {"/g0": f"{wf}#/g0", "/g1": f"{wf}#/g1"}
    v.schritt(wf, "/g0", 1_700_000_000.0, "task_cancelled")
    v.schritt(wf, "/g1", 1_700_000_010.0, "task_started")
    v.lesung(wf, _DOWNLOADS, step_tasks=step_tasks)
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
        assert _dauerzelle(html, "knoten-g0") == "--"
    finally:
        await c.__aexit__(None, None, None)


async def test_die_dauer_steht_nach_ende_und_waehrend_des_laufs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(wf, "/1", 1_700_000_010.0, "task_started", name="b")
    v.lesung(wf, _kette(_sig_top("a"), _sig_top("b")),
             step_tasks={"/0": f"{wf}#/0", "/1": f"{wf}#/1"})
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}", params=_LISTE)).text
        assert _dauerzelle(html, "knoten-0") == "1,0 s"
        assert _dauerzelle(html, "knoten-1").startswith("läuft seit ")
    finally:
        await c.__aexit__(None, None, None)


async def test_kaputte_topologie_ist_ein_benannter_zustand(tmp_path: Path) -> None:
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    kaputt, heil = _wf(1), _wf(2)
    v.schritt(kaputt, "/0", 1_700_000_000.0, "task_started", name="a")
    v.lesung(kaputt, {"kind": "chain"}, step_tasks={"/0": f"{kaputt}#/0"})
    v.schritt(heil, "", 1_700_000_010.0, "task_started", name="einzeln")
    v.lesung(heil, _sig_top("einzeln"))
    v.close()
    c = await _angemeldet(config)
    try:
        liste = await c.get("/workflows")
        assert liste.status_code == 200
        assert "Topologie nicht lesbar" in _zeile(liste.text, f"workflow-{kaputt}")
        assert "Topologie nicht lesbar" not in _zeile(liste.text, f"workflow-{heil}")

        detail = await c.get(f"/workflows/{kaputt}", params=_LISTE)
        assert detail.status_code == 200
        oben = _text(detail.text[:detail.text.index('id="workflow-baum"')])
        assert "Topologie nicht lesbar" in oben
        assert "a" in _text(_zeile(detail.text, "knoten-0"))

        glieder = await c.get(f"/workflows/{kaputt}/glieder", params={"gruppe": ""})
        assert glieder.status_code == 200
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 9 -----------------------------------------------------------


class _ZaehlApp:
    """Die App der Bibliothek, soweit der Leser sie braucht: ``get_result``,
    mit einer Antwort je Task-Kennung und jedem Aufruf festgehalten."""

    def __init__(self, antworten: Mapping[str, object]) -> None:
        self._antworten = dict(antworten)
        self.rufe: list[str] = []

    async def get_result(self, task_id: str, timeout: float = 10) -> Any:
        self.rufe.append(task_id)
        antwort = self._antworten.get(task_id, {"ok": True})
        if isinstance(antwort, BaseException):
            raise antwort
        return antwort


async def test_die_detailseite_liest_ein_storniertes_kopfglied_nur_einmal(
        tmp_path: Path) -> None:
    """R3, KTD3: jede Live-Meldung rendert den Baum neu. Das stornierte
    Kopf-Glied aendert sich nicht mehr, das gescheiterte kann ein Requeue
    aus der Dead-Letter-Queue noch ueberschreiben."""
    from swarmq.exceptions import TaskCancelledError, TaskError

    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    top = _chord(_gruppe(*[_sig_top("download")] * 3), _sig_top("verarbeiten"))
    storniert = v.schritt(wf, "/h/g0", 1_700_000_000.0, "task_cancelled")
    gescheitert = v.schritt(wf, "/h/g1", 1_700_000_010.0, "task_started", "task_failed",
                            fehler="kaputt")
    v.schritt(wf, "/h/g2", 1_700_000_020.0, "task_started")
    v.lesung(wf, top)
    v.close()
    app = _ZaehlApp({storniert: TaskCancelledError(storniert),
                     gescheitert: TaskError("RuntimeError: kaputt")})
    leser = SwarmQErgebnisleser(config.broker_url, config.backend_url, app=app)
    c = await _angemeldet(config, ergebnisleser=leser)
    try:
        erste = await c.get(f"/workflows/{wf}", params=_LISTE)
        assert erste.status_code == 200
        assert sorted(app.rufe) == sorted([storniert, gescheitert])

        zweite = await c.get(f"/workflows/{wf}", params=_LISTE)
        assert zweite.status_code == 200
        assert sorted(app.rufe) == sorted([storniert, gescheitert, gescheitert])
    finally:
        await c.__aexit__(None, None, None)


async def test_die_fertigen_glieder_werden_beim_zweiten_abruf_nicht_gelesen(
        tmp_path: Path) -> None:
    """R3: die Seite der fertigen Glieder liest je Glied einmal; ein zweiter
    Abruf -- oder das Blaettern zurueck -- kostet keine Backend-Lesung."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    top = _chord(_gruppe(*[_sig_top("download")] * 4), _sig_top("verarbeiten"))
    fertige = [v.schritt(wf, f"/h/g{i}", 1_700_000_000.0 + i, "task_started", "task_completed")
               for i in range(3)]
    v.schritt(wf, "/h/g3", 1_700_000_010.0, "task_started")
    v.lesung(wf, top)
    v.close()
    app = _ZaehlApp({})
    leser = SwarmQErgebnisleser(config.broker_url, config.backend_url, app=app)
    c = await _angemeldet(config, ergebnisleser=leser)
    try:
        for _ in range(2):
            seite = await c.get(f"/workflows/{wf}/glieder", params={"gruppe": "/h"})
            assert seite.status_code == 200
            assert len(re.findall(r'<tr id="knoten', seite.text)) == 3
        assert sorted(app.rufe) == sorted(fertige)
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 10: Das Diagramm (U3) ------------------------------------------


def _svg(html: str) -> str:
    """Das Diagramm der Seite."""
    treffer = re.search(r'<svg id="workflow-diagramm".*?</svg>', html, re.S)
    assert treffer, "kein Diagramm auf der Seite"
    return treffer.group(0)


def _element(svg: str, kennung: str) -> str:
    """Das Element mit ``id="<kennung>"`` samt Inhalt -- ein Knoten ist ein
    ``<a>`` oder ``<g>``, eine Linie ein ``<path>``."""
    treffer = re.search(
        rf'<(a|g|path|circle)\b[^>]*\bid="{re.escape(kennung)}"[^>]*?(?:/>|>.*?</\1>)',
        svg, re.S)
    assert treffer, f"kein Element {kennung!r} im Diagramm"
    return treffer.group(0)


def _chord_mit_fuenfzig(v: _Verzeichnis, wf: str) -> None:
    """AE1: 47 fertig, 2 laufend, 1 in Wiederholung, Body nicht eingereiht."""
    for i in range(50):
        signale: tuple[str, ...] = ("task_started", "task_completed")
        if i in (12, 31):
            signale = ("task_started",)
        elif i == 40:
            signale = ("task_started", "task_failed", "task_retrying")
        v.schritt(wf, f"/h/g{i}", 1_700_000_000.0 + i, *signale, name=f"bericht-{i}")
    v.lesung(wf, _chord(_gruppe(*[_sig_top("download")] * 50), _sig_top("zusammenfassen")))


async def test_das_diagramm_faltet_die_fertigen_und_fuehrt_in_den_body(
        tmp_path: Path) -> None:
    """Covers AE1. Links der Sammelknoten und drei einzelne Knoten, rechts
    der Body mit der Zahl offener Glieder -- und je eine Linie zum Body."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    _chord_mit_fuenfzig(v, wf)
    v.close()
    c = await _angemeldet(config)
    try:
        svg = _svg((await c.get(f"/workflows/{wf}")).text)
        assert "47 fertig" in _text(_element(svg, "dk-h-fertig"))
        for glied in ("dk-h-g12", "dk-h-g31", "dk-h-g40"):
            _element(svg, glied)
        assert "wartet auf 3 Glieder" in _text(_element(svg, "dk-b"))
        zum_body = re.findall(r'<path[^>]*\bid="(dk-kante-[^"]*-b)"', svg)
        assert sorted(zum_body) == sorted(
            f"dk-kante-{q}-b" for q in ("h-fertig", "h-g12", "h-g31", "h-g40")), zum_body
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_knoten_fuehrt_zur_task_ansicht_der_sammelknoten_zu_den_gliedern(
        tmp_path: Path) -> None:
    """R5: ein Klick vom Knoten zur Task-Ansicht -- die Kennung traegt ``#``
    und muss kodiert sein, sonst ist sie ein Sprungziel. Der Body ist nicht
    eingereiht und hat keine Task-Ansicht, also keinen Link."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    _chord_mit_fuenfzig(v, wf)
    v.close()
    c = await _angemeldet(config)
    try:
        svg = _svg((await c.get(f"/workflows/{wf}")).text)
        glied = _element(svg, "dk-h-g12")
        assert glied.startswith("<a ") and f'href="/tasks/{wf}%23/h/g12"' in glied, glied
        sammel = _element(svg, "dk-h-fertig")
        assert sammel.startswith("<a ")
        assert f'href="/workflows/{wf}/glieder?gruppe=/h"' in sammel, sammel
        body = _element(svg, "dk-b")
        assert body.startswith("<g ") and "href=" not in body, body
    finally:
        await c.__aexit__(None, None, None)


async def test_linien_zu_einem_nie_gestarteten_body_sind_gestrichelt(tmp_path: Path) -> None:
    """Covers AE2. Der Chord ist gescheitert, der Body wird nie starten: er
    sagt es selbst, und die Linien zu ihm sind gestrichelt (R3)."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/h/g0", 1_700_000_000.0, "task_started", "task_completed")
    v.schritt(wf, "/h/g1", 1_700_000_010.0, "task_started", "task_failed", fehler="kaputt")
    v.lesung(wf, _chord(_gruppe(*[_sig_top("download")] * 2), _sig_top("zusammenfassen")),
             status="failed", fehler={"type": "ValueError", "message": "kaputt"})
    v.close()
    c = await _angemeldet(config)
    try:
        svg = _svg((await c.get(f"/workflows/{wf}")).text)
        assert "nie gestartet, Workflow gescheitert" in _text(_element(svg, "dk-b"))
        linien = re.findall(r'<path[^>]*\bid="dk-kante-[^"]*-b"[^>]*>', svg)
        assert len(linien) == 2, linien
        for linie in linien:
            assert "is-gestrichelt" in linie, linie
        assert "is-gestrichelt" not in _element(svg, "dk-kante-start-h-g1")
    finally:
        await c.__aexit__(None, None, None)


async def test_das_diagramm_ist_fuer_screenreader_eine_gruppe_aus_links(
        tmp_path: Path) -> None:
    """KTD3: ``role="img"`` verbaerge die Links; ``group`` mit Titel laesst
    sie erreichbar. Jeder Knoten nennt Zeichen und Wort (R1), und ein
    gekuerzter Name steht voll im Titel des Knotens."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf, lang = _wf(1), _wf(2)
    _chord_mit_fuenfzig(v, wf)
    name = "berichte.erzeugen_und_versenden_an_alle_kunden"
    v.schritt(lang, "", 1_700_000_000.0, "task_started", name=name)
    v.lesung(lang, _sig_top(name))
    v.close()
    c = await _angemeldet(config)
    try:
        svg = _svg((await c.get(f"/workflows/{wf}")).text)
        kopf = svg[:svg.index(">") + 1]
        assert 'role="group"' in kopf and 'role="img"' not in svg, kopf
        assert re.match(r'<svg[^>]*>\s*<title[^>]*>[^<]*\S[^<]*</title>', svg), svg[:300]
        glied = _element(svg, "dk-h-g40")
        assert re.search(r'aria-label="download, Wiederholung', glied), glied
        assert "↻" in glied and "Wiederholung" in _text(glied)

        knoten = _element(_svg((await c.get(f"/workflows/{lang}")).text), "dk")
        assert f"<title>{name}</title>" in knoten, knoten
        assert "…" in _text(knoten.replace(f"<title>{name}</title>", ""))
    finally:
        await c.__aexit__(None, None, None)


async def test_die_zweite_zeile_doppelt_das_wort_nicht(tmp_path: Path) -> None:
    """R1: Wort und Dauer wie in der Liste -- "läuft seit" sagt das Wort
    schon; "läuft · läuft seit" liest sich wie ein Fehler der Anzeige."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(wf, "/1", 1_700_000_010.0, "task_started", name="b")
    v.lesung(wf, _kette(_sig_top("a"), _sig_top("b")))
    v.close()
    c = await _angemeldet(config)
    try:
        svg = _svg((await c.get(f"/workflows/{wf}")).text)
        assert "fertig · 1,0 s" in _text(_element(svg, "dk-0"))
        laufend = _text(_element(svg, "dk-1"))
        assert "läuft seit " in laufend and "läuft · " not in laufend, laufend
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_breites_diagramm_scrollt_statt_zu_schrumpfen(tmp_path: Path) -> None:
    """R6: eine lange Kette ist breiter als jede Karte. Mit ``width="100%"``
    schrumpfte das SVG samt Schrift; es behaelt seine Breite, und sein
    Behaelter schiebt waagerecht -- wie ``.card-table`` bei breiten Tabellen."""
    from swarmq_dashboard.web.templates import STATIC_DIR

    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/0", 1_700_000_000.0, "task_started", name="s0")
    v.lesung(wf, _kette(*[_sig_top(f"s{i}") for i in range(8)]))
    v.close()
    c = await _angemeldet(config)
    try:
        html = (await c.get(f"/workflows/{wf}")).text
        rahmen = re.search(r'<div class="dk-rahmen">\s*<svg id="workflow-diagramm"[^>]*>', html)
        assert rahmen, "das Diagramm steht nicht in seinem Behaelter"
        breite = re.search(r'\bwidth="([0-9.]+)"', rahmen.group(0))
        assert breite and float(breite.group(1)) > 2000, rahmen.group(0)
        css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
        regel = re.search(r"\n  \.dk-rahmen \{([^}]*)\}", css)
        assert regel and re.search(r"overflow-x:\s*auto", regel.group(1)), regel
    finally:
        await c.__aexit__(None, None, None)


def _kennungen(svg: str) -> list[str]:
    return re.findall(r'\bid="(dk-[^"]*)"', svg)


async def test_die_kennungen_bleiben_ueber_ein_neuladen_gleich(tmp_path: Path) -> None:
    """Covers AE5. ``live.js`` laedt die Seite und Idiomorph gleicht Element
    fuer Element ab -- nur mit festen Kennungen tauscht es den geaenderten
    Knoten, statt das ganze Bild neu aufzubauen (R9)."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    _chord_mit_fuenfzig(v, wf)
    c = await _angemeldet(config)
    try:
        erste = _kennungen(_svg((await c.get(f"/workflows/{wf}")).text))
        assert len(erste) == len(set(erste)) and "dk-b" in erste, erste
        assert erste == _kennungen(_svg((await c.get(f"/workflows/{wf}")).text))

        v.schritt(wf, "/h/g31", 1_700_000_031.0, "task_started", "task_failed",
                  name="bericht-31")
        danach = (await c.get(f"/workflows/{wf}")).text
        assert "is-fail" in _element(_svg(danach), "dk-h-g31")
        assert _kennungen(_svg(danach)) == erste
    finally:
        v.close()
        await c.__aexit__(None, None, None)


async def test_start_und_knotenpunkt_sind_gezeichnet(tmp_path: Path) -> None:
    """KTD2: zwei Gruppen hintereinander treffen sich in einem Knotenpunkt;
    ohne ihn liefen die Linien ins Leere -- sie enden an seinem Rand."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    v.schritt(wf, "/0/g0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(wf, "/0/g1", 1_700_000_001.0, "task_started", name="b")
    v.lesung(wf, _kette(_gruppe(_sig_top("a"), _sig_top("b")),
                        _gruppe(_sig_top("c"), _sig_top("d"))))
    v.close()
    c = await _angemeldet(config)
    try:
        svg = _svg((await c.get(f"/workflows/{wf}")).text)
        assert _element(svg, "dk-start").startswith("<circle ")
        punkt = _element(svg, "dk-1-ein")
        assert punkt.startswith("<circle "), punkt
        art = re.search(r"\b(is-(?:ok|run|wait|fail|idle))\b",
                        _element(svg, "dk-kante-0-fertig-1-ein"))
        assert art and art.group(1) in punkt, punkt
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 11: Umschalter und gemerkte Wahl (U4) ----------------------------


async def test_ohne_wahl_zeigt_die_seite_das_diagramm(tmp_path: Path) -> None:
    """R8: wer nie gewaehlt hat, sieht das Diagramm -- und nur das; beide
    Ansichten untereinander waeren die verworfene Variante aus KD2."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    _chord_mit_fuenfzig(v, wf)
    v.close()
    c = await _angemeldet(config)
    try:
        antwort = await c.get(f"/workflows/{wf}")
        _svg(antwort.text)
        assert 'id="workflow-baum"' not in antwort.text
        assert "sqd_workflow_ansicht" not in antwort.headers.get("set-cookie", "")
    finally:
        await c.__aexit__(None, None, None)


def _zwei_workflows(config: DashboardConfig) -> tuple[str, str]:
    v = _Verzeichnis(config)
    erster, zweiter = _wf(1), _wf(2)
    for wf in (erster, zweiter):
        v.schritt(wf, "/0", 1_700_000_000.0, "task_started", name="a")
        v.lesung(wf, _kette(_sig_top("a"), _sig_top("b")))
    v.close()
    return erster, zweiter


def _ansicht_cookie(antwort: Any) -> str:
    """Die Set-Cookie-Zeile der Ansicht, oder ``""``."""
    for zeile in antwort.headers.get_list("set-cookie"):
        if zeile.startswith("sqd_workflow_ansicht="):
            return str(zeile)
    return ""


async def test_die_gewaehlte_liste_bleibt_fuer_den_naechsten_workflow(
        tmp_path: Path) -> None:
    """Covers AE4. Die Wahl steht in der Adresse und im Cookie (KTD5): wer
    "Liste" gewaehlt hat, bekommt sie auch beim naechsten Workflow."""
    config = _config(tmp_path)
    erster, zweiter = _zwei_workflows(config)
    c = await _angemeldet(config)
    try:
        gewaehlt = await c.get(f"/workflows/{erster}", params={"ansicht": "liste"})
        assert 'id="workflow-baum"' in gewaehlt.text
        cookie = _ansicht_cookie(gewaehlt)
        assert cookie.startswith("sqd_workflow_ansicht=liste;"), cookie
        teile = {t.strip().lower() for t in cookie.split(";")}
        assert {"path=/workflows", "samesite=strict", "secure"} <= teile, cookie

        spaeter = await c.get(f"/workflows/{zweiter}")
        assert 'id="workflow-baum"' in spaeter.text
        assert 'id="workflow-diagramm"' not in spaeter.text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_unbekannter_wert_zeigt_das_diagramm_und_merkt_nichts(
        tmp_path: Path) -> None:
    """KTD5: ein verstuemmelter Verweis aus einem Ticket ist keine Wahl --
    weder fuer diese Seite noch fuer die naechsten; auch ein untergeschobenes
    Cookie waehlt nichts Unbekanntes."""
    config = _config(tmp_path)
    erster, zweiter = _zwei_workflows(config)
    c = await _angemeldet(config)
    try:
        antwort = await c.get(f"/workflows/{erster}", params={"ansicht": "tabelle"})
        _svg(antwort.text)
        assert 'id="workflow-baum"' not in antwort.text
        assert _ansicht_cookie(antwort) == ""

        c.cookies.set("sqd_workflow_ansicht", "<b>", domain="testserver.local",
                      path="/workflows")
        _svg((await c.get(f"/workflows/{zweiter}")).text)

        # Auch neben einer gemerkten Liste gilt ein unbekannter Wert als
        # Diagramm (KTD5) -- und die gemerkte Wahl bleibt, wie sie war.
        await c.get(f"/workflows/{erster}", params={"ansicht": "liste"})
        neben = await c.get(f"/workflows/{zweiter}", params={"ansicht": "tabelle"})
        _svg(neben.text)
        assert _ansicht_cookie(neben) == ""
        assert 'id="workflow-baum"' in (await c.get(f"/workflows/{zweiter}")).text
    finally:
        await c.__aexit__(None, None, None)


async def test_die_adresse_schlaegt_das_cookie_und_setzt_es_um(tmp_path: Path) -> None:
    """KTD5: erst die Adresse, dann das Cookie. Wer von der Liste zurueck
    zum Diagramm schaltet, bekommt es auch beim naechsten Workflow."""
    config = _config(tmp_path)
    erster, zweiter = _zwei_workflows(config)
    c = await _angemeldet(config)
    try:
        await c.get(f"/workflows/{erster}", params=_LISTE)
        zurueck = await c.get(f"/workflows/{erster}", params={"ansicht": "diagramm"})
        _svg(zurueck.text)
        assert _ansicht_cookie(zurueck).startswith("sqd_workflow_ansicht=diagramm;")
        _svg((await c.get(f"/workflows/{zweiter}")).text)
    finally:
        await c.__aexit__(None, None, None)


async def test_das_diagramm_liest_keine_ergebnisse(tmp_path: Path) -> None:
    """U4: das Diagramm zeigt die Eingabe des Body nicht -- ein Backend-Zugriff
    je Live-Meldung waere dort eine Last fuer nichts. Die Liste liest wie
    bisher."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    wf = _wf(1)
    gescheitert = v.schritt(wf, "/h/g0", 1_700_000_000.0, "task_started", "task_failed",
                            fehler="kaputt")
    v.schritt(wf, "/h/g1", 1_700_000_010.0, "task_started")
    v.lesung(wf, _chord(_gruppe(*[_sig_top("download")] * 2), _sig_top("zusammenfassen")))
    v.close()
    leser = _Zaehlleser()
    c = await _angemeldet(config, ergebnisleser=leser)
    try:
        _svg((await c.get(f"/workflows/{wf}")).text)
        assert leser.aufrufe == []
        await c.get(f"/workflows/{wf}", params=_LISTE)
        assert leser.aufrufe == [gescheitert]
    finally:
        await c.__aexit__(None, None, None)


async def test_kopfangaben_stehen_in_beiden_ansichten(tmp_path: Path) -> None:
    """R7: Status, Fehlerkopf und Stornieren stehen in beiden Ansichten. Der
    Sprung aus dem Fehlerkopf fuehrt zum Knoten der **gezeigten** Ansicht --
    ``#knoten-1`` zeigte im Diagramm ins Leere."""
    config = _config(tmp_path)
    v = _Verzeichnis(config)
    laeuft, kaputt = _wf(1), _wf(2)
    v.schritt(laeuft, "/0", 1_700_000_000.0, "task_started", name="a")
    v.lesung(laeuft, _kette(_sig_top("a"), _sig_top("b")))
    v.schritt(kaputt, "/0", 1_700_000_000.0, "task_started", "task_completed", name="a")
    v.schritt(kaputt, "/1", 1_700_000_010.0, "task_started", "task_failed", name="b",
              fehler="ValueError: kaputt")
    v.lesung(kaputt, _kette(_sig_top("a"), _sig_top("b"), _sig_top("c")), status="failed",
             fehler={"type": "ValueError", "message": "kaputt im Zustand"})
    v.close()
    c = await _angemeldet(config)
    try:
        for ansicht, sprung in (("diagramm", "dk-1"), ("liste", "knoten-1")):
            seite = (await c.get(f"/workflows/{laeuft}", params={"ansicht": ansicht})).text
            assert "Workflow stornieren" in seite, ansicht
            seite = (await c.get(f"/workflows/{kaputt}", params={"ansicht": ansicht})).text
            assert "ValueError: kaputt im Zustand" in seite, ansicht
            assert f'href="#{sprung}"' in seite and f'id="{sprung}"' in seite, ansicht
    finally:
        await c.__aexit__(None, None, None)


def _umschalter(html: str) -> dict[str, str]:
    """Die Links des Umschalters: Ziel -> ganzes Tag."""
    nav = re.search(r'<nav class="ansicht"[^>]*>(.*?)</nav>', html, re.S)
    assert nav, "kein Umschalter auf der Seite"
    return {m.group(1).replace("&amp;", "&"): m.group(0)
            for m in re.finditer(r'<a [^>]*href="([^"]*)"[^>]*>', nav.group(1))}


async def test_der_umschalter_markiert_die_aktive_ansicht(tmp_path: Path) -> None:
    """Covers AE5. Zwei Links "Diagramm | Liste" mit ``?ansicht=``; der
    aktive traegt ``aria-current`` -- auch, wenn die Wahl nur im Cookie
    steht und die Adresse keine traegt."""
    config = _config(tmp_path)
    erster, zweiter = _zwei_workflows(config)
    c = await _angemeldet(config)
    try:
        links = _umschalter((await c.get(f"/workflows/{erster}")).text)
        assert set(links) == {"?ansicht=diagramm", "?ansicht=liste"}, links
        assert 'aria-current="page"' in links["?ansicht=diagramm"]
        assert "aria-current" not in links["?ansicht=liste"]

        await c.get(f"/workflows/{erster}", params=_LISTE)
        links = _umschalter((await c.get(f"/workflows/{zweiter}")).text)
        assert 'aria-current="page"' in links["?ansicht=liste"]
        assert "aria-current" not in links["?ansicht=diagramm"]
    finally:
        await c.__aexit__(None, None, None)
