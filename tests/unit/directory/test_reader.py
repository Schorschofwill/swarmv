"""Strom-Leser: Fortschritt, Rueckstand und Neuaufbau.

Die gefaehrliche Stelle ist der Rueckstand. Faellt der Leser hinter den
Laengendeckel zurueck -- bei einem Task-Stoss oder waehrend eines Neustarts --,
sind die ueberholten Eintraege fort, und ein Leser, der einfach am aeltesten
Rest aufsetzt, ueberspringt sie still. Die betroffenen Tasks fehlen dann im
Verzeichnis, und ein Teammitglied liest das als "nicht gelaufen". Genau das
schliesst das vierte Erfolgskriterium aus.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Fortschritt ===
1. Ein Durchlauf arbeitet die neuen Eintraege ein.
2. Der Fortschritt wird gemerkt; ein zweiter Durchlauf liest nicht erneut.
3. Ein neuer Leser auf derselben Datei setzt am gemerkten Stand auf.

=== ZYKLUS 2: Rueckstand hinter dem Laengendeckel (R4a, R9) ===
4. Liegt der Fortschritt vor dem aeltesten Stromeintrag, wird die Zeitspanne
   dazwischen als unvollstaendig markiert.
5. Ohne Rueckstand wird nichts markiert.
6. Der Rueckstand erscheint im Zustandsbericht.

=== ZYKLUS 3: Neuaufbau (R8) ===
7. Ohne gemerkten Fortschritt liest der Leser den Strom von vorn.
8. Waehrend des Neuaufbaus meldet der Zustand das.
9. Nach dem Neuaufbau meldet er es nicht mehr.

=== ZYKLUS 4: Aufbewahrung (R5, R9) ===
10. Die Pflege trimmt den Strom nach Alter.
11. Die Pflege laesst das Verzeichnis die eigene Frist anwenden.
"""

from __future__ import annotations

from pathlib import Path

from swarmq_dashboard.directory.reader import StreamReader
from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin, encode


class FakeStream:
    """Ein Strom im Speicher, mit Kennungen wie Valkey sie vergibt."""

    def __init__(self) -> None:
        self.eintraege: list[tuple[str, bytes]] = []
        self.getrimmt: list[int] = []

    def add(self, ev: Event) -> str:
        """Eintrag anhaengen. Die Kennung folgt der Ereigniszeit.

        Wie in Valkey: die Stromkennung ist die Wanduhr in Millisekunden, und
        der Zeitstempel im Ereignis dieselbe Wanduhr in Sekunden. Eine
        Attrappe, die beide auseinanderlaufen laesst, prueft etwas, das es
        nicht gibt.
        """
        sid = f"{int(ev.ts * 1000)}-0"
        self.eintraege.append((sid, encode(ev)))
        return sid

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        def schluessel(s: str) -> tuple[int, int]:
            a, b = s.split("-")
            return (int(a), int(b))

        grenze = schluessel(last_id) if last_id else (0, 0)
        return [(i, r) for i, r in self.eintraege if schluessel(i) > grenze][:count]

    async def min_id(self, key: str) -> str | None:
        return self.eintraege[0][0] if self.eintraege else None

    async def trim_by_min_id(self, key: str, min_ms: int) -> int:
        vorher = len(self.eintraege)
        self.eintraege = [(i, r) for i, r in self.eintraege if int(i.split("-")[0]) >= min_ms]
        self.getrimmt.append(min_ms)
        return vorher - len(self.eintraege)


def _ev(tid: str, ts: float) -> Event:
    return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
                 payload={"task_id": tid, "task_name": "render_pdf", "queue": "reports",
                          "args": [], "kwargs": {}})


def _reader(tmp_path: Path, strom: FakeStream) -> StreamReader:
    return StreamReader(strom, DirectoryWriter(connect(tmp_path / "v.sqlite3")))


# --- Zyklus 1 -----------------------------------------------------------


async def test_durchlauf_arbeitet_neue_eintraege_ein(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("a", 100.0))
    s.add(_ev("b", 200.0))
    r = _reader(tmp_path, s)
    assert await r.poll_once() == 2
    assert r.writer.row("a") is not None
    assert r.writer.row("b") is not None


async def test_zweiter_durchlauf_liest_nicht_erneut(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("a", 100.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    assert await r.poll_once() == 0, "der Fortschritt wurde nicht gemerkt"


async def test_neuer_leser_setzt_am_gemerkten_stand_auf(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("a", 100.0))
    r1 = _reader(tmp_path, s)
    await r1.poll_once()
    r1.writer.close()

    s.add(_ev("b", 200.0))
    r2 = _reader(tmp_path, s)
    assert await r2.poll_once() == 1, "der zweite Leser hat von vorn gelesen"


# --- Zyklus 2 -----------------------------------------------------------


async def test_rueckstand_markiert_die_zeitspanne(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("a", 100.0))
    r = _reader(tmp_path, s)
    await r.poll_once()                       # Fortschritt steht auf 1000-0

    # Der Deckel schneidet weg, was der Leser noch nicht hatte.
    s.eintraege.clear()
    s.add(_ev("c", 500.0))
    await r.poll_once()

    assert r.behind is True, "der Rueckstand wurde nicht erkannt"
    assert r.writer.row("a")["incomplete"] == 1, "die Zeile vor der Luecke ist nicht markiert"


async def test_ohne_rueckstand_wird_nichts_markiert(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("a", 100.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    s.add(_ev("b", 200.0))
    await r.poll_once()
    assert r.behind is False
    assert r.writer.row("a")["incomplete"] == 0


async def test_rueckstand_erscheint_im_zustandsbericht(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("a", 100.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    s.eintraege.clear()
    s.add(_ev("c", 500.0))
    await r.poll_once()
    assert r.status()["behind"] is True
    assert r.status()["position"]


# --- Zyklus 3 -----------------------------------------------------------


async def test_ohne_fortschritt_liest_er_von_vorn(tmp_path: Path) -> None:
    s = FakeStream()
    for i in range(3):
        s.add(_ev(f"t{i}", 100.0 + i))
    r = _reader(tmp_path, s)
    assert r.rebuilding is True, "ein leeres Verzeichnis ist ein Neuaufbau (R8)"
    assert await r.poll_once() == 3


async def test_neuaufbau_endet_sichtbar(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("a", 100.0))
    r = _reader(tmp_path, s)
    assert r.rebuilding is True
    await r.poll_once()
    assert r.rebuilding is False, "der Neuaufbau wurde nicht als beendet gemeldet"
    assert r.status()["rebuilding"] is False


# --- Zyklus 4 -----------------------------------------------------------


async def test_pflege_trimmt_den_strom_nach_alter(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("alt", 1000.0))
    s.add(_ev("neu", 9000.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    await r.maintain(now=9_000.0, retention_s=1_000.0)
    assert len(s.eintraege) == 1, "die Trimmung nach Alter hat nicht gegriffen"
    assert s.getrimmt, "kein Trimm-Aufruf"


async def test_pflege_laesst_das_verzeichnis_ablaufen(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("alt", 1000.0))
    s.add(_ev("neu", 9000.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    await r.maintain(now=9_000.0, retention_s=1_000.0)
    assert r.writer.row("alt") is None, "die alte Zeile blieb im Verzeichnis"
    assert r.writer.row("neu") is not None
