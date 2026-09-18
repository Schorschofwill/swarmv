"""Konten und Passwoerter -- die eigene Datei neben dem Verzeichnis.

Die gefaehrliche Stelle ist hier nicht die Speicherung, sondern die
**Ununterscheidbarkeit**. Ein Anmeldeversuch auf ein Konto, das es nicht gibt,
darf nicht schneller beantwortet werden als einer auf ein bestehendes: sonst
liest jemand aus den Antwortzeiten die Kontenliste ab, ohne ein einziges
Passwort zu kennen. Dasselbe gilt fuer ein gesperrtes Konto (R22, AE9a).

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Anlegen und Pruefen ===
1. Ein angelegtes Konto laesst sich mit seinem Passwort pruefen, mit einem
   falschen nicht.
2. Der gespeicherte Wert enthaelt das Passwort nicht im Klartext und nennt
   das Verfahren.
3. Zwei Konten mit demselben Passwort tragen verschiedene Werte (Salz).
4. Ein Konto zweimal anzulegen wird benannt abgewiesen.

=== ZYKLUS 2: Ununterscheidbarkeit (R22) ===
5. Covers AE9a. Ein unbekanntes Konto braucht nicht erkennbar weniger Zeit
   als ein bekanntes.
6. Ein gesperrtes Konto braucht nicht erkennbar weniger Zeit als ein offenes.

=== ZYKLUS 3: Sperren ===
7. Ein gesperrtes Konto wird abgewiesen, auch mit richtigem Passwort.
8. Entsperren macht es wieder brauchbar.
9. Sperren ist kein Loeschen: das Konto bleibt in der Liste, gekennzeichnet.

=== ZYKLUS 4: Passwort aendern ===
10. Nach der Aenderung gilt nur noch das neue Passwort.
11. Eine Aenderung an einem unbekannten Konto wird benannt abgewiesen.

=== ZYKLUS 5: Ablage (KTD8, R7a) ===
12. Die Kontendatei ist nur fuer den Eigentuemer lesbar -- auch dann, wenn
    sie schon vorhanden war.
13. Die Kontendatei ist von der Verzeichnisdatei getrennt: das Loeschen des
    Verzeichnisses laesst die Konten unberuehrt.
14. Ein Wert aus schwaecheren Verfahrensparametern wird beim naechsten
    erfolgreichen Anmelden erneuert.

=== ZYKLUS 6: Befunde aus der Pruefung von Stufe C ===
15. Die Ununterscheidbarkeit haelt auch, wenn der Bestand noch andere
    Verfahrensparameter traegt als das eingestellte Verfahren.
16. Jeder Fehlversuch kostet gleich viele Datenbankbefehle und schreibt nie.
17. Das Erneuern dreht einen gleichzeitigen Passwortwechsel nicht zurueck.
18. Der Speicher ist aus einem zweiten Faden benutzbar.
20. Kontonamen werden geprueft; gleich aussehende Namen sind dasselbe Konto.
21. Gleich aussehende Passwoerter gelten beide; uebermaessig lange nicht.
22. Rechte werden nachgezogen, aber nie gelockert -- auch am Verzeichnis.
23. Die Verbindung traegt ihre vier Einstellungen.

=== ZYKLUS 7: Zusicherungen, die bisher fehlten ===
24. Ein unbekanntes Konto wird abgewiesen -- der Rueckgabewert, nicht nur die
    Dauer.
25. Ein leerer Bestand weist ab, statt zu werfen.
26. Alle Wege sehen dieselbe Unicode-Schreibweise, nicht nur das Anlegen.
27. Die Zeitschranke faengt beide Richtungen.
"""

from __future__ import annotations

import statistics
import threading
import time
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from swarmq_dashboard.accounts import (
    BUSY_TIMEOUT_MS,
    AccountExists,
    AccountStore,
    InvalidAccountName,
    InvalidPassword,
    UnknownAccount,
)

#: Billige Parameter fuer die Testreihe. Sie aendern nichts am geprueften
#: Verhalten -- nur die Dauer je Pruefung, sonst laege die Reihe bei Minuten.
_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)


@pytest.fixture
def store(tmp_path: Path) -> AccountStore:
    return AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)


# --- Zyklus 1 -----------------------------------------------------------


def test_angelegtes_konto_laesst_sich_pruefen(store: AccountStore) -> None:
    store.create("ops", "richtig-und-lang")
    assert store.verify("ops", "richtig-und-lang") is True
    assert store.verify("ops", "falsch") is False


def test_gespeicherter_wert_ist_kein_klartext(store: AccountStore, tmp_path: Path) -> None:
    store.create("ops", "Sonnenblume-2026")
    store.close()
    rohe = (tmp_path / "konten.sqlite3").read_bytes()
    assert b"Sonnenblume-2026" not in rohe, "das Passwort steht im Klartext in der Datei"
    assert b"$argon2id$" in rohe, "der Wert nennt sein Verfahren nicht"


def test_gleiches_passwort_ergibt_verschiedene_werte(store: AccountStore) -> None:
    """Ohne Salz verriete die Datei, welche Konten dasselbe Passwort haben."""
    store.create("a", "dasselbe-passwort")
    store.create("b", "dasselbe-passwort")
    werte = {z.password_hash for z in store.accounts()}
    assert len(werte) == 2, "beide Konten tragen denselben Wert"


def test_zweimal_anlegen_wird_abgewiesen(store: AccountStore) -> None:
    store.create("ops", "erstes-passwort")
    with pytest.raises(AccountExists):
        store.create("ops", "zweites-passwort")
    assert store.verify("ops", "erstes-passwort") is True, "das erste Passwort wurde ueberschrieben"


# --- Zyklus 2 -----------------------------------------------------------


def _dauer_paar(a: object, b: object, runden: int = 15) -> tuple[float, float]:
    """Die Median-Dauern beider Aufrufe in Sekunden, **abwechselnd** gemessen.

    Nacheinander zu messen waere anfaellig: eine Lastspitze waehrend der ersten
    Messreihe blaeht deren Median auf und kippt den Vergleich, ohne dass sich
    am Code etwas geaendert haette. Abwechselnd gemessen trifft dieselbe
    Spitze beide Reihen -- der Quotient bleibt aussagekraeftig.
    """
    proben_a: list[float] = []
    proben_b: list[float] = []
    for _ in range(runden):
        for fn, proben in ((a, proben_a), (b, proben_b)):
            t = time.perf_counter()
            fn()  # type: ignore[operator]
            proben.append(time.perf_counter() - t)
    return statistics.median(proben_a), statistics.median(proben_b)


def test_unbekanntes_konto_ist_nicht_schneller(store: AccountStore) -> None:
    """Covers AE9a.

    Antwortet ein unbekanntes Konto sofort, laesst sich aus den Antwortzeiten
    die Kontenliste ablesen, ohne ein einziges Passwort zu kennen.
    """
    store.create("bekannt", "ein-langes-passwort")
    bekannt, unbekannt = _dauer_paar(
        lambda: store.verify("bekannt", "falsches-passwort"),
        lambda: store.verify("gibtesnicht", "falsches-passwort"),
    )
    # Beidseitig: auch **laenger** zu brauchen verraet das unbekannte Konto.
    assert 0.5 < unbekannt / bekannt < 2.0, (
        f"unbekannt {unbekannt * 1000:.2f} ms gegen bekannt {bekannt * 1000:.2f} ms -- "
        "die Antwortzeit verraet, welche Konten es gibt"
    )


def test_gesperrtes_konto_ist_nicht_schneller(store: AccountStore) -> None:
    """Sonst verraet die Antwortzeit, welche Konten gesperrt sind."""
    store.create("offen", "ein-langes-passwort")
    store.create("gesperrt", "ein-langes-passwort")
    store.lock("gesperrt")
    offen, zu = _dauer_paar(
        lambda: store.verify("offen", "falsch"),
        lambda: store.verify("gesperrt", "ein-langes-passwort"),
    )
    assert 0.5 < zu / offen < 2.0, (
        f"gesperrt {zu * 1000:.2f} ms gegen offen {offen * 1000:.2f} ms -- "
        "die Antwortzeit verraet den Sperrzustand"
    )


# --- Zyklus 3 -----------------------------------------------------------


def test_gesperrtes_konto_wird_abgewiesen(store: AccountStore) -> None:
    store.create("ops", "richtiges-passwort")
    store.lock("ops")
    assert store.verify("ops", "richtiges-passwort") is False


def test_entsperren_macht_wieder_brauchbar(store: AccountStore) -> None:
    store.create("ops", "richtiges-passwort")
    store.lock("ops")
    store.unlock("ops")
    assert store.verify("ops", "richtiges-passwort") is True


def test_sperren_ist_kein_loeschen(store: AccountStore) -> None:
    """Ein geloeschtes Konto liesse sich neu anlegen -- eine Sperre nicht."""
    store.create("ops", "richtiges-passwort")
    store.lock("ops")
    konten = {k.name: k.locked for k in store.accounts()}
    assert konten == {"ops": True}, f"das Konto ist aus der Liste verschwunden: {konten}"
    with pytest.raises(AccountExists):
        store.create("ops", "neues-passwort")


# --- Zyklus 4 -----------------------------------------------------------


def test_passwortaenderung_entwertet_das_alte(store: AccountStore) -> None:
    store.create("ops", "altes-passwort")
    store.set_password("ops", "neues-passwort")
    assert store.verify("ops", "altes-passwort") is False
    assert store.verify("ops", "neues-passwort") is True


def test_aenderung_an_unbekanntem_konto(store: AccountStore) -> None:
    with pytest.raises(UnknownAccount):
        store.set_password("gibtesnicht", "egal")


# --- Zyklus 5 -----------------------------------------------------------


def test_kontendatei_ist_nur_fuer_den_eigentuemer_lesbar(tmp_path: Path) -> None:
    """Covers R7a fuer die Kontendatei.

    Auch an einer vorhandenen Datei: eine aus einer Ruecksicherung oder von
    einem ``ExecStartPre`` angelegte Datei braechte sonst ihre eigenen Rechte
    mit, und die Passwort-Werte laegen fuer jeden lesbar da.
    """
    pfad = tmp_path / "konten.sqlite3"
    pfad.touch(mode=0o644)
    s = AccountStore(pfad, hasher=_BILLIG)
    s.create("ops", "ein-passwort")
    s.close()
    for begleiter in ("", "-wal", "-shm"):
        f = pfad.with_name(pfad.name + begleiter)
        if f.exists():
            assert not (f.stat().st_mode & 0o077), (
                f"{f.name} ist fuer andere lesbar: {oct(f.stat().st_mode & 0o777)}"
            )


def test_neuaufbau_des_verzeichnisses_laesst_die_konten_stehen(tmp_path: Path) -> None:
    """KTD8. Laegen beide in einer Datei, spaerrte ihr Loeschen das Team aus --
    und der Neuaufbau des Verzeichnisses aus dem Strom ist ein vorgesehener Weg.
    """
    from swarmq_dashboard.directory.schema import connect

    verzeichnis = tmp_path / "verzeichnis.sqlite3"
    konten = tmp_path / "konten.sqlite3"
    connect(verzeichnis).close()
    s = AccountStore(konten, hasher=_BILLIG)
    s.create("ops", "ein-passwort")
    s.close()

    for begleiter in ("", "-wal", "-shm"):
        verzeichnis.with_name(verzeichnis.name + begleiter).unlink(missing_ok=True)

    s2 = AccountStore(konten, hasher=_BILLIG)
    assert s2.verify("ops", "ein-passwort") is True, "der Neuaufbau nahm die Konten mit"


def test_schwacher_wert_wird_beim_anmelden_erneuert(tmp_path: Path) -> None:
    """Werden die Verfahrensparameter spaeter angehoben, muss der gespeicherte
    Wert nachziehen -- sonst bleibt ein altes Konto dauerhaft auf der
    schwaecheren Einstellung, ohne dass es jemand sieht (R21).
    """
    pfad = tmp_path / "konten.sqlite3"
    schwach = AccountStore(pfad, hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    schwach.create("ops", "ein-passwort")
    alt = schwach.accounts()[0].password_hash
    schwach.close()

    stark = AccountStore(pfad, hasher=_BILLIG)
    assert stark.verify("ops", "ein-passwort") is True
    neu = stark.accounts()[0].password_hash
    assert neu != alt, "der Wert blieb auf den schwaecheren Parametern stehen"
    assert stark.verify("ops", "ein-passwort") is True, "nach dem Erneuern gilt das Passwort nicht mehr"


# --- ZYKLUS 6: Befunde aus der Pruefung von Stufe C ---------------------


def _mediane(store: AccountStore, faelle: dict[str, tuple[str, str]], runden: int = 9) -> dict[str, float]:
    """Je Fall die mittlere Dauer in Millisekunden, verschraenkt gemessen.

    Verschraenkt, nicht blockweise: sonst faengt ein Lastwechsel der Maschine
    einen ganzen Fall ein und verschiebt den Vergleich.
    """
    proben: dict[str, list[float]] = {k: [] for k in faelle}
    for _ in range(runden):
        for schluessel, (konto, passwort) in faelle.items():
            t = time.perf_counter()
            store.verify(konto, passwort)
            proben[schluessel].append((time.perf_counter() - t) * 1000)
    return {k: statistics.median(v) for k, v in proben.items()}


def test_ununterscheidbar_auch_bei_altem_bestand(tmp_path: Path) -> None:
    """Covers AE9a fuer den Fall, der die Zusage wirklich bedroht.

    Die Attrappe, gegen die ein unbekanntes Konto geprueft wird, muss die
    Parameter des **Bestands** tragen, nicht die des gerade eingestellten
    Verfahrens. Wurde sie mit den neuen gebildet, waehrend die gespeicherten
    Werte noch die alten tragen, war ein unbekanntes Konto gemessen 2,7-mal
    langsamer als ein bekanntes -- aus der Ferne trivial ablesbar. Ausgerechnet
    der Zustand, den das Erneuern der Werte erzeugt, hebelte die Zusage aus.
    """
    pfad = tmp_path / "konten.sqlite3"
    alt = AccountStore(pfad, hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
    alt.create("bekannt", "ein-langes-passwort")
    alt.create("gesperrt", "ein-langes-passwort")
    alt.lock("gesperrt")
    alt.close()

    # Deutlich teurere Parameter -- so sieht eine Anhebung aus.
    neu = AccountStore(pfad, hasher=PasswordHasher(time_cost=6, memory_cost=8192, parallelism=1))
    m = _mediane(neu, {
        "unbekannt": ("gibtesnicht", "falsches-passwort"),
        "bekannt": ("bekannt", "falsches-passwort"),
        "gesperrt": ("gesperrt", "ein-langes-passwort"),
    })
    verhaeltnis = m["unbekannt"] / m["bekannt"]
    assert 0.6 < verhaeltnis < 1.6, (
        f"unbekannt {m['unbekannt']:.2f} ms gegen bekannt {m['bekannt']:.2f} ms "
        f"(Verhaeltnis {verhaeltnis:.2f}) -- die Antwortzeit verraet, welche Konten es gibt"
    )
    verhaeltnis_sperre = m["gesperrt"] / m["bekannt"]
    assert 0.6 < verhaeltnis_sperre < 1.6, (
        f"gesperrt {m['gesperrt']:.2f} ms gegen bekannt {m['bekannt']:.2f} ms "
        f"(Verhaeltnis {verhaeltnis_sperre:.2f}) -- die Antwortzeit verraet den Sperrzustand"
    )
    neu.close()


def test_jeder_fehlversuch_kostet_gleich_viele_abfragen(tmp_path: Path) -> None:
    """Nicht nur die Rechenzeit: auch die Zahl der Datenbankbefehle muss
    gleich sein, sonst verraet sie dasselbe ueber einen anderen Kanal.
    """
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    s.create("bekannt", "ein-langes-passwort")
    s.create("gesperrt", "ein-langes-passwort")
    s.lock("gesperrt")

    def zaehlen(konto: str, passwort: str) -> list[str]:
        befehle: list[str] = []
        s._con.set_trace_callback(befehle.append)
        try:
            s.verify(konto, passwort)
        finally:
            s._con.set_trace_callback(None)
        return befehle

    a = zaehlen("gibtesnicht", "falsch")
    b = zaehlen("bekannt", "falsch")
    c = zaehlen("gesperrt", "ein-langes-passwort")
    assert len(a) == len(b) == len(c), (
        f"unbekannt {len(a)}, bekannt {len(b)}, gesperrt {len(c)} Befehle: "
        "die Zahl der Abfragen verraet den Fall"
    )
    assert not any("update" in x.lower() or "insert" in x.lower() for x in a + b + c), (
        "ein Fehlversuch schreibt -- das ist an Dateizeit und Journal ablesbar"
    )


def test_erneuern_dreht_einen_gleichzeitigen_wechsel_nicht_zurueck(tmp_path: Path) -> None:
    """R20. Zwischen dem Lesen des Werts und dem Erneuern liegen rund fuenfzig
    Millisekunden argon2. Faellt ein Passwortwechsel in dieses Fenster, machte
    das Erneuern ihn zunichte -- die Kommandozeile meldete Erfolg, und das
    alte Passwort galt weiter. Ein Wechsel nach einem Vorfall verpuffte still.
    """
    pfad = tmp_path / "konten.sqlite3"
    schwach = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
    vorbereiten = AccountStore(pfad, hasher=schwach)
    vorbereiten.create("ops", "altes-passwort")
    vorbereiten.close()

    wechsler = AccountStore(pfad, hasher=_BILLIG)

    class WechseltWaehrendGeprueftWird:
        """Ein Hasher, der den Passwortwechsel genau in das Fenster legt.

        Zwischen dem Lesen des Werts und dem Erneuern liegen im Betrieb rund
        fuenfzig Millisekunden argon2. Diese Huelle trifft das Fenster
        zuverlaessig, statt auf eine Verschraenkung zu hoffen.
        """

        def __init__(self) -> None:
            self._echt = _BILLIG
            self._schon = False

        def hash(self, passwort: str) -> str:
            return str(self._echt.hash(passwort))

        def check_needs_rehash(self, wert: str) -> bool:
            return bool(self._echt.check_needs_rehash(wert))

        def verify(self, wert: str, passwort: str) -> bool:
            if not self._schon:
                self._schon = True
                wechsler.set_password("ops", "neues-passwort")
            return bool(self._echt.verify(wert, passwort))

    anmelder = AccountStore(pfad, hasher=WechseltWaehrendGeprueftWird())  # type: ignore[arg-type]
    anmelder.verify("ops", "altes-passwort")

    pruefer = AccountStore(pfad, hasher=_BILLIG)
    assert pruefer.verify("ops", "neues-passwort") is True, (
        "der Passwortwechsel wurde vom Erneuern zurueckgedreht"
    )
    assert pruefer.verify("ops", "altes-passwort") is False, "das alte Passwort gilt noch"
    for x in (anmelder, wechsler, pruefer):
        x.close()


def test_aus_einem_zweiten_faden_benutzbar(tmp_path: Path) -> None:
    """Die Anmeldung ist ein rund fuenfzig Millisekunden teurer Rechenblock.
    Er gehoert in einen Arbeitsfaden -- und dort ist jede Anfrage
    moeglicherweise ein anderer. Ohne Fadensicherheit wirft SQLite dort.
    """
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    s.create("ops", "ein-langes-passwort")
    fehler: list[str] = []
    ergebnis: list[bool] = []

    def im_faden() -> None:
        try:
            ergebnis.append(s.verify("ops", "ein-langes-passwort"))
        except Exception as exc:
            fehler.append(f"{type(exc).__name__}: {exc}")

    t = threading.Thread(target=im_faden)
    t.start()
    t.join()
    assert not fehler, fehler[0]
    assert ergebnis == [True]


def test_kontonamen_werden_geprueft(tmp_path: Path) -> None:
    """Ein Name mit Wagenruecklauf faelscht die Ausgabe von ``account list``:
    er ueberschreibt die Zeile darueber und kann ein gesperrtes Konto als
    offen erscheinen lassen. Ein Name mit einer Million Zeichen sprengt die
    Ausrichtung der ganzen Liste.
    """
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    for name in ("", "   ", "opfer\rboeser", "a\nb", "\x1b[2K", "a" * 200, "a\x00b"):
        with pytest.raises(InvalidAccountName):
            s.create(name, "ein-langes-passwort")
    assert s.accounts() == []
    s.create("ops.team-1", "ein-langes-passwort")   # zulaessig bleibt zulaessig
    assert [k.name for k in s.accounts()] == ["ops.team-1"]


def test_gleich_aussehende_namen_sind_dasselbe_konto(tmp_path: Path) -> None:
    """``mueller`` in zwei Unicode-Schreibweisen sieht gleich aus. Standen
    beide nebeneinander, sperrte der Betreiber eines und liess das andere
    offen -- ohne den Unterschied sehen zu koennen.
    """
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    zusammengesetzt = "müller"        # NFC
    zerlegt = "müller"               # NFD, sieht identisch aus
    s.create(zusammengesetzt, "ein-langes-passwort")
    with pytest.raises(AccountExists):
        s.create(zerlegt, "anderes-passwort")
    assert len(s.accounts()) == 1


def test_gleich_aussehende_passwoerter_gelten_beide(tmp_path: Path) -> None:
    """Sonst haengt die Anmeldung davon ab, welche Tastatur das Passwort
    erzeugt hat -- und das Teammitglied sieht keinen Unterschied.
    """
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    s.create("ops", "Bärenstark-2026")          # NFC
    assert s.verify("ops", "Bärenstark-2026") is True   # NFD


def test_uebermaessig_langes_passwort_wird_abgewiesen(tmp_path: Path) -> None:
    """Sonst dreht der Anmeldeversuch selbst an der Rechenkosten-Schraube."""
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    with pytest.raises(InvalidPassword):
        s.create("ops", "x" * 10_000)
    assert s.accounts() == []


def test_rechte_werden_nicht_gelockert(tmp_path: Path) -> None:
    """Eine absichtlich auf 0400 gesetzte Datei ist schreibgeschuetzt. Sie auf
    0600 zu heben machte den Schutz wirkungslos -- nachgezogen wird nur, was
    **zu weit** offen steht.
    """
    pfad = tmp_path / "konten.sqlite3"
    AccountStore(pfad, hasher=_BILLIG).close()
    pfad.chmod(0o400)
    try:
        AccountStore(pfad, hasher=_BILLIG).close()
    except Exception:
        pass
    assert pfad.stat().st_mode & 0o777 == 0o400, (
        f"der Schreibschutz wurde aufgehoben: {oct(pfad.stat().st_mode & 0o777)}"
    )


def test_rechte_eines_vorhandenen_verzeichnisses(tmp_path: Path) -> None:
    ordner = tmp_path / "daten"
    ordner.mkdir(mode=0o755)
    AccountStore(ordner / "konten.sqlite3", hasher=_BILLIG).close()
    assert not (ordner.stat().st_mode & 0o077), (
        f"das Datenverzeichnis blieb begehbar: {oct(ordner.stat().st_mode & 0o777)}"
    )


def test_die_verbindung_traegt_ihre_einstellungen(tmp_path: Path) -> None:
    """Wartefrist, sicheres Loeschen und die Haltbarkeit der Schreibvorgaenge
    sind je eine Zusage, keine Feinabstimmung -- und ohne Zusicherung liesse
    sich jede still herausnehmen.
    """
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    assert s._con.execute("pragma busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
    assert s._con.execute("pragma secure_delete").fetchone()[0] == 1
    assert s._con.execute("pragma synchronous").fetchone()[0] == 2, "nicht 'full'"
    assert s._con.execute("pragma journal_mode").fetchone()[0] == "wal"


# --- ZYKLUS 7: Zusicherungen, die bisher fehlten ------------------------


def test_ein_unbekanntes_konto_wird_abgewiesen(tmp_path: Path) -> None:
    """Die wichtigste fehlende Zusicherung der ganzen Einheit.

    ``verify`` prueft ein unbekanntes Konto gegen eine **echte** Zeile aus dem
    Bestand -- nur so tragen Attrappe und Bestand dieselben Parameter (R22).
    Das heisst aber auch: faellt die eine Bedingung ``zeile is None`` weg,
    kommt jeder mit **irgendeinem** gueltigen Passwort unter **jedem** Namen
    herein. Die vorhandenen Tests riefen ``verify`` fuer unbekannte Konten nur
    zum Zeitmessen auf und warfen das Ergebnis weg.
    """
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    s.create("ops", "ein-langes-passwort")
    s.create("dev", "anderes-langes-passwort")

    assert s.verify("gibtesnicht", "ein-langes-passwort") is False, (
        "ein unbekanntes Konto wurde mit dem Passwort eines anderen angenommen"
    )
    for name in ("gibtesnicht", "", "   ", "OPS", "ops2", "op"):
        assert s.verify(name, "ein-langes-passwort") is False, f"{name!r} kam durch"
        assert s.verify(name, "anderes-langes-passwort") is False, f"{name!r} kam durch"


def test_ein_leerer_bestand_weist_ab(tmp_path: Path) -> None:
    """Der allererste Anmeldeversuch nach einer Neuinstallation.

    Ohne ein einziges Konto gibt es keine Ersatzzeile; der Zweig war von
    keinem Test betreten. Im Betrieb waere das ein 500 statt eines
    "Konto oder Passwort stimmt nicht".
    """
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    assert s.accounts() == []
    assert s.verify("wer", "auch-immer") is False
    assert s.verify("", "") is False


def test_alle_wege_sehen_dieselbe_schreibweise(tmp_path: Path) -> None:
    """Die Normalisierung war nur beim Anlegen geprueft.

    Ohne sie in ``verify`` kaeme das Teammitglied mit der anderen Schreibweise
    nicht herein; ohne sie in ``lock`` meldete ``account lock mueller``
    "das Konto gibt es nicht", waehrend das Konto offen bliebe -- genau der
    Schaden, den der Docstring beschreibt.
    """
    zusammengesetzt = "müller"        # NFC
    zerlegt = "müller"               # NFD, sieht identisch aus
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    s.create(zusammengesetzt, "ein-langes-passwort")

    assert s.verify(zerlegt, "ein-langes-passwort") is True, "verify normalisiert nicht"
    assert s.state_of(zerlegt) is not None, "state_of normalisiert nicht"
    s.lock(zerlegt)
    assert s.state_of(zusammengesetzt).locked is True, "lock normalisiert nicht"  # type: ignore[union-attr]
    s.unlock(zerlegt)
    s.set_password(zerlegt, "neues-langes-passwort")
    assert s.verify(zusammengesetzt, "neues-langes-passwort") is True, (
        "set_password normalisiert nicht"
    )


def test_die_zeitschranke_faengt_beide_richtungen(tmp_path: Path) -> None:
    """Die alte Schranke ``unbekannt > bekannt * 0.5`` fing nur "zu schnell"
    und liess Faktor zwei zu -- genau die Richtung, in die der gefundene
    Fehler ging (unbekannt war 2,7-mal **langsamer**).
    """
    s = AccountStore(tmp_path / "konten.sqlite3", hasher=_BILLIG)
    s.create("bekannt", "ein-langes-passwort")
    m = _mediane(s, {
        "unbekannt": ("gibtesnicht", "falsches-passwort"),
        "bekannt": ("bekannt", "falsches-passwort"),
    }, runden=15)
    verhaeltnis = m["unbekannt"] / m["bekannt"]
    assert 0.6 < verhaeltnis < 1.6, (
        f"unbekannt {m['unbekannt']:.3f} ms, bekannt {m['bekannt']:.3f} ms, "
        f"Verhaeltnis {verhaeltnis:.2f}"
    )
