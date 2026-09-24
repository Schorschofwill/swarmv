"""Tasks-Liste und Detail (U13, R8, R10, R11, R31, R4a).

Der Hauptfluss: ein Teammitglied sucht nach einem Fragment aus den
Argumenten, sieht Status und Argumente, oeffnet den Task und liest die
Signalfolge. Drei stehende Hinweise gehoeren zwingend dazu, und alle drei
verhindern dieselbe Sorte Irrtum -- eine Seite, die vollstaendig **aussieht**,
es aber nicht ist:

* **Reichweite** (R8): wie weit das Verzeichnis zurueckreicht. Ohne sie liest
  sich "keine Treffer" wie "hat es nie gegeben", auch wenn der Task
  schlicht aelter ist als die Aufbewahrung.
* **Herkunft** (R31): Scheduler- und verzoegerte Einreihungen kennt das
  Verzeichnis erst ab dem Aufgreifen. Ein geratener Einreihungszeitpunkt
  waere eine Falschaussage, ein leeres Feld ohne Erklaerung ein Raetsel.
* **Luecken** (R4a): eine Zeitspanne, aus der Ereignisse fehlen, muss
  erkennbar sein.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Die Liste ===
1. Covers AE1. Eine Suche nach einem Argument-Fragment zeigt den Treffer mit
   Status und Argumenten.
2. Die vier Filter wirken ueber die Adresszeile.
3. Ein zu kurzer Suchbegriff wird benannt, nicht als "keine Treffer" gezeigt.
4. Ohne Treffer steht ein benannter Leerzustand da (R33).

=== ZYKLUS 2: Die drei Hinweise ===
5. Die Reichweiten-Leiste nennt den aeltesten Eintrag (R8).
6. Covers AE14. Eine Zeile mit Herkunft "aufgreifen" zeigt keinen geratenen
   Einreihungszeitpunkt, und der Hinweis steht auf der Seite (R31).
7. Eine als unvollstaendig markierte Zeile ist erkennbar (R4a).

=== ZYKLUS 3: Das Detail ===
8. Die Detailseite zeigt alle Felder und die Argumente im Klartext.
9. Die Signalfolge steht in Stromreihenfolge (R11).
10. Ein unbekannter Task ergibt eine benannte Antwort, keinen Absturz.

=== ZYKLUS 4: Zugang ===
11. Liste und Detail brauchen eine Sitzung.
12. Waehrend eines Neuaufbaus sagt der Leerzustand genau das.

=== ZYKLUS 5: Stornieren (R12, R12a, R27) ===
13. Covers AE13. Ein wartender Task laesst sich stornieren; die Ansicht sagt
    "angefordert", nicht "storniert".
14. Covers AE3. Ein beendeter Task wird abgewiesen, begruendet, ohne zu
    schreiben.
15. Covers AE3a. Eine zweite Stornierung ist kein Fehler.
16. Ein Cancel steht im Audit mit Konto und Ziel.
17. Ein nicht erreichbares Backend wird benannt, statt eine Anforderung zu
    behaupten.
18. Stornieren ohne Sitzung und ohne Merkmal wird abgewiesen.

=== ZYKLUS 6: Das Ergebnis (U2, F1) ===
19. Covers AE1. Ein fertiger Task zeigt sein Ergebnis im Klartext.
20. Ein laufender Task loest keinen Backend-Zugriff aus -- und traegt
    trotzdem ein Wort.
21. Eine als unvollstaendig markierte Zeile loest den Nachschlag **doch**
    aus: Ging das Abschlusssignal verloren, ist das Backend die letzte
    Quelle (R4a).
22. Covers AE2. Ein fehlendes Ergebnis nennt beide Ursachen, ohne eine zu
    behaupten.
23. Covers AE3, R5. Ein totes Backend nennt den Grund, und die Seite bleibt
    vollstaendig.
24. Covers AE4, F2. Ein gescheiterter Task zeigt beide Fehlertexte, jeden
    beschriftet.
25. Covers AE5, R4. Ein abgeschnittenes Ergebnis ist gekennzeichnet und
    nennt die Laenge der Darstellung.
26. Ein storniertes Ergebnis traegt dieselbe Marke wie sein Status.
27. Covers R6a, KD1. Das Ergebnis landet weder im Verzeichnis noch im Audit.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from litestar.testing import AsyncTestClient

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin
from swarmq_dashboard.web.app import create_app

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


def _verzeichnis_fuellen(config: DashboardConfig) -> None:
    """Drei Tasks, die je einen der Hinweise ausloesen."""
    w = DirectoryWriter(connect(config.directory_file))

    def enq(tid: str, ts: float, **kw: object) -> Event:
        nutzlast: dict[str, object] = {
            "task_id": tid, "task_name": "render_pdf", "queue": "reports",
            "args": [], "kwargs": {"report_id": 812, "mail": "kunde3@example.com"},
        }
        nutzlast.update(kw)
        return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
                     payload=nutzlast)

    def sig(tid: str, typ: str, ts: float, **kw: object) -> Event:
        return Event(type=typ, ts=ts, source="phx-w1:0", origin=Origin.PICKUP,
                     payload={"task_id": tid, "task_name": "render_pdf", **kw})

    w.apply([
        ("1000-0", enq("a41f", 1_700_000_000.0)),
        ("2000-0", sig("a41f", "task_started", 1_700_000_001.0)),
        ("3000-0", sig("a41f", "task_completed", 1_700_000_002.0, duration_ms=1234.0)),
        # Nur aufgegriffen: kein Einreihungszeitpunkt (R31).
        ("4000-0", Event(type="task_pickup", ts=1_700_000_100.0, source="phx-w2:0",
                         origin=Origin.PICKUP,
                         payload={"task_id": "b22c", "task_name": "sync_invoice",
                                  "queue": "billing", "args": [], "kwargs": {}})),
        ("5000-0", enq("c33d", 1_700_000_200.0, task_name="cleanup")),
    ])
    w.mark_incomplete(1_700_000_190.0, 1_700_000_210.0)     # c33d ist unvollstaendig
    # Der Leser gilt als eingeholt. Ohne diese Marke meldet der Dienst einen
    # laufenden Neuaufbau -- richtig, aber es ist nicht das, was diese Tests
    # pruefen wollen, und der Leerzustand naennte dann diese Ursache.
    w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                   "on conflict(key) do nothing")
    w._con.commit()
    w.close()


async def _angemeldet(config: DashboardConfig, *, canceller: object | None = None,
                      ergebnisleser: object | None = None,
                      mit_app: bool = False) -> Any:
    app = create_app(config, hasher=_BILLIG, canceller=canceller,
                     ergebnisleser=ergebnisleser)
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="?_csrf_token"?\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return (c, app) if mit_app else c


# --- Zyklus 1 -----------------------------------------------------------


async def test_suche_nach_argument_fragment(tmp_path: Path) -> None:
    """Covers AE1. Das ist der Hauptfluss: ein Fragment aus den Argumenten,
    und der Task steht da -- mit Status und Argumenten.
    """
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/tasks?q=3@example")
        assert antwort.status_code == 200
        assert "a41f" in antwort.text
        assert "fertig" in antwort.text
        assert "kunde3@example.com" in antwort.text, "die Argumente fehlen"
        assert "b22c" not in antwort.text, "ein Task ohne Treffer steht in der Liste"
    finally:
        await c.__aexit__(None, None, None)


async def test_die_filter_wirken(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        nur_fertig = await c.get("/tasks?status=fertig")
        assert "a41f" in nur_fertig.text and "c33d" not in nur_fertig.text

        nach_name = await c.get("/tasks?task_name=cleanup")
        assert "c33d" in nach_name.text and "a41f" not in nach_name.text

        nach_worker = await c.get("/tasks?worker_id=phx-w2:0")
        assert "b22c" in nach_worker.text and "a41f" not in nach_worker.text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_zu_kurzer_suchbegriff_wird_benannt(tmp_path: Path) -> None:
    """Eine leere Liste ohne Begruendung liest sich wie "nicht gelaufen"."""
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/tasks?q=81")
        assert antwort.status_code == 200
        assert "Zeichen" in antwort.text, "die Untergrenze wird nicht benannt"
        assert "a41f" not in antwort.text
    finally:
        await c.__aexit__(None, None, None)


async def test_ohne_treffer_steht_ein_leerzustand_da(tmp_path: Path) -> None:
    """Covers R33."""
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/tasks?q=gibtesnichtimargument")
        assert "Kein Task passt zu dieser Suche" in antwort.text
        # R33: die **wahrscheinliche Ursache**, nicht nur "keine Treffer".
        assert "reicht bis" in antwort.text, "der Leerzustand nennt keine Ursache"
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 2 -----------------------------------------------------------


async def test_die_reichweite_steht_auf_der_seite(tmp_path: Path) -> None:
    """Covers R8. Ohne sie liest sich "keine Treffer" wie "hat es nie
    gegeben" -- auch wenn der Task schlicht aelter ist als die Aufbewahrung.
    """
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/tasks")
        assert "reicht" in antwort.text.lower() or "Reichweite" in antwort.text
        assert "2023-11-14" in antwort.text or "1700000000" in antwort.text
    finally:
        await c.__aexit__(None, None, None)


async def test_herkunft_aufgreifen_ohne_geratenen_zeitpunkt(tmp_path: Path) -> None:
    """Covers AE14, R31."""
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/tasks?task_name=sync_invoice")
        assert "b22c" in antwort.text

        # Zwei verschiedene Dinge, darum zwei Zusicherungen. In einer ersten
        # Fassung war es eine, und jede Mutation liess das jeweils andere
        # stehen -- die Zusicherung hielt also keines von beidem.
        #
        # 1. Die **Zelle** sagt, dass es keinen Einreihungszeitpunkt gibt --
        #    kein Strich, der wie "unbekannt" aussieht, und schon gar kein
        #    geratener Zeitpunkt.
        zelle = re.search(r"<td[^>]*>\s*(<span[^>]*>)?\s*erst beim Aufgreifen", antwort.text)
        assert zelle, "die Zelle nennt die Herkunft nicht"
        # 2. Der **stehende Hinweis** erklaert, warum. Er steht auf jeder
        #    Listenseite, auch ohne betroffene Zeile: die Abwesenheit einer
        #    solchen Zeile ist selbst keine Aussage.
        #    Geprueft wird der **sichtbare** Absatz, nicht das blosse Vorkommen
        #    der Zeichenkette: sie steht auch im ``title`` der Zelle, und eine
        #    Zusicherung darauf hielt den Hinweis nicht.
        sichtbar = re.search(
            r'<p class="notice">\s*Scheduler-Auslösungen', antwort.text
        )
        assert sichtbar, "der stehende Hinweis aus R31 steht nicht sichtbar auf der Seite"
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_unvollstaendige_zeile_ist_erkennbar(tmp_path: Path) -> None:
    """Covers R4a. Eine Luecke, die man nicht sieht, ist keine Meldung."""
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/tasks?task_name=cleanup")
        assert "c33d" in antwort.text
        assert "unvollst" in antwort.text.lower() or "Luecke" in antwort.text
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 3 -----------------------------------------------------------


async def test_die_detailseite_zeigt_alles(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/tasks/a41f")
        assert antwort.status_code == 200
        for stueck in ("a41f", "render_pdf", "reports", "fertig", "phx-w1:0",
                       "kunde3@example.com"):
            assert stueck in antwort.text, f"{stueck} fehlt auf der Detailseite"
    finally:
        await c.__aexit__(None, None, None)


async def test_die_signalfolge_steht_in_stromreihenfolge(tmp_path: Path) -> None:
    """Covers R11. Die Host-Uhren koennen gegeneinander laufen, der Strom nicht."""
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        text = (await c.get("/tasks/a41f")).text
        stellen = [text.index(t) for t in ("task_enqueue", "task_started", "task_completed")]
        assert stellen == sorted(stellen), f"die Signalfolge ist vertauscht: {stellen}"
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_unbekannter_task_wird_benannt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/tasks/gibtesnicht", follow_redirects=False)
        assert antwort.status_code == 404
        assert "traceback" not in antwort.text.lower()
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 4 -----------------------------------------------------------


async def test_liste_und_detail_brauchen_eine_sitzung(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    async with AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS) as c:
        for pfad in ("/tasks", "/tasks/a41f"):
            antwort = await c.get(pfad, follow_redirects=False)
            assert antwort.status_code in (302, 303, 401), f"{pfad}: {antwort.status_code}"


async def test_waehrend_des_neuaufbaus_sagt_der_leerzustand_das(tmp_path: Path) -> None:
    """R33 und R8 zusammen. "Keine Treffer" waehrend eines Neuaufbaus ist die
    gefaehrlichste Antwort dieser Seite: sie liest sich wie "es ist nichts
    passiert", obwohl die Liste nur noch nicht fertig ist.
    """
    config = _config(tmp_path)          # Verzeichnis leer, kein Lesefortschritt
    c = await _angemeldet(config)
    try:
        antwort = await c.get("/tasks")
        assert "neu aufgebaut" in antwort.text, antwort.text[-400:]
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 5: Stornieren (R12, R12a, R27) ------------------------------


class _SpeicherStrom:
    """Ein Audit-Strom im Arbeitsspeicher, fuer Zusicherungen ohne Valkey."""

    def __init__(self) -> None:
        self.eintraege: list[tuple[str, bytes]] = []

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        self.eintraege.extend((f"{i}-0", w) for i, w in enumerate(events, len(self.eintraege) + 1))

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        return self.eintraege[:count]

    async def trim_by_min_id(self, key: str, min_ms: int) -> int:
        return 0


class FakeCancel:
    """Die Cancel-Funktion der Bibliothek, wie sie sich wirklich verhaelt.

    ``cancel`` meldet **"Merker neu gesetzt"**, nicht "storniert": beim
    zweiten Aufruf kommt ``False``, und das ist kein Fehler, sondern die
    Auskunft "es lief schon eine Anforderung". Eine Attrappe, die immer
    ``True`` sagt, verdeckte genau den Fall aus AE3a.
    """

    def __init__(self, kaputt: bool = False) -> None:
        self.gesetzt: set[str] = set()
        self.aufrufe: list[str] = []
        self.kaputt = kaputt

    async def cancel(self, task_id: str) -> bool:
        if self.kaputt:
            raise RuntimeError("das Backend ist nicht erreichbar")
        self.aufrufe.append(task_id)
        neu = task_id not in self.gesetzt
        self.gesetzt.add(task_id)
        return neu


async def _stornieren(c: AsyncTestClient, task_id: str) -> object:
    seite = await c.get(f"/tasks/{task_id}")
    merkmal = str(c.cookies.get("csrftoken"))
    return await c.post(f"/tasks/{task_id}/cancel", headers={"x-csrftoken": merkmal},
                        follow_redirects=False), seite


async def test_ein_wartender_task_laesst_sich_stornieren(tmp_path: Path) -> None:
    """Covers AE13, R12a. Die Ansicht wechselt auf "Stornierung angefordert" --
    **nicht** auf "storniert": ob der Worker die Anforderung bemerkt, weiss
    erst das Signal.
    """
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    abbruch = FakeCancel()
    c = await _angemeldet(config, canceller=abbruch)
    try:
        antwort, _ = await _stornieren(c, "c33d")
        assert antwort.status_code == 303, antwort.text[:200]
        assert abbruch.aufrufe == ["c33d"]

        seite = await c.get("/tasks/c33d")
        assert "Stornierung angefordert" in seite.text
        assert ">storniert<" not in seite.text, (
            "die Ansicht behauptet einen Vollzug, den nur das Signal belegen kann"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_beendeter_task_wird_abgewiesen(tmp_path: Path) -> None:
    """Covers AE3. Abgewiesen, begruendet -- und **ohne** etwas zu schreiben."""
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    abbruch = FakeCancel()
    c = await _angemeldet(config, canceller=abbruch)
    try:
        antwort, _ = await _stornieren(c, "a41f")       # steht auf "fertig"
        assert antwort.status_code in (303, 409)
        assert abbruch.aufrufe == [], "es wurde ein Cancel geschrieben"

        # Der Weiterleitung folgen, wie ein Browser es tut -- der Hinweis
        # steht dahinter, nicht in der Antwort auf die Aktion selbst.
        seite = await c.get(antwort.headers["location"])
        assert "bereits beendet" in seite.text or "beendet" in seite.text
        assert "Stornierung angefordert" not in seite.text
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_zweite_stornierung_ist_kein_fehler(tmp_path: Path) -> None:
    """Covers AE3a. Ein bereits gesetzter Merker ist eine Auskunft, kein
    Fehler -- sonst meldet das Dashboard einem zweiten Teammitglied eine
    Stoerung, wo nur jemand schneller war.
    """
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    abbruch = FakeCancel()
    c = await _angemeldet(config, canceller=abbruch)
    try:
        erste, _ = await _stornieren(c, "c33d")
        zweite, _ = await _stornieren(c, "c33d")
        assert erste.status_code == zweite.status_code == 303
        assert abbruch.aufrufe == ["c33d", "c33d"]

        seite = await c.get("/tasks/c33d")
        assert "Stornierung angefordert" in seite.text
        assert "fehler" not in seite.text.lower() or "Fehler</dt>" not in seite.text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_cancel_steht_im_audit(tmp_path: Path) -> None:
    """Covers R27. "Wer hat das storniert" ist die Begruendung des Audits
    ueberhaupt.
    """
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c, app = await _angemeldet(config, canceller=FakeCancel(), mit_app=True)
    try:
        # Ein Strom im Speicher: die Einstellung zeigt auf tote Ports, und ohne
        # Strom ist das Audit leer -- jede Zusicherung darauf hielte trivial.
        from swarmq_dashboard.audit import AuditLog

        app.state.services.audit = AuditLog(_SpeicherStrom())
        await _stornieren(c, "c33d")
        eintraege = [e for e in await app.state.services.audit.read()
                     if e.action == "task_cancel"]
        assert eintraege, "der Cancel steht nicht im Audit"
        assert eintraege[0].account == "ops"
        assert eintraege[0].target == "c33d"
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_nicht_erreichbares_backend_wird_benannt(tmp_path: Path) -> None:
    """Der Merker liegt in Valkey. Ist es weg, darf die Ansicht nicht
    behaupten, die Stornierung sei angefordert.
    """
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config, canceller=FakeCancel(kaputt=True))
    try:
        antwort, _ = await _stornieren(c, "c33d")
        assert antwort.status_code in (303, 503)
        seite = await c.get(antwort.headers["location"])
        assert "Stornierung angefordert" not in seite.text, (
            "die Ansicht behauptet eine Anforderung, die nie ankam"
        )
        assert "nicht erreichbar" in seite.text
    finally:
        await c.__aexit__(None, None, None)


async def test_stornieren_ohne_sitzung_und_ohne_merkmal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    async with AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS) as c:
        ohne = await c.post("/tasks/c33d/cancel", follow_redirects=False)
        assert ohne.status_code in (302, 303, 401), ohne.status_code

    c = await _angemeldet(config, canceller=FakeCancel())
    try:
        ohne_merkmal = await c.post("/tasks/c33d/cancel", follow_redirects=False)
        assert ohne_merkmal.status_code == 403
    finally:
        await c.__aexit__(None, None, None)


async def test_die_liste_benennt_eine_geschnittene_luecke(tmp_path: Path) -> None:
    """R4a, U12a. Der Fall aus der Pruefung von Stufe B.

    Gefiltert auf einen Zeitraum, aus dem der Laengendeckel alles
    weggeschnitten hat, bevor der Leser dort war. "Keine Treffer" hiesse dann
    "ist nichts passiert" -- ueber einen Zeitraum, der nie gelesen wurde.
    """
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    basis = 1_700_000_000.0
    w = DirectoryWriter(connect(config.directory_file))
    w.record_gap(basis + 200, basis + 700, quelle="stream_trim")
    w.close()

    c = await _angemeldet(config)
    try:
        antwort = await c.get(f"/tasks?since={basis + 300}&until={basis + 600}")
        assert antwort.status_code == 200
        # Der Hinweisabsatz selbst, nicht die Seite: "2023-11-14" steht
        # ohnehin in jeder Task-Zeile, und die Spanne durfte aus dem Hinweis
        # verschwinden, ohne dass etwas kippte.
        absatz = re.search(
            r'<p class="notice is-wait"><span class="tag-dot"></span>\s*([^<]*)', antwort.text
        )
        assert absatz, "die Luecke wird verschwiegen"
        hinweis = " ".join(absatz.group(1).split())
        assert "verlorengegangen" in hinweis, hinweis
        assert "2023-11-14 22:16:40" in hinweis, (
            f"die Spanne wird nicht genannt: {hinweis!r}"
        )
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 7: Der Zeitraumfilter und die Beschriftungen (R10, AE2) -----


async def test_die_filterleiste_hat_einen_zeitraum(tmp_path: Path) -> None:
    """Covers AE2. R10 verlangt den Zeitraum ausdruecklich.

    Die Route las ``since``/``until`` schon aus der Adresszeile, und
    ``Filter`` kennt sie -- aber die Leiste bot kein Feld dafuer. Damit war
    "ein Teammitglied filtert auf die letzten zehn Tage" ueber die
    Oberflaeche schlicht nicht ausfuehrbar.
    """
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        leiste = (await c.get("/tasks")).text
        leiste = leiste[leiste.find('<form class="filters"'):]
        leiste = leiste[:leiste.find("</form>")]
        assert 'name="seit"' in leiste, f"kein Zeitraumfeld: {leiste}"
        assert "10" in leiste, "die zehn Tage aus AE2 sind nicht waehlbar"
    finally:
        await c.__aexit__(None, None, None)


async def test_der_zeitraum_wirkt(tmp_path: Path) -> None:
    """Ein Feld, das nichts einschraenkt, waere schlimmer als keins."""
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        # Alles im Verzeichnis liegt 2023 -- "letzte 10 Tage" trifft nichts.
        eng = await c.get("/tasks?seit=10")
        assert "a41f" not in eng.text, "der Zeitraum schraenkt nicht ein"
        weit = await c.get("/tasks")
        assert "a41f" in weit.text
    finally:
        await c.__aexit__(None, None, None)


async def test_jedes_filterfeld_traegt_eine_beschriftung(tmp_path: Path) -> None:
    """Ein Platzhalter ist kein Name: er verschwindet beim Tippen, und ein
    Vorleseprogramm nennt ihn nicht. Vorher hatte die Seite fuenf Eingaben
    und **null** ``<label>``."""
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config)
    try:
        text = (await c.get("/tasks")).text
        leiste = text[text.find('<form class="filters"'):]
        leiste = leiste[:leiste.find("</form>")]
        # ``for`` verweist auf die **id**, nicht auf den Namen -- genau das
        # ist die Verbindung, die ein Vorleseprogramm herstellt.
        eingaben = re.findall(r'<(?:input|select)[^>]*\sid="([^"]+)"', leiste)
        ohne_id = re.findall(r'<(?:input|select)(?![^>]*\sid=)[^>]*name="([^"]+)"', leiste)
        beschriftet = re.findall(r'<label[^>]*for="([^"]+)"', leiste)
        assert eingaben, "die Leiste hat keine Felder"
        assert not ohne_id, f"Felder ohne id koennen keine Beschriftung haben: {ohne_id}"
        for kennung in eingaben:
            assert kennung in beschriftet, (
                f"das Feld {kennung!r} hat keine Beschriftung: {beschriftet}"
            )
    finally:
        await c.__aexit__(None, None, None)


async def test_die_seite_nennt_keine_reichweite_vor_der_aufbewahrung(
    tmp_path: Path,
) -> None:
    """R8, ueber die **Seite** statt ueber die Hilfsfunktion.

    Die Reparatur war nur an ``DirectoryQueries.reach`` geprueft. Die Stelle
    im Betrieb -- die Route, die die Aufbewahrungsgrenze uebergibt, und der
    Satz, der die Zahl ausspricht -- war ungedeckt: den Parameter aus dem
    Aufruf zu streichen liess die ganze Testreihe gruen.
    """
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    jetzt = time.time()
    w = DirectoryWriter(connect(config.directory_file))
    # Ein Dauerlaeufer: erster Zeitpunkt vor 30 Tagen, noch am Leben.
    w.apply([(None, Event(type="task_enqueue", ts=jetzt - 30 * 86400, source="app",
                          origin=Origin.ENQUEUE,
                          payload={"task_id": "dauer", "task_name": "x",
                                   "queue": "reports", "args": [], "kwargs": {}}))])
    w._con.execute("update task set last_seen_at=? where task_id='dauer'", (jetzt,))
    w._con.commit()
    w.close()

    c = await _angemeldet(config)
    try:
        text = (await c.get("/tasks")).text
        gesagt = re.search(r"reicht zurück bis ([0-9-]{10} [0-9:]{8})", text)
        assert gesagt, f"die Reichweite wird nicht genannt: {text[:200]}"
        genannt = datetime.strptime(gesagt.group(1), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC).timestamp()
        grenze = jetzt - config.retention_days * 86400.0
        assert genannt >= grenze - 1.0, (
            f"die Seite verspricht {(grenze - genannt) / 86400:.0f} Tage mehr "
            "Abdeckung, als die Aufbewahrung hergibt"
        )
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 8: Kennungen mit Sonderzeichen (aus dem ersten echten Lauf) --


async def test_eine_kennung_mit_raute_bleibt_anklickbar(tmp_path: Path) -> None:
    """Gefunden beim ersten Lauf gegen einen echten Worker.

    swarmq vergibt die Kennung eines Workflow-Schritts als
    ``{workflow_id}#{step_path}`` -- und ``apply()`` mit einer einzelnen
    Signatur ist ein einschrittiger Workflow. **Jede** so eingereihte Task
    traegt also eine Raute, und in einer Adresse trennt die Raute das
    Fragment ab: der Browser holt ``/tasks/<id>`` ohne den Rest, das
    Verzeichnis kennt die gekuerzte Kennung nicht, und die Detailseite
    antwortet mit 404.

    Nachgemessen am laufenden Dienst: **jeder** Verweis der Liste war
    kaputt. Der Hauptfluss endete an seiner ersten Verzweigung, und keine
    Attrappe der Testreihe hatte je eine Raute in einer Kennung.
    """
    config = _config(tmp_path)
    kennung = "01a0b4ca-75db-70ef-8e98-8faff0d1db27#"
    w = DirectoryWriter(connect(config.directory_file))
    w.apply([("1000-0", Event(type="task_enqueue", ts=1_700_000_000.0, source="app",
                              origin=Origin.ENQUEUE,
                              payload={"task_id": kennung, "task_name": "reports.build",
                                       "queue": "reports", "args": [], "kwargs": {}}))])
    w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                   "on conflict(key) do nothing")
    w._con.commit()
    w.close()

    c = await _angemeldet(config)
    try:
        text = (await c.get("/tasks")).text
        verweis = re.search(r'href="/tasks/([^"]+)"', text)
        assert verweis, "kein Verweis auf die Detailseite"
        assert "#" not in verweis.group(1), (
            f"die Raute steht roh in der Adresse: {verweis.group(1)!r} -- "
            "der Browser schneidet dort ab"
        )
        # Und der Verweis muss wirklich tragen.
        antwort = await c.get(f"/tasks/{verweis.group(1)}")
        assert antwort.status_code == 200, (
            f"der Verweis fuehrt ins Leere: {antwort.status_code}"
        )
        assert "reports.build" in antwort.text
    finally:
        await c.__aexit__(None, None, None)


async def test_der_zeitraum_ueberlebt_das_blaettern(tmp_path: Path) -> None:
    """Sonst sucht man ab Seite zwei in einem anderen Zeitraum als gewaehlt.

    Gemessen vor der Korrektur: Seite 1 mit "letzter Tag" zeigte 50 Zeilen
    von heute, Seite 2 lieferte 20 von vor fuenf Tagen -- und die Leiste
    stellte sich wieder auf "beliebig". Der Stufe-D-Review hatte das schon
    gemeldet; beim Bau des Zeitraumfilters habe ich die Uebergabe nicht
    nachgezogen.
    """
    config = _config(tmp_path)
    jetzt = time.time()
    w = DirectoryWriter(connect(config.directory_file))
    w.apply([(f"{i}000-0", Event(
        type="task_enqueue", ts=jetzt - (0 if i < 60 else 5 * 86400) - i,
        source="app", origin=Origin.ENQUEUE,
        payload={"task_id": f"{'neu' if i < 60 else 'alt'}{i:03d}",
                 "task_name": "x", "queue": "reports", "args": [], "kwargs": {}}))
        for i in range(80)])
    w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                   "on conflict(key) do nothing")
    w._con.commit()
    w.close()

    c = await _angemeldet(config)
    try:
        erste = (await c.get("/tasks?seit=1")).text
        verweis = re.search(r'href="/tasks\?([^"]+)"', erste)
        assert verweis, "kein Weiter-Verweis"
        assert "seit=1" in verweis.group(1), (
            f"der Zeitraum fehlt im Weiter-Verweis: {verweis.group(1)}"
        )
        zweite = (await c.get("/tasks?" + verweis.group(1).replace("&amp;", "&"))).text
        assert "alt0" not in zweite, "Seite zwei zeigt Zeilen ausserhalb des Zeitraums"
        assert 'value="1" selected' in zweite or '="1"selected' in zweite.replace(" ", ""), (
            "die Leiste steht auf Seite zwei wieder auf 'beliebig'"
        )
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 6: Das Ergebnis (U2) ----------------------------------------


def _ergebniskarte(html: str) -> str:
    """Nur die Ergebniskarte, nicht die ganze Seite.

    Zwei Tests dieses Projekts waren schon einmal gruen, ohne zu messen, was
    sie messen sollten: Sie suchten Woerter im gesamten Seitentext, und die
    standen dort aus einem anderen Grund. Hier wird der Abschnitt
    herausgeschnitten, bevor etwas behauptet wird.
    """
    treffer = re.search(r'<section class="card ergebnis".*?</section>', html, re.S)
    return treffer.group(0) if treffer else ""


async def test_ein_fertiger_task_zeigt_sein_ergebnis_im_klartext(tmp_path: Path) -> None:
    """Covers AE1. Die Frage, wegen der dieser Plan entstand.

    Das Ergebnis steht **unter** den Argumenten, in derselben Form: Bisher
    endete die Seite bei "fertig" und der Laufzeit, und wer wissen wollte,
    was herauskam, griff zur Konsole.
    """
    from swarmq_dashboard.web.ergebnis import Ergebnisbild

    class _Leser:
        async def lesen(self, task_id: str) -> Ergebnisbild:
            return Ergebnisbild(zustand="da", art="fertig",
                                text='{"pid": 4711, "worker": "phx-w1"}')

    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config, ergebnisleser=_Leser())
    try:
        seite = await c.get("/tasks/a41f")
    finally:
        await c.__aexit__(None, None, None)

    karte = _ergebniskarte(seite.text)
    assert karte, "es gibt keine Ergebniskarte auf der Seite"
    assert "4711" in karte


class _Zaehlleser:
    """Ein Leser, der jeden Aufruf mitzaehlt."""

    def __init__(self, bild: Any = None) -> None:
        from swarmq_dashboard.web.ergebnis import Ergebnisbild

        self.rufe: list[str] = []
        self.bild = bild or Ergebnisbild(zustand="da", art="fertig", text="{}")

    async def lesen(self, task_id: str) -> Any:
        self.rufe.append(task_id)
        return self.bild


async def test_ein_laufender_task_loest_keinen_backend_zugriff_aus(tmp_path: Path) -> None:
    """Und traegt trotzdem ein Wort -- keines der beiden allein genuegt.

    Der Zugriff waere fuer nichts: Ein Task, der noch rechnet, hat kein
    Ergebnis. Eine leere Karte waere aber die stille Falschaussage, gegen die
    R33 steht -- sie laese sich wie "nichts zurueckgegeben".
    """
    leser = _Zaehlleser()
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config, ergebnisleser=leser)
    try:
        seite = await c.get("/tasks/b22c")
    finally:
        await c.__aexit__(None, None, None)

    assert leser.rufe == [], "fuer einen laufenden Task wurde das Backend gefragt"
    karte = _ergebniskarte(seite.text)
    assert "noch nicht beendet" in karte, karte


async def test_eine_unvollstaendige_zeile_loest_den_nachschlag_doch_aus(tmp_path: Path) -> None:
    """Der Fall, in dem das Backend die letzte verbliebene Quelle ist.

    Ging das Abschlusssignal verloren, steht die Zeile auf einem nicht
    beendeten Status, obwohl das Ergebnis im Backend liegt. Ohne diese
    Ausnahme verweigerte die Seite die Auskunft ausgerechnet im Vorfall --
    und das Verzeichnis weiss hier sogar, dass ihm etwas fehlt: Es hat die
    Zeitspanne als unvollstaendig markiert (R4a).
    """
    from swarmq_dashboard.web.ergebnis import Ergebnisbild

    leser = _Zaehlleser(Ergebnisbild(zustand="da", art="fertig", text='{"gerettet": true}'))
    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config, ergebnisleser=leser)
    try:
        seite = await c.get("/tasks/c33d")
    finally:
        await c.__aexit__(None, None, None)

    assert leser.rufe == ["c33d"], "die Luecke wurde nicht beruecksichtigt"
    assert "gerettet" in _ergebniskarte(seite.text)


async def _detailseite(tmp_path: Path, bild: Any, tid: str = "a41f") -> str:
    """Die Detailseite mit diesem Ergebnisbild."""
    class _Leser:
        async def lesen(self, task_id: str) -> Any:
            return bild

    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c = await _angemeldet(config, ergebnisleser=_Leser())
    try:
        return (await c.get(f"/tasks/{tid}")).text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_fehlendes_ergebnis_nennt_beide_ursachen(tmp_path: Path) -> None:
    """Covers AE2. Ein leeres Feld waere die stille Falschaussage (R33).

    Und die Seite darf sich nicht auf **eine** Ursache festlegen: Sie kann
    nicht unterscheiden, ob die Frist abgelaufen ist oder die Anwendung
    ueberhaupt keine Ergebnisse ablegt -- Fire-and-Forget ohne eigene Frist
    loest auf 0 auf, und dann wird nie etwas gespeichert.
    """
    from swarmq_dashboard.web.ergebnis import Ergebnisbild

    karte = _ergebniskarte(await _detailseite(
        tmp_path, Ergebnisbild(zustand="fehlt", art=None, text="")))

    assert "abgelaufen" in karte, karte
    assert "speichert keine Ergebnisse" in karte, karte


async def test_ein_totes_backend_nennt_den_grund_und_die_seite_bleibt_ganz(tmp_path: Path) -> None:
    """Covers AE3, R5. Die Seite traegt mehr als das Ergebnis.

    Argumente, Signalfolge und der Stornier-Knopf muessen stehen, auch wenn
    das Backend weg ist. Waere der Nachschlag ein Fehler statt einer
    Auskunft, verloere man im Vorfall die ganze Seite -- wegen des einen
    Feldes, das gerade nicht zu haben ist.
    """
    from swarmq_dashboard.web.ergebnis import Ergebnisbild

    seite = await _detailseite(tmp_path, Ergebnisbild(
        zustand="nicht_erreichbar", art=None, text="",
        grund="das Backend ist nicht erreichbar: connection refused"))

    karte = _ergebniskarte(seite)
    assert "connection refused" in karte, karte
    # Die Seite selbst ist vollstaendig geblieben.
    assert "Signalfolge" in seite
    assert "render_pdf" in seite


async def test_ein_gescheiterter_task_zeigt_beide_fehlertexte_beschriftet(tmp_path: Path) -> None:
    """Covers AE4, F2. Zwei Quellen, die auseinandergehen koennen.

    Das Verzeichnis haelt den Fehler aus dem **Signal**, das Backend den aus
    dem **Ergebnis**. Gingen Signale verloren, stehen dort verschiedene
    Texte -- und eines davon stillschweigend zu bevorzugen hiesse, sich auf
    eine Quelle festzulegen, ohne zu sagen welche.
    """
    from swarmq_dashboard.web.ergebnis import Ergebnisbild

    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    w = DirectoryWriter(connect(config.directory_file))
    w.apply([
        ("6000-0", Event(type="task_pickup", ts=1_700_000_300.0, source="phx-w3:0",
                         origin=Origin.PICKUP,
                         payload={"task_id": "d44e", "task_name": "charge", "queue": "billing",
                                  "args": [], "kwargs": {}})),
        ("7000-0", Event(type="task_failed", ts=1_700_000_301.0, source="phx-w3:0",
                         origin=Origin.PICKUP,
                         payload={"task_id": "d44e", "task_name": "charge",
                                  "error": "Timeout beim ersten Versuch"})),
    ])
    w.close()

    class _Leser:
        async def lesen(self, task_id: str) -> Any:
            return Ergebnisbild(zustand="da", art="gescheitert",
                                text="ConnectionError: gateway weg")

    c = await _angemeldet(config, ergebnisleser=_Leser())
    try:
        seite = (await c.get("/tasks/d44e")).text
    finally:
        await c.__aexit__(None, None, None)

    karte = _ergebniskarte(seite)
    assert "gateway weg" in karte, karte
    assert "aus dem Ergebnis" in karte, karte
    assert "Timeout beim ersten Versuch" in karte, karte
    assert "aus dem Signal" in karte, karte


async def test_ein_abgeschnittenes_ergebnis_ist_gekennzeichnet(tmp_path: Path) -> None:
    """Covers AE5, R4. Ein stumm gekuerzter Text waere eine Falschaussage.

    Wer die ersten acht Kilobyte eines Datensatzes sieht und nicht weiss,
    dass er gekuerzt ist, liest sie als das Ganze. Die genannte Zahl ist die
    Laenge der **Darstellung** -- nicht die des im Backend abgelegten
    Pakets, an dessen Rohbytes das Dashboard nach KD2 nicht herankommt.
    """
    from swarmq_dashboard.web.ergebnis import Ergebnisbild

    karte = _ergebniskarte(await _detailseite(tmp_path, Ergebnisbild(
        zustand="da", art="fertig", text="x" * 100,
        abgeschnitten=True, groesse=2_097_152)))

    assert "abgeschnitten" in karte, karte
    assert "2,0 MB" in karte, karte
    assert "Darstellung" in karte, "die Zahl wird als Speicherbelegung gelesen"


async def test_ein_storniertes_ergebnis_traegt_dieselbe_marke_wie_sein_status(
    tmp_path: Path,
) -> None:
    """Der Befund des Gestaltungs-Reviews am Plan.

    Der Plan bildete "storniert" zunaechst auf die Warte-Marke ab. Dann
    stuende auf **derselben Seite** der Status-Marker neutral und die
    Ergebniskarte gelb -- fuer denselben Sachverhalt. ``_ZUSTANDSART`` fuehrt
    "storniert" als ``is-idle``, und die Farbtabelle der Oberflaeche meint
    mit "wartet" die *angeforderte* Stornierung, nicht den Ausgang.
    """
    from swarmq_dashboard.web.ergebnis import Ergebnisbild
    from swarmq_dashboard.web.templates import zustandsart

    assert zustandsart("storniert") == "is-idle", "die Vorlage hat sich geaendert"

    karte = _ergebniskarte(await _detailseite(tmp_path, Ergebnisbild(
        zustand="da", art="storniert", text="Der Task wurde storniert.")))

    treffer = re.search(r'<span class="tag (is-\w+)"', karte)
    assert treffer, f"die Karte traegt keine Zustandsmarke: {karte}"
    assert treffer.group(1) == "is-idle", treffer.group(1)


async def test_das_ergebnis_landet_weder_im_audit_noch_im_verzeichnis(tmp_path: Path) -> None:
    """Covers R6a, KD1. Gelesen, nicht gespeichert.

    Es hat eine eigene Verfallsfrist, die das Verzeichnis nicht kennt und
    nicht einhalten koennte -- eine Kopie dort waere ein zweiter Ort mit
    einer zweiten Loeschpflicht (R7a). Und ein Seitenaufruf ist keine
    Nutzeraktion, die ins Protokoll gehoert.
    """
    from swarmq_dashboard.web.ergebnis import Ergebnisbild

    geheim = "KONTONUMMER-9917-SICHTBAR"

    class _Leser:
        async def lesen(self, task_id: str) -> Any:
            return Ergebnisbild(zustand="da", art="fertig", text=geheim)

    config = _config(tmp_path)
    _verzeichnis_fuellen(config)
    c, app = await _angemeldet(config, ergebnisleser=_Leser(), mit_app=True)
    protokolliert: list[tuple[Any, ...]] = []
    app.state.services.audit.record = (          # type: ignore[method-assign]
        lambda *a, **k: protokolliert.append((a, k)))  # type: ignore[func-returns-value]
    try:
        seite = (await c.get("/tasks/a41f")).text
        # **Vor** dem Herunterfahren: danach ist der Schreiber geschlossen,
        # und die Abfrage liefe gegen None -- gruen, ohne etwas zu pruefen.
        schreiber = app.state.services.services_directory_writer
        treffer = schreiber._con.execute(
            "select count(*) from task where coalesce(args_text,'') like ? "
            "or coalesce(error,'') like ?",
            (f"%{geheim}%", f"%{geheim}%"),
        ).fetchone()[0]
    finally:
        await c.__aexit__(None, None, None)

    assert geheim in _ergebniskarte(seite), "die Karte zeigt es gar nicht"
    assert treffer == 0, "das Ergebnis steht im Verzeichnis"
    assert protokolliert == [], "ein Seitenaufruf hat einen Audit-Eintrag erzeugt"
