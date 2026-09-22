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
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from argon2 import PasswordHasher

from swarmq_dashboard.accounts import AccountError, AccountStore
from swarmq_dashboard.config import DEFAULT_DATA_DIR, accounts_path

#: Breiteste Spalte in ``account list``. Die Namenspruefung deckelt Namen
#: bereits; ein Bestandsname aus einer aelteren Fassung koennte laenger sein,
#: und dann polsterte die Ausrichtung **jede** Zeile auf seine Laenge.
MAX_SPALTE = 64

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


def _datenverzeichnis(wert: str) -> Path:
    """``--data-dir`` pruefen und umwandeln.

    Die Pruefung muss **vor** der Umwandlung greifen: ``Path("")`` ergibt
    ``Path(".")``, und die Kontendatei landete damit stillschweigend im
    Arbeitsverzeichnis -- an einem Ort, den der Betreiber weder sichert noch
    schuetzt.
    """
    if not wert.strip():
        raise argparse.ArgumentTypeError("--data-dir darf nicht leer sein")
    return Path(wert)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="swarmq-dashboard",
        description="Ops-Dashboard fuer swarmq: Konten verwalten und den Dienst fahren.",
    )
    p.add_argument(
        "--data-dir",
        type=_datenverzeichnis,
        # **Ohne** Vorgabewert: sonst liesse sich ein ausdruecklich
        # angegebenes Verzeichnis nicht von der Vorgabe unterscheiden, und die
        # Vorgabe wuerde ein gesetztes SQD_DATA_DIR ueberschreiben. Die
        # Aufloesung macht ``_datenort``.
        default=None,
        help=("Verzeichnis fuer Verzeichnis- und Kontendatei "
              f"(sonst SQD_DATA_DIR, sonst {DEFAULT_DATA_DIR})"),
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

    dienst = unter.add_parser("serve", help="den Dienst fahren")
    dienst.add_argument("--host", default="127.0.0.1",
                        help="Adresse, auf der gelauscht wird (Vorgabe: 127.0.0.1)")
    dienst.add_argument("--port", type=int, default=8080, help="Port (Vorgabe: 8080)")
    dienst.add_argument("--tls-cert", type=Path, help="Zertifikat im PEM-Format")
    dienst.add_argument("--tls-key", type=Path, help="Schluessel im PEM-Format")
    dienst.add_argument(
        "--insecure-http", action="store_true",
        help="ohne TLS auf 127.0.0.1 lauschen -- nur fuer die Entwicklung. "
             "Die Sitzungscookies verlieren dabei ihr 'Secure'.",
    )

    audit = unter.add_parser("audit", help="nachsehen, wer was getan hat (R27)")
    audit_aktionen = audit.add_subparsers(dest="aktion", required=True)
    liste = audit_aktionen.add_parser("list", help="Eintraege in Zeitreihenfolge zeigen")
    liste.add_argument("--account", help="nur Eintraege dieses Kontos")
    liste.add_argument("--action", help="nur Eintraege dieser Aktionsart")
    liste.add_argument("--limit", type=int, default=200, help="hoechstens so viele Eintraege")
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Prompt = getpass.getpass,
    out: TextIO | None = None,
    hasher: PasswordHasher | None = None,
    audit_stream: Any | None = None,
    server: Any | None = None,
    umgebung: Mapping[str, str] | None = None,
) -> int:
    """Einstiegspunkt. Gibt den Rueckgabewert des Prozesses zurueck.

    Die drei Schluesselwortargumente sind die Naht fuer die Testreihe: ohne
    sie liesse sich weder eine verdeckte Eingabe noch die Ausgabe pruefen, und
    die Testreihe muesste mit den vollen argon2-Parametern rechnen -- fuenfzig
    Millisekunden je Pruefung.
    """
    ziel = sys.stdout if out is None else out
    args = _build_parser().parse_args(argv)
    if args.befehl == "serve":
        return _serve(args, ziel, server, umgebung)
    if args.befehl == "audit":
        return _audit(args, ziel, audit_stream)

    store = None
    try:
        # Die Konstruktion gehoert **in** den try: sie legt Verzeichnis und
        # Datei an, und jeder Fehler dabei -- ein Pfad, der auf eine Datei
        # zeigt, ein toter Symlink, ein Verzeichnis ohne Schreibrecht, eine
        # volle Platte -- kam sonst als Traceback heraus und sah aus, als
        # waere das Werkzeug kaputt.
        store = AccountStore(accounts_path(_datenort(args)), hasher=hasher)
        return _konto(args, store, prompt, ziel)
    except AccountError as exc:
        # Benannt und mit Rueckgabewert ungleich null: ein Fehlschlag mit 0
        # faellt in einem Skript nicht auf.
        print(str(exc), file=ziel)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(f"Die Kontendatei unter {_datenort(args)} ist nicht benutzbar: {exc}", file=ziel)
        return 1
    finally:
        if store is not None:
            store.close()


def _datenort(args: argparse.Namespace, umgebung: Mapping[str, str] | None = None) -> Path:
    """Wo die beiden lokalen Dateien liegen -- fuer **alle** Unterbefehle gleich.

    Die Rangfolge steht hier an einer Stelle, weil sie sonst auseinanderlief:
    ``account`` und ``audit`` lasen nur die Kommandozeile, der Dienst nur die
    Umgebung. Wer seine Konten mit ``--data-dir`` anlegte und den Dienst
    genauso startete, bekam zwei verschiedene Verzeichnisse und eine
    Anmeldung, die "Konto oder Passwort stimmt nicht" sagte.
    """
    if args.data_dir is not None:
        return Path(args.data_dir)
    quelle: Mapping[str, str] = os.environ if umgebung is None else umgebung
    return Path(quelle.get("SQD_DATA_DIR", "").strip() or DEFAULT_DATA_DIR)


#: Adressen, auf denen eine Klartextverbindung vertretbar ist: nur der eigene
#: Rechner. Ueberall sonst schickt der Browser das Sitzungscookie nicht mit --
#: es traegt ``secure`` --, und die Anmeldung kann gar nicht gelingen.
OERTLICH = ("127.0.0.1", "::1", "localhost")


def _serve(args: argparse.Namespace, out: TextIO, server: Any,
           umgebung: Mapping[str, str] | None) -> int:
    """Den Dienst fahren (KTD5: **ein** Arbeiter).

    Die Zahl ist nicht Geschmack. Der prozessinterne Riegel der
    Dead-Letter-Bedienung und die Kanal-Verteilung der Live-Schicht leben im
    Prozess. Mit zwei Arbeitern traegt beides nicht mehr, und zwar **still**:
    zwei Teammitglieder koennten dieselbe Queue gleichzeitig sichten, ohne
    dass es jemand bemerkt.
    """
    if bool(args.tls_cert) != bool(args.tls_key):
        fehlt = "Schluessel (--tls-key)" if args.tls_cert else "Zertifikat (--tls-cert)"
        print(f"Es fehlt der {fehlt}. Ohne beides zusammen gibt es kein TLS -- "
              "und still ohne zu starten waere schlimmer, als gar nicht zu starten.",
              file=out)
        return 2
    if not args.tls_cert:
        # Ohne TLS ueberhaupt nicht -- auch nicht oertlich. Beim ersten
        # echten Start nachgemessen: die Cookies tragen 'Secure', kein Klient
        # schickt sie ueber http, der POST kommt ohne Sitzung an, und die
        # Anmeldung antwortet "Das Anmeldeformular ist abgelaufen". Ein
        # Dashboard, in das niemand hineinkommt, ist kein laufendes
        # Dashboard.
        if not args.insecure_http:
            print(
                f"Auf {args.host} ohne TLS zu lauschen geht nicht auf: die "
                "Sitzungscookies tragen 'Secure', kein Browser schickt sie "
                "ueber eine Klartextverbindung mit, und die Anmeldung "
                "scheitert mit 'Das Anmeldeformular ist abgelaufen'.\n"
                "Drei Wege: --tls-cert und --tls-key angeben; hinter einem "
                "vorgelagerten TLS-Abschluss lauschen (der spricht dann https "
                "nach aussen); oder fuer die Entwicklung auf 127.0.0.1 mit "
                "--insecure-http starten.",
                file=out,
            )
            return 2
        if args.host not in OERTLICH:
            print(
                f"--insecure-http gilt nur oertlich, nicht auf {args.host}. "
                "Sonst waere die Ausnahme fuer die Entwicklung der bequemste "
                "Weg, TLS ganz wegzulassen.",
                file=out,
            )
            return 2

    # Die Einstellungen **vor** dem Start pruefen: ein Traceback aus dem
    # Arbeitsprozess sieht aus, als waere das Werkzeug kaputt, dabei fehlt
    # nur eine Variable.
    from swarmq_dashboard.config import DashboardConfig

    try:
        DashboardConfig.from_env(umgebung)
    except Exception as exc:
        print(f"Die Einstellungen taugen nicht: {exc}", file=out)
        return 2

    if args.data_dir is not None:
        # Derselbe Weg wie bei --insecure-http: Granian bekommt einen
        # Modulpfad und ruft die Fabrik selbst auf; der Arbeitsprozess kennt
        # die Kommandozeile nicht. Ohne diese Zeile lief der Dienst auf
        # ./sqd-data, waehrend die Konten woanders lagen.
        os.environ["SQD_DATA_DIR"] = str(args.data_dir)
    if args.insecure_http:
        os.environ["SQD_INSECURE_HTTP"] = "1"

    if server is None:
        from granian import Granian

        server = Granian
    lauf = server(
        "swarmq_dashboard.web.app:aus_der_umgebung",
        address=args.host,
        port=args.port,
        interface="asgi",
        factory=True,
        # **Einer.** Siehe Docstring; KTD5.
        workers=1,
        ssl_cert=args.tls_cert,
        ssl_key=args.tls_key,
    )
    ziel = f"https://{args.host}:{args.port}" if args.tls_cert else \
        f"http://{args.host}:{args.port}"
    print(f"Dashboard laeuft auf {ziel} -- mit einem Arbeiter (KTD5).", file=out)
    if args.insecure_http:
        print("ACHTUNG: ohne TLS, und die Sitzungscookies verlieren ihr "
              "'Secure'. Nur fuer die Entwicklung auf diesem Rechner.",
              file=out)
    lauf.serve()
    return 0


def _konto(args: argparse.Namespace, store: AccountStore, prompt: Prompt, out: TextIO) -> int:
    if args.aktion == "list":
        konten = store.accounts()
        if not konten:
            print("Noch kein Konto angelegt.", file=out)
            return 0
        breite = min(max(len(k.name) for k in konten), MAX_SPALTE)
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


def _audit(args: argparse.Namespace, out: TextIO, stream: Any | None) -> int:
    """Den Audit-Strom lesbar ausgeben (R27).

    Ein nicht erreichbarer Strom ergibt einen Rueckgabewert ungleich null.
    Stille leere Ausgabe waere hier die gefaehrlichste Antwort: ein Skript
    laese "keine Aktionen", wo in Wahrheit "kein Strom" gilt.
    """
    import asyncio

    from swarmq_dashboard.audit import AuditLog
    from swarmq_dashboard.config import DashboardConfig
    from swarmq_dashboard.stream import StreamUnavailable, ValkeyStreamClient

    async def lesen() -> int:
        klient = stream
        if klient is None:
            try:
                url = DashboardConfig.from_env().stream_url
            except Exception as exc:
                print(f"Der Ereignisstrom ist nicht eingestellt: {exc}", file=out)
                return 1
            klient = ValkeyStreamClient(url)
        try:
            await klient.connect()
        except StreamUnavailable as exc:
            print(f"Der Audit-Strom ist nicht erreichbar: {exc}", file=out)
            return 1
        try:
            eintraege = await AuditLog(klient).read(
                account=args.account, action=args.action, limit=args.limit
            )
        finally:
            await klient.close()

        if not eintraege:
            print("Keine Eintraege im Audit-Strom.", file=out)
            return 0
        for e in eintraege:
            zeit = datetime.fromtimestamp(e.ts, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
            teile = [zeit, e.account, e.action, e.outcome]
            if e.target:
                teile.append(f"-> {e.target}")
            if e.detail:
                teile.append(f"({e.detail})")
            print("  ".join(teile), file=out)
        return 0

    return asyncio.run(lesen())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
