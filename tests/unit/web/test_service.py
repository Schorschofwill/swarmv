"""Dienstgeruest: haelt es aus, wenn eine Voraussetzung fehlt, und sagt es.

Der Kern von R33: der Dienst faehrt hoch, **auch** wenn Broker oder Backend
fehlen, und benennt die Ursache. Ein Dienst, der beim Fehlen eines Dienstes
gar nicht startet, ist im Vorfall genau dann weg, wenn man ihn braucht -- und
das ist der Vorfall, fuer den dieses Dashboard existiert.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Hochfahren trotz fehlender Dienste (R33) ===
1. Ohne erreichbaren Strom faehrt der Dienst hoch und nennt den Grund.
2. Ohne erreichbaren Broker ebenso.
3. Ohne beides ebenso -- und das Verzeichnis steht trotzdem.
4. Ein Fehlschlag beim Hochfahren wirft nie aus dem Lebenszyklus heraus.

=== ZYKLUS 2: Zustandsbericht (R26, R9) ===
5. Der Bericht nennt Broker, Backend und Strom je als erreichbar oder nicht.
6. Er nennt Fuellstand und wirksame Grenze des Stroms (R9).
7. Er nennt den Zeitpunkt des zuletzt empfangenen Signals und ob der Strom
   verzoegert ist.
8. Er weist einen Rueckstand des Lesers hinter dem Laengendeckel aus.
9. Er weist verworfene Ereignisse, unlesbare Eintraege und ein offenes
   Loeschversprechen aus -- bindender Punkt aus der Pruefung von Stufe B.

=== ZYKLUS 3: Rueckkehr und Herunterfahren ===
10. Kehrt ein Dienst zurueck, wechselt die Anzeige ohne Neustart.
11. Das Herunterfahren schliesst auch das, was nie hochkam.
12. Die Zustandsseite antwortet, auch wenn alles fehlt.
"""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest

from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.web.app import Services, create_app


def _erreichbar(wirt: str, port: int, frist: float = 0.5) -> bool:
    try:
        with socket.create_connection((wirt, port), timeout=frist):
            return True
    except OSError:
        return False


def _toter_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _config(tmp_path: Path, *, broker: str | None = None, strom: str | None = None) -> DashboardConfig:
    tot = _toter_port()
    return DashboardConfig(
        broker_url=broker or f"amqp://guest:guest@127.0.0.1:{tot}/",
        backend_url=strom or f"valkey://127.0.0.1:{tot}/0",
        stream_url=strom or f"valkey://127.0.0.1:{tot}/1",
        retention_days=7,
        unconfirmed_after_s=15.0,
        disconnected_after_s=40.0,
        session_idle_s=1800.0,
        backlog_cap=300,
        stream_stale_after_s=30.0,
        data_dir=tmp_path / "sqd-data",
    )


# --- Zyklus 1 -----------------------------------------------------------


async def test_faehrt_hoch_ohne_strom_und_nennt_den_grund(tmp_path: Path) -> None:
    """Covers R33."""
    s = Services(_config(tmp_path))
    await s.startup()
    try:
        zustand = s.status()
        assert zustand["stream"]["ok"] is False
        assert zustand["stream"]["reason"], "der Grund fehlt -- eine leere Anzeige erklaert nichts"
        assert zustand["directory"] is not None, "das Verzeichnis muss trotzdem stehen"
    finally:
        await s.shutdown()


async def test_faehrt_hoch_ohne_broker_und_nennt_den_grund(tmp_path: Path) -> None:
    s = Services(_config(tmp_path))
    await s.startup()
    try:
        zustand = s.status()
        assert zustand["broker"]["ok"] is False
        assert zustand["broker"]["reason"]
    finally:
        await s.shutdown()


async def test_hochfahren_wirft_nie(tmp_path: Path) -> None:
    """Ein Lebenszyklus, der wirft, laesst den Dienst gar nicht erst starten --
    und dann ist er im Vorfall weg, fuer den es ihn gibt.
    """
    s = Services(_config(tmp_path))
    await s.startup()          # darf nicht werfen
    await s.startup()          # auch ein zweites Mal nicht
    await s.shutdown()


# --- Zyklus 2 -----------------------------------------------------------


async def test_bericht_nennt_alle_drei_teile(tmp_path: Path) -> None:
    """Covers R26."""
    s = Services(_config(tmp_path))
    await s.startup()
    try:
        zustand = s.status()
        for teil in ("broker", "backend", "stream"):
            assert teil in zustand, f"{teil} fehlt im Bericht"
            assert "ok" in zustand[teil] and "reason" in zustand[teil]
    finally:
        await s.shutdown()


async def test_bericht_nennt_die_wirksame_grenze(tmp_path: Path) -> None:
    """Covers R9. Die angezeigte Grenze muss die wirksame sein -- eine Zahl
    aus der Einstellung, die niemand anwendet, waere schlimmer als keine.
    """
    s = Services(_config(tmp_path))
    await s.startup()
    try:
        assert s.status()["stream"]["retention_days"] == 7
    finally:
        await s.shutdown()


async def test_bericht_weist_die_zaehler_des_lesers_aus(tmp_path: Path) -> None:
    """Bindender Punkt aus der Pruefung von Stufe B: verworfene Ereignisse,
    unlesbare Stromeintraege und ein noch offenes Loeschversprechen (R7a)
    gehoeren in die Kopfzeile. Still zu bleiben hiesse, eine Luecke als
    Vollstaendigkeit auszugeben.
    """
    s = Services(_config(tmp_path))
    await s.startup()
    try:
        v = s.status()["directory"]
        for feld in ("rebuilding", "behind", "rejected", "dropped", "erasure_pending",
                     "oldest_entry"):
            assert feld in v, f"{feld} fehlt im Verzeichnis-Bericht"
    finally:
        await s.shutdown()


async def test_verzoegerter_strom_wird_als_solcher_gemeldet(tmp_path: Path) -> None:
    """R26: "aktuell oder verzoegert, mit dem Zeitpunkt des zuletzt
    empfangenen Signals". Ohne die Angabe liest sich ein stehender Strom wie
    ein ruhiges System.
    """
    s = Services(_config(tmp_path))
    await s.startup()
    try:
        # Noch nie ein Signal gesehen -- das ist nicht "aktuell".
        assert s.status()["stream"]["stale"] is True
        assert s.status()["stream"]["last_event_at"] is None

        s.services_directory_writer.apply([(None, _ereignis(1000.0))])
        assert s.status(now=1010.0)["stream"]["stale"] is False
        assert s.status(now=1010.0)["stream"]["last_event_at"] == 1000.0
        assert s.status(now=9000.0)["stream"]["stale"] is True, (
            "nach der Frist muss der Strom als verzoegert gelten"
        )
    finally:
        await s.shutdown()


# --- Zyklus 3 -----------------------------------------------------------


async def test_rueckkehr_ohne_neustart(tmp_path: Path) -> None:
    """Kehrt ein Dienst zurueck, muss die Anzeige umspringen -- sonst startet
    der Betreiber das Dashboard neu, weil es faelschlich "weg" sagt.
    """
    if not _erreichbar("127.0.0.1", 6399):
        pytest.skip("Valkey laeuft nicht -- dieser Test braucht den echten Dienst")

    s = Services(_config(tmp_path))
    await s.startup()
    try:
        assert s.status()["stream"]["ok"] is False
        s._stream_url = "valkey://127.0.0.1:6399/1"    # der Dienst ist zurueck
        await s.refresh()
        zustand = s.status()
        # Die Ausweichklausel steht **oben**, vor dem Aufbau: haenge sie
        # hierher, verdeckte sie genau den Fehler, den dieser Test sucht --
        # ein Dienst, der die Rueckkehr nie bemerkt, saehe aus wie ein Valkey,
        # das nicht laeuft, und der Test uebersprunge sich selbst.
        assert zustand["stream"]["ok"] is True, "die Rueckkehr wurde nicht bemerkt"
        assert zustand["stream"]["reason"] is None, "der alte Grund steht noch"
    finally:
        await s.shutdown()


async def test_herunterfahren_auch_wenn_nichts_hochkam(tmp_path: Path) -> None:
    s = Services(_config(tmp_path))
    await s.startup()
    await s.shutdown()
    await s.shutdown()     # zweimal ist erlaubt


async def test_die_zustandsseite_antwortet_auch_ohne_dienste(tmp_path: Path) -> None:
    """Die eine Stelle, die im Vorfall zuerst aufgerufen wird, darf nicht die
    erste sein, die ausfaellt.
    """
    from argon2 import PasswordHasher
    from litestar.testing import AsyncTestClient

    from swarmq_dashboard.accounts import AccountStore

    billig = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
    config = _config(tmp_path)
    konten = AccountStore(config.accounts_file, hasher=billig)
    konten.create("ops", "ein-langes-passwort")
    konten.close()

    # Ueber TLS, weil die Cookies ``Secure`` tragen -- dieselbe Bedingung wie
    # im Betrieb. Und angemeldet: die Zusage ist, dass die Seite **waehrend
    # eines Ausfalls** antwortet, nicht dass sie ohne Sitzung antwortet (R23).
    app = create_app(config, hasher=billig)
    async with AsyncTestClient(app=app, base_url="https://testserver.local") as client:
        seite = await client.get("/login")
        merkmal = re.search(r'name=_csrf_token value="([^"]*)"', seite.text).group(1)  # type: ignore[union-attr]
        await client.post("/login", data={"account": "ops", "password": "ein-langes-passwort",
                                          "_csrf_token": merkmal},
                          follow_redirects=False)
        antwort = await client.get("/api/status")
        assert antwort.status_code == 200
        koerper = antwort.json()
        assert koerper["broker"]["ok"] is False
        assert koerper["stream"]["ok"] is False
        assert koerper["directory"]["rebuilding"] is True


def _ereignis(ts: float) -> object:
    from swarmq_dashboard.events import Event, Origin

    return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
                 payload={"task_id": "a", "task_name": "x", "queue": "q",
                          "args": [], "kwargs": {}})
