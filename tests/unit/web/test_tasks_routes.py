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
"""

from __future__ import annotations

import re
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
                      mit_app: bool = False) -> Any:
    app = create_app(config, hasher=_BILLIG, canceller=canceller)
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
            r'<p class="notice">\s*Scheduler-Ausloesungen', antwort.text
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
        assert "verlorengegangen" in antwort.text, "die Luecke wird verschwiegen"
        assert "2023-11-14" in antwort.text, "die Spanne wird nicht genannt"
    finally:
        await c.__aexit__(None, None, None)
