"""Sitzungen und Anmeldebremse -- in der eigenen Datei neben den Konten.

Zwei Zusagen tragen diese Einheit, und beide haben denselben Grund (KTD8):

* **Die Anmeldung muss gehen, wenn Valkey weg ist.** Genau dann braucht
  jemand das Dashboard. Laegen Sitzungen oder Bremszaehler in Valkey,
  verschwaende beides bei dem einen Ausfall, fuer den es das Werkzeug gibt.
* **Die Bremse wird nie zur Sperre** (R22). Sie verteuert Fehlversuche, aber
  wer das richtige Passwort hat, kommt herein -- sonst sperrt ein Fremder ein
  Team aus, indem er dessen Konten falsch anmeldet.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Sitzungsspeicher ===
1. Was geschrieben wurde, kommt zurueck; was nicht geschrieben wurde, nicht.
2. Eine abgelaufene Sitzung gilt nicht mehr und wird nicht ausgeliefert.
3. Loeschen macht sie sofort ungueltig.
4. Die Frist laesst sich verlaengern -- aber nicht bei jedem Zugriff.
5. Der Speicher liegt in der Kontendatei, nicht im Arbeitsspeicher.
6. Abgelaufene Zeilen werden aufgeraeumt, statt die Datei zu fuellen.

=== ZYKLUS 2: Anmeldebremse (R22) ===
7. Der erste Fehlversuch kostet keine Verzoegerung.
8. Weitere Fehlversuche verzoegern wachsend.
9. Die Verzoegerung hat eine Obergrenze -- sie wird nie zur Sperre.
10. Ein gelungener Versuch setzt den Zaehler des Kontos zurueck.
11. Fehlversuche einer Herkunft bremsen dieselbe Herkunft auch fuer andere
    Konten.
12. Covers AE9a. Ein unbekanntes Konto wird genauso gebremst wie ein
    bekanntes -- sonst verraet die Bremse, welche Konten es gibt.
13. Die Zaehler ueberleben einen Neustart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from argon2 import PasswordHasher

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.web.auth import MAX_DELAY_S, LoginThrottle, SqliteStore

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)


@pytest.fixture
def pfad(tmp_path: Path) -> Path:
    AccountStore(tmp_path / "accounts.sqlite3", hasher=_BILLIG).close()
    return tmp_path / "accounts.sqlite3"


# --- Zyklus 1 -----------------------------------------------------------


async def test_geschriebenes_kommt_zurueck(pfad: Path) -> None:
    s = SqliteStore(pfad)
    await s.set("abc", b"nutzlast", expires_in=60)
    assert await s.get("abc") == b"nutzlast"
    assert await s.get("gibtesnicht") is None
    assert await s.exists("abc") is True


async def test_abgelaufene_sitzung_gilt_nicht(pfad: Path) -> None:
    """Eine abgelaufene Sitzung auszuliefern hiesse, die Inaktivitaetsfrist
    aus R21 nicht einzuhalten -- und zwar unsichtbar.
    """
    jetzt = [1000.0]
    s = SqliteStore(pfad, clock=lambda: jetzt[0])
    await s.set("abc", b"nutzlast", expires_in=60)
    jetzt[0] = 1059.0
    assert await s.get("abc") == b"nutzlast"
    jetzt[0] = 1061.0
    assert await s.get("abc") is None, "die abgelaufene Sitzung wurde ausgeliefert"
    assert await s.exists("abc") is False


async def test_loeschen_wirkt_sofort(pfad: Path) -> None:
    s = SqliteStore(pfad)
    await s.set("abc", b"x", expires_in=60)
    await s.delete("abc")
    assert await s.get("abc") is None


async def test_verlaengern_aber_nicht_bei_jedem_zugriff(pfad: Path) -> None:
    """Sonst ist jede Anfrage ein Schreibvorgang, und die Schreiber reihen
    sich auf -- bei einer Ansicht mit Live-Verbindung jede Sekunde einer.
    """
    jetzt = [1000.0]
    s = SqliteStore(pfad, clock=lambda: jetzt[0], renew_every_s=60.0)
    await s.set("abc", b"x", expires_in=600)

    jetzt[0] = 1010.0
    await s.get("abc", renew_for=600)
    assert await s.expires_in("abc") == 590, "zu frueh verlaengert"

    jetzt[0] = 1070.0
    await s.get("abc", renew_for=600)
    assert await s.expires_in("abc") == 600, "nach der Frist wurde nicht verlaengert"


async def test_der_speicher_liegt_in_der_datei(pfad: Path) -> None:
    """KTD8: nicht im Arbeitsspeicher und nicht in Valkey. Ein
    prozesslokaler Speicher meldete jede Sitzung nach einem Neustart als
    abgelaufen -- und bei Valkey verschwaende sie beim Ausfall, fuer den es
    das Dashboard gibt.
    """
    s = SqliteStore(pfad)
    await s.set("abc", b"nutzlast", expires_in=600)
    del s
    zweiter = SqliteStore(pfad)
    assert await zweiter.get("abc") == b"nutzlast", "die Sitzung ueberlebte den Neustart nicht"


async def test_abgelaufene_zeilen_werden_aufgeraeumt(pfad: Path) -> None:
    jetzt = [1000.0]
    s = SqliteStore(pfad, clock=lambda: jetzt[0])
    for i in range(5):
        await s.set(f"k{i}", b"x", expires_in=10)
    jetzt[0] = 2000.0
    await s.set("frisch", b"x", expires_in=600)
    await s.purge()
    uebrig = s._con.execute("select count(*) from session_store").fetchone()[0]
    assert uebrig == 1, f"{uebrig} Zeilen uebrig -- die abgelaufenen blieben stehen"


# --- Zyklus 2 -----------------------------------------------------------


async def test_erster_fehlversuch_kostet_nichts(pfad: Path) -> None:
    b = LoginThrottle(pfad)
    assert b.delay_for("ops", "10.0.0.1") == 0.0
    b.record_failure("ops", "10.0.0.1")
    assert b.delay_for("ops", "10.0.0.1") == 0.0, "schon der erste Versuch wurde bestraft"


async def test_weitere_fehlversuche_verzoegern_wachsend(pfad: Path) -> None:
    b = LoginThrottle(pfad)
    vorher = 0.0
    gesehen = []
    for _ in range(6):
        b.record_failure("ops", "10.0.0.1")
        jetzt = b.delay_for("ops", "10.0.0.1")
        gesehen.append(jetzt)
        assert jetzt >= vorher, f"die Verzoegerung sank: {gesehen}"
        vorher = jetzt
    assert gesehen[-1] > gesehen[1], f"die Verzoegerung waechst nicht: {gesehen}"


async def test_die_verzoegerung_wird_nie_zur_sperre(pfad: Path) -> None:
    """R22 ausdruecklich: "die Verzoegerung wird nie zur Sperre". Ohne
    Obergrenze sperrt ein Fremder ein Team aus, indem er dessen Konten
    falsch anmeldet.
    """
    b = LoginThrottle(pfad)
    for _ in range(200):
        b.record_failure("ops", "10.0.0.1")
    assert b.delay_for("ops", "10.0.0.1") <= MAX_DELAY_S, (
        f"die Verzoegerung stieg auf {b.delay_for('ops', '10.0.0.1')} s"
    )


async def test_gelungener_versuch_setzt_zurueck(pfad: Path) -> None:
    b = LoginThrottle(pfad)
    for _ in range(5):
        b.record_failure("ops", "10.0.0.1")
    assert b.delay_for("ops", "10.0.0.1") > 0
    b.record_success("ops", "10.0.0.1")
    assert b.delay_for("ops", "10.0.0.1") == 0.0, "nach gelungener Anmeldung bremst es weiter"


async def test_herkunft_bremst_auch_andere_konten(pfad: Path) -> None:
    """Sonst probiert derselbe Rechner einfach das naechste Konto durch."""
    b = LoginThrottle(pfad)
    for _ in range(5):
        b.record_failure("ops", "10.0.0.1")
    assert b.delay_for("anderes-konto", "10.0.0.1") > 0, (
        "eine gebremste Herkunft kommt ueber ein anderes Konto ungebremst durch"
    )
    assert b.delay_for("ops", "10.0.0.2") > 0, "das Konto selbst ist von anderswo ungebremst"


async def test_unbekanntes_konto_wird_genauso_gebremst(pfad: Path) -> None:
    """Covers AE9a. Bremste nur ein bekanntes Konto, verriete die Bremse,
    welche Konten es gibt -- dieselbe Aufzaehlung, die die Pruefung in U8
    verhindert.
    """
    b = LoginThrottle(pfad)
    for _ in range(5):
        b.record_failure("gibtesnicht", "10.0.0.1")
    assert b.delay_for("gibtesnicht", "10.0.0.1") > 0


async def test_die_zaehler_ueberleben_einen_neustart(pfad: Path) -> None:
    """Laegen sie im Arbeitsspeicher, waere die Bremse nach einem Neustart
    weg -- und ein Neustart ist im Vorfall das Naheliegendste.
    """
    b = LoginThrottle(pfad)
    for _ in range(5):
        b.record_failure("ops", "10.0.0.1")
    erwartet = b.delay_for("ops", "10.0.0.1")
    del b
    zweiter = LoginThrottle(pfad)
    assert zweiter.delay_for("ops", "10.0.0.1") == erwartet
