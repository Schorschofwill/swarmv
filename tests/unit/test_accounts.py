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
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from swarmq_dashboard.accounts import AccountExists, AccountStore, UnknownAccount

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


def _dauer(fn: object, runden: int = 15) -> float:
    """Mittlere Dauer in Sekunden. Median, damit ein Ausreisser nichts kippt."""
    proben = []
    for _ in range(runden):
        t = time.perf_counter()
        fn()  # type: ignore[operator]
        proben.append(time.perf_counter() - t)
    return statistics.median(proben)


def test_unbekanntes_konto_ist_nicht_schneller(store: AccountStore) -> None:
    """Covers AE9a.

    Antwortet ein unbekanntes Konto sofort, laesst sich aus den Antwortzeiten
    die Kontenliste ablesen, ohne ein einziges Passwort zu kennen.
    """
    store.create("bekannt", "ein-langes-passwort")
    bekannt = _dauer(lambda: store.verify("bekannt", "falsches-passwort"))
    unbekannt = _dauer(lambda: store.verify("gibtesnicht", "falsches-passwort"))
    assert unbekannt > bekannt * 0.5, (
        f"unbekannt {unbekannt * 1000:.2f} ms gegen bekannt {bekannt * 1000:.2f} ms -- "
        "die Antwortzeit verraet, welche Konten es gibt"
    )


def test_gesperrtes_konto_ist_nicht_schneller(store: AccountStore) -> None:
    """Sonst verraet die Antwortzeit, welche Konten gesperrt sind."""
    store.create("offen", "ein-langes-passwort")
    store.create("gesperrt", "ein-langes-passwort")
    store.lock("gesperrt")
    offen = _dauer(lambda: store.verify("offen", "falsch"))
    zu = _dauer(lambda: store.verify("gesperrt", "ein-langes-passwort"))
    assert zu > offen * 0.5, (
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
