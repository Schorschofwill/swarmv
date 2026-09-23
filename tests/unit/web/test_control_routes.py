"""Sollwert-Steuerung in der Oberflaeche (U16, R15--R17, R33).

Der Bereich ist der einzige der ganzen Anwendung, der etwas **verandert**,
das nicht dem Dashboard gehoert: er schreibt in den Kontrollkanal, aus dem
fremde Supervisoren ihre Prozesszahl beziehen. Zwei Dinge folgen daraus.

* **Geprueft wird vor dem Schreiben** (R15b). Was die Oberflaeche schon als
  unmoeglich erkennt, gehoert nicht in den Kanal.
* **Ablehnung und Warten teilen sich keine Darstellung** (KTD13). Eine
  Ablehnung bietet kein erneutes Senden an -- sonst dreht sich das
  Teammitglied in einer Schleife auf einem Wert, den der Supervisor bereits
  verworfen hat.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Naht und Verfuegbarkeit ===
1. [x] Der Dienst baut die Steuerung und weist sie als verfuegbar aus.
2. [x] Ohne Kontrollkanal ist sie abgeschaltet und begruendet (AE15).

=== ZYKLUS 2: Die Gruppe in der Uebersicht ===
3. [x] Mit Kontrollkanal nennt die Gruppe Spielraum und Herkunft der Werte.
4. [x] Ein gesetzter Sollwert erscheint mit seinem Zustand.

=== ZYKLUS 3: Der Dialog (U16 Punkt 5, AE4) ===
5. [x] Die Eingabe ist "je Supervisor" beschriftet, nennt die Supervisor-Zahl
       und die daraus folgende Gesamtzahl.
6. [x] Eine Gruppe ohne Autoskalierung bekommt das Feld fuer die feste Zahl.

=== ZYKLUS 4: Setzen, Korrigieren, Aufheben (R27) ===
7. [x] Setzen schreibt in den Kanal und in den Audit-Strom.
8. [x] Ein unmoeglicher Wert wird vor dem Schreiben abgewiesen und begruendet.
9. [x] Aufheben loescht den Sollwert und steht im Audit.
10. [x] Beide Aktionen brauchen Sitzung und Merkmal.

=== ZYKLUS 5: Die Zustaende in der Ansicht (R17, AE6) ===
11. [x] Abgelehnt zeigt den Grund im Wortlaut und bietet kein erneutes Senden.
12. [x] Unbestaetigt nennt den Zeitpunkt des Sendens.

=== ZYKLUS 6: Befunde aus dem ersten echten Lauf ===
13. [x] Die Sammelgruppe "ohne Gruppe" bekommt keine Steuerung -- ein
        Sollwert unter diesem Schluessel erreicht niemanden.
14. [x] Eine Gruppe ohne meldenden Supervisor nennt die beiden Ursachen.
15. [x] Bei genau einem Supervisor stimmen Numerus und Rechnung.
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
from swarmq_dashboard.control import Sollwert
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin
from swarmq_dashboard.web.app import create_app

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
_PASSWORT = "ein-langes-passwort"
BASIS = "https://testserver.local"
JETZT = 1_700_000_000.0


class FakeBroker:
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def stats_many(self, queues: list[str]) -> dict[str, QueueStats]:
        return {q: QueueStats(queue=q, exists=True) for q in queues}


class FakeKanal:
    """Der Kontrollkanal ohne Valkey."""

    def __init__(self, meldungen: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.meldungen = meldungen or {}
        self.sollwerte: dict[str, Sollwert] = {}
        self.geschrieben: list[tuple[str, Sollwert]] = []
        self.geloescht: list[str] = []

    async def status_aller(self, gruppe: str) -> list[dict[str, Any]]:
        return self.meldungen.get(gruppe, [])

    async def schreiben(self, gruppe: str, wert: Sollwert) -> None:
        self.geschrieben.append((gruppe, wert))
        self.sollwerte[gruppe] = wert

    async def loeschen(self, gruppe: str) -> None:
        self.geloescht.append(gruppe)
        self.sollwerte.pop(gruppe, None)

    async def gelesen(self, gruppe: str) -> Sollwert | None:
        return self.sollwerte.get(gruppe)

    async def close(self) -> None: ...


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


def _worker(config: DashboardConfig, gruppe: str = "reports", wid: str = "phx-w1:0") -> None:
    """Ein Worker im Verzeichnis -- ohne ihn gibt es die Gruppe in der Ansicht nicht."""
    e = Event(type="sqd_worker_meta", ts=JETZT, source=wid, origin=Origin.PICKUP,
              payload={"worker_id": wid, "slot": 0, "group": gruppe, "queues": ["reports"],
                       "all_queues": ["reports"], "concurrency": 4, "pool": "thread",
                       "started_at": JETZT})
    w = DirectoryWriter(connect(config.directory_file))
    w.apply([("1000-0", e)])
    w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                   "on conflict(key) do nothing")
    w._con.commit()
    w.close()


def _meldung(kennung: str = "a:p1", **felder: Any) -> dict[str, Any]:
    """Eine Rueckmeldung, wie der Supervisor sie schreibt."""
    grund = {"supervisor": kennung, "headroom": 8, "store": "present", "setpoint": None,
             "effective": {"min": 2, "max": 4}, "source": "startup", "autoscaling": True,
             "rejection": None, "slots": {"size": 8, "occupied": 2, "locked": 0},
             "workers": 2, "reported_at": "2026-09-22T06:12:21+00:00", "ttl_s": 40.0}
    grund.update(felder)
    return grund


async def _angemeldet(config: DashboardConfig, *, kanal: Any | None = None) -> Any:
    app = create_app(config, hasher=_BILLIG, broker=FakeBroker(), setpoint_store=kanal)
    c = AsyncTestClient(app=app, base_url=BASIS)
    await c.__aenter__()
    seite = await c.get("/login")
    merkmal = re.search(r'name="?_csrf_token"?\s+value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
    await c.post("/login", data={"account": "ops", "password": _PASSWORT,
                                 "_csrf_token": merkmal}, follow_redirects=False)
    return c, app


# --- Zyklus 1 -----------------------------------------------------------


async def test_der_dienst_baut_die_steuerung_und_weist_sie_aus(tmp_path: Path) -> None:
    """Die Naht: ohne eingesetzten Kanal faende die Testreihe nur den echten Valkey."""
    c, app = await _angemeldet(_config(tmp_path), kanal=FakeKanal())
    try:
        assert app.state.services.steuerung.verfuegbar is True
    finally:
        await c.__aexit__(None, None, None)


async def test_ohne_kontrollkanal_bleibt_die_startseite_vollstaendig(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Covers AE15, R33. Die Bibliothek ohne Kontrollkanal ist kein Startfehler.

    Sie ist der Auslieferungszustand von heute: das Dashboard erscheint,
    bevor die Bibliotheksfassung mit dem Kanal draussen ist.
    """
    monkeypatch.setattr(
        "swarmq_dashboard.web.app.kontrollkanal_grund",
        lambda: "Steuerung nicht verfügbar: die installierte swarmq-Fassung bringt den Kontrollkanal nicht mit.",
    )
    c, app = await _angemeldet(_config(tmp_path))
    try:
        assert app.state.services.steuerung.verfuegbar is False
        antwort = await c.get("/")
        assert antwort.status_code == 200
        assert "Steuerung nicht verfügbar" in antwort.text
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 2 -----------------------------------------------------------


async def test_mit_kontrollkanal_nennt_die_gruppe_spielraum_und_herkunft(tmp_path: Path) -> None:
    """R14, R33: der Satz ueber das Fehlende darf nicht stehen bleiben, wenn nichts fehlt.

    Er ist bis heute fest in der Vorlage -- gegen eine Bibliothek **mit**
    Kanal waere er die Falschaussage, gegen die das vierte Erfolgskriterium
    des Plans steht.
    """
    config = _config(tmp_path)
    _worker(config)
    c, _ = await _angemeldet(config, kanal=FakeKanal({"reports": [_meldung()]}))
    try:
        antwort = await c.get(f"/?jetzt={JETZT + 10}")
        assert antwort.status_code == 200
        assert "Steuerung nicht verfügbar" not in antwort.text
        assert "Spielraum" in antwort.text
        assert "Startargumente" in antwort.text, "die Herkunft der wirksamen Werte fehlt"
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 3 -----------------------------------------------------------


async def test_der_dialog_beschriftet_die_eingabe_je_supervisor(tmp_path: Path) -> None:
    """Covers AE4, R15a. Zwei Supervisoren, 2 bis 8 je Supervisor: die Gruppe faehrt 4 bis 16.

    Wer die Eingabe fuer die Gruppenzahl haelt, setzt die Haelfte dessen, was
    er will -- oder das Doppelte. Deshalb ein Dialog und keine editierbare
    Tabellenzelle: die Beschriftung, die Supervisor-Zahl und die Gesamtzahl
    brauchen mehr Platz, als eine Zelle hergibt.
    """
    config = _config(tmp_path)
    _worker(config)
    kanal = FakeKanal({"reports": [
        _meldung("a:p1", effective={"min": 2, "max": 8}, source="setpoint"),
        _meldung("b:p1", effective={"min": 2, "max": 8}, source="setpoint"),
    ]})
    kanal.sollwerte["reports"] = Sollwert(2, 8)
    c, _ = await _angemeldet(config, kanal=kanal)
    try:
        antwort = await c.get("/worker/gruppe/reports/sollwert")
        assert antwort.status_code == 200
        text = antwort.text
        assert "je Supervisor" in text
        assert "2 Supervisoren" in text or "2&nbsp;Supervisoren" in text
        assert "4" in text and "16" in text, "die Gesamtzahl der Gruppe fehlt"
    finally:
        await c.__aexit__(None, None, None)


class AuditSpeicher:
    """Der Audit-Strom im Arbeitsspeicher."""

    def __init__(self) -> None:
        self.e: list[tuple[str, bytes]] = []

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        self.e.extend((f"{i}-0", w) for i, w in enumerate(events, len(self.e) + 1))

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        return self.e[:count]


# --- Zyklus 4 -----------------------------------------------------------


async def test_setzen_schreibt_in_den_kanal_und_in_den_audit(tmp_path: Path) -> None:
    """R15, R27: die Aktion wirkt nach aussen -- und wer sie ausgeloest hat, bleibt nachlesbar."""
    from swarmq_dashboard.audit import AuditLog

    config = _config(tmp_path)
    _worker(config)
    kanal = FakeKanal({"reports": [_meldung("a:p1"), _meldung("b:p1")]})
    c, app = await _angemeldet(config, kanal=kanal)
    try:
        app.state.services.audit = AuditLog(AuditSpeicher())
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/worker/gruppe/reports/sollwert",
                               data={"unten": "2", "oben": "6"},
                               headers={"x-csrftoken": merkmal}, follow_redirects=False)
        assert antwort.status_code == 303
        assert kanal.geschrieben == [("reports", Sollwert(2, 6))]

        eintraege = [e for e in await app.state.services.audit.read() if e.action == "setpoint_set"]
        assert eintraege, "die Aktion steht nicht im Audit-Strom"
        assert eintraege[0].target == "reports"
        assert eintraege[0].account == "ops"
        assert "2" in (eintraege[0].detail or "") and "6" in (eintraege[0].detail or "")
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_unmoeglicher_wert_wird_abgewiesen_und_begruendet(tmp_path: Path) -> None:
    """R15b: geprueft wird vor dem Schreiben, und die Begruendung nennt die Zahlen.

    "Abgelehnt" ohne Grund schickt das Teammitglied ins Raten -- bei einer
    Zahl, die der Supervisor selbst gemeldet hat.
    """
    from swarmq_dashboard.audit import AuditLog

    config = _config(tmp_path)
    _worker(config)
    kanal = FakeKanal({"reports": [_meldung("a:p1", headroom=8)]})
    c, app = await _angemeldet(config, kanal=kanal)
    try:
        app.state.services.audit = AuditLog(AuditSpeicher())
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/worker/gruppe/reports/sollwert",
                               data={"unten": "2", "oben": "12"},
                               headers={"x-csrftoken": merkmal}, follow_redirects=True)
        assert kanal.geschrieben == [], "der Wert landete trotzdem im Kanal"
        assert "12" in antwort.text and "8" in antwort.text
        eintraege = [e for e in await app.state.services.audit.read() if e.action == "setpoint_set"]
        assert eintraege and eintraege[0].outcome == "abgelehnt", (
            "der abgewiesene Versuch steht nicht als solcher im Audit"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_aufheben_loescht_und_steht_im_audit(tmp_path: Path) -> None:
    """R15, R27: danach gelten wieder die Startargumente."""
    from swarmq_dashboard.audit import AuditLog

    config = _config(tmp_path)
    _worker(config)
    kanal = FakeKanal({"reports": [_meldung("a:p1")]})
    kanal.sollwerte["reports"] = Sollwert(2, 4)
    c, app = await _angemeldet(config, kanal=kanal)
    try:
        app.state.services.audit = AuditLog(AuditSpeicher())
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/worker/gruppe/reports/sollwert/aufheben",
                               headers={"x-csrftoken": merkmal}, follow_redirects=False)
        assert antwort.status_code == 303
        assert kanal.geloescht == ["reports"]
        eintraege = [e for e in await app.state.services.audit.read() if e.action == "setpoint_clear"]
        assert eintraege and eintraege[0].target == "reports"
    finally:
        await c.__aexit__(None, None, None)


async def test_beide_aktionen_brauchen_sitzung_und_merkmal(tmp_path: Path) -> None:
    """R21, R22: eine Aktion, die nach aussen wirkt, braucht beides."""
    config = _config(tmp_path)
    _worker(config)
    kanal = FakeKanal({"reports": [_meldung("a:p1")]})
    kanal.sollwerte["reports"] = Sollwert(2, 4)
    c, _ = await _angemeldet(config, kanal=kanal)
    try:
        # Angemeldet, aber ohne Merkmal.
        ohne = await c.post("/worker/gruppe/reports/sollwert",
                            data={"unten": "2", "oben": "4"}, follow_redirects=False)
        assert ohne.status_code >= 400
        ohne_auf = await c.post("/worker/gruppe/reports/sollwert/aufheben", follow_redirects=False)
        assert ohne_auf.status_code >= 400
        assert kanal.geschrieben == [] and kanal.geloescht == []
    finally:
        await c.__aexit__(None, None, None)

    # Ohne Sitzung: der Dialog selbst ist schon zu.
    app = create_app(config, hasher=_BILLIG, broker=FakeBroker(), setpoint_store=kanal)
    fremd = AsyncTestClient(app=app, base_url=BASIS)
    await fremd.__aenter__()
    try:
        antwort = await fremd.get("/worker/gruppe/reports/sollwert", follow_redirects=False)
        assert antwort.status_code in (302, 303, 401)
    finally:
        await fremd.__aexit__(None, None, None)


# --- Zyklus 5 -----------------------------------------------------------


async def test_die_uebersicht_zeigt_den_sollwert_und_fuehrt_zum_dialog(tmp_path: Path) -> None:
    """R14, R15a: die Startseite beantwortet "laeuft die Gruppe auf einem Sollwert?".

    Ohne die Zahl auf der Uebersicht muesste man jede Gruppe einzeln
    aufmachen, um zu sehen, ob jemand etwas gesetzt hat.
    """
    config = _config(tmp_path)
    _worker(config)
    kanal = FakeKanal({"reports": [
        _meldung("a:p1", setpoint={"min": 2, "max": 6}, source="setpoint",
                 effective={"min": 2, "max": 6}),
    ]})
    kanal.sollwerte["reports"] = Sollwert(2, 6)
    c, _ = await _angemeldet(config, kanal=kanal)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "2..6" in text, "der gesetzte Sollwert steht nicht auf der Uebersicht"
        assert "je Supervisor" in text
        assert "/worker/gruppe/reports/sollwert" in text, "kein Weg zum Dialog"
        assert "bestätigt" in text
    finally:
        await c.__aexit__(None, None, None)


async def test_abgelehnt_zeigt_den_grund_und_bietet_kein_erneutes_senden(tmp_path: Path) -> None:
    """Covers AE6, R16a, R17, KTD13.

    Drei Dinge zusammen: der Grund im Wortlaut, **kein** Knopf zum erneuten
    Senden, und der verworfene Wert ist aus dem Kanal entfernt -- sonst
    wendet ihn ein spaeter startender Supervisor mit groesserem Spielraum an.
    """
    config = _config(tmp_path)
    _worker(config)
    kanal = FakeKanal({"reports": [_meldung(
        "a:p1", setpoint={"min": 2, "max": 12}, source="startup",
        rejection="max 12 exceeds headroom 8",
    )]})
    kanal.sollwerte["reports"] = Sollwert(2, 12)
    c, _ = await _angemeldet(config, kanal=kanal)
    try:
        text = (await c.get("/worker/gruppe/reports/sollwert")).text
        assert "abgelehnt" in text
        assert "max 12 exceeds headroom 8" in text, "der Grund steht nicht im Wortlaut da"
        assert "erneutes Senden ändert daran nichts" in text
        assert kanal.geloescht == ["reports"], "der verworfene Wert liegt noch im Kanal"
    finally:
        await c.__aexit__(None, None, None)


async def test_unbestaetigt_nennt_den_zeitpunkt_des_sendens(tmp_path: Path) -> None:
    """Covers AE6a, R17. "Unbestaetigt" ohne Zeitpunkt ist ein Warten ohne Mass."""
    config = _config(tmp_path)
    _worker(config)
    # Der Supervisor meldet weiter den alten Stand -- er hat den neuen Wert
    # nie gelesen.
    kanal = FakeKanal({"reports": [_meldung("a:p1", setpoint=None, source="startup")]})
    c, app = await _angemeldet(config, kanal=kanal)
    try:
        merkmal = str(c.cookies.get("csrftoken"))
        await c.post("/worker/gruppe/reports/sollwert", data={"unten": "2", "oben": "6"},
                     headers={"x-csrftoken": merkmal}, follow_redirects=False)
        # Die Frist ist 15 s; der Dialog rechnet gegen seine eigene Uhr.
        app.state.services.steuerung._gesendet["reports"] = 0.0
        text = (await c.get("/worker/gruppe/reports/sollwert")).text
        assert "unbestätigt" in text
        assert "1970" in text, "der Zeitpunkt des Sendens fehlt"
    finally:
        await c.__aexit__(None, None, None)


# --- Zyklus 6: Befunde aus dem ersten echten Lauf -----------------------


async def test_die_sammelgruppe_ohne_gruppe_bekommt_keine_steuerung(tmp_path: Path) -> None:
    """"ohne Gruppe" ist ein Platzhalter, kein Gruppenname (KEINE_GRUPPE).

    Im ersten echten Lauf stand an dieser Karte "kein Sollwert · Sollwert
    ändern". Wer dem folgt, schreibt ``sq:control:setpoint:ohne Gruppe`` --
    einen Schluessel, den kein Supervisor je liest, weil es diese Gruppe
    nicht gibt. Der Wert laege dort fuer immer, und die Ansicht fuehrte ihn
    als gesetzt.
    """
    config = _config(tmp_path)
    # Ein Worker ohne Stammdaten: er landet in der Sammelgruppe.
    e = Event(type="task_started", ts=JETZT, source="phx-x1:0", origin=Origin.PICKUP,
              payload={"task_id": "t1", "worker_id": "phx-x1:0", "task_name": "t"})
    w = DirectoryWriter(connect(config.directory_file))
    w.apply([("1000-0", e)])
    w._con.execute("insert into progress(key, value) values ('rebuild_done','1') "
                   "on conflict(key) do nothing")
    w._con.commit()
    w.close()

    c, _ = await _angemeldet(config, kanal=FakeKanal())
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "ohne Gruppe" in text, "Vorbedingung: die Sammelgruppe steht auf der Seite"
        assert "/sollwert" not in text, (
            "die Sammelgruppe bietet Steuerung an -- ein Sollwert dort erreicht niemanden"
        )
    finally:
        await c.__aexit__(None, None, None)


async def test_eine_gruppe_ohne_meldenden_supervisor_nennt_die_ursache(tmp_path: Path) -> None:
    """R33, und der teuerste Befund des ersten echten Laufs.

    Die Uebersicht fuehrt die Gruppe unter dem Namen aus den Stammdaten des
    Hooks, der Kontrollkanal unter dem Label aus ``--group``. Im Lauf hiess
    dieselbe Gruppe im Verzeichnis "probe" (die Queue-Menge) und im Kanal
    "probe-gruppe" -- der Hook kennt ``--group`` nicht, er liest
    ``SQD_WORKER_GROUP``. Die Folge: die Steuerung findet nie einen
    Supervisor, und die Karte sagte dazu "kein Sollwert".

    Beide Ursachen gehoeren an die Stelle, an der die Auskunft fehlt: kein
    eingeschalteter Kontrollkanal im Worker, oder zwei Namen fuer dieselbe
    Gruppe.
    """
    config = _config(tmp_path)
    _worker(config, gruppe="probe")
    # Der Kanal antwortet, meldet aber unter einem anderen Gruppennamen.
    kanal = FakeKanal({"probe-gruppe": [_meldung("a:p1")]})
    c, _ = await _angemeldet(config, kanal=kanal)
    try:
        text = (await c.get(f"/?jetzt={JETZT + 10}")).text
        assert "kein Supervisor" in text, "die fehlende Rueckmeldung wird verschwiegen"
        assert "SQD_WORKER_GROUP" in text, "die wahrscheinliche Ursache fehlt"
    finally:
        await c.__aexit__(None, None, None)


async def test_bei_einem_einzigen_supervisor_stimmen_die_saetze(tmp_path: Path) -> None:
    """Im echten Lauf stand da: "1 Supervisor melden" und "das 1-Fache".

    Der Numerus ist das eine. Das andere ist die Rechnung: bei einem
    Supervisor ist die Gesamtzahl dieselbe Zahl, und ein Satz, der sie
    trotzdem als Produkt erklaert, laesst den Leser nach dem Faktor suchen,
    den es nicht gibt.
    """
    config = _config(tmp_path)
    _worker(config)
    kanal = FakeKanal({"reports": [_meldung("a:p1", effective={"min": 4, "max": 4},
                                            source="setpoint", autoscaling=False)]})
    kanal.sollwerte["reports"] = Sollwert(4, 4)
    c, _ = await _angemeldet(config, kanal=kanal)
    try:
        text = (await c.get("/worker/gruppe/reports/sollwert")).text
        assert "1 Supervisor melden" not in text
        assert "1-Fache" not in text
        assert "gesamt 4..4" not in text, (
            "bei einem Supervisor ist die Gesamtzahl dieselbe Zahl -- zweimal "
            "dasselbe nebeneinander liest sich wie zwei Groessen"
        )
        assert "Supervisor" in text
    finally:
        await c.__aexit__(None, None, None)


async def test_ein_meldender_supervisor_haelt_seine_worker_verbunden(tmp_path: Path) -> None:
    """R14a, erste Quelle: die Rueckmeldung des Supervisors.

    Im Betrieb gemeldet: "alle cpu worker stehen auf getrennt" -- waehrend
    die Prozesse liefen, vier Konsumenten an der Queue hingen und der
    Supervisor sich im Kontrollkanal meldete. Auf derselben Seite stand
    "Spielraum 4 (1 Supervisor)" aus genau dieser Rueckmeldung.

    Der Grund: die Verbundenheit haengt an Ereignissen im Strom, und ohne
    Tasks und ohne Metrik-Port schickt ein Worker keine. Nach der Frist der
    schwaechsten Quelle steht er auf getrennt -- obwohl die belastbarste
    Quelle danebenliegt und das Gegenteil sagt.

    R14a nennt sie als **erste** der drei. Sie zaehlt je Gruppe, nicht je
    Worker: der Kanal kennt Supervisoren, keine Slots. Mehr als "diese
    Gruppe laeuft" ist daraus nicht abzuleiten -- und genau das soll dort
    stehen, statt einer Stille, die nach Ausfall aussieht.
    """
    config = _config(tmp_path)
    _worker(config, gruppe="reports", wid="phx-r1:0")
    kanal = FakeKanal({"reports": [_meldung("a:p1", workers=2,
                                            slots={"size": 8, "occupied": 2, "locked": 0})]})
    c, _ = await _angemeldet(config, kanal=kanal)
    try:
        # Weit hinter jeder Frist der schwaechsten Quelle.
        text = (await c.get(f"/?jetzt={JETZT + 100_000}")).text
        assert "phx-r1:0" in text
        assert "getrennt" not in text.lower(), (
            "der Worker steht auf getrennt, obwohl sein Supervisor meldet"
        )
        assert "Supervisor-Rückmeldung" in text, "die Quelle wird nicht genannt"
    finally:
        await c.__aexit__(None, None, None)
