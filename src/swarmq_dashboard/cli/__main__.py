"""``swarmq-dashboard`` -- Kommandozeile des Betreibers.

Ein Grundsatz traegt diese Datei: **kein Passwort als Argument**. Ein Passwort
in der Kommandozeile steht in der Prozessliste, im Shell-Verlauf und in jedem
Protokoll, das Kommandozeilen mitschreibt -- dauerhaft und fuer jeden auf dem
Rechner sichtbar. Der Parser kennt deshalb weder eine Passwort-Option noch ein
zweites Wortargument, in das eines rutschen koennte; er fragt danach, verdeckt,
und laesst es bestaetigen (R20).

``argparse`` aus der Standardbibliothek, kein weiteres Paket: der Umfang sind
fuenf Unterbefehle, und die Gegenleistung eines Rahmenwerks waere hier nur
eine weitere Abhaengigkeit.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from argon2 import PasswordHasher

from swarmq_dashboard.accounts import AccountError, AccountStore
from swarmq_dashboard.config import DEFAULT_DATA_DIR, accounts_path

#: Kuerzestes zulaessiges Passwort. Keine Zeichenklassen-Regeln: sie treiben
#: nachweislich zu vorhersagbaren Mustern, waehrend die Laenge wirklich traegt.
MIN_PASSWORD_LEN = 12

Prompt = Callable[[str], str]


def _passwort_erfragen(prompt: Prompt, out: TextIO, *, wofuer: str) -> str | None:
    """Passwort verdeckt erfragen und bestaetigen lassen.

    ``None`` heisst: abgebrochen, und der Grund steht bereits in ``out``. Die
    Bestaetigung ist kein Zierrat -- ein Vertipper in einer verdeckten Eingabe
    faellt sonst erst beim Anmelden auf, wenn niemand mehr weiss, was getippt
    wurde.
    """
    try:
        erste = prompt(f"Passwort fuer {wofuer}: ")
    except (EOFError, KeyboardInterrupt):
        # Kein Terminal (systemd, CI, eine Pipe am Ende) oder Strg-C. Beides
        # ist ein Abbruch, kein Absturz: ein Traceback saehe hier aus, als
        # waere das Werkzeug kaputt.
        print("\nAbgebrochen. Es wurde nichts geaendert.", file=out)
        return None
    if len(erste) < MIN_PASSWORD_LEN:
        print(f"Das Passwort braucht mindestens {MIN_PASSWORD_LEN} Zeichen.", file=out)
        return None
    try:
        zweite = prompt("Passwort wiederholen: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAbgebrochen. Es wurde nichts geaendert.", file=out)
        return None
    if erste != zweite:
        print("Die beiden Eingaben stimmen nicht ueberein. Es wurde nichts geaendert.", file=out)
        return None
    return erste


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarmq-dashboard",
        description="Ops-Dashboard fuer swarmq: Konten verwalten und den Dienst fahren.",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=Path(DEFAULT_DATA_DIR),
        help=f"Verzeichnis fuer Verzeichnis- und Kontendatei (Vorgabe: {DEFAULT_DATA_DIR})",
    )
    unter = p.add_subparsers(dest="befehl", required=True)

    konto = unter.add_parser("account", help="Konten anlegen, aendern, sperren")
    aktionen = konto.add_subparsers(dest="aktion", required=True)

    # Bewusst je nur *ein* Wortargument: ein zweites waere die Stelle, an der
    # ein Passwort in die Prozessliste rutscht.
    for name, hilfe in (
        ("create", "Konto anlegen; das Passwort wird verdeckt erfragt"),
        ("passwd", "Passwort ersetzen; es wird verdeckt erfragt"),
        ("lock", "Konto sperren -- es bleibt bestehen"),
        ("unlock", "Sperre aufheben"),
    ):
        a = aktionen.add_parser(name, help=hilfe)
        a.add_argument("name", help="Kontoname")
    aktionen.add_parser("list", help="Konten mit ihrem Zustand zeigen")
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Prompt = getpass.getpass,
    out: TextIO | None = None,
    hasher: PasswordHasher | None = None,
) -> int:
    """Einstiegspunkt. Gibt den Rueckgabewert des Prozesses zurueck.

    Die drei Schluesselwortargumente sind die Naht fuer die Testreihe: ohne
    sie liesse sich weder eine verdeckte Eingabe noch die Ausgabe pruefen, und
    die Testreihe muesste mit den vollen argon2-Parametern rechnen -- fuenfzig
    Millisekunden je Pruefung.
    """
    ziel = sys.stdout if out is None else out
    args = _build_parser().parse_args(argv)

    store = AccountStore(accounts_path(args.data_dir), hasher=hasher)
    try:
        return _konto(args, store, prompt, ziel)
    except AccountError as exc:
        # Benannt und mit Rueckgabewert ungleich null: ein Fehlschlag mit 0
        # faellt in einem Skript nicht auf.
        print(str(exc), file=ziel)
        return 1
    finally:
        store.close()


def _konto(args: argparse.Namespace, store: AccountStore, prompt: Prompt, out: TextIO) -> int:
    if args.aktion == "list":
        konten = store.accounts()
        if not konten:
            print("Noch kein Konto angelegt.", file=out)
            return 0
        breite = max(len(k.name) for k in konten)
        for k in konten:
            # Nie der gespeicherte Wert: eine Kontenliste landet im Ticket,
            # im Chat und im Bildschirmfoto.
            print(f"{k.name:<{breite}}  {'gesperrt' if k.locked else 'offen'}", file=out)
        return 0

    if args.aktion in ("create", "passwd"):
        passwort = _passwort_erfragen(prompt, out, wofuer=args.name)
        if passwort is None:
            return 1
        if args.aktion == "create":
            store.create(args.name, passwort)
            print(f"Konto {args.name} angelegt.", file=out)
        else:
            store.set_password(args.name, passwort)
            print(f"Passwort von {args.name} ersetzt.", file=out)
        return 0

    if args.aktion == "lock":
        store.lock(args.name)
        print(f"Konto {args.name} gesperrt.", file=out)
        return 0

    store.unlock(args.name)
    print(f"Sperre von {args.name} aufgehoben.", file=out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
