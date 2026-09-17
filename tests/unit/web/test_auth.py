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

=== ZYKLUS 3: Befunde aus der Pruefung von Stufe C ===
14. Auch sehr hohe Zaehlerstaende werfen nicht und sperren nicht.
15. Die Obergrenze bleibt im Ertraeglichen -- gegen eine feste Zahl geprueft.
16. Die Verzoegerung verdoppelt sich wirklich.
17. Verfallene Zaehler bremsen nicht mehr und werden weggeraeumt.
18. Der Speicher nimmt nur selbst erzeugte Kennungen an.
19. Eine leere Sitzung ist keine Zeile wert.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from argon2 import PasswordHasher

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.web.auth import (
    ATTEMPT_TTL_S,
    MAX_DELAY_S,
    LoginThrottle,
    SqliteStore,
)

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)


def _kennung(n: int = 0) -> str:
    """Eine Kennung, wie der Rahmen sie erzeugt: 32 Bytes als Hex.

    Phantasienamen wie ``_kennung(1)`` prueften eine Form, die im Betrieb nie
    vorkommt -- und liessen den Speicher Schluessel annehmen, die aus dem
    Cookie des Klienten stammen und damit fremdbestimmt sind.
    """
    return f"{n:064x}"


@pytest.fixture
def pfad(tmp_path: Path) -> Path:
    AccountStore(tmp_path / "accounts.sqlite3", hasher=_BILLIG).close()
    return tmp_path / "accounts.sqlite3"


# --- Zyklus 1 -----------------------------------------------------------


async def test_geschriebenes_kommt_zurueck(pfad: Path) -> None:
    s = SqliteStore(pfad)
    await s.set(_kennung(1), b"nutzlast", expires_in=60)
    assert await s.get(_kennung(1)) == b"nutzlast"
    assert await s.get(_kennung(99)) is None
    assert await s.exists(_kennung(1)) is True


async def test_abgelaufene_sitzung_gilt_nicht(pfad: Path) -> None:
    """Eine abgelaufene Sitzung auszuliefern hiesse, die Inaktivitaetsfrist
    aus R21 nicht einzuhalten -- und zwar unsichtbar.
    """
    jetzt = [1000.0]
    s = SqliteStore(pfad, clock=lambda: jetzt[0])
    await s.set(_kennung(1), b"nutzlast", expires_in=60)
    jetzt[0] = 1059.0
    assert await s.get(_kennung(1)) == b"nutzlast"
    jetzt[0] = 1061.0
    assert await s.get(_kennung(1)) is None, "die abgelaufene Sitzung wurde ausgeliefert"
    assert await s.exists(_kennung(1)) is False


async def test_loeschen_wirkt_sofort(pfad: Path) -> None:
    s = SqliteStore(pfad)
    await s.set(_kennung(1), b"x", expires_in=60)
    await s.delete(_kennung(1))
    assert await s.get(_kennung(1)) is None


async def test_verlaengern_aber_nicht_bei_jedem_zugriff(pfad: Path) -> None:
    """Sonst ist jede Anfrage ein Schreibvorgang, und die Schreiber reihen
    sich auf -- bei einer Ansicht mit Live-Verbindung jede Sekunde einer.
    """
    jetzt = [1000.0]
    s = SqliteStore(pfad, clock=lambda: jetzt[0], renew_every_s=60.0)
    await s.set(_kennung(1), b"x", expires_in=600)

    jetzt[0] = 1010.0
    await s.get(_kennung(1), renew_for=600)
    assert await s.expires_in(_kennung(1)) == 590, "zu frueh verlaengert"

    jetzt[0] = 1070.0
    await s.get(_kennung(1), renew_for=600)
    assert await s.expires_in(_kennung(1)) == 600, "nach der Frist wurde nicht verlaengert"


async def test_der_speicher_liegt_in_der_datei(pfad: Path) -> None:
    """KTD8: nicht im Arbeitsspeicher und nicht in Valkey. Ein
    prozesslokaler Speicher meldete jede Sitzung nach einem Neustart als
    abgelaufen -- und bei Valkey verschwaende sie beim Ausfall, fuer den es
    das Dashboard gibt.
    """
    s = SqliteStore(pfad)
    await s.set(_kennung(1), b"nutzlast", expires_in=600)
    del s
    zweiter = SqliteStore(pfad)
    assert await zweiter.get(_kennung(1)) == b"nutzlast", "die Sitzung ueberlebte den Neustart nicht"


async def test_abgelaufene_zeilen_werden_aufgeraeumt(pfad: Path) -> None:
    jetzt = [1000.0]
    s = SqliteStore(pfad, clock=lambda: jetzt[0])
    for i in range(5):
        await s.set(_kennung(i), b"x", expires_in=10)
    jetzt[0] = 2000.0
    await s.set(_kennung(50), b"x", expires_in=600)
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


def test_die_verzoegerung_wird_auch_bei_sehr_hohen_staenden_nicht_zur_sperre(pfad: Path) -> None:
    """R22 woertlich: "die Verzoegerung wird nie zur Sperre".

    Die Potenz wurde als ``int`` gebildet, **bevor** gedeckelt wurde. Ab 1027
    Fehlversuchen passte ``2 ** 1024`` nicht mehr in ein ``float``, und der
    ``OverflowError`` traf den Handler *vor* der Passwortpruefung: die
    Anmeldung war ein HTTP 500, auch mit dem richtigen Passwort, und wegen des
    Herkunftszaehlers fuer die ganze Herkunft. Ausgerechnet die Zeile, die
    "nie eine Sperre" sichern soll, war die Sperre.
    """
    import time as _t

    b = LoginThrottle(pfad)
    for stand in (10, 1000, 1026, 1027, 5000, 100_000):
        b._con.execute(
            "insert into login_attempt(scope,key,count,last_at) values ('account','ops',?,?) "
            "on conflict(scope,key) do update set count=excluded.count, last_at=excluded.last_at",
            (stand, _t.time()),
        )
        b._con.commit()
        v = b.delay_for("ops", "10.0.0.1")     # darf nie werfen
        assert 0.0 <= v <= MAX_DELAY_S, f"bei count={stand}: {v} s"


def test_die_obergrenze_bleibt_im_ertraeglichen(pfad: Path) -> None:
    """Gegen einen festen Wert, nicht gegen die Konstante selbst -- sonst
    wandert die Schranke mit, und zehn Minuten Verzoegerung gaelten als
    "Bremse". Zehn Minuten sind die Sperre, die R22 ausschliesst.
    """
    assert MAX_DELAY_S <= 30.0, f"{MAX_DELAY_S} s sind keine Bremse mehr, sondern eine Sperre"


def test_die_verzoegerung_verdoppelt_sich_wirklich(pfad: Path) -> None:
    """Eine feste Verzoegerung erfuellte die alte Zusicherung auch -- sie
    verglich nur den letzten Wert mit dem zweiten, und der ist wegen der
    Freiversuche immer null.
    """
    b = LoginThrottle(pfad)
    gesehen = []
    for _ in range(7):
        b.record_failure("ops", "10.0.0.1")
        gesehen.append(round(b.delay_for("ops", "10.0.0.1"), 3))
    assert gesehen == [0.0, 0.0, 0.25, 0.5, 1.0, 2.0, 4.0], gesehen


def test_verfallene_zaehler_bremsen_nicht_mehr(pfad: Path) -> None:
    """Ohne Verfall bliebe ein Teammitglied, das gestern fuenfmal danebentippte,
    fuer immer gebremst. Der Zweig war ueberhaupt nicht erreichbar: kein Test
    der Bremse uebergab je eine Uhr.
    """
    jetzt = [1000.0]
    b = LoginThrottle(pfad, clock=lambda: jetzt[0])
    for _ in range(5):
        b.record_failure("ops", "10.0.0.1")
    assert b.delay_for("ops", "10.0.0.1") > 0

    jetzt[0] = 1000.0 + ATTEMPT_TTL_S + 1
    assert b.delay_for("ops", "10.0.0.1") == 0.0, "der Zaehler verfaellt nie"
    b.record_failure("ops", "10.0.0.1")
    assert b.delay_for("ops", "10.0.0.1") == 0.0, "nach dem Verfall wurde nicht zurueckgesetzt"


def test_verfallene_zeilen_werden_weggeraeumt(pfad: Path) -> None:
    jetzt = [1000.0]
    b = LoginThrottle(pfad, clock=lambda: jetzt[0])
    for i in range(5):
        b.record_failure(f"konto{i}", "10.0.0.1")
    jetzt[0] = 1000.0 + ATTEMPT_TTL_S + 1
    b.record_failure("frisch", "10.0.0.2")
    b.purge()
    uebrig = b._con.execute("select count(*) from login_attempt").fetchone()[0]
    assert uebrig == 2, f"{uebrig} Zeilen uebrig -- die verfallenen blieben stehen"


def test_der_speicher_nimmt_nur_eigene_kennungen_an(pfad: Path) -> None:
    """Der Schluessel kommt aus dem Cookie des Klienten und ist damit
    fremdbestimmt. Gemessen landeten Schluessel mit zweitausend Zeichen in der
    Kontendatei -- der Datei, die laut KTD8 nicht geloescht werden darf.
    """
    import asyncio

    async def lauf() -> None:
        s = SqliteStore(pfad)
        for fremd in ("ANGREIFER-" + "x" * 2000, "kurz", "", "../../etc/passwd", "A" * 64):
            await s.set(fremd, b'{"account":"ops"}', expires_in=600)
            assert await s.get(fremd) is None, f"{fremd[:20]!r} wurde angenommen"
        uebrig = s._con.execute("select count(*) from session_store").fetchone()[0]
        assert uebrig == 0, f"{uebrig} fremdbestimmte Zeilen gespeichert"

    asyncio.run(lauf())


def test_eine_leere_sitzung_ist_keine_zeile_wert(pfad: Path) -> None:
    import asyncio

    async def lauf() -> None:
        s = SqliteStore(pfad)
        await s.set(_kennung(7), b"{}", expires_in=600)
        uebrig = s._con.execute("select count(*) from session_store").fetchone()[0]
        assert uebrig == 0, "eine leere Sitzung hat eine Zeile erzeugt"

    asyncio.run(lauf())


def test_die_bremse_ist_aus_mehreren_faden_benutzbar(pfad: Path) -> None:
    """Die Anmeldung gehoert in einen Arbeitsfaden -- und dort ist jede
    Anfrage moeglicherweise ein anderer.

    Gemessen brachen ohne Riegel unter sechzehn Faden fuenfzehn mit
    "bad parameter or other API misuse" ab, und der Zaehlerstand war danach
    falsch: eine Bremse, die still nicht zaehlt. Die Behebung der
    Loop-Blockade loest genau diesen Schaden aus -- der Riegel muss zuerst da
    sein.
    """
    import threading as _th

    b = LoginThrottle(pfad)
    fehler: list[str] = []

    def arbeiten() -> None:
        try:
            for _ in range(25):
                b.record_failure("ops", "10.0.0.1")
                b.delay_for("ops", "10.0.0.1")
        except Exception as exc:
            fehler.append(f"{type(exc).__name__}: {exc}")

    faeden = [_th.Thread(target=arbeiten) for _ in range(8)]
    for f in faeden:
        f.start()
    for f in faeden:
        f.join()
    assert not fehler, f"{len(fehler)} von 8 Faden brachen ab: {fehler[0]}"
    stand = b._con.execute(
        "select count from login_attempt where scope='account' and key='ops'"
    ).fetchone()[0]
    assert stand == 200, f"gezaehlt wurden {stand} von 200 Versuchen"


def test_der_sitzungsspeicher_ist_aus_mehreren_faden_benutzbar(pfad: Path) -> None:
    import asyncio
    import threading as _th

    s = SqliteStore(pfad)
    fehler: list[str] = []

    def arbeiten(n: int) -> None:
        try:
            for i in range(25):
                asyncio.run(s.set(_kennung(n * 100 + i), b'{"account":"ops"}', expires_in=600))
        except Exception as exc:
            fehler.append(f"{type(exc).__name__}: {exc}")

    faeden = [_th.Thread(target=arbeiten, args=(n,)) for n in range(8)]
    for f in faeden:
        f.start()
    for f in faeden:
        f.join()
    assert not fehler, f"{len(fehler)} von 8 Faden brachen ab: {fehler[0]}"
    zahl = s._con.execute("select count(*) from session_store").fetchone()[0]
    assert zahl == 200, f"{zahl} von 200 Zeilen geschrieben"
