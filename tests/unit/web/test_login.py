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
22. Die Anmeldung rechnet nicht im Faden des Ereignis-Loops.

=== ZYKLUS 9: Zusicherungen, die bisher fehlten ===
23. Ein Fehlschlag hinterlaesst keine Sitzung.
24. Die Bremse wird ueber HTTP nie zur Sperre.
25. Der Fehlschlag sagt nicht, woran es lag.
26. Die Herkunft kommt nicht aus einer frei waehlbaren Kopfzeile.
27. Die Bremse greift vor der Passwortpruefung.

=== ZYKLUS 10: Befunde des Nachlaufs ueber die Korrekturen ===
28. Ein Cookie allein ist kein Merkmal -- es muss mitgeschickt werden.
29. Ein Nicht-ASCII-Merkmal wird abgewiesen, nicht mit 500 quittiert.
30. Die Anmeldeseite entwertet kein gueltiges Merkmal.
31. Eine unbrauchbare Kontendatei laesst den Dienst antworten.

=== ZYKLUS 11: Audit (U10, R27) ===
32. Ein fehlendes Audit haelt die Anmeldung nicht auf, verschwindet aber nicht.
    (Dass die Eintraege wirklich entstehen, prueft die Integrationsreihe --
    ohne echten Strom ist das Audit leer, und jede Zusicherung darauf haelte
    trivial.)
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


#: Massstab der Bremse in der Testreihe: dasselbe Verhalten, ein Fuenfzigstel
#: der Wartezeit. Ohne ihn bezahlt jeder Test die Bremse in echten Sekunden.
_BREMSE = 0.02


def _klient(config: DashboardConfig) -> AsyncTestClient:
    return AsyncTestClient(
        app=create_app(config, hasher=_BILLIG, throttle_scale=_BREMSE), base_url=BASIS
    )


def _mit_konto(tmp_path: Path) -> DashboardConfig:
    c = _config(tmp_path)
    s = AccountStore(c.accounts_file, hasher=_BILLIG)
    s.create("ops", _PASSWORT)
    s.close()
    return c


def _merkmal_aus(html: str) -> str:
    """Das Merkmal aus dem Formular ziehen, wie ein Browser es abschickt."""
    treffer = re.search(r'name="?_csrf_token"?\s+value="([^"]*)"', html)
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
    app = create_app(config, hasher=_BILLIG, throttle_scale=_BREMSE)
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
        assert gebremst > 0.004, f"nach fuenf Fehlversuchen keine spuerbare Bremse ({gebremst:.3f} s)"

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
        assert time.perf_counter() - beginn > 0.004, "ohne Valkey bremst die Anmeldung nicht"


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
    app = create_app(config, hasher=_BILLIG, throttle_scale=_BREMSE)
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
    app = create_app(_mit_konto(tmp_path), hasher=_BILLIG, throttle_scale=_BREMSE)
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
    app = create_app(_mit_konto(tmp_path), hasher=_BILLIG, throttle_scale=_BREMSE)
    async with AsyncTestClient(app=app, base_url=BASIS) as c:
        await _anmelden(c)
        assert (await c.get("/api/status")).status_code == 200

        app.state.services.accounts = None      # der Speicher ist fort
        antwort = await c.get("/api/status", follow_redirects=False)
        assert antwort.status_code in (302, 303, 401), (
            "ohne Kontenspeicher wurde die Anfrage durchgelassen"
        )


async def test_die_anmeldung_blockiert_den_ereignis_loop_nicht(tmp_path: Path) -> None:
    """Granian faehrt **einen** Arbeiter.

    Gemessen liefen argon2 (42-52 ms) und die SQLite-Schreibvorgaenge der
    Bremse vollstaendig im Loop-Faden: bei zwanzig gleichzeitigen Anmeldungen
    stand er 805 ms still, bei fuenfzig 2003 ms -- und damit auch
    ``/api/status``, die Seite, die im Vorfall zuerst gerufen wird. Dieselbe
    Bauart hat der Plan fuer U9 nach der Pruefung von Stufe B bereits als
    bindend verworfen; der Anmelde-Handler zog nicht nach.

    Geprueft wird der **Ort**, nicht die Dauer: eine Zeitschranke haengt an
    der Auslastung der Maschine und wird auf einem vollen Bauserver launisch.
    """
    import asyncio as _aio

    config = _mit_konto(tmp_path)
    app = create_app(config, hasher=_BILLIG, throttle_scale=_BREMSE)
    gesehen: list[bool] = []

    async with AsyncTestClient(app=app, base_url=BASIS) as c:
        speicher = app.state.services.accounts
        echt = speicher.verify

        def beobachtet(name: str, passwort: str) -> bool:
            # In einem Arbeitsfaden laeuft **kein** Ereignis-Loop. Das ist der
            # verlaessliche Unterschied: die Faden-Kennung taugt hier nicht,
            # weil der Testklient die Anwendung ohnehin in einem anderen Faden
            # faehrt als die Testfunktion -- diese Zusicherung war in einer
            # ersten Fassung deshalb schon vor der Korrektur gruen.
            try:
                _aio.get_running_loop()
                gesehen.append(True)      # im Loop -- er steht so lange still
            except RuntimeError:
                gesehen.append(False)     # im Arbeitsfaden
            return bool(echt(name, passwort))

        speicher.verify = beobachtet
        await _anmelden(c, passwort="falsch")

    assert gesehen, "die Passwortpruefung lief gar nicht"
    assert not any(gesehen), "die Passwortpruefung lief im Ereignis-Loop"


# --- Zyklus 9: Zusicherungen, die bisher fehlten -------------------------


async def test_ein_fehlschlag_hinterlaesst_keine_sitzung(tmp_path: Path) -> None:
    """Eine vollstaendige Umgehung der Anmeldung waere unbemerkt geblieben.

    Kein Test folgte einem fehlgeschlagenen Anmeldeversuch mit einem Aufruf
    auf eine geschuetzte Route -- die Zusicherung endete beim Statuscode der
    Anmeldung selbst.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        for passwort in ("falsch", "", "x" * 200):
            antwort = await _anmelden(c, passwort=passwort)
            assert antwort.status_code != 303, f"{passwort!r} wurde angenommen"
            weiter = await c.get("/api/status", follow_redirects=False)
            assert weiter.status_code in (302, 303, 401), (
                f"nach dem Fehlschlag mit {passwort!r} war die Seite erreichbar"
            )
        for konto in ("gibtesnicht", "", "OPS"):
            await _anmelden(c, konto=konto)
            weiter = await c.get("/api/status", follow_redirects=False)
            assert weiter.status_code in (302, 303, 401), f"{konto!r} kam durch"


async def test_die_bremse_wird_ueber_http_nie_zur_sperre(tmp_path: Path) -> None:
    """R22 auf dem Weg, auf dem sie verletzt wuerde.

    Geprueft wurde bisher nur am Speicher, dass das Konto noch existiert --
    aber nicht, dass man sich damit auch **anmelden** kann. Eine Bremse, die
    zur Sperre wird, haette das nicht gezeigt.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        for _ in range(6):
            await _anmelden(c, passwort="falsch")

        antwort = await _anmelden(c)                   # das richtige Passwort
        assert antwort.status_code == 303, (
            f"nach sechs Fehlversuchen kommt das richtige Passwort nicht mehr durch: "
            f"{antwort.status_code}"
        )
        assert (await c.get("/api/status")).status_code == 200


async def test_der_fehlschlag_sagt_nicht_woran_es_lag(tmp_path: Path) -> None:
    """Ein Satz fuer alle drei Faelle. Jede Unterscheidung waere dieselbe
    Aufzaehlung, die die gleichen Antwortzeiten verhindern (R22).
    """
    config = _mit_konto(tmp_path)
    s = AccountStore(config.accounts_file, hasher=_BILLIG)
    s.create("gesperrt", _PASSWORT)
    s.lock("gesperrt")
    s.close()

    async with _klient(config) as c:
        antworten = []
        for konto, passwort in (("gibtesnicht", _PASSWORT), ("ops", "falsch"),
                                ("gesperrt", _PASSWORT)):
            a = await _anmelden(c, konto=konto, passwort=passwort)
            antworten.append((a.status_code, a.text))
    codes = {c for c, _ in antworten}
    texte = {t for _, t in antworten}
    assert codes == {401}, f"verschiedene Statuscodes: {codes}"
    assert len(texte) == 1, f"verschiedene Antworttexte: {texte}"
    assert "gibt" not in texte.pop().lower().replace("stimmt", ""), "der Text nennt den Grund"


async def test_die_herkunft_kommt_nicht_aus_einer_kopfzeile(tmp_path: Path) -> None:
    """``X-Forwarded-For`` ist frei waehlbar -- die Herkunftsbremse waere damit
    vollstaendig umgehbar, indem der Angreifer je Versuch eine andere Adresse
    behauptet.
    """
    # Eigener, groesserer Massstab: mit dem schnellen Massstab der uebrigen
    # Tests liegt die erwartete Bremse bei 40 ms, und allein der Aufwand einer
    # Anfrage ueberschreitet jede Schranke darunter -- die Zusicherung war
    # damit blind fuer genau den Fehler, den sie sucht.
    app = create_app(_mit_konto(tmp_path), hasher=_BILLIG, throttle_scale=0.4)
    async with AsyncTestClient(app=app, base_url=BASIS) as c:
        # Je Versuch ein **anderes Konto** und eine andere behauptete Adresse.
        # Dass der Kontozaehler auch mitbremst, macht diesen Test sonst blind:
        # in einer ersten Fassung stand ueberall "ops", und die Bremse kam vom
        # Konto, nicht von der Herkunft -- die Zusicherung war wirkungslos.
        for i in range(6):
            seite = await c.get("/login")
            await c.post(
                "/login",
                data={"account": f"konto{i}", "password": "falsch",
                      "_csrf_token": _merkmal_aus(seite.text)},
                headers={"x-forwarded-for": f"10.0.0.{i}"},
                follow_redirects=False,
            )

        beginn = time.perf_counter()
        await _anmelden(c, konto="wiederandersA", passwort="falsch")
        gebremst = time.perf_counter() - beginn
    assert gebremst > 0.3, (
        f"sechs Fehlversuche von derselben echten Herkunft, aber mit wechselndem "
        f"X-Forwarded-For und wechselndem Konto, bremsen nicht ({gebremst:.3f} s) -- "
        "die frei waehlbare Kopfzeile wird als Herkunft benutzt"
    )


async def test_die_bremse_greift_vor_der_passwortpruefung(tmp_path: Path) -> None:
    """Sonst traefe die Verzoegerung nur den Fall, den die Pruefung ohnehin
    ablehnt -- und ein Angreifer koennte sie durch Abbrechen der Verbindung
    umgehen, waehrend der Server noch wartet.
    """
    import asyncio as _aio

    config = _mit_konto(tmp_path)
    app = create_app(config, hasher=_BILLIG, throttle_scale=_BREMSE)
    reihenfolge: list[str] = []

    async with AsyncTestClient(app=app, base_url=BASIS) as c:
        for _ in range(4):
            await _anmelden(c, passwort="falsch")

        speicher = app.state.services.accounts
        echt_verify = speicher.verify
        echt_sleep = _aio.sleep

        def beobachtet(name: str, passwort: str) -> bool:
            reihenfolge.append("pruefung")
            return bool(echt_verify(name, passwort))

        async def beobachtet_sleep(dauer: float, *a: object, **k: object) -> None:
            if dauer > 0:
                reihenfolge.append("warten")
            await echt_sleep(0)

        speicher.verify = beobachtet
        import swarmq_dashboard.web.routes.login as modul
        modul.asyncio.sleep = beobachtet_sleep  # type: ignore[assignment]
        try:
            await _anmelden(c, passwort="falsch")
        finally:
            modul.asyncio.sleep = echt_sleep  # type: ignore[assignment]

    assert reihenfolge[:2] == ["warten", "pruefung"], f"Reihenfolge: {reihenfolge}"


# --- Zyklus 10: Befunde des Nachlaufs ueber die Korrekturen --------------


async def test_ein_cookie_allein_ist_kein_merkmal(tmp_path: Path) -> None:
    """Die doppelte Absendung entartet, wenn das Cookie selbst als Beleg gilt.

    Nach der Anmeldung traegt das Merkmal-Cookie denselben Wert wie die
    Sitzung -- der Browser schickt es bei jeder Anfrage von selbst mit. Faellt
    der Waechter darauf zurueck, vergleicht er den Sitzungswert mit einer
    Kopie seiner selbst, und ein Merkmal, das der Aufrufer **aktiv**
    mitschicken muss, gibt es nicht mehr. Uebrig bleibt allein die
    Seitenbindung, und die traegt nicht gegen eine Nachbardomaene.

    Der alte Test loeschte das Cookie und pruefte damit den Fall, den es im
    Browser gar nicht gibt.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        await _anmelden(c)
        assert c.cookies.get("csrftoken"), "Vorbedingung: das Cookie muss da sein"
        antwort = await c.post("/logout", follow_redirects=False)
        assert antwort.status_code == 403, (
            "die Aktion lief ohne mitgeschicktes Merkmal -- nur mit den Cookies"
        )


async def test_ein_nicht_ascii_merkmal_wirft_nicht(tmp_path: Path) -> None:
    """``secrets.compare_digest`` lehnt Zeichenketten mit Nicht-ASCII mit
    ``TypeError`` ab. Das ergab einen Serverfehler auf dem Anmeldepfad --
    unangemeldet ausloesbar, vor der Bremse, also unbegrenzt wiederholbar.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        await c.get("/login")
        antwort = await c.post(
            "/login", data={"account": "ops", "password": "x", "_csrf_token": "ä"},
            follow_redirects=False,
        )
        assert antwort.status_code == 403, f"{antwort.status_code} statt einer Abweisung"

    # Ueber eine Kopfzeile laesst sich Nicht-ASCII gar nicht senden -- HTTP
    # erlaubt dort nur Latin-1, und httpx weist es schon beim Bauen ab. Der
    # Vergleich selbst muss den Rand trotzdem aushalten: er bekommt auch
    # Formularwerte, und dort kommt alles an.
    from swarmq_dashboard.web.auth import gleich

    assert gleich("ä", "ä") is True
    assert gleich("ä", "a") is False
    assert gleich("\U0001f600", "x") is False


async def test_die_anmeldeseite_entwertet_kein_gueltiges_merkmal(tmp_path: Path) -> None:
    """Ein zweiter Tab, ein Lesezeichen oder die Zurueck-Taste genuegte.

    ``login_form`` wuerfelte bei jedem Aufruf ein neues Merkmal und setzte es
    als Cookie -- auch fuer einen bereits Angemeldeten. Danach passte das
    Cookie nicht mehr zur Sitzung, und jede Schreibaktion scheiterte mit 403,
    bis sich das Teammitglied neu anmeldet. Genau der Schaden, vor dem der
    Docstring des alten CSRF-Geheimnisses gewarnt hat.
    """
    async with _klient(_mit_konto(tmp_path)) as c:
        await _anmelden(c)
        vorher = str(c.cookies.get("csrftoken"))
        await c.get("/login", follow_redirects=False)
        nachher = str(c.cookies.get("csrftoken"))
        assert nachher == vorher, "die Anmeldeseite hat das gueltige Merkmal ersetzt"

        antwort = await c.post("/logout", headers={"x-csrftoken": nachher},
                               follow_redirects=False)
        assert antwort.status_code == 303, (
            f"nach einem Aufruf der Anmeldeseite scheitert das Schreiben: {antwort.status_code}"
        )


async def test_eine_unbrauchbare_kontendatei_laesst_den_dienst_antworten(tmp_path: Path) -> None:
    """R33 auf dem ganzen Weg, nicht nur bis ``Services.startup()``.

    Der Wurf war nur von ``startup()`` nach ``create_app()`` gewandert -- und
    wo er nicht mehr warf, antwortete der Dienst auf **jede** Anfrage mit 500,
    weil die Sitzungs-Middleware auf jeder Antwort in den Speicher schreibt.
    Der Zustand war schlechter als vorher: statt eines klaren Fehlers beim
    Start lief ein Dienst, der nur noch Serverfehler lieferte.
    """
    config = _mit_konto(tmp_path)
    config.accounts_file.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)

    app = create_app(config, hasher=_BILLIG)          # darf nicht werfen
    async with AsyncTestClient(app=app, base_url=BASIS) as c:
        seite = await c.get("/login")
        assert seite.status_code == 200, f"die Anmeldeseite antwortet mit {seite.status_code}"
        zustand = await c.get("/api/status", follow_redirects=False)
        assert zustand.status_code in (302, 303, 401), (
            f"/api/status antwortet mit {zustand.status_code} statt einer Abweisung"
        )
        assert app.state.services.status()["accounts"]["ok"] is False
        assert app.state.services.status()["accounts"]["reason"]


# --- Zyklus 11: Audit (U10, R27) ----------------------------------------


async def test_ein_fehlendes_audit_haelt_die_anmeldung_nicht_auf(tmp_path: Path) -> None:
    """KTD8: die Anmeldung muss gehen, wenn Valkey weg ist -- der Audit liegt
    aber in Valkey. Ein nicht geschriebener Eintrag darf sie nicht aufhalten;
    verschwinden darf er auch nicht.
    """
    config = _mit_konto(tmp_path)          # zeigt auf tote Ports
    app = create_app(config, hasher=_BILLIG, throttle_scale=_BREMSE)
    async with AsyncTestClient(app=app, base_url=BASIS) as c:
        antwort = await _anmelden(c)
        assert antwort.status_code == 303, "die Anmeldung scheiterte ohne Valkey"
        assert (await c.get("/api/status")).status_code == 200
        assert app.state.services.audit.lost > 0, "der nicht geschriebene Eintrag wurde verschwiegen"
        assert (await c.get("/api/status")).json()["audit"]["lost"] > 0
