"""Anmeldung, Sitzungspflicht und CSRF -- die Verweigerungen zuerst.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Ohne Sitzung geht nichts (R23) ===
1. Covers AE10. Ein Aufruf ohne Sitzung liefert keine Daten und fuehrt keine
   Aktion aus.
2. Die Anmeldeseite und die statischen Dateien sind ausgenommen.
3. Nach der Anmeldung ist derselbe Aufruf erlaubt.

=== ZYKLUS 2: CSRF (R23) ===
4. Eine schreibende Anfrage ohne gueltiges Merkmal wird abgewiesen.
5. Das Merkmal aus einer Sitzung gilt in einer anderen nicht.

=== ZYKLUS 3: Die Cookies tragen ihre Merkmale ===
6. Sitzungs- und CSRF-Cookie tragen 'secure' und strenge Seitenbindung; das
   Sitzungscookie zusaetzlich den Skriptschutz.

=== ZYKLUS 4: Lebensdauer ===
7. Abmelden macht die Sitzung sofort ungueltig.
8. Das Sperren eines Kontos beendet dessen bestehende Sitzungen.

=== ZYKLUS 5: Bremse und Ausfall (R22, KTD8) ===
9. Covers AE9. Nach mehreren Fehlversuchen verzoegert sich die Antwort
   spuerbar, und das Konto bleibt bestehen.
10. Die Anmeldung gelingt auch dann, wenn Valkey nicht erreichbar ist.
11. Auch dann bremst sie nach Fehlversuchen weiter.

=== ZYKLUS 6: Der Sitzungsspeicher ===
12. Ein unbekannter Speichername laesst den Start scheitern, statt still auf
    einen prozesslokalen Speicher zurueckzufallen.

=== ZYKLUS 7: Sitzungsuebernahme ===
13. Eine vor der Anmeldung untergeschobene Kennung gilt danach nicht.
14. Die alte Kennung ist danach wertlos, nicht nur ersetzt.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from litestar.testing import AsyncTestClient

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.config import DashboardConfig
from swarmq_dashboard.web.app import create_app

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)
_PASSWORT = "ein-langes-passwort"


def _toter_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _config(tmp_path: Path) -> DashboardConfig:
    tot = _toter_port()
    return DashboardConfig(
        broker_url=f"amqp://guest:guest@127.0.0.1:{tot}/",
        backend_url=f"valkey://127.0.0.1:{tot}/0",
        stream_url=f"valkey://127.0.0.1:{tot}/1",
        retention_days=7, unconfirmed_after_s=15.0, disconnected_after_s=40.0,
        session_idle_s=1800.0, backlog_cap=300, stream_stale_after_s=120.0,
        data_dir=tmp_path / "sqd-data",
    )


#: Der Testklient muss ueber TLS sprechen, sonst schickt er die Cookies gar
#: nicht zurueck -- sie tragen ``Secure``. Das ist keine Testkruecke, sondern
#: dieselbe Bedingung wie im Betrieb: der Dienst ist ausschliesslich ueber TLS
#: erreichbar, direkt oder hinter einem vorgelagerten Abschluss (U17).
BASIS = "https://testserver.local"


def _klient(config: DashboardConfig) -> AsyncTestClient:
    return AsyncTestClient(app=create_app(config, hasher=_BILLIG), base_url=BASIS)


def _mit_konto(tmp_path: Path) -> DashboardConfig:
    c = _config(tmp_path)
    s = AccountStore(c.accounts_file, hasher=_BILLIG)
    s.create("ops", _PASSWORT)
    s.close()
    return c


async def _anmelden(client: AsyncTestClient, konto: str = "ops",
                    passwort: str = _PASSWORT) -> Any:
    """Wie ein Teammitglied: erst die Seite holen, dann abschicken.

    Das Holen ist nicht Zierrat -- dabei setzt der Rahmen das CSRF-Cookie, und
    ohne es ist jede spaetere schreibende Anfrage abgewiesen. Ein Test, der
    gleich abschickt, prueft eine Reihenfolge, die es im Browser nicht gibt.
    """
    await client.get("/login")
    return await client.post(
        "/login", data={"account": konto, "password": passwort}, follow_redirects=False
    )


# --- Zyklus 1 -----------------------------------------------------------


async def test_ohne_sitzung_keine_daten(tmp_path: Path) -> None:
    """Covers AE10."""
    async with _klient(_mit_konto(tmp_path)) as c:
        antwort = await c.get("/api/status", follow_redirects=False)
        assert antwort.status_code in (302, 303, 401), antwort.status_code
        assert "broker" not in antwort.text, "der Zustandsbericht kam ohne Sitzung heraus"


async def test_anmeldeseite_ist_ausgenommen(tmp_path: Path) -> None:
    async with _klient(_mit_konto(tmp_path)) as c:
        assert (await c.get("/login")).status_code == 200


async def test_nach_der_anmeldung_erlaubt(tmp_path: Path) -> None:
    async with _klient(_mit_konto(tmp_path)) as c:
        await _anmelden(c)
        antwort = await c.get("/api/status")
        assert antwort.status_code == 200
        assert "broker" in antwort.json()


# --- Zyklus 2 -----------------------------------------------------------


async def test_schreiben_ohne_merkmal_wird_abgewiesen(tmp_path: Path) -> None:
    async with _klient(_mit_konto(tmp_path)) as c:
        await _anmelden(c)
        c.cookies.delete("csrftoken")
        antwort = await c.post("/logout")
        assert antwort.status_code == 403, antwort.status_code


async def test_fremdes_merkmal_gilt_nicht(tmp_path: Path) -> None:
    """Sonst holt sich ein Angreifer ein Merkmal aus seiner eigenen Sitzung
    und benutzt es gegen eine fremde.
    """
    config = _mit_konto(tmp_path)
    app = create_app(config, hasher=_BILLIG)
    async with (
        AsyncTestClient(app=app, base_url=BASIS) as eine,
        AsyncTestClient(app=app, base_url=BASIS) as andere,
    ):
        await _anmelden(eine)
        await _anmelden(andere)
        fremdes = andere.cookies.get("csrftoken")
        antwort = await eine.post("/logout", headers={"x-csrftoken": str(fremdes)})
        assert antwort.status_code == 403, "ein fremdes Merkmal wurde angenommen"


# --- Zyklus 3 -----------------------------------------------------------


async def test_die_cookies_tragen_ihre_merkmale(tmp_path: Path) -> None:
    """Der Rahmen setzt nichts davon von selbst: seine Vorgaben sind
    ``secure=False`` und lockere Seitenbindung. Ohne ``secure`` geht das
    Sitzungscookie beim ersten Aufruf ueber eine mitlesbare Leitung.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        # Die Cookies werden nur gesetzt, solange der Klient sie noch nicht
        # hat -- ein spaeterer Aufruf liefert keine ``Set-Cookie``-Zeile mehr.
        erster = await c.get("/login")
        antwort = await c.post(
            "/login", data={"account": "ops", "password": _PASSWORT}, follow_redirects=False
        )
        gesetzt = list(erster.headers.get_list("set-cookie"))
        gesetzt += list(antwort.headers.get_list("set-cookie"))
        assert gesetzt, "es wurde gar kein Cookie gesetzt"
        sitzung = [w for w in gesetzt if w.startswith("session=")]
        merkmal = [w for w in gesetzt if w.startswith("csrftoken=")]
        assert sitzung, f"kein Sitzungscookie: {gesetzt}"
        assert merkmal, f"kein CSRF-Cookie: {gesetzt}"
        for w in sitzung + merkmal:
            klein = w.lower()   # Cookie-Merkmale sind gross-klein-egal
            assert "secure" in klein, f"ohne Secure: {w}"
            assert "samesite=strict" in klein, f"ohne strenge Seitenbindung: {w}"
        assert "httponly" in sitzung[0].lower(), (
            f"das Sitzungscookie ist ueber Skript lesbar: {sitzung[0]}"
        )
        # Das CSRF-Cookie **muss** lesbar bleiben: die Fragmentaufrufe der
        # Oberflaeche holen es sich und schicken es als Kopfzeile zurueck.
        assert "httponly" not in merkmal[0].lower(), (
            "das CSRF-Cookie ist fuer das Skript unlesbar -- dann kann kein "
            "Fragmentaufruf mehr schreiben"
        )


# --- Zyklus 4 -----------------------------------------------------------


async def test_abmelden_wirkt_sofort(tmp_path: Path) -> None:
    async with _klient(_mit_konto(tmp_path)) as c:
        await _anmelden(c)
        merkmal = str(c.cookies.get("csrftoken"))
        assert (await c.post("/logout", headers={"x-csrftoken": merkmal})).status_code in (200, 302)
        antwort = await c.get("/api/status", follow_redirects=False)
        assert antwort.status_code in (302, 303, 401), "die Sitzung galt nach dem Abmelden weiter"


async def test_sperren_beendet_bestehende_sitzungen(tmp_path: Path) -> None:
    """R21. Sperren, das erst bei der naechsten Anmeldung wirkt, ist keine
    Sperre -- wer schon drin ist, bleibt drin.
    """
    config = _mit_konto(tmp_path)
    async with _klient(config) as c:
        await _anmelden(c)
        assert (await c.get("/api/status")).status_code == 200

        s = AccountStore(config.accounts_file, hasher=_BILLIG)
        s.lock("ops")
        s.close()

        antwort = await c.get("/api/status", follow_redirects=False)
        assert antwort.status_code in (302, 303, 401), "die Sitzung des gesperrten Kontos galt weiter"


# --- Zyklus 5 -----------------------------------------------------------


async def test_fehlversuche_verzoegern_und_sperren_nicht(tmp_path: Path) -> None:
    """Covers AE9."""
    config = _mit_konto(tmp_path)
    async with _klient(config) as c:
        for _ in range(5):
            antwort = await _anmelden(c, passwort="falsch")
            assert antwort.status_code in (401, 403, 200)

        beginn = time.perf_counter()
        await _anmelden(c, passwort="auch-falsch")
        gebremst = time.perf_counter() - beginn
        assert gebremst > 0.2, f"nach fuenf Fehlversuchen keine spuerbare Bremse ({gebremst:.3f} s)"

        # Und das Konto lebt: die Bremse ist keine Sperre.
        s = AccountStore(config.accounts_file, hasher=_BILLIG)
        assert s.verify("ops", _PASSWORT) is True, "das Konto wurde gesperrt statt gebremst"
        s.close()


async def test_anmeldung_gelingt_ohne_valkey(tmp_path: Path) -> None:
    """KTD8, der eigentliche Grund fuer die eigene Datei: liegt Valkey, kaeme
    sonst niemand mehr herein -- und das ist genau der Vorfall, fuer den es
    das Dashboard gibt. Die Konfiguration zeigt hier auf tote Ports.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        await _anmelden(c)
        assert (await c.get("/api/status")).status_code == 200


async def test_bremse_wirkt_auch_ohne_valkey(tmp_path: Path) -> None:
    async with _klient(_mit_konto(tmp_path)) as c:
        for _ in range(5):
            await _anmelden(c, passwort="falsch")
        beginn = time.perf_counter()
        await _anmelden(c, passwort="falsch")
        assert time.perf_counter() - beginn > 0.2, "ohne Valkey bremst die Anmeldung nicht"


# --- Zyklus 6 -----------------------------------------------------------


async def test_unbekannter_speichername_laesst_den_start_scheitern(tmp_path: Path) -> None:
    """Litestars Registratur legt fuer einen unbekannten Namen still einen
    prozesslokalen Speicher an. Die Sitzungen laegen dann im Arbeitsspeicher
    statt in der Datei -- und niemand saehe es, bis nach einem Neustart alle
    abgemeldet sind.
    """
    from swarmq_dashboard.web.app import UnknownStore, build_store_registry

    registratur = build_store_registry(tmp_path / "accounts.sqlite3")
    assert registratur.get("sessions") is not None
    with pytest.raises(UnknownStore):
        registratur.get("gibtesnicht")


# --- Zyklus 7: Sitzungsuebernahme ---------------------------------------


async def test_untergeschobene_kennung_gilt_nach_der_anmeldung_nicht(tmp_path: Path) -> None:
    """Wer dem Browser vorher ein ``session``-Cookie unterschiebt, kennt sonst
    die Kennung, unter der das Opfer danach angemeldet ist.

    Nachgemessen: ohne die Erneuerung blieb die untergeschobene Kennung
    unveraendert gueltig. Der Rahmen bietet dafuer nichts an -- dieser Test
    schlaegt an, sobald die selbstgebaute Erneuerung nicht mehr greift, statt
    dass sie still ausfaellt.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        await c.get("/login")
        untergeschoben = "a" * 64
        c.cookies.set("session", untergeschoben, domain="testserver.local")
        await c.post("/login", data={"account": "ops", "password": _PASSWORT},
                     follow_redirects=False)
        danach = str(c.cookies.get("session"))
        assert danach != untergeschoben, (
            "die untergeschobene Sitzungskennung gilt weiter -- Sitzungsuebernahme moeglich"
        )
        assert (await c.get("/api/status")).status_code == 200, "die neue Sitzung gilt nicht"


async def test_die_alte_sitzung_ist_danach_wertlos(tmp_path: Path) -> None:
    """Die Erneuerung genuegt nicht, wenn die alte Kennung weiterlebt.

    Die alte Kennung traegt hier wirklich Daten -- eine erste, gueltige
    Anmeldung. Eine bloss untergeschobene Kennung ohne Daten prueft nichts:
    sie wird ohnehin abgewiesen, und der Test liefe auch dann gruen, wenn die
    alte Sitzung im Speicher stehen bliebe.
    """
    config = _mit_konto(tmp_path)
    app = create_app(config, hasher=_BILLIG)
    async with AsyncTestClient(app=app, base_url=BASIS) as c:
        await c.get("/login")
        await c.post("/login", data={"account": "ops", "password": _PASSWORT},
                     follow_redirects=False)
        erste = str(c.cookies.get("session"))
        assert (await c.get("/api/status")).status_code == 200, "Vorbedingung: sie muss gelten"

        # Noch einmal anmelden -- dieselbe Lage wie nach einem Passwortwechsel
        # oder einem zweiten Anmelden im selben Browser.
        await c.post("/login", data={"account": "ops", "password": _PASSWORT},
                     follow_redirects=False)
        zweite = str(c.cookies.get("session"))
        assert zweite != erste, "die Kennung wurde nicht erneuert"

    async with AsyncTestClient(app=app, base_url=BASIS) as angreifer:
        angreifer.cookies.set("session", erste, domain="testserver.local")
        antwort = await angreifer.get("/api/status", follow_redirects=False)
        assert antwort.status_code in (302, 303, 401), (
            "die alte Sitzung fuehrt weiter zu den Daten -- sie wurde nur ersetzt, "
            "nicht entwertet"
        )
