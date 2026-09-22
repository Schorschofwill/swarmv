"""Worker-Register im Verzeichnis (U14 Teil 1, R14, R14a, R18, R19).

Die Verbundenheit eines Workers kommt aus der **besten verfuegbaren Quelle**,
und die Ansicht nennt sie (KTD10). Die Reihenfolge ist Supervisor-Rueckmeldung,
Lebenszeichen-Signal, letztes Signal ueberhaupt -- und sie ist noetig, weil
das Lebenszeichen einen Metrik-Port und ein optionales Paket voraussetzt und
sonst **ohne Fehlermeldung schweigt**. Ein Dashboard, das daraus "getrennt"
schliesst, meldet einen Ausfall, den es nicht gibt.

Daraus folgt die zweite Eigenheit: die Frist haengt an der Quelle. Vierzig
Sekunden sind bei einer Rueckmeldung alle fuenf Sekunden ein Ausfall, beim
letzten Signal eines ruhigen Workers dagegen voellig normal.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Aufnehmen ===
1. Ein Stammdaten-Ereignis legt einen Worker an, mit Gruppe, Queues und Slot.
2. Jedes weitere Signal hebt sein letztes Lebenszeichen und leert die
   Stammdaten nicht.
2a. Das Lebenszeichen wird nur gehoben, nie gesenkt.
3. Ein Worker, den nur Signale kennen, erscheint trotzdem -- mit der
   schwaechsten Quelle.
4. Ein Ereignis ohne Worker-Kennung legt keinen Worker an.

=== ZYKLUS 2: Verbundenheit (KTD10, R14a) ===
5. Die beste Quelle gewinnt und wird festgehalten.
6. Eine schwaechere Quelle senkt die festgehaltene nicht.
7. Die Frist fuer "getrennt" haengt an der Quelle.
8. Ein zurueckkehrender Worker gilt wieder als verbunden, ohne Zutun.

=== ZYKLUS 3: Gruppen und Entfernen ===
9. Worker kommen nach Gruppe gebuendelt.
10. Entfernen loescht die Zeile; ein zurueckkehrender Worker legt sie neu an.
"""

from __future__ import annotations

from pathlib import Path

from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.workers import (
    HEARTBEAT_STALE_S,
    SIGNAL_STALE_S,
    SUPERVISOR_STALE_S,
    Quelle,
    WorkerRegistry,
)
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin


def _meta(wid: str, ts: float, gruppe: str = "reports", **kw: object) -> Event:
    nutzlast: dict[str, object] = {
        "worker_id": wid, "slot": 0, "group": gruppe,
        "queues": ["reports"], "all_queues": ["reports", "reports.high"],
        "concurrency": 4, "pool": "thread", "started_at": ts,
    }
    nutzlast.update(kw)
    return Event(type="sqd_worker_meta", ts=ts, source=wid, origin=Origin.PICKUP,
                 payload=nutzlast)


def _signal(wid: str, typ: str, ts: float, **kw: object) -> Event:
    return Event(type=typ, ts=ts, source=wid, origin=Origin.PICKUP,
                 payload={"task_id": "t1", "worker_id": wid, **kw})


def _reg(tmp_path: Path) -> tuple[DirectoryWriter, WorkerRegistry]:
    con = connect(tmp_path / "v.sqlite3")
    return DirectoryWriter(con), WorkerRegistry(con)


# --- Zyklus 1 -----------------------------------------------------------


def test_stammdaten_legen_einen_worker_an(tmp_path: Path) -> None:
    w, r = _reg(tmp_path)
    w.apply([("1000-0", _meta("phx-w1:0", 100.0))])
    (worker,) = r.all()
    assert worker.worker_id == "phx-w1:0"
    assert worker.group == "reports"
    assert worker.queues == ["reports"]
    assert worker.slot == 0
    assert worker.concurrency == 4


def test_jedes_signal_hebt_das_lebenszeichen(tmp_path: Path) -> None:
    w, r = _reg(tmp_path)
    w.apply([("1000-0", _meta("phx-w1:0", 100.0))])
    w.apply([("2000-0", _signal("phx-w1:0", "task_completed", 150.0, duration_ms=1.0))])
    (worker,) = r.all()
    assert worker.last_seen_at == 150.0
    # Ein gewoehnliches Task-Signal traegt keine Gruppe. Sie deswegen zu
    # leeren hiesse, den Worker nach seinem ersten Task aus seiner Gruppe zu
    # verlieren -- und die Uebersicht gruppiert danach.
    assert worker.group == "reports", "die Stammdaten wurden vom Signal geleert"
    assert worker.queues == ["reports"]
    assert worker.concurrency == 4


def test_das_lebenszeichen_wird_nur_gehoben(tmp_path: Path) -> None:
    """Die Puffer der Hooks leeren unterschiedlich schnell -- ein aelteres
    Signal trifft also regelmaessig nach einem juengeren ein. Senkte es das
    Lebenszeichen, meldete die Uebersicht einen Ausfall, den es nicht gibt.
    """
    w, r = _reg(tmp_path)
    w.apply([("1000-0", _signal("phx-w1:0", "task_completed", 150.0, duration_ms=1.0))])
    w.apply([("2000-0", _signal("phx-w1:0", "task_started", 100.0))])   # verspaetet
    assert r.get("phx-w1:0").last_seen_at == 150.0


def test_ein_worker_den_nur_signale_kennen(tmp_path: Path) -> None:
    """Ein Worker ohne Hook-Stammdaten -- etwa weil der Hook erst spaeter
    dazukam -- darf nicht unsichtbar sein. Er erscheint mit der schwaechsten
    Quelle, und die Ansicht sagt das.
    """
    w, r = _reg(tmp_path)
    w.apply([("1000-0", _signal("phx-w9:0", "task_started", 100.0))])
    (worker,) = r.all()
    assert worker.worker_id == "phx-w9:0"
    assert worker.source is Quelle.SIGNAL
    assert worker.group is None, "eine geratene Gruppe waere eine Falschaussage"


def test_ein_ereignis_ohne_worker_legt_keinen_an(tmp_path: Path) -> None:
    w, r = _reg(tmp_path)
    w.apply([(None, Event(type="task_enqueue", ts=100.0, source="app",
                          origin=Origin.ENQUEUE,
                          payload={"task_id": "t1", "task_name": "x"}))])
    assert r.all() == []


# --- Zyklus 2 -----------------------------------------------------------


def test_die_beste_quelle_gewinnt(tmp_path: Path) -> None:
    """KTD10. Das Lebenszeichen setzt einen Metrik-Port und ein optionales
    Paket voraus und schweigt sonst **ohne Fehlermeldung** -- die Ansicht muss
    darum wissen, worauf ihre Aussage beruht.
    """
    w, r = _reg(tmp_path)
    w.apply([("1000-0", _signal("phx-w1:0", "task_started", 100.0))])
    assert r.get("phx-w1:0").source is Quelle.SIGNAL

    w.apply([("2000-0", Event(type="worker_heartbeat", ts=110.0, source="phx-w1:0",
                              origin=Origin.PICKUP, payload={"worker_id": "phx-w1:0"}))])
    assert r.get("phx-w1:0").source is Quelle.HEARTBEAT


def test_eine_schwaechere_quelle_senkt_nicht(tmp_path: Path) -> None:
    """Sonst faellt die Ansicht nach jedem gewoehnlichen Signal wieder auf die
    schwaechste Aussage zurueck -- und meldet sich als eingeschraenkt, obwohl
    das Lebenszeichen laeuft.
    """
    w, r = _reg(tmp_path)
    w.apply([("1000-0", Event(type="worker_heartbeat", ts=100.0, source="phx-w1:0",
                              origin=Origin.PICKUP, payload={"worker_id": "phx-w1:0"}))])
    w.apply([("2000-0", _signal("phx-w1:0", "task_started", 110.0))])
    assert r.get("phx-w1:0").source is Quelle.HEARTBEAT


def test_die_frist_haengt_an_der_quelle(tmp_path: Path) -> None:
    """Vierzig Sekunden sind bei einer Rueckmeldung alle fuenf Sekunden ein
    Ausfall -- beim letzten Signal eines ruhigen Workers voellig normal.
    """
    assert SUPERVISOR_STALE_S < SIGNAL_STALE_S
    assert HEARTBEAT_STALE_S < SIGNAL_STALE_S

    w, r = _reg(tmp_path)
    w.apply([("1000-0", _signal("phx-w1:0", "task_started", 100.0))])
    assert r.get("phx-w1:0").connected(now=100.0 + SIGNAL_STALE_S - 1) is True
    assert r.get("phx-w1:0").connected(now=100.0 + SIGNAL_STALE_S + 1) is False

    w.apply([("2000-0", Event(type="worker_heartbeat", ts=200.0, source="phx-w2:0",
                              origin=Origin.PICKUP, payload={"worker_id": "phx-w2:0"}))])
    assert r.get("phx-w2:0").connected(now=200.0 + HEARTBEAT_STALE_S + 1) is False


def test_ein_zurueckkehrender_worker_gilt_wieder_als_verbunden(tmp_path: Path) -> None:
    w, r = _reg(tmp_path)
    w.apply([("1000-0", _meta("phx-w1:0", 100.0))])
    assert r.get("phx-w1:0").connected(now=100.0 + SIGNAL_STALE_S + 1) is False
    w.apply([("2000-0", _signal("phx-w1:0", "task_started", 9_000.0))])
    assert r.get("phx-w1:0").connected(now=9_001.0) is True


# --- Zyklus 3 -----------------------------------------------------------


def test_worker_kommen_nach_gruppe_gebuendelt(tmp_path: Path) -> None:
    w, r = _reg(tmp_path)
    w.apply([
        ("1000-0", _meta("phx-w1:0", 100.0, gruppe="reports")),
        ("2000-0", _meta("phx-w2:0", 100.0, gruppe="reports")),
        ("3000-0", _meta("phx-b1:0", 100.0, gruppe="billing", queues=["billing"])),
        ("4000-0", _signal("fremd:0", "task_started", 100.0)),
    ])
    gruppen = r.by_group()
    assert sorted(gruppen) == ["billing", "ohne Gruppe", "reports"]
    assert [x.worker_id for x in gruppen["reports"]] == ["phx-w1:0", "phx-w2:0"]
    assert [x.worker_id for x in gruppen["ohne Gruppe"]] == ["fremd:0"]


def test_entfernen_loescht_und_rueckkehr_legt_neu_an(tmp_path: Path) -> None:
    """R19. Entfernen ist eine Aufraeumhandlung, kein Bann -- ein Worker, der
    zurueckkommt, muss wieder erscheinen, ohne dass jemand etwas tut.
    """
    w, r = _reg(tmp_path)
    w.apply([("1000-0", _meta("phx-w1:0", 100.0))])
    assert r.remove("phx-w1:0") is True
    assert r.all() == []
    assert r.remove("phx-w1:0") is False, "ein zweites Entfernen ist kein Treffer"

    w.apply([("2000-0", _signal("phx-w1:0", "task_started", 200.0))])
    assert [x.worker_id for x in r.all()] == ["phx-w1:0"]
