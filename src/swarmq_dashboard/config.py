"""Einstellungen aus der Umgebung.

Zwei Leser mit verschiedenem Anspruch (U1, U3):

* Der **Dienst** ist streng. Fehlt eine Pflichtangabe, faehrt er nicht hoch und
  sagt welche -- ein spaeterer Absturz mitten im Betrieb ist teurer als ein
  Startfehler.
* Die **Hooks** sind nachsichtig. Sie laufen in fremden Prozessen, und swarmq
  huellt jede Ausnahme des ``--configure``-Ziels in einen Konfigurationsfehler
  und beendet den Worker, statt ihn neu zu starten. Eine vergessene Variable
  wuerde dort die ganze Worker-Flotte am Hochfahren hindern. Also wirft der
  Hook-Leser nie, sondern meldet seine Unvollstaendigkeit.

Die Vorgaben der Fristen sind nicht geschaetzt, sondern aus den Konstanten des
Kerns abgeleitet; die Herleitung steht im Anhang des Plans.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

#: Aufbewahrung des Verzeichnisses und des Stroms (KD7).
DEFAULT_RETENTION_DAYS = 7
#: Ab wann ein gesendeter Sollwert als unbestaetigt gilt (R17): der Supervisor
#: tastet alle 5 s ab, also zwei Intervalle plus Nachsicht.
DEFAULT_UNCONFIRMED_AFTER_S = 15.0
#: Ab wann ein Worker als getrennt gilt (R19), gerechnet fuer die beste Quelle:
#: Gnadenfrist 30 s + Terminierungsfrist 5 s + Abtastintervall 5 s.
DEFAULT_DISCONNECTED_AFTER_S = 40.0
#: Inaktivitaetsfrist einer Sitzung (R21).
DEFAULT_SESSION_IDLE_S = 1800.0
#: Rueckstau je Live-Verbindung (R13a), gerechnet fuer 100.000 Tasks am Tag.
DEFAULT_BACKLOG_CAP = 300
#: Valkey-Datenbankindex des Ereignisstroms (KTD9). Nicht 0 -- die
#: Integrationsvorrichtung der Bibliothek leert Index 0 bei jedem Testlauf.
DEFAULT_STREAM_DB = 1

_SHARED_DB_WARNING = (
    "Ereignisstrom und Backend liegen auf demselben Valkey-Datenbankindex. "
    "Die Integrationstests von swarmq fuehren dort ein FLUSHDB aus und wuerden "
    "den Strom mitloeschen. Einen eigenen Index setzen (SQD_STREAM_URL)."
)


class ConfigError(Exception):
    """Eine Pflichtangabe des Dienstes fehlt oder ist unbrauchbar."""


def _require(source: Mapping[str, str], name: str) -> str:
    value = source.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} ist nicht gesetzt")
    return value


def _number[T: (int, float)](source: Mapping[str, str], name: str, default: T, cast: type[T]) -> T:
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} ist keine Zahl") from exc


def _with_db(url: str, db: int) -> str:
    """Dieselbe Valkey-Adresse mit einem anderen Datenbankindex."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, parts.fragment))


def _same_db(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.netloc, pa.path.strip("/") or "0") == (pb.netloc, pb.path.strip("/") or "0")


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    """Einstellungen des Dashboard-Dienstes. Streng: unvollstaendig heisst Fehler."""

    broker_url: str
    backend_url: str
    stream_url: str
    retention_days: int
    unconfirmed_after_s: float
    disconnected_after_s: float
    session_idle_s: float
    backlog_cap: int

    @property
    def shares_backend_db(self) -> bool:
        """Liegt der Strom auf demselben Valkey-Index wie das Backend (KTD9)?"""
        return _same_db(self.stream_url, self.backend_url)

    @property
    def shared_db_warning(self) -> str | None:
        """Warnung fuer den Betreiber, oder ``None`` wenn die Indizes getrennt sind."""
        return _SHARED_DB_WARNING if self.shares_backend_db else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DashboardConfig:
        """Aus der Umgebung lesen. Fehlt eine Pflichtangabe, wirft sie benannt."""
        source: Mapping[str, str] = os.environ if env is None else env
        broker_url = _require(source, "SWARMQ_BROKER_URL")
        backend_url = _require(source, "SWARMQ_BACKEND_URL")
        stream_url = source.get("SQD_STREAM_URL", "").strip() or _with_db(backend_url, DEFAULT_STREAM_DB)
        return cls(
            broker_url=broker_url,
            backend_url=backend_url,
            stream_url=stream_url,
            retention_days=_number(source, "SQD_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, int),
            unconfirmed_after_s=_number(source, "SQD_UNCONFIRMED_AFTER_S", DEFAULT_UNCONFIRMED_AFTER_S, float),
            disconnected_after_s=_number(source, "SQD_DISCONNECTED_AFTER_S", DEFAULT_DISCONNECTED_AFTER_S, float),
            session_idle_s=_number(source, "SQD_SESSION_IDLE_S", DEFAULT_SESSION_IDLE_S, float),
            backlog_cap=_number(source, "SQD_BACKLOG_CAP", DEFAULT_BACKLOG_CAP, int),
        )


@dataclass(frozen=True, slots=True)
class HookConfig:
    """Einstellungen der Hooks. Nachsichtig: unvollstaendig heisst untaetig.

    ``usable`` entscheidet, ob der Hook sich ueberhaupt registriert. Ist es
    ``False``, laeuft der Worker exakt wie ohne Hook (R2) und ``reason`` sagt
    warum -- protokolliert, nie geworfen.
    """

    stream_url: str | None
    reason: str | None

    @property
    def usable(self) -> bool:
        return self.stream_url is not None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HookConfig:
        """Aus der Umgebung lesen. Wirft unter keinen Umstaenden."""
        try:
            source: Mapping[str, str] = os.environ if env is None else env
            explicit = source.get("SQD_STREAM_URL", "").strip()
            if explicit:
                return cls(stream_url=explicit, reason=None)
            backend_url = source.get("SWARMQ_BACKEND_URL", "").strip()
            if not backend_url:
                return cls(
                    stream_url=None,
                    reason="SWARMQ_BACKEND_URL ist nicht gesetzt; der Hook bleibt untaetig",
                )
            return cls(stream_url=_with_db(backend_url, DEFAULT_STREAM_DB), reason=None)
        # Breit gefangen mit Absicht: ein Hook darf den Worker nie scheitern lassen.
        except Exception as exc:
            return cls(stream_url=None, reason=f"Einstellung nicht lesbar: {exc!r}")
