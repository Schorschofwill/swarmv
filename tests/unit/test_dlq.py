"""Dead-Letter-Bedienung (U15 Teil 1, R28, R28a, R29, R30).

Die gefaehrliche Stelle ist das **Sichten**. Die Bibliothek nennt
``list_dead_letters`` "non-destructive", und im Wortsinn stimmt das -- jede
gelesene Nachricht wird wieder eingereiht. Aber: sie wird **neu** eingereiht.
Die Reihenfolge aendert sich, der Zustellzaehler beginnt von vorn, und
stuerzt der Dienst mitten im Vorgang ab, sind die schon gelesenen Nachrichten
fort. R28a verlangt darum, dass die Oberflaeche das **vorher** sagt und dass
zwei Teammitglieder es nicht gleichzeitig ausloesen koennen.

Der Riegel ist der einzige Schutz gegen den zweiten Fall. Er traegt, weil der
Dienst in einem Prozess laeuft (KTD5) -- diese Einschraenkung ist hier keine
Sparsamkeit, sondern die Voraussetzung.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Der Riegel (R28a) ===
1. Ein zweiter gleichzeitiger Sicht-Versuch auf dieselbe Queue wird
   abgewiesen und erklaert.
2. Zwei verschiedene Queues lassen sich gleichzeitig sichten.
3. Nach dem Ende ist der Riegel wieder frei -- auch wenn die Sichtung warf.

=== ZYKLUS 2: Sichten ===
4. Das Sichten liefert Eintraege und die Zahl der gelesenen Nachrichten.
5. Ein Fehler wird benannt, nicht geworfen.

=== ZYKLUS 3: Wiedereinreihen (R29) ===
6. Einzeln: gefunden oder nicht gefunden, beides benannt.
7. Sammel: erfolgreiche und nicht verarbeitete Zahl **getrennt**.

=== ZYKLUS 4: Leeren (R30) ===
8. Leeren verlangt Queue-Name und Zahl als Bestaetigung.
9. Eine falsche Bestaetigung loescht nichts.
10. Laesst sich die Zahl nicht pruefen, wird nicht geloescht.
"""

from __future__ import annotations

import asyncio

import pytest

from swarmq_dashboard.dlq import DlqBusy, DlqService


class FakeAdmin:
    """Die DLQ-Schnittstelle der Bibliothek, mit ihren echten Rueckgabeformen."""

    def __init__(self, nachrichten: int = 3, kaputt: bool = False) -> None:
        self.nachrichten = nachrichten
        self.kaputt = kaputt
        self.aufrufe: list[str] = []
        self.verzoegerung = 0.0

    async def list_dead_letters(self, queue: str, limit: int = 100) -> tuple[list[dict], int]:
        self.aufrufe.append(f"list:{queue}")
        if self.verzoegerung:
            await asyncio.sleep(self.verzoegerung)
        if self.kaputt:
            raise RuntimeError("der Broker ist weg")
        eintraege = [{"task_id": f"t{i}", "task_name": "render_pdf", "queue": queue,
                      "headers": {}, "body_size": 42} for i in range(self.nachrichten)]
        return eintraege, len(eintraege)

    async def retry_dead_letter(self, queue: str, task_id: str,
                                scan_limit: int = 1000) -> tuple[bool, int]:
        self.aufrufe.append(f"retry:{queue}:{task_id}")
        return (task_id == "t0", 3)

    async def retry_all_dead_letters(self, queue: str, limit: int = 10_000) -> tuple[int, int]:
        self.aufrufe.append(f"retry_all:{queue}")
        return (2, 3)

    async def dead_letter_depth(self, queue: str) -> int:
        """Die passive Tiefe (R28). Der echte Admin hat sie, und die
        Bestaetigung des Leerens haengt daran -- eine Attrappe ohne sie liesse
        die Zahlenpruefung still ausfallen."""
        return self.nachrichten

    async def purge_dead_letters(self, queue: str) -> int:
        self.aufrufe.append(f"purge:{queue}")
        return self.nachrichten


# --- Zyklus 1 -----------------------------------------------------------


async def test_ein_zweiter_sicht_versuch_wird_abgewiesen() -> None:
    """R28a. Zwei gleichzeitige Durchlaeufe auf derselben Queue wuerden die
    Nachrichten zweimal umreihen und ihre Zustellzaehler zweimal
    zuruecksetzen -- und im schlimmsten Fall haelt keiner der beiden die
    Nachrichten, die der andere gerade in der Hand hat.
    """
    admin = FakeAdmin()
    admin.verzoegerung = 0.2
    s = DlqService(admin)

    erste = asyncio.ensure_future(s.peek("reports"))
    await asyncio.sleep(0.02)
    with pytest.raises(DlqBusy) as fehler:
        await s.peek("reports")
    assert "reports" in str(fehler.value)
    assert "gesichtet" in str(fehler.value).lower()
    await erste


async def test_zwei_queues_gleichzeitig() -> None:
    """Der Riegel gilt je Queue, nicht fuer alles: zwei Teams duerfen sich
    nicht gegenseitig blockieren.
    """
    admin = FakeAdmin()
    admin.verzoegerung = 0.1
    s = DlqService(admin)
    a, b = await asyncio.gather(s.peek("reports"), s.peek("billing"))
    assert a.gelesen == b.gelesen == 3
    assert sorted(admin.aufrufe) == ["list:billing", "list:reports"]


async def test_der_riegel_wird_auch_nach_einem_fehler_frei() -> None:
    """Sonst ist die Queue nach der ersten Stoerung dauerhaft gesperrt, und
    niemand kann sie mehr sichten -- ohne dass jemand weiss, warum.
    """
    admin = FakeAdmin(kaputt=True)
    s = DlqService(admin)
    ergebnis = await s.peek("reports")
    assert ergebnis.error

    admin.kaputt = False
    zweites = await s.peek("reports")
    assert zweites.error is None, "der Riegel blieb nach dem Fehler haengen"


# --- Zyklus 2 -----------------------------------------------------------


async def test_sichten_liefert_eintraege_und_zahl() -> None:
    s = DlqService(FakeAdmin(nachrichten=5))
    e = await s.peek("reports", limit=10)
    assert e.gelesen == 5
    assert [x["task_id"] for x in e.entries] == ["t0", "t1", "t2", "t3", "t4"]
    assert e.error is None


async def test_ein_fehler_wird_benannt() -> None:
    s = DlqService(FakeAdmin(kaputt=True))
    e = await s.peek("reports")
    assert e.entries == []
    assert e.error and "Broker" in e.error


# --- Zyklus 3 -----------------------------------------------------------


async def test_einzelnes_wiedereinreihen() -> None:
    s = DlqService(FakeAdmin())
    assert (await s.requeue("reports", "t0")).moved == 1
    nicht = await s.requeue("reports", "gibtesnicht")
    assert nicht.moved == 0
    assert nicht.scanned == 3


async def test_sammel_wiedereinreihen_meldet_beide_zahlen() -> None:
    """R29. Zwei von drei heisst: eine Nachricht kam **nicht** durch. Nur die
    erfolgreiche Zahl zu melden liesse "alles erledigt" lesen, wo eine
    Nachricht liegen blieb.
    """
    e = await DlqService(FakeAdmin()).requeue_all("reports")
    assert e.moved == 2
    assert e.scanned == 3
    assert e.unverarbeitet == 1


# --- Zyklus 4 -----------------------------------------------------------


async def test_leeren_verlangt_name_und_zahl() -> None:
    """R30. Leeren ist die einzige Handlung im ganzen Dashboard, die Daten
    endgueltig vernichtet -- die Bestaetigung muss beides nennen, was man
    vernichtet, und wie viel.
    """
    admin = FakeAdmin(nachrichten=7)
    s = DlqService(admin)
    e = await s.purge("reports", bestaetigung="reports", erwartet=7)
    assert e.moved == 7
    assert admin.aufrufe == ["purge:reports"]


async def test_eine_falsche_bestaetigung_loescht_nichts() -> None:
    admin = FakeAdmin(nachrichten=7)
    s = DlqService(admin)
    for name, zahl in (("falsch", 7), ("reports", 3), ("", 7)):
        e = await s.purge("reports", bestaetigung=name, erwartet=zahl)
        assert e.error, f"{name!r}/{zahl} wurde angenommen"
        assert e.moved == 0
    assert admin.aufrufe == [], "es wurde geloescht"


async def test_ohne_pruefbare_zahl_wird_nicht_geloescht() -> None:
    """Lieber eine Handlung, die nicht stattfindet, als eine, die mehr
    vernichtet als bestaetigt wurde.
    """
    class OhneTiefe(FakeAdmin):
        dead_letter_depth = None  # type: ignore[assignment]

    admin = OhneTiefe(nachrichten=7)
    e = await DlqService(admin).purge("reports", bestaetigung="reports", erwartet=7)
    assert e.error and "nicht pruefen" in e.error
    assert admin.aufrufe == [], "es wurde geloescht"
