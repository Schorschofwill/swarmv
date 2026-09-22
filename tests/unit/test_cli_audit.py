"""Der Audit-Unterbefehl: "wer hat das storniert", ohne Protokolldateien.

Test-Liste (AGENTS.md):

1. Die Eintraege kommen in Zeitreihenfolge, lesbar.
2. Es laesst sich nach Konto und nach Aktionsart filtern.
3. Ein leerer Strom wird benannt, nicht als leere Ausgabe gezeigt.
4. Ein nicht erreichbarer Strom wird benannt und ergibt einen Rueckgabewert
   ungleich null -- sonst liest ein Skript "keine Aktionen" statt "kein Strom".
"""

from __future__ import annotations

import io
from pathlib import Path

from swarmq_dashboard.audit import AuditEntry
from swarmq_dashboard.cli.__main__ import main


class FakeStream:
    def __init__(self, eintraege: list[AuditEntry] | None = None, kaputt: bool = False) -> None:
        import msgspec

        self.kaputt = kaputt
        e = msgspec.json.Encoder()
        self.eintraege = [(f"{i}-0", e.encode(x)) for i, x in enumerate(eintraege or [])]

    async def connect(self) -> None:
        if self.kaputt:
            from swarmq_dashboard.stream import StreamUnavailable

            raise StreamUnavailable("Valkey ist weg")

    async def close(self) -> None:
        return None

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        return self.eintraege[:count]


def _lauf(tmp_path: Path, *argv: str, strom: FakeStream) -> tuple[int, str]:
    ausgabe = io.StringIO()
    code = main(["--data-dir", str(tmp_path), *argv], out=ausgabe, audit_stream=strom)
    return code, ausgabe.getvalue()


_EINTRAEGE = [
    AuditEntry(action="login", ts=1_700_000_000.0, account="ops", target="10.0.0.1"),
    AuditEntry(action="task_cancel", ts=1_700_000_060.0, account="dev", target="t-9",
               outcome="abgelehnt", detail="lief bereits"),
    AuditEntry(action="logout", ts=1_700_000_120.0, account="ops", target="10.0.0.1"),
]


def test_eintraege_in_zeitreihenfolge(tmp_path: Path) -> None:
    code, text = _lauf(tmp_path, "audit", "list", strom=FakeStream(_EINTRAEGE))
    assert code == 0
    zeilen = [z for z in text.splitlines() if z.strip()]
    assert len(zeilen) == 3, text
    assert "login" in zeilen[0] and "ops" in zeilen[0]
    assert "task_cancel" in zeilen[1] and "abgelehnt" in zeilen[1] and "t-9" in zeilen[1]
    assert "logout" in zeilen[2]
    assert "2023" in zeilen[0] or "1700000000" in zeilen[0], "der Zeitpunkt fehlt"


def test_filter_nach_konto_und_art(tmp_path: Path) -> None:
    code, text = _lauf(tmp_path, "audit", "list", "--account", "ops", strom=FakeStream(_EINTRAEGE))
    assert code == 0
    assert "dev" not in text and text.count("ops") == 2

    code, text = _lauf(tmp_path, "audit", "list", "--action", "task_cancel",
                       strom=FakeStream(_EINTRAEGE))
    assert code == 0
    assert "task_cancel" in text and "login" not in text


def test_leerer_strom_wird_benannt(tmp_path: Path) -> None:
    code, text = _lauf(tmp_path, "audit", "list", strom=FakeStream([]))
    assert code == 0
    assert text.strip(), "eine leere Ausgabe liest sich wie 'keine Aktionen'"


def test_nicht_erreichbarer_strom_wird_benannt(tmp_path: Path) -> None:
    """Sonst liest ein Skript "keine Aktionen" statt "kein Strom" -- und das
    ist genau die stille Falschaussage, gegen die dieses Dashboard gebaut ist.
    """
    code, text = _lauf(tmp_path, "audit", "list", strom=FakeStream(kaputt=True))
    assert code != 0
    assert "traceback" not in text.lower()
    assert text.strip()
