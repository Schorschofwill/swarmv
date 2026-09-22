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
2. Der Fortschritt wird auf die *letzte* Kennung eines Stapels gemerkt;
   ein zweiter Durchlauf liest nicht erneut.
3. Ein neuer Leser auf derselben Datei setzt am gemerkten Stand auf.

=== ZYKLUS 2: Rueckstand hinter dem Laengendeckel (R4a, R9) ===
4. Liegt der Fortschritt vor dem aeltesten Stromeintrag, wird die Zeitspanne
   dazwischen als unvollstaendig markiert.
5. Ohne Rueckstand wird nichts markiert.
6. Der Rueckstand erscheint im Zustandsbericht, mit Zeitspanne und
   Reichweite.

=== ZYKLUS 3: Neuaufbau (R8) ===
7. Ohne gemerkten Fortschritt liest der Leser den Strom von vorn.
8. Waehrend des Neuaufbaus meldet der Zustand das.
9. Nach dem Neuaufbau meldet er es nicht mehr.

=== ZYKLUS 4: Aufbewahrung (R5, R9) ===
10. Die Pflege trimmt den Strom nach Alter -- und laesst stehen, was
    innerhalb der Frist liegt.
11. Die Pflege laesst das Verzeichnis die eigene Frist anwenden.

=== ZYKLUS 5: Befunde aus der Pruefung von Stufe B ===
12. Der Neuaufbau gilt erst als beendet, wenn der Strom eingeholt ist.
13. Ein unlesbarer Eintrag zaehlt als Luecke statt spurlos zu verschwinden.
13a. Und der Zaehler ueberlebt den Neustart -- er steht in der Datei.
14. Die eigene Altersbereinigung loest keinen Rueckstands-Alarm aus.
15. Die gemeldete Luecke verschwindet mit dem Rueckstand.

=== ZYKLUS 6: Der Ereignis-Loop bleibt frei ===
16. Ein Durchlauf blockiert den Ereignis-Loop nicht.
16a. Dieselbe Zusage ohne Stoppuhr: die Datenbankarbeit laeuft nicht im
     Thread des Ereignis-Loops.
17. Die Pflege blockiert ihn ebenso wenig.
18. Zwei gleichzeitige Durchlaeufe verschraenken sich nicht.
"""

from __future__ import annotations

import asyncio
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
    """Mehr als ein Eintrag je Durchlauf -- das ist der Normalfall und der
    einzige, in dem sich zeigt, ob der Leser sich die **letzte** Kennung
    merkt. Mit nur einem Eintrag sind erste und letzte dieselbe, und ein
    Leser, der die erste merkt, kaeme damit durch: in Betrieb laese er ab dem
    zweiten Eintrag jedes Stapels alles endlos erneut ein.
    """
    s = FakeStream()
    letzte = ""
    for i in range(3):
        letzte = s.add(_ev(f"t{i}", 100.0 + i))
    r = _reader(tmp_path, s)
    assert await r.poll_once() == 3
    assert r.position == letzte, (
        f"der Fortschritt steht auf {r.position!r} statt auf der letzten "
        f"Kennung {letzte!r}"
    )
    assert await r.poll_once() == 0, "der Fortschritt wurde nicht gemerkt"


async def test_neuer_leser_setzt_am_gemerkten_stand_auf(tmp_path: Path) -> None:
    s = FakeStream()
    for i in range(3):
        s.add(_ev(f"t{i}", 100.0 + i))
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
    # Und **dauerhaft**, nicht nur im Arbeitsspeicher (U12a). Die Markierung
    # an der Zeile trifft nur, was ueberlebt hat; die weggeschnittenen Tasks
    # haben gar keine Zeile, und gerade sie sind der Grund fuer die Meldung.
    # Ohne den Eintrag war die Luecke nach einem Neustart vergessen.
    assert r.writer.gap_count() == 1, "die Luecke ueberlebt keinen Neustart"


async def test_ohne_rueckstand_wird_nichts_markiert(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("a", 100.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    s.add(_ev("b", 200.0))
    await r.poll_once()
    assert r.behind is False
    assert r.writer.row("a")["incomplete"] == 0
    assert r.writer.gap_count() == 0, "Fehlalarm: eine Luecke ohne Rueckstand"


async def test_rueckstand_erscheint_im_zustandsbericht(tmp_path: Path) -> None:
    """Nicht nur das Kennzeichen: die Kopfzeile nennt dem Teammitglied die
    betroffene Zeitspanne und die Reichweite. Beide Felder kommen nur hier
    vor -- ohne Zusicherung darauf koennten sie leer bleiben, und die
    Kopfzeile meldete einen Rueckstand ohne zu sagen, welchen.
    """
    s = FakeStream()
    s.add(_ev("a", 100.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    stand = r.position
    s.eintraege.clear()
    juengste = s.add(_ev("c", 500.0))
    await r.poll_once()

    zustand = r.status()
    assert zustand["behind"] is True
    assert zustand["gap"] == (stand, juengste), (
        f"die Luecke wird nicht benannt: {zustand['gap']!r}"
    )
    assert zustand["oldest_entry"] == 100.0, (
        f"die Reichweite fehlt oder stimmt nicht: {zustand['oldest_entry']!r}"
    )


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
    """Drei Eintraege, und die Grenze liegt **zwischen** ihnen.

    Mit nur einem alten und einem taufrischen Eintrag koennte die Frist auch
    ganz ignoriert werden -- der Pflegelauf schnitte dann alles ausser dem
    juengsten weg und der Test bliebe gruen. Der mittlere Eintrag liegt
    innerhalb des Fensters und muss ueberleben.
    """
    s = FakeStream()
    s.add(_ev("alt", 1000.0))       # weit ausserhalb
    s.add(_ev("knapp_drin", 8500.0))  # innerhalb: Grenze ist 8000
    s.add(_ev("neu", 9000.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    await r.maintain(now=9_000.0, retention_s=1_000.0)

    uebrig = {i for i, _ in s.eintraege}
    assert len(s.eintraege) == 2, (
        f"die Frist wurde nicht angewandt -- uebrig: {sorted(uebrig)}"
    )
    assert s.getrimmt[-1] == 8_000_000, (
        f"geschnitten wurde bei {s.getrimmt[-1]} statt bei jetzt minus Frist"
    )


async def test_pflege_laesst_das_verzeichnis_ablaufen(tmp_path: Path) -> None:
    s = FakeStream()
    s.add(_ev("alt", 1000.0))
    s.add(_ev("knapp_drin", 8500.0))
    s.add(_ev("neu", 9000.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    await r.maintain(now=9_000.0, retention_s=1_000.0)
    assert r.writer.row("alt") is None, "die alte Zeile blieb im Verzeichnis"
    assert r.writer.row("knapp_drin") is not None, (
        "eine Zeile innerhalb der Frist wurde geloescht -- die Frist wirkt nicht"
    )
    assert r.writer.row("neu") is not None


# --- Zyklus 5: Befunde aus der Pruefung von Stufe B ----------------------


async def test_neuaufbau_gilt_erst_als_beendet_wenn_eingeholt(tmp_path: Path) -> None:
    """R8. Ein Durchlauf holt hoechstens ``batch`` Eintraege.

    Wer nur prueft, *ob* schon einmal gelesen wurde, meldet nach dem ersten
    Durchlauf "kein Neuaufbau" -- bei einem grossen Strom ueber eine Liste,
    die zu neun Zehnteln leer ist. Genau die halbe Wahrheit, die R8 ausschliesst.
    """
    s = FakeStream()
    for i in range(25):
        s.add(_ev(f"t{i}", 100.0 + i))
    r = StreamReader(s, DirectoryWriter(connect(tmp_path / "v.sqlite3")), batch=10)

    assert r.rebuilding is True
    assert await r.poll_once() == 10
    assert r.rebuilding is True, "nach einem von drei Durchlaeufen schon fertig gemeldet"
    await r.poll_once()
    assert r.rebuilding is True
    await r.poll_once()
    assert r.rebuilding is False, "eingeholt, aber immer noch als Neuaufbau gemeldet"


async def test_unlesbarer_eintrag_wird_als_luecke_gezaehlt(tmp_path: Path) -> None:
    """Ein Hook einer neueren Fassung mit einem unbekannten Herkunftswert
    liesse sonst *alle* seine Ereignisse lautlos ausfallen, und die
    betroffenen Tasks laesen sich als "nie gelaufen" (R4a).
    """
    s = FakeStream()
    s.add(_ev("lang", 100.0))                 # laeuft noch, wenn der Ausfall faellt
    s.add(_ev("kurz", 110.0))
    s.eintraege.append(("150000-0", b"\xff kein gueltiges Ereignis"))
    s.add(Event(type="task_completed", ts=200.0, source="w1", origin=Origin.PICKUP,
                payload={"task_id": "lang", "duration_ms": 1.0}))
    s.add(_ev("c", 300.0))
    r = _reader(tmp_path, s)
    await r.poll_once()

    zustand = r.status()
    assert zustand["dropped"] == 1, f"der unlesbare Eintrag verschwand spurlos: {zustand}"
    assert r.writer.row("lang")["incomplete"] == 1, (
        "die Zeile, die den Ausfall ueberspannt, ist nicht markiert"
    )
    assert r.writer.row("kurz")["incomplete"] == 0, (
        "eine Zeile ausserhalb des Ausfalls wurde markiert"
    )
    assert r.writer.row("c") is not None, "der Ausfall hat den Durchlauf angehalten"


async def test_kein_rueckstand_nach_eigener_altersbereinigung(tmp_path: Path) -> None:
    """Die eigene Pflege entfernt Abgelaufenes, nicht Verlorenes.

    Ohne die Unterscheidung meldete jeder Pflegelauf einen Rueckstand und
    markierte gesunde Zeilen dauerhaft als unvollstaendig -- ein lauter
    Fehlalarm, der die echte Meldung entwertet.
    """
    s = FakeStream()
    s.add(_ev("alt", 1000.0))
    s.add(_ev("neu", 9000.0))
    r = StreamReader(s, DirectoryWriter(connect(tmp_path / "v.sqlite3")), batch=1)
    await r.poll_once()                       # nur "alt" gelesen
    await r.maintain(now=9_000.0, retention_s=1_000.0)   # entfernt "alt"
    await r.poll_once()
    assert r.behind is False, "Fehlalarm: die eigene Altersbereinigung als Verlust gedeutet"


async def test_luecke_verschwindet_mit_dem_rueckstand(tmp_path: Path) -> None:
    """Sonst meldet der Bericht "kein Rueckstand" und nennt zugleich eine
    Zeitspanne -- er widerspraeche sich selbst.
    """
    s = FakeStream()
    s.add(_ev("a", 100.0))
    r = _reader(tmp_path, s)
    await r.poll_once()
    s.eintraege.clear()
    s.add(_ev("c", 500.0))
    await r.poll_once()
    assert r.status()["gap"] is not None, "Vorbedingung: eine Luecke muss gemeldet sein"

    s.add(_ev("d", 600.0))
    await r.poll_once()
    zustand = r.status()
    assert zustand["behind"] is False
    assert zustand["gap"] is None, f"die alte Luecke steht noch: {zustand['gap']}"


# --- Zyklus 6: Der Ereignis-Loop bleibt frei ----------------------------


async def _groesste_luecke(arbeit: object, takt: float = 0.002) -> tuple[float, float]:
    """Arbeit fahren und dabei den Herzschlag messen.

    Gibt (groesste Luecke, Gesamtdauer) zurueck. Ein Ereignis-Loop, der
    blockiert, laesst den Herzschlag genauso lange aussetzen wie die Arbeit
    dauert -- das Verhaeltnis der beiden Zahlen ist die Aussage, nicht eine
    absolute Schranke: die haengt an der Maschine.
    """
    luecke = 0.0
    laeuft = True
    messen = False

    async def herzschlag() -> None:
        nonlocal luecke
        vorher = asyncio.get_running_loop().time()
        while laeuft:
            await asyncio.sleep(takt)
            jetzt = asyncio.get_running_loop().time()
            if messen:
                luecke = max(luecke, jetzt - vorher)
            vorher = jetzt

    h = asyncio.ensure_future(herzschlag())
    # Erst laufen lassen, dann messen. Ohne dieses Warten startet der
    # Herzschlag ueberhaupt nicht: ``ensure_future`` plant ihn nur ein, und
    # eine Koroutine, die nie wirklich aussetzt, gibt den Ereignis-Loop nicht
    # ab. Die Messung stand dann auf null und der Test bewies nichts.
    await asyncio.sleep(takt * 4)
    assert not messen
    messen = True

    beginn = asyncio.get_running_loop().time()
    await arbeit()  # type: ignore[operator]
    dauer = asyncio.get_running_loop().time() - beginn
    laeuft = False
    await h
    assert luecke > 0.0, "der Herzschlag hat nicht gemessen -- der Test bewiese nichts"
    return luecke, dauer


async def test_ein_durchlauf_blockiert_den_ereignis_loop_nicht(tmp_path: Path) -> None:
    """R8, R26: waehrend des Neuaufbaus soll die Kopfzeile sagen, dass gerade
    neu aufgebaut wird. Blockiert der Leser den Ereignis-Loop, kann sie das
    nicht -- gemessen lag der Anteil blockierter Zeit bei 100 %, und ein
    Neuaufbau ueber sieben Tage haette den Dienst rund sieben Minuten lang
    stumm gestellt.
    """
    s = FakeStream()
    for i in range(4000):
        s.add(_ev(f"t{i}", 100.0 + i))
    r = StreamReader(s, DirectoryWriter(connect(tmp_path / "v.sqlite3")), batch=4000)

    luecke, dauer = await _groesste_luecke(r.poll_once)
    assert dauer > 0.05, f"Vorbedingung: die Arbeit muss spuerbar dauern ({dauer:.3f} s)"
    # Grosszuegige Schranke mit Absicht: gemessen liegt das Verhaeltnis auch
    # unter voller Maschinenlast bei 0,01 bis 0,06, waehrend es ohne den
    # Arbeitsthread gegen 1 geht. Der Zwischenraum traegt die Aussage; enger
    # gezogen bewiese der Test nicht mehr, sondern haenge nur noch an der
    # Auslastung der Maschine. Die eigentliche Zusicherung steht im Test
    # darunter -- sie misst keine Zeit.
    assert luecke < dauer / 2, (
        f"der Ereignis-Loop stand {luecke * 1000:.0f} ms still, waehrend die Arbeit "
        f"{dauer * 1000:.0f} ms dauerte -- er wird blockiert"
    )


async def test_die_datenbankarbeit_laeuft_nicht_im_ereignis_loop(tmp_path: Path) -> None:
    """Dieselbe Zusage, aber ohne Stoppuhr.

    Eine Zeitmessung als Tor haengt an der Auslastung der Maschine und wird
    auf einem vollen Bauserver launisch. Die Frage dahinter ist ohnehin keine
    Frage der Dauer, sondern eine des Ortes: laeuft die blockierende Arbeit im
    Thread des Ereignis-Loops oder nicht.
    """
    import threading

    s = FakeStream()
    for i in range(50):
        s.add(_ev(f"t{i}", 100.0 + i))
    w = DirectoryWriter(connect(tmp_path / "v.sqlite3"))
    r = StreamReader(s, w, batch=50)

    gesehen: list[int] = []
    echt = w.apply

    def beobachtet(ereignisse: object) -> int:
        gesehen.append(threading.get_ident())
        return echt(ereignisse)  # type: ignore[arg-type]

    w.apply = beobachtet  # type: ignore[method-assign]
    await r.poll_once()

    assert gesehen, "die Einarbeitung lief gar nicht"
    assert threading.get_ident() not in gesehen, (
        "die Datenbankarbeit lief im Thread des Ereignis-Loops"
    )


async def test_die_pflege_blockiert_den_ereignis_loop_nicht(tmp_path: Path) -> None:
    """Gemessen blockierte ein Pflegelauf auf einem vollen Verzeichnis 6,4 s.
    Granian faehrt einen Arbeiter -- so lange beantwortet das Dashboard keine
    einzige Anfrage.
    """
    s = FakeStream()
    for i in range(12000):
        s.add(_ev(f"t{i}", 1000.0 + i * 0.001))
    r = StreamReader(s, DirectoryWriter(connect(tmp_path / "v.sqlite3")), batch=12000)
    await r.poll_once()

    async def pflegen() -> None:
        await r.maintain(now=9_000.0, retention_s=1_000.0)

    luecke, dauer = await _groesste_luecke(pflegen)
    assert dauer > 0.03, f"Vorbedingung: die Pflege muss spuerbar dauern ({dauer:.3f} s)"
    assert luecke < dauer / 2, (
        f"der Ereignis-Loop stand {luecke * 1000:.0f} ms still bei {dauer * 1000:.0f} ms Arbeit"
    )


async def test_zwei_durchlaeufe_verschraenken_sich_nicht(tmp_path: Path) -> None:
    """Die Arbeit liegt jetzt in einem Thread. Zwei gleichzeitige Durchlaeufe
    haetten dann dieselbe SQLite-Verbindung aus zwei Threads in der Hand --
    das beschaedigt sie. Ein Riegel haelt sie auseinander.
    """
    s = FakeStream()
    for i in range(200):
        s.add(_ev(f"t{i}", 100.0 + i))
    r = StreamReader(s, DirectoryWriter(connect(tmp_path / "v.sqlite3")), batch=50)

    ergebnisse = await asyncio.gather(*(r.poll_once() for _ in range(4)))
    assert sum(ergebnisse) == 200, f"Eintraege doppelt oder gar nicht gelesen: {ergebnisse}"
    zeilen = r.writer._con.execute("select count(*) from task").fetchone()[0]
    assert zeilen == 200, f"{zeilen} Zeilen statt 200"


async def test_ein_unlesbarer_eintrag_wird_dauerhaft_gezaehlt(tmp_path: Path) -> None:
    """R4a. Der Zaehler muss den Neustart ueberleben.

    ``dropped`` war ein reiner Speicherzaehler: nach dem ersten Neustart
    schwieg die Kopfzeile, obwohl der Eintrag dauerhaft fehlt -- und
    Schweigen liest sich als "nichts verloren". Geprueft wird hier ueber den
    **Leser**, nicht ueber ``bump_lost`` von Hand: die Stelle, an der es im
    Betrieb passiert, war ungedeckt, und genau solche Naehte sind in diesem
    Projekt schon mehrfach unangeschlossen geblieben.
    """
    s = FakeStream()
    s.add(_ev("gut", 100.0))
    # Ein Eintrag, den diese Fassung nicht lesen kann.
    s.eintraege.append(("150000-0", b"das ist kein Ereignis"))
    r = _reader(tmp_path, s)
    await r.poll_once()

    assert r.status()["dropped"] == 1, f"der Verlust wird nicht gezaehlt: {r.status()}"
    assert r.writer.lost_counts()[0] == 1, (
        "der Verlust steht nicht in der Datei -- nach einem Neustart schweigt "
        "die Kopfzeile darueber"
    )
