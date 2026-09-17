"""Konto-Unterbefehle der Kommandozeile.

Die gefaehrliche Stelle ist die Passworteingabe. Ein Passwort als Argument
steht in der Prozessliste, im Shell-Verlauf und in jedem Protokoll, das
Kommandozeilen mitschreibt -- und zwar dauerhaft. Deshalb nimmt der Befehl
gar keines entgegen; er fragt danach, verdeckt, und laesst es bestaetigen.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Kein Passwort als Argument (R20) ===
1. ``account create`` kennt keine Passwort-Option.
2. Ein zweites Wortargument wird abgewiesen, statt als Passwort zu gelten.
3. Das Passwort wird verdeckt erfragt und bestaetigt.
4. Weichen die beiden Eingaben ab, entsteht kein Konto.
5. Ein leeres Passwort wird abgewiesen.

=== ZYKLUS 2: Die uebrigen Unterbefehle (R20) ===
6. ``account list`` nennt Name und Zustand -- nie den gespeicherten Wert.
7. ``account lock`` und ``unlock`` wirken.
8. ``account passwd`` ersetzt das Passwort.

=== ZYKLUS 3: Fehlerfaelle ===
9. Ein unbekanntes Konto wird benannt und mit einem Rueckgabewert
   ungleich null gemeldet.
10. Ein zweites Anlegen desselben Namens ebenso.
11. Ohne Terminal -- systemd, CI, eine Pipe am Ende -- bricht der Befehl
    sauber ab, statt mit einem Traceback auszusehen wie kaputt.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from swarmq_dashboard.accounts import AccountStore
from swarmq_dashboard.cli.__main__ import main

_BILLIG = PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1)


@pytest.fixture
def daten(tmp_path: Path) -> Path:
    return tmp_path


def _eingaben(*werte: str) -> Iterator[str]:
    return iter(werte)


def _lauf(daten: Path, *argv: str, eingaben: tuple[str, ...] = ()) -> tuple[int, str]:
    """Einen Befehl fahren. Gibt Rueckgabewert und Ausgabe zurueck."""
    quelle = _eingaben(*eingaben)
    ausgabe = io.StringIO()
    code = main(
        ["--data-dir", str(daten), *argv],
        prompt=lambda _: next(quelle),
        out=ausgabe,
        hasher=_BILLIG,
    )
    return code, ausgabe.getvalue()


def _store(daten: Path) -> AccountStore:
    return AccountStore(daten / "accounts.sqlite3", hasher=_BILLIG)


# --- Zyklus 1 -----------------------------------------------------------


def test_kein_passwort_als_option(daten: Path) -> None:
    """Ein Passwort als Argument steht in der Prozessliste und im
    Shell-Verlauf -- dauerhaft und fuer jeden auf dem Rechner sichtbar.
    """
    with pytest.raises(SystemExit) as e:
        _lauf(daten, "account", "create", "ops", "--password", "geheim")
    assert e.value.code == 2


def test_zweites_wortargument_wird_abgewiesen(daten: Path) -> None:
    """Sonst wuerde ``account create ops geheim`` klaglos angenommen und das
    Passwort landete doch in der Prozessliste.
    """
    with pytest.raises(SystemExit) as e:
        _lauf(daten, "account", "create", "ops", "geheim")
    assert e.value.code == 2


def test_passwort_wird_erfragt_und_bestaetigt(daten: Path) -> None:
    gefragt: list[str] = []
    ausgabe = io.StringIO()
    quelle = _eingaben("langes-passwort", "langes-passwort")

    def prompt(text: str) -> str:
        gefragt.append(text)
        return next(quelle)

    code = main(["--data-dir", str(daten), "account", "create", "ops"],
                prompt=prompt, out=ausgabe, hasher=_BILLIG)
    assert code == 0
    assert len(gefragt) == 2, f"nicht bestaetigen lassen: {gefragt}"
    assert _store(daten).verify("ops", "langes-passwort") is True


def test_abweichende_bestaetigung_legt_nichts_an(daten: Path) -> None:
    code, text = _lauf(daten, "account", "create", "ops",
                       eingaben=("langes-passwort", "vertippt"))
    assert code != 0
    assert "stimmen nicht" in text.lower() or "abweich" in text.lower(), text
    assert _store(daten).accounts() == [], "trotz Abbruch wurde ein Konto angelegt"


def test_leeres_passwort_wird_abgewiesen(daten: Path) -> None:
    code, _ = _lauf(daten, "account", "create", "ops", eingaben=("", ""))
    assert code != 0
    assert _store(daten).accounts() == []


# --- Zyklus 2 -----------------------------------------------------------


def test_liste_nennt_name_und_zustand_nie_den_wert(daten: Path) -> None:
    s = _store(daten)
    s.create("ops", "langes-passwort")
    s.lock("ops")
    s.create("dev", "anderes-passwort")
    wert = next(k.password_hash for k in s.accounts() if k.name == "ops")
    s.close()

    code, text = _lauf(daten, "account", "list")
    assert code == 0
    assert "ops" in text and "dev" in text
    assert "gesperrt" in text.lower(), f"der Sperrzustand fehlt: {text}"
    assert wert not in text and "$argon2" not in text, "der gespeicherte Wert steht in der Ausgabe"


def test_sperren_und_entsperren(daten: Path) -> None:
    s = _store(daten)
    s.create("ops", "langes-passwort")
    s.close()

    assert _lauf(daten, "account", "lock", "ops")[0] == 0
    assert _store(daten).verify("ops", "langes-passwort") is False
    assert _lauf(daten, "account", "unlock", "ops")[0] == 0
    assert _store(daten).verify("ops", "langes-passwort") is True


def test_passwort_aendern(daten: Path) -> None:
    s = _store(daten)
    s.create("ops", "altes-passwort")
    s.close()

    code, _ = _lauf(daten, "account", "passwd", "ops",
                    eingaben=("neues-passwort", "neues-passwort"))
    assert code == 0
    s2 = _store(daten)
    assert s2.verify("ops", "altes-passwort") is False
    assert s2.verify("ops", "neues-passwort") is True


# --- Zyklus 3 -----------------------------------------------------------


def test_unbekanntes_konto_wird_benannt(daten: Path) -> None:
    code, text = _lauf(daten, "account", "lock", "gibtesnicht")
    assert code != 0, "ein Fehlschlag mit Rueckgabewert 0 faellt in einem Skript nicht auf"
    assert "gibtesnicht" in text


def test_zweites_anlegen_wird_benannt(daten: Path) -> None:
    s = _store(daten)
    s.create("ops", "langes-passwort")
    s.close()
    code, text = _lauf(daten, "account", "create", "ops",
                       eingaben=("neues-passwort", "neues-passwort"))
    assert code != 0
    assert "ops" in text
    assert _store(daten).verify("ops", "langes-passwort") is True, "das Passwort wurde ersetzt"


def test_ohne_terminal_bricht_er_sauber_ab(daten: Path) -> None:
    """``getpass`` wirft ``EOFError``, wenn nichts mehr zu lesen ist. Ein
    Traceback saehe fuer den Betreiber aus, als waere das Werkzeug kaputt --
    dabei fehlt nur die Eingabe.
    """
    ausgabe = io.StringIO()

    def kein_terminal(_: str) -> str:
        raise EOFError

    code = main(["--data-dir", str(daten), "account", "create", "ops"],
                prompt=kein_terminal, out=ausgabe, hasher=_BILLIG)
    assert code != 0
    assert "abgebrochen" in ausgabe.getvalue().lower(), ausgabe.getvalue()
    assert _store(daten).accounts() == []
