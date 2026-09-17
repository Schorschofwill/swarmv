"""Konten und Passwoerter -- in einer eigenen Datei neben dem Verzeichnis.

**Warum eine eigene Datei** (KTD8): Der Neuaufbau des Verzeichnisses aus dem
Strom ist ein vorgesehener Weg (R8, U5). Laegen Konten in derselben Datei,
spaerrte ihr Loeschen das ganze Team aus -- ausgerechnet in dem Vorfall, fuer
den das Dashboard existiert. Und nicht in Valkey: liegt Valkey, kaeme niemand
mehr hinein, und genau dann braucht man das Werkzeug.

**Warum gegen einen Attrappen-Wert geprueft wird** (R22, AE9a): Ein Versuch
auf ein Konto, das es nicht gibt, darf nicht schneller beantwortet werden als
einer auf ein bestehendes. Antwortete er sofort, liesse sich aus den
Antwortzeiten die Kontenliste ablesen -- ohne ein einziges Passwort zu kennen.
Dasselbe gilt fuer ein gesperrtes Konto: sonst verraet die Antwortzeit den
Sperrzustand. Deshalb laeuft **jeder** Versuch durch dieselbe Pruefung, und
erst danach entscheidet sich, ob das Ergebnis zaehlt.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: Wartefrist auf gesperrte Schreibvorgaenge -- wie beim Verzeichnis.
BUSY_TIMEOUT_MS = 5000
#: Laengster zulaessiger Kontoname. Ohne Grenze polsterte ein Name mit einer
#: Million Zeichen jede Zeile von ``account list`` auf diese Laenge.
MAX_NAME_LEN = 64
#: Laengste zulaessige Passworteingabe. Ohne Grenze dreht der Anmeldeversuch
#: selbst an der Rechenkosten-Schraube: gemessen kostet ein Passwort mit zehn
#: Millionen Zeichen rund zwanzig Millisekunden zusaetzlich -- je Versuch.
MAX_PASSWORD_LEN = 1024

_SCHEMA = """
create table if not exists account (
    name          text primary key,
    -- argon2id, mit Verfahren und Parametern im Wert selbst. Nie Klartext.
    password_hash text not null,
    -- Sperren ist ein Zustand, kein Loeschen: ein geloeschtes Konto liesse
    -- sich neu anlegen, ein gesperrtes nicht (R20).
    locked        integer not null default 0,
    created_at    real not null,
    updated_at    real not null
) without rowid;
"""


class AccountError(RuntimeError):
    """Oberklasse der Kontenfehler."""


class AccountExists(AccountError):
    """Das Konto gibt es bereits."""


class UnknownAccount(AccountError):
    """Das Konto gibt es nicht."""


class InvalidAccountName(AccountError):
    """Der Name taugt nicht als Kontoname."""


class InvalidPassword(AccountError):
    """Das Passwort taugt nicht -- zu lang, oder gar keines."""


def _normalisiert(text: str) -> str:
    """Unicode auf eine Schreibweise bringen.

    ``mueller`` laesst sich auf zwei Arten schreiben, die gleich aussehen. Ohne
    Normalisierung stuenden beide als getrennte Konten nebeneinander, und wer
    eines sperrt, liesse das andere offen -- ohne den Unterschied sehen zu
    koennen. Beim Passwort dasselbe: sonst haengt die Anmeldung davon ab,
    welche Tastatur es erzeugt hat.
    """
    return unicodedata.normalize("NFC", text)


def _pruef_name(name: str) -> str:
    """Kontonamen normalisieren und pruefen.

    Bewusst **keine** Beschraenkung auf ASCII: ``mueller`` mit Umlaut ist ein
    naheliegender Kontoname, und ihn abzuweisen waere Schikane ohne Gewinn.
    Abgewiesen wird, was die Ausgabe faelschen kann -- Steuerzeichen und
    Leerraum -- und was sie sprengt: ein Name mit einer Million Zeichen
    polsterte jede Zeile von ``account list`` auf diese Laenge, ein
    Wagenruecklauf ueberschreibt die Zeile darueber und kann ein gesperrtes
    Konto als offen erscheinen lassen.
    """
    sauber = _normalisiert(name)
    if not 1 <= len(sauber) <= MAX_NAME_LEN:
        raise InvalidAccountName(
            f"ein Kontoname braucht 1 bis {MAX_NAME_LEN} Zeichen, dieser hat {len(sauber)}"
        )
    if not sauber.isprintable() or any(z.isspace() for z in sauber):
        raise InvalidAccountName(
            f"unbrauchbarer Kontoname {name!r}: Steuerzeichen und Leerraum sind "
            "nicht erlaubt -- sie koennen die Ausgabe von 'account list' faelschen"
        )
    return sauber


def _pruef_passwort(passwort: str) -> str:
    """Passwort normalisieren und auf Laenge pruefen."""
    sauber = _normalisiert(passwort)
    if not sauber:
        raise InvalidPassword("leeres Passwort")
    if len(sauber) > MAX_PASSWORD_LEN:
        raise InvalidPassword(f"das Passwort ist laenger als {MAX_PASSWORD_LEN} Zeichen")
    return sauber


@dataclass(frozen=True, slots=True)
class Account:
    """Ein Konto, wie die Kommandozeile es zeigt."""

    name: str
    locked: bool
    created_at: float
    updated_at: float
    #: Der gespeicherte Wert. Steht hier, weil die Testreihe ihn braucht --
    #: die Kommandozeile gibt ihn nie aus.
    password_hash: str


class AccountStore:
    """Lokale Konten mit argon2id-Passwoertern."""

    def __init__(self, path: str | Path, *, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher()
        self._con = _connect(Path(path))
        self._con.row_factory = sqlite3.Row
        # Die Verbindung wird aus mehreren Faden benutzt -- die Anmeldung ist
        # ein rund fuenfzig Millisekunden teurer Rechenblock und gehoert in
        # einen Arbeitsfaden, und dort ist jede Anfrage moeglicherweise ein
        # anderer. Der Riegel haelt die Zugriffe auseinander.
        self._riegel = threading.Lock()
        # Kein Attrappen-Wert auf Vorrat: ihn beim Oeffnen zu bilden kostete
        # gemessen 59 ms -- auch fuer ein blosses ``account list``. Und er
        # traegt die Parameter des **Bestands**, nicht die des gerade
        # eingestellten Verfahrens; siehe ``verify``.
        self._ersatz: str | None = None

    def close(self) -> None:
        """Verbindung schliessen."""
        self._con.close()

    # -- Lesen -----------------------------------------------------------

    def accounts(self) -> list[Account]:
        """Alle Konten, nach Namen geordnet."""
        with self._riegel:
            zeilen = self._con.execute("select * from account order by name").fetchall()
        return [
            Account(
                name=str(z["name"]),
                locked=bool(z["locked"]),
                created_at=float(z["created_at"]),
                updated_at=float(z["updated_at"]),
                password_hash=str(z["password_hash"]),
            )
            for z in zeilen
        ]

    def _row(self, name: str) -> sqlite3.Row | None:
        with self._riegel:
            zeile: sqlite3.Row | None = self._con.execute(
                "select * from account where name=?", (name,)
            ).fetchone()
        return zeile

    def _zeile_oder_ersatz(self, name: str) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        """Die gesuchte Zeile -- oder, wenn es sie nicht gibt, irgendeine.

        **Eine** Abfrage fuer beide Faelle, und das ist der Punkt: Rechenzeit
        allein genuegt nicht, auch die Zahl der Datenbankbefehle muss gleich
        sein, sonst verraet sie ueber einen anderen Kanal dasselbe.

        Die Ersatzzeile ist eine echte, vorhandene: nur so traegt der Wert,
        gegen den geprueft wird, dieselben Verfahrensparameter wie der
        Bestand. Eine frisch gebildete Attrappe trug die des eingestellten
        Verfahrens -- und sobald beide auseinanderliefen, war ein unbekanntes
        Konto gemessen 2,7-mal langsamer als ein bekanntes. Ausgerechnet der
        Zustand, den das Erneuern erzeugt, hebelte die Zusage aus.

        Die Sortierung laeuft ueber die ganze Tabelle. Das ist hier eine
        Handvoll Zeilen -- ein Team, kein Verzeichnis.
        """
        with self._riegel:
            zeile: sqlite3.Row | None = self._con.execute(
                "select * from account order by (name = ?) desc, name limit 1", (name,)
            ).fetchone()
        if zeile is not None and str(zeile["name"]) == name:
            return zeile, zeile
        return None, zeile

    # -- Schreiben -------------------------------------------------------

    def create(self, name: str, password: str) -> None:
        """Konto anlegen. Wirft, wenn es den Namen schon gibt."""
        sauber = _pruef_name(name)
        wert = self._hasher.hash(_pruef_passwort(password))
        jetzt = time.time()
        with self._riegel:
            try:
                self._con.execute(
                    "insert into account(name, password_hash, locked, created_at, updated_at) "
                    "values (?,?,0,?,?)",
                    (sauber, wert, jetzt, jetzt),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountExists(f"das Konto {sauber!r} gibt es bereits") from exc
            self._con.commit()

    def set_password(self, name: str, password: str) -> None:
        """Passwort ersetzen. Das alte gilt danach nicht mehr."""
        sauber = _normalisiert(name).strip()
        wert = self._hasher.hash(_pruef_passwort(password))
        with self._riegel:
            cur = self._con.execute(
                "update account set password_hash=?, updated_at=? where name=?",
                (wert, time.time(), sauber),
            )
            if not cur.rowcount:
                raise UnknownAccount(f"das Konto {sauber!r} gibt es nicht")
            self._con.commit()

    def lock(self, name: str) -> None:
        """Konto sperren. Es bleibt bestehen und bleibt in der Liste."""
        self._set_locked(name, True)

    def unlock(self, name: str) -> None:
        """Sperre aufheben."""
        self._set_locked(name, False)

    def _set_locked(self, name: str, wert: bool) -> None:
        sauber = _normalisiert(name).strip()
        with self._riegel:
            cur = self._con.execute(
                "update account set locked=?, updated_at=? where name=?",
                (int(wert), time.time(), sauber),
            )
            if not cur.rowcount:
                raise UnknownAccount(f"das Konto {sauber!r} gibt es nicht")
            self._con.commit()

    # -- Pruefen ---------------------------------------------------------

    def verify(self, name: str, password: str) -> bool:
        """Passwort pruefen. ``False`` heisst: nicht anmelden.

        Der Weg ist fuer alle drei Faelle derselbe -- Konto unbekannt, Konto
        gesperrt, Passwort falsch -- und er kostet jedes Mal eine vollstaendige
        argon2-Pruefung. Anders liesse sich aus der Antwortzeit ablesen, welche
        Konten es gibt und welche gesperrt sind (R22). Deshalb wird auch bei
        einem unbekannten Konto gegen den Attrappen-Wert geprueft, statt frueh
        zurueckzukehren.
        """
        try:
            passwort = _pruef_passwort(password)
        except InvalidPassword:
            passwort = "x" * MAX_PASSWORD_LEN  # abweisen, aber gleich teuer

        sauber = _normalisiert(name).strip()
        zeile, ersatz = self._zeile_oder_ersatz(sauber)
        wert = str(ersatz["password_hash"]) if ersatz is not None else self._leerer_bestand()
        try:
            self._hasher.verify(wert, passwort)
            stimmt = True
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            stimmt = False

        if zeile is None or bool(zeile["locked"]) or not stimmt:
            return False

        # Erst jetzt, nach der erfolgreichen Anmeldung: wurden die Parameter
        # des Verfahrens seither angehoben, zieht der gespeicherte Wert nach.
        # Ohne das bliebe ein altes Konto dauerhaft auf der schwaecheren
        # Einstellung, ohne dass es jemand sieht (R21).
        #
        # Dass dieser eine Fall zusaetzlich rechnet und schreibt, ist bewusst
        # hingenommen: er tritt nur bei einer **gelungenen** Anmeldung ein, und
        # die ist fuer den Versuchenden ohnehin am Ergebnis erkennbar. Die
        # gefaehrlichen Faelle -- unbekanntes Konto, gesperrtes Konto, falsches
        # Passwort -- rechnen und schreiben alle gleich.
        if self._hasher.check_needs_rehash(wert):
            self._erneuern(sauber, wert, passwort)
        return True

    def _erneuern(self, name: str, gelesen: str, passwort: str) -> None:
        """Den gespeicherten Wert auf die aktuellen Parameter heben.

        Die Bedingung auf den **gelesenen** Wert ist der Kern: zwischen dem
        Lesen und diesem Schreiben liegen rund fuenfzig Millisekunden argon2.
        Faellt ein Passwortwechsel in dieses Fenster, machte das Erneuern ihn
        ohne die Bedingung zunichte -- reproduziert in drei von drei Laeufen.
        Die Kommandozeile meldete Erfolg, und das alte Passwort galt weiter;
        ein Wechsel nach einem Vorfall verpuffte still.
        """
        neu = self._hasher.hash(passwort)
        with self._riegel:
            self._con.execute(
                "update account set password_hash=?, updated_at=? "
                "where name=? and password_hash=?",
                (neu, time.time(), name, gelesen),
            )
            self._con.commit()

    def _leerer_bestand(self) -> str:
        """Ein Wert fuer den Fall, dass es ueberhaupt kein Konto gibt.

        Dann gibt es auch nichts aufzuzaehlen, also traegt hier ein einmal
        gebildeter Wert. Er wird erst gebraucht, wenn er gebraucht wird --
        ihn beim Oeffnen zu bilden kostete jedes ``account list`` 59 ms.
        """
        if self._ersatz is None:
            self._ersatz = self._hasher.hash("kein-konto")
        return self._ersatz


def _nur_eigentuemer(pfad: Path, voll: int) -> None:
    """Fremde Rechte wegnehmen -- und nur die.

    Nachgezogen wird, was **zu weit** offen steht, nicht was zu eng ist. Eine
    absichtlich auf 0400 gesetzte Datei ist schreibgeschuetzt; sie auf 0600 zu
    heben machte den Schutz wirkungslos, ohne dass jemand danach gefragt hat.
    """
    modus = pfad.stat().st_mode & 0o777
    if modus & 0o077:
        os.chmod(pfad, modus & voll & ~0o077)


def _connect(pfad: Path) -> sqlite3.Connection:
    """Kontendatei oeffnen oder anlegen, Rechte sichern, Schema stellen."""
    pfad.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Auch an einem **vorhandenen** Verzeichnis: ``mkdir(exist_ok=True)`` setzt
    # den Modus dort nicht nach, und ein begehbares Datenverzeichnis macht die
    # Rechte an der Datei nicht wett.
    _nur_eigentuemer(pfad.parent, 0o777)

    if not pfad.exists():
        # Rechte vor dem ersten Schreiben, nicht danach: dazwischen laegen die
        # Passwort-Werte sonst fuer jeden lesbar da (R7a).
        os.close(os.open(pfad, os.O_CREAT | os.O_WRONLY, 0o600))
    else:
        # Eine Datei aus einer Ruecksicherung oder von einem ``ExecStartPre``
        # braechte sonst ihre eigenen Rechte mit -- und ``-wal`` und ``-shm``
        # erben den Modus der Hauptdatei.
        _nur_eigentuemer(pfad, 0o777)

    con = sqlite3.connect(pfad, isolation_level=None, check_same_thread=False)
    try:
        con.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
        _journal_auf_wal(con)
        # Ausdruecklich, obwohl es heute der Vorgabe entspricht -- eine
        # Mutationsprobe kann diese Zeile darum nicht kippen. Sie steht hier
        # als Festlegung gegen eine kuenftige Aenderung der Vorgabe: bei
        # Konten ist ein verlorener Schreibvorgang nach einem Stromausfall ein
        # Konto, das es nicht mehr gibt. Das Verzeichnis darf sich mit
        # ``normal`` begnuegen, weil es sich aus dem Strom neu aufbaut.
        con.execute("pragma synchronous=full")
        con.execute("pragma secure_delete=on")
        con.executescript(_SCHEMA)
    except Exception:
        con.close()
        raise
    return con


def _journal_auf_wal(con: sqlite3.Connection) -> None:
    """Auf das WAL-Journal umschalten, mit kurzer Wiederholung.

    Das Umschalten braucht einen Augenblick eine Exklusivsperre, und die
    Wartefrist greift dort **nicht**: gemessen scheiterte jedes dritte
    gleichzeitige Oeffnen einer frischen Datei mit "database is locked" --
    nach 1,2 Millisekunden, nicht nach den fuenf Sekunden der Frist. Das ist
    genau der Fall, den ein Dienst und eine parallel gefahrene Kommandozeile
    erzeugen.
    """
    if str(con.execute("pragma journal_mode").fetchone()[0]).lower() == "wal":
        return
    for versuch in range(6):
        try:
            con.execute("pragma journal_mode=wal")
            return
        except sqlite3.OperationalError:
            if versuch == 5:
                raise
            time.sleep(0.02 * (versuch + 1))
