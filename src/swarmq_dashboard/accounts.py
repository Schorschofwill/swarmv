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
import time
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: Wartefrist auf gesperrte Schreibvorgaenge -- wie beim Verzeichnis.
BUSY_TIMEOUT_MS = 5000

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
        # Ein Wert, gegen den geprueft wird, wenn es das Konto nicht gibt oder
        # es gesperrt ist. Einmal beim Oeffnen gebildet, damit die Pruefung
        # spaeter genauso lange dauert wie eine echte.
        self._dummy = self._hasher.hash("kein-konto")

    def close(self) -> None:
        """Verbindung schliessen."""
        self._con.close()

    # -- Lesen -----------------------------------------------------------

    def accounts(self) -> list[Account]:
        """Alle Konten, nach Namen geordnet."""
        return [
            Account(
                name=str(z["name"]),
                locked=bool(z["locked"]),
                created_at=float(z["created_at"]),
                updated_at=float(z["updated_at"]),
                password_hash=str(z["password_hash"]),
            )
            for z in self._con.execute("select * from account order by name")
        ]

    def _row(self, name: str) -> sqlite3.Row | None:
        zeile: sqlite3.Row | None = self._con.execute(
            "select * from account where name=?", (name,)
        ).fetchone()
        return zeile

    # -- Schreiben -------------------------------------------------------

    def create(self, name: str, password: str) -> None:
        """Konto anlegen. Wirft, wenn es den Namen schon gibt."""
        jetzt = time.time()
        try:
            self._con.execute(
                "insert into account(name, password_hash, locked, created_at, updated_at) "
                "values (?,?,0,?,?)",
                (name, self._hasher.hash(password), jetzt, jetzt),
            )
        except sqlite3.IntegrityError as exc:
            raise AccountExists(f"das Konto {name!r} gibt es bereits") from exc
        self._con.commit()

    def set_password(self, name: str, password: str) -> None:
        """Passwort ersetzen. Das alte gilt danach nicht mehr."""
        cur = self._con.execute(
            "update account set password_hash=?, updated_at=? where name=?",
            (self._hasher.hash(password), time.time(), name),
        )
        if not cur.rowcount:
            raise UnknownAccount(f"das Konto {name!r} gibt es nicht")
        self._con.commit()

    def lock(self, name: str) -> None:
        """Konto sperren. Es bleibt bestehen und bleibt in der Liste."""
        self._set_locked(name, True)

    def unlock(self, name: str) -> None:
        """Sperre aufheben."""
        self._set_locked(name, False)

    def _set_locked(self, name: str, wert: bool) -> None:
        cur = self._con.execute(
            "update account set locked=?, updated_at=? where name=?",
            (int(wert), time.time(), name),
        )
        if not cur.rowcount:
            raise UnknownAccount(f"das Konto {name!r} gibt es nicht")
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
        zeile = self._row(name)
        wert = self._dummy if zeile is None else str(zeile["password_hash"])
        try:
            self._hasher.verify(wert, password)
            stimmt = True
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            stimmt = False

        if zeile is None or bool(zeile["locked"]) or not stimmt:
            return False

        # Erst jetzt, nach der erfolgreichen Anmeldung: wurden die Parameter
        # des Verfahrens seither angehoben, zieht der gespeicherte Wert nach.
        # Ohne das bliebe ein altes Konto dauerhaft auf der schwaecheren
        # Einstellung, ohne dass es jemand sieht (R21).
        if self._hasher.check_needs_rehash(wert):
            self._con.execute(
                "update account set password_hash=?, updated_at=? where name=?",
                (self._hasher.hash(password), time.time(), name),
            )
            self._con.commit()
        return True


def _connect(pfad: Path) -> sqlite3.Connection:
    """Kontendatei oeffnen oder anlegen, Rechte sichern, Schema stellen."""
    pfad.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not pfad.exists():
        # Rechte vor dem ersten Schreiben, nicht danach: dazwischen laegen die
        # Passwort-Werte sonst fuer jeden lesbar da (R7a).
        os.close(os.open(pfad, os.O_CREAT | os.O_WRONLY, 0o600))
    elif pfad.stat().st_mode & 0o777 != 0o600:
        # Auch an einer vorhandenen Datei nachziehen: eine aus einer
        # Ruecksicherung oder von einem ``ExecStartPre`` angelegte Datei
        # braechte sonst ihre eigenen Rechte mit -- und ``-wal`` und ``-shm``
        # erben den Modus der Hauptdatei.
        os.chmod(pfad, 0o600)

    con = sqlite3.connect(pfad, isolation_level=None)
    try:
        con.execute("pragma journal_mode=wal")
        con.execute("pragma synchronous=full")
        con.execute("pragma secure_delete=on")
        con.execute(f"pragma busy_timeout={BUSY_TIMEOUT_MS}")
        con.executescript(_SCHEMA)
    except Exception:
        con.close()
        raise
    return con
