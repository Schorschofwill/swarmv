"""Der Probe-Task: etwas, das man von der Oberflaeche aus losschicken kann.

Der Zweck ist eng: pruefen, ob der Weg steht. Reiht das Dashboard ein, greift
ein Worker auf, laeuft der Task, kommt das Ergebnis an, wandert ein
Fehlschlag ins Dead-Letter? Dafuer braucht es einen Task, der **auf Ansage**
langsam ist, scheitert oder rechnet -- und der in jedem Worker registriert
werden kann, ohne dessen eigene Tasks zu beruehren.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Die Zeit ===
1. [ ] Der Task schlaeft die angegebene Zeit und gibt sie zurueck.
2. [ ] Ohne Angabe schlaeft er gar nicht.
3. [ ] Eine unsinnige Zeit wird abgewiesen, nicht stillschweigend gedeutet.
4. [ ] Die Schlafzeit ist gedeckelt -- ein Tippfehler legt keinen Slot lahm.

=== ZYKLUS 2: Scheitern auf Ansage ===
5. [ ] ``fail=True`` wirft, und der Fehlertext nennt den Grund.
6. [ ] ``fail_times=2`` scheitert zweimal und laeuft beim dritten Versuch durch.

=== ZYKLUS 3: Rechnen statt warten ===
7. [ ] ``burn_s`` haelt die CPU beschaeftigt, statt zu schlafen.

=== ZYKLUS 4: Was der Task ueber sich meldet ===
8. [ ] Das Ergebnis nennt Worker, Queue und die gemessene Laufzeit.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from swarmq_dashboard.probe import ProbeTask


async def test_der_task_schlaeft_die_angegebene_zeit() -> None:
    """Der Kern der Sache: eine Laufzeit, die der Absender bestimmt."""
    task = ProbeTask()
    begonnen = time.perf_counter()
    ergebnis = await task.run(sleep_s=0.2)
    gebraucht = time.perf_counter() - begonnen
    assert 0.2 <= gebraucht < 1.0, f"geschlafen wurde {gebraucht:.2f}s"
    assert ergebnis["sleep_s"] == 0.2


async def test_ohne_angabe_schlaeft_er_nicht() -> None:
    """Die Vorgabe ist "sofort fertig": so prueft man den Weg, nicht die Geduld."""
    begonnen = time.perf_counter()
    ergebnis = await ProbeTask().run()
    assert time.perf_counter() - begonnen < 0.1
    assert ergebnis["sleep_s"] == 0.0


async def test_eine_unsinnige_zeit_wird_benannt_abgewiesen() -> None:
    """Ein Task, der "abc" als 0 liest, verschweigt einen Eingabefehler.

    Das Formular schickt Text; was dort steht, entscheidet ein Mensch. Ein
    Komma statt eines Punktes ist der haeufigste Fall, und er darf nicht als
    "sofort fertig" durchgehen.
    """
    for wert in ("abc", "0,5", None, -1):
        with pytest.raises(ValueError, match="sleep_s"):
            await ProbeTask().run(sleep_s=wert)  # type: ignore[arg-type]


async def test_die_schlafzeit_ist_gedeckelt() -> None:
    """Ein Tippfehler darf keinen Slot fuer Stunden belegen.

    Wer laenger messen will, schickt mehrere Tasks -- das ist die ehrlichere
    Last und blockiert nicht einen einzelnen Slot.
    """
    with pytest.raises(ValueError, match="Grenze"):
        await ProbeTask().run(sleep_s=3600)


async def test_scheitern_auf_ansage_nennt_den_grund() -> None:
    """Ohne diesen Schalter liesse sich der Weg ins Dead-Letter nicht gehen."""
    from swarmq_dashboard.probe import ProbeFehler

    with pytest.raises(ProbeFehler, match="auf Ansage"):
        await ProbeTask().run(fail=True, note="Vorfalluebung")


async def test_der_fehlertext_traegt_die_notiz() -> None:
    """Die Notiz ist der Faden, an dem man den Versuch spaeter wiederfindet.

    Auf der Task-Liste stehen zwanzig gleiche Proben; ohne eigenen Text
    weiss niemand mehr, welche zu welchem Versuch gehoerte.
    """
    from swarmq_dashboard.probe import ProbeFehler

    with pytest.raises(ProbeFehler) as fehler:
        await ProbeTask().run(fail=True, note="Lasttest 14:30")
    assert "Lasttest 14:30" in str(fehler.value)


async def test_rechnen_belastet_die_cpu_statt_zu_warten() -> None:
    """Fuer den Rechen-Pool ist Schlafen die falsche Probe.

    Ein schlafender Task belegt dort einen Compute-Slot, ohne ihn zu
    beanspruchen -- gemessen wuerde die Zustellung, nicht die Rechenarbeit.
    """
    begonnen = time.perf_counter()
    cpu_vorher = time.process_time()
    ergebnis = await ProbeTask().run(burn_s=0.3)
    gerechnet = time.process_time() - cpu_vorher
    assert time.perf_counter() - begonnen >= 0.3
    assert gerechnet >= 0.2, f"nur {gerechnet:.2f}s Rechenzeit -- da wurde gewartet"
    assert ergebnis["burn_s"] == 0.3


async def test_das_ergebnis_nennt_worker_und_laufzeit() -> None:
    """Wer die Probe abschickt, will wissen, **wo** sie lief.

    Bei zwei Gruppen und vier Prozessen ist "fertig" allein keine Auskunft:
    die Frage ist, ob der Task dort ankam, wo er hinsollte.
    """
    ergebnis = await ProbeTask().run(sleep_s=0.05, note="Probe 1")
    assert ergebnis["worker"], "der Worker wird nicht genannt"
    assert ergebnis["pid"] > 0
    assert ergebnis["laufzeit_s"] >= 0.05
    assert ergebnis["note"] == "Probe 1"


async def test_rechnen_laesst_den_ereignis_loop_atmen() -> None:
    """Im Betrieb gemeldet: "Task steht auf wartend", 60 s lang.

    Der Grund war diese Schleife. Sie rechnete im Ereignis-Loop des Workers,
    und dort haengt alles andere mit dran: der Flusher des Dashboard-Hooks,
    der die Ereignisse nach Valkey schreibt, die Lebenszeichen, die Annahme
    weiterer Nachrichten. Eine Minute Rechenlast hiess eine Minute ohne
    jedes Lebenszeichen -- der Task blieb auf seinem letzten gemeldeten
    Stand stehen ("wartend"), und der Worker galt danach als getrennt.

    Im Rechen-Pool faellt das nicht auf, weil der Task dort ohnehin einen
    eigenen Prozess hat. Im io-Pool schon, und genau dorthin schickt das
    Formular die Probe als Vorgabe.
    """
    takte = 0

    async def nebenher() -> None:
        nonlocal takte
        for _ in range(40):
            await asyncio.sleep(0.01)
            takte += 1

    mit = asyncio.create_task(nebenher())
    await ProbeTask().run(burn_s=0.3)
    mit.cancel()

    assert takte >= 5, (
        f"nur {takte} Takte waehrend 0,3 s Rechenlast -- der Loop stand still"
    )
