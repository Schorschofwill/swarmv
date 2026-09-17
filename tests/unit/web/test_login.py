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

=== ZYKLUS 8: Befunde aus der Pruefung von Stufe C ===
15. Ein untergeschobenes Cookie kann kein Skript in die Anmeldeseite bringen.
16. Unangemeldete Anfragen schreiben nicht in die Kontendatei.
17. Die Sitzung wird nicht bei jeder Anfrage neu geschrieben.
18. Ein Merkmal aus einer fremden Sitzung gilt nicht.
19. Die Anmeldung von einer Fremdseite wird abgewiesen.
20. Abmelden nimmt das Merkmal mit.
21. Ohne Kontenspeicher wird abgewiesen, nicht durchgelassen.
"""

from __future__ import annotations

import re
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


def _merkmal_aus(html: str) -> str:
    """Das Merkmal aus dem Formular ziehen, wie ein Browser es abschickt."""
    treffer = re.search(r'name=_csrf_token value="([^"]*)"', html)
    assert treffer, f"das Formular traegt kein Merkmal: {html[:200]}"
    return treffer.group(1)


async def _anmelden(client: AsyncTestClient, konto: str = "ops",
                    passwort: str = _PASSWORT) -> Any:
    """Wie ein Teammitglied: erst die Seite holen, dann abschicken.

    Das Holen ist nicht Zierrat -- dabei entsteht das Merkmal, in Cookie und
    Formularfeld, und ohne beides wird die Anmeldung abgewiesen. Ein Test, der
    gleich abschickt, prueft eine Reihenfolge, die es im Browser nicht gibt.
    """
    seite = await client.get("/login")
    return await client.post(
        "/login",
        data={"account": konto, "password": passwort,
              "_csrf_token": _merkmal_aus(seite.text)},
        follow_redirects=False,
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
            "/login",
            data={"account": "ops", "password": _PASSWORT,
                  "_csrf_token": _merkmal_aus(erster.text)},
            follow_redirects=False,
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
        await _anmelden(c)
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
        await _anmelden(c)
        erste = str(c.cookies.get("session"))
        assert (await c.get("/api/status")).status_code == 200, "Vorbedingung: sie muss gelten"

        # Noch einmal anmelden -- dieselbe Lage wie nach einem Passwortwechsel
        # oder einem zweiten Anmelden im selben Browser.
        await _anmelden(c)
        zweite = str(c.cookies.get("session"))
        assert zweite != erste, "die Kennung wurde nicht erneuert"

    async with AsyncTestClient(app=app, base_url=BASIS) as angreifer:
        angreifer.cookies.set("session", erste, domain="testserver.local")
        antwort = await angreifer.get("/api/status", follow_redirects=False)
        assert antwort.status_code in (302, 303, 401), (
            "die alte Sitzung fuehrt weiter zu den Daten -- sie wurde nur ersetzt, "
            "nicht entwertet"
        )


# --- Zyklus 8: Befunde aus der Pruefung von Stufe C ----------------------


async def test_ein_cookie_kann_kein_skript_einschleusen(tmp_path: Path) -> None:
    """Die Anmeldeseite ist die **einzige** Seite ohne Sitzung, und sie traegt
    ein Passwortfeld.

    Frueher stand dort das Merkmal aus dem Verbindungszustand -- und das ist
    bei sicheren Methoden der rohe, ungeprueft uebernommene Cookie-Wert des
    Klienten. Unmaskiert in ein Attribut geschrieben lief damit fremdes
    Skript auf genau dieser Seite. Der Rahmen dekodiert ausserdem
    prozentkodierte Cookie-Werte, es braucht also nicht einmal rohe
    Anfuehrungszeichen.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        for gift in ('"><script>alert(1)</script>',
                     '%22%3E%3Cscript%3Ealert(1)%3C/script%3E',
                     "' onmouseover='alert(1)"):
            c.cookies.set("csrftoken", gift, domain="testserver.local")
            text = (await c.get("/login")).text
            assert "<script>alert(1)</script>" not in text, f"eingeschleust ueber {gift!r}"
            assert "onmouseover" not in text, f"eingeschleust ueber {gift!r}"


async def test_unangemeldete_anfragen_schreiben_nicht_in_die_kontendatei(tmp_path: Path) -> None:
    """Der Rahmen legt bei jeder Anfrage ``session = {}`` an, und ``{}`` ist
    nicht ``Empty`` -- also schrieb er auch auf der 401 des Waechters, mit
    einem Schluessel aus dem Cookie des Klienten. Gemessen: 25 unangemeldete
    Anfragen, 25 Zeilen, Schluessel bis 2010 Zeichen. Betroffen ist die eine
    Datei, die laut KTD8 nicht geloescht werden darf.
    """
    import sqlite3

    config = _mit_konto(tmp_path)
    async with _klient(config) as c:
        for _ in range(10):
            await c.get("/api/status", follow_redirects=False)
            c.cookies.clear()
        c.cookies.set("session", "ANGREIFER-" + "x" * 2000, domain="testserver.local")
        await c.get("/api/status", follow_redirects=False)
        c.cookies.clear()
        for _ in range(5):
            await c.get("/login")

    con = sqlite3.connect(config.accounts_file)
    zahl, laenge = con.execute(
        "select count(*), coalesce(max(length(key)), 0) from session_store"
    ).fetchone()
    con.close()
    assert zahl == 0, f"{zahl} Zeilen aus unangemeldeten Anfragen"
    assert laenge == 0, f"fremdbestimmter Schluessel mit {laenge} Zeichen gespeichert"


async def test_die_sitzung_wird_nicht_bei_jeder_anfrage_neu_geschrieben(tmp_path: Path) -> None:
    """U17 sagt woertlich "hoechstens einmal je Minute, sonst reihen sich die
    Schreiber auf". Meine Drossel sass in ``get()`` -- der Rahmen ruft aber auf
    **jeder** Antwort ``set()``, und das schrieb bedingungslos. Gemessen
    wanderte ``renewed_at`` in 0,4 s um 0,42 s.
    """
    import sqlite3

    config = _mit_konto(tmp_path)
    async with _klient(config) as c:
        await _anmelden(c)
        kennung = str(c.cookies.get("session"))
        con = sqlite3.connect(config.accounts_file)
        vorher = con.execute(
            "select renewed_at from session_store where key=?", (kennung,)
        ).fetchone()[0]
        for _ in range(5):
            await c.get("/api/status")
        nachher = con.execute(
            "select renewed_at from session_store where key=?", (kennung,)
        ).fetchone()[0]
        con.close()
    assert nachher == vorher, (
        f"die Sitzung wurde bei jeder Anfrage neu geschrieben (Versatz {nachher - vorher:.3f} s)"
    )


async def test_ein_merkmal_aus_fremder_sitzung_gilt_nicht(tmp_path: Path) -> None:
    """R23 verlangt ein **sitzungsgebundenes** Merkmal.

    Das doppelte Absenden des Rahmens prueft nur, dass Cookie und Kopfzeile
    dieselbe signierte Zeichenfolge tragen -- ohne Bezug zur Sitzung. Gemessen
    wurde ein Merkmal, das sich ein **nie angemeldeter** Klient mit einem
    einfachen ``GET /login`` abholt, in einer fremden angemeldeten Sitzung
    angenommen.
    """
    app = create_app(_mit_konto(tmp_path), hasher=_BILLIG)
    async with AsyncTestClient(app=app, base_url=BASIS) as fremd:
        await fremd.get("/login")
        fremdes = str(fremd.cookies.get("csrftoken"))

    async with AsyncTestClient(app=app, base_url=BASIS) as opfer:
        await _anmelden(opfer)
        opfer.cookies.set("csrftoken", fremdes, domain="testserver.local")
        antwort = await opfer.post("/logout", headers={"x-csrftoken": fremdes},
                                   follow_redirects=False)
        assert antwort.status_code == 403, (
            "ein Merkmal aus einer fremden Sitzung wurde angenommen"
        )


async def test_anmeldung_von_einer_fremdseite_wird_abgewiesen(tmp_path: Path) -> None:
    """Ohne Merkmal landet das Opfer sonst unbemerkt im Konto des Angreifers,
    und alle seine Handlungen stehen in dessen Audit.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        antwort = await c.post("/login", data={"account": "ops", "password": _PASSWORT},
                               follow_redirects=False)
        assert antwort.status_code == 403, antwort.status_code


async def test_abmelden_nimmt_das_merkmal_mit(tmp_path: Path) -> None:
    """Auf einem geteilten Arbeitsplatz waere es sonst das Merkmal des
    Vorgaengers.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        await _anmelden(c)
        merkmal = str(c.cookies.get("csrftoken"))
        antwort = await c.post("/logout", headers={"x-csrftoken": merkmal},
                               follow_redirects=False)
        gesetzt = " ".join(antwort.headers.get_list("set-cookie"))
        assert "csrftoken=" in gesetzt and ("Max-Age=0" in gesetzt or "expires=" in gesetzt.lower()), (
            f"das Merkmal wurde nicht geloescht: {gesetzt}"
        )


async def test_ohne_kontenspeicher_wird_abgewiesen(tmp_path: Path) -> None:
    """Wer den Kontozustand nicht nachsehen kann, darf ihn nicht raten.

    Frueher stand dort ein ``return`` -- die Sperrpruefung fiel dann ersatzlos
    weg, und ein gesperrtes Konto kam durch. Die Verzweigung muss im Zweifel
    **zu**fallen, nicht auf.
    """
    app = create_app(_mit_konto(tmp_path), hasher=_BILLIG)
    async with AsyncTestClient(app=app, base_url=BASIS) as c:
        await _anmelden(c)
        assert (await c.get("/api/status")).status_code == 200

        app.state.services.accounts = None      # der Speicher ist fort
        antwort = await c.get("/api/status", follow_redirects=False)
        assert antwort.status_code in (302, 303, 401), (
            "ohne Kontenspeicher wurde die Anfrage durchgelassen"
        )
