"""Eine Verbindung, zwei Faeden (Nachlauf ueber U18).

Der Befund kam aus einem sporadischen Fehlschlag: etwa jeder vierte Lauf von
``test_der_neuaufbau_gilt_danach_als_beendet`` kippte, mit zwei verschiedenen
Fehlern -- ``IndexError: tuple index out of range`` beim Lesen des
Fortschritts und ``sqlite3.InterfaceError: bad parameter or other API misuse``
beim Schreiben. Beides ist dasselbe: die Verbindung des Verzeichnisses wird
aus zwei Faeden zugleich benutzt.

``check_same_thread=False`` erlaubt das, aber es macht es nicht sicher. Die
blockierende Datenbankarbeit laeuft in einem Arbeitsfaden (``to_thread``),
waehrend ``status()`` auf der Ereignisschleife liest -- und ``status()`` wird
bei **jedem** Seitenaufruf gerufen. Die Kontendatei hat fuer genau diesen Fall
seit je einen Riegel; das Verzeichnis hatte keinen.

Sporadisch heisst hier nicht harmlos: im Betrieb trifft es die Seite, die
gerade sagen soll, wie es um den Dienst steht.

Test-Liste (AGENTS.md):

1. Lesen und Schreiben aus zwei Faeden zugleich wirft nicht.
2. Der Zustandsbericht bleibt dabei brauchbar.
3. Der Dienstweg selbst: Einlesen im Arbeitsfaden gegen Bericht auf
   der Ereignisschleife.
4. Ein fremder Schreibvorgang kommt nicht in einen laufenden Stapel.
"""

from __future__ import annotations

import threading
from pathlib import Path

from swarmq_dashboard.directory.schema import connect
from swarmq_dashboard.directory.writer import DirectoryWriter
from swarmq_dashboard.events import Event, Origin


def _ev(tid: str, ts: float) -> Event:
    return Event(type="task_enqueue", ts=ts, source="app", origin=Origin.ENQUEUE,
                 payload={"task_id": tid, "task_name": "x", "queue": "reports",
                          "args": [], "kwargs": {}})


def test_lesen_und_schreiben_aus_zwei_faeden(tmp_path: Path) -> None:
    """Der Weg, den der Dienst wirklich geht: Einlesen im Arbeitsfaden,
    Zustandsbericht auf der Ereignisschleife."""
    w = DirectoryWriter(connect(tmp_path / "v.sqlite3"))
    fehler: list[BaseException] = []
    halt = threading.Event()

    def schreiben() -> None:
        try:
            for i in range(300):
                w.apply([(f"{i}-0", _ev(f"t{i}", 1000.0 + i))])
                w.bump_lost(dropped=1)
                w.set_coverage_start(1000.0)
                if halt.is_set():
                    return
        except BaseException as exc:
            fehler.append(exc)
        finally:
            halt.set()

    def lesen() -> None:
        try:
            while not halt.is_set():
                w.lost_counts()
                w.coverage_start()
                w.newest_entry()
                w.oldest_entry()
                w.gap_count()
        except BaseException as exc:
            fehler.append(exc)

    faeden = [threading.Thread(target=schreiben), threading.Thread(target=lesen)]
    for f in faeden:
        f.start()
    for f in faeden:
        f.join(timeout=30)
    halt.set()
    w.close()

    assert not fehler, (
        "die Verbindung wird aus zwei Faeden zugleich benutzt: "
        + "; ".join(f"{type(e).__name__}: {e}" for e in fehler[:3])
    )


def test_der_bericht_bleibt_dabei_brauchbar(tmp_path: Path) -> None:
    """Nicht nur "wirft nicht": die Zahlen duerfen auch nicht durcheinander
    geraten. Ein Bericht, der unter Last Unsinn meldet, waere schlimmer als
    einer, der wirft -- er faellt niemandem auf."""
    w = DirectoryWriter(connect(tmp_path / "v.sqlite3"))
    halt = threading.Event()
    gesehen: list[tuple[int, int]] = []

    def schreiben() -> None:
        for _ in range(200):
            w.bump_lost(dropped=1)
        halt.set()

    def lesen() -> None:
        while not halt.is_set():
            gesehen.append(w.lost_counts())

    faeden = [threading.Thread(target=schreiben), threading.Thread(target=lesen)]
    for f in faeden:
        f.start()
    for f in faeden:
        f.join(timeout=30)
    w.close()

    assert gesehen, "es wurde nie gelesen"
    assert all(0 <= a <= 200 and b == 0 for a, b in gesehen), (
        f"unsinnige Zwischenstaende: {[g for g in gesehen if not (0 <= g[0] <= 200)][:3]}"
    )
    # Monoton: ein Zaehler, der zurueckspringt, laesst eine Meldung
    # verschwinden, die schon dastand.
    zahlen = [a for a, _ in gesehen]
    assert zahlen == sorted(zahlen), f"der Zaehler springt zurueck: {zahlen[:20]}"


async def test_der_dienstweg_einlesen_gegen_zustandsbericht(tmp_path: Path) -> None:
    """Der Weg, auf dem es wirklich kippte.

    Nicht zwei nackte Faeden: der Dienst schiebt die Datenbankarbeit mit
    ``asyncio.to_thread`` in einen Arbeitsfaden und liest den Bericht auf der
    Ereignisschleife -- bei **jedem** Seitenaufruf. Genau diese Verschraenkung
    erzeugte den ``IndexError`` im Fortschritt und den ``InterfaceError`` beim
    Schreiben.
    """
    import asyncio

    from swarmq_dashboard.directory.reader import StreamReader

    class Strom:
        def __init__(self) -> None:
            self.n = 0

        async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
            from swarmq_dashboard.events import encode
            self.n += 1
            if self.n > 60:
                return []
            return [(f"{self.n}000-0", encode(_ev(f"t{self.n}", 1000.0 + self.n)))]

        async def min_id(self, key: str) -> str | None:
            return None

        async def trim_by_min_id(self, key: str, min_ms: int) -> int:
            return 0

    w = DirectoryWriter(connect(tmp_path / "v.sqlite3"))
    r = StreamReader(Strom(), w)
    fehler: list[BaseException] = []

    async def einlesen() -> None:
        try:
            for _ in range(60):
                await r.poll_once()
        except BaseException as exc:
            fehler.append(exc)

    async def berichten() -> None:
        try:
            for _ in range(400):
                r.status()
                await asyncio.sleep(0)
        except BaseException as exc:
            fehler.append(exc)

    await asyncio.gather(einlesen(), berichten())
    w.close()
    assert not fehler, (
        "Einlesen und Zustandsbericht teilen sich die Verbindung ohne Riegel: "
        + "; ".join(f"{type(e).__name__}: {e}" for e in fehler[:3])
    )


def test_ein_fremder_schreibvorgang_kommt_nicht_in_den_stapel(tmp_path: Path) -> None:
    """Ein Stapel ist **eine** Transaktion (R4a-Fortschritt, Atomarität).

    Die Verbindung nimmt den Riegel je Anweisung -- das genuegt gegen
    zerrissene Cursor, nicht gegen einen fremden Schreibvorgang mitten in
    einer laufenden Transaktion. Der liefe in ihr mit, und ein Ruecklauf des
    Stapels risse ihn mit zurueck: eine Stornierungsanforderung waere
    verschwunden, ohne dass jemand etwas davon erfaehrt.

    Gemessen wird, dass der fremde Vorgang **wartet**, solange der Stapel
    laeuft -- ohne den Riegel ueber die ganze Transaktion liefe er sofort
    durch.
    """
    w = DirectoryWriter(connect(tmp_path / "v.sqlite3"))
    w.apply([("1-0", _ev("ziel", 1000.0))])

    drin = threading.Event()
    weiter = threading.Event()
    fremder_fertig = threading.Event()

    echt_upsert = w._upsert

    def langsam(task_id: str, ev: object, stream_id: str) -> None:
        echt_upsert(task_id, ev, stream_id)     # type: ignore[arg-type]
        drin.set()
        weiter.wait(timeout=5)

    def fremd() -> None:
        drin.wait(timeout=5)
        w.mark_cancel_requested("ziel")
        fremder_fertig.set()

    w._upsert = langsam                          # type: ignore[method-assign]
    faden = threading.Thread(target=fremd)
    faden.start()
    try:
        stapel = threading.Thread(target=lambda: w.apply([("2-0", _ev("neu", 2000.0))]))
        stapel.start()
        assert drin.wait(timeout=5), "der Stapel lief nicht an"
        # **Jetzt** laeuft die Transaktion. Der fremde Vorgang darf nicht
        # durchkommen.
        assert not fremder_fertig.wait(timeout=0.5), (
            "ein fremder Schreibvorgang lief mitten in den Stapel hinein -- "
            "ein Ruecklauf risse ihn mit zurueck"
        )
        weiter.set()
        stapel.join(timeout=5)
        assert fremder_fertig.wait(timeout=5), "der fremde Vorgang kam nie durch"
    finally:
        weiter.set()
        faden.join(timeout=5)
        w._upsert = echt_upsert                  # type: ignore[method-assign]
        w.close()
