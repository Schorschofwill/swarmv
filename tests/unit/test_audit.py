"""Audit-Strom: wer hat was getan.

Der Zweck steht in R27 und ist eng: die Frage "wer hat das storniert" muss
sich ohne Protokolldateien beantworten lassen. Zwei Eigenheiten folgen daraus,
und beide haben einen Test:

* **Ein eigener Strom mit eigener Frist.** Das Laengenbudget des Task-Stroms
  wirkt hier nicht (R9 ausdruecklich). Bei hundertausend Tasks am Tag
  schnitte es den Audit sonst binnen Minuten weg -- und gerade die
  seltenen Aktionen eines Teammitglieds sind die, die man spaeter sucht.
* **Ein Schreibfehler darf keine Aktion verhindern.** Die Anmeldung muss
  gehen, wenn Valkey weg ist (KTD8) -- also darf ein nicht geschriebener
  Audit-Eintrag sie nicht aufhalten. Er verschwindet aber nicht schweigend:
  der Aufruf sagt, dass er nicht geschrieben hat.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Festhalten ===
1. Jede Aktionsart aus R27 erzeugt genau einen Eintrag mit Konto, Zeitpunkt
   und Ziel.
2. Eine fehlgeschlagene Aktion wird als Versuch mit Ergebnis festgehalten,
   nicht als Erfolg.
3. Fehlanmeldungen erscheinen im Audit (R22).
4. Der Detailtext ist gedeckelt (was darin steht, verantwortet der
   Aufrufer -- siehe test_login.py).

=== ZYKLUS 2: Ein Schreibfehler haelt nichts auf ===
5. Ohne Strom meldet das Festhalten einen Fehlschlag, statt zu werfen.
6. Der Zaehler der nicht geschriebenen Eintraege ist ablesbar.

=== ZYKLUS 3: Eigener Strom, eigene Frist (R9, R27) ===
7. Der Audit-Strom liegt unter einem eigenen Schluessel.
8. Seine Trimmung nach Alter ist von der des Task-Stroms getrennt.
9. Sein Laengendeckel ist eigenstaendig.

=== ZYKLUS 4: Lesen ===
10. Die Eintraege kommen in Zeitreihenfolge zurueck.
11. Sie lassen sich nach Konto und nach Aktionsart filtern.

=== ZYKLUS 5: Workflow stornieren (Workflow-Ansicht, U7, KTD12) ===
12. ``workflow_cancel`` ist eine Aktionsart und laesst sich festhalten und
    filtern (R15).

=== ZYKLUS 6: Die Obergrenze gilt nach dem Filter, von hinten ===
13. ``limit`` liefert die neuesten Eintraege, nicht die aeltesten.
14. Ein Filter findet einen Eintrag auch dann, wenn er nicht unter den
    ersten ``limit`` Eintraegen des Stroms steht (gefunden beim Test der
    Workflow-Ansicht: ``audit list --action workflow_cancel --limit 5`` war
    leer, obwohl der Eintrag da war).
15. Das gilt auch hinter der ersten Leseseite des Stroms.
"""

from __future__ import annotations

import pytest

from swarmq_dashboard.audit import (
    AUDIT_ACTIONS,
    AuditEntry,
    AuditLog,
)
from swarmq_dashboard.events import AUDIT_STREAM


class FakeStream:
    """Ein Strom im Speicher, mit den Aufrufen, die der Audit braucht."""

    def __init__(self, kaputt: bool = False) -> None:
        self.eintraege: list[tuple[str, bytes]] = []
        self.getrimmt: list[tuple[str, int]] = []
        self.deckel: list[int] = []
        self.kaputt = kaputt
        self._n = 0

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
        if self.kaputt:
            raise RuntimeError("Valkey ist weg")
        self.deckel.append(maxlen)
        for roh in events:
            self._n += 1
            self.eintraege.append((f"{self._n}-0", roh))

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]:
        # Wie XRANGE mit exklusiver Untergrenze: nach ``last_id``, hoechstens ``count``.
        nach = [e for e in self.eintraege if not last_id or int(e[0].split("-")[0]) > int(last_id.split("-")[0])]
        return nach[:count]

    async def trim_by_min_id(self, key: str, min_ms: int) -> int:
        self.getrimmt.append((key, min_ms))
        return 0


# --- Zyklus 1 -----------------------------------------------------------


async def test_jede_aktionsart_erzeugt_einen_eintrag() -> None:
    """Covers R27. Die Liste der Arten steht im Modul, nicht im Test-Kopf --
    damit eine neue Aktion nicht vergessen werden kann.
    """
    s = FakeStream()
    a = AuditLog(s)
    for art in AUDIT_ACTIONS:
        assert await a.record(art, account="ops", target="t-1", now=100.0) is True
    assert len(s.eintraege) == len(AUDIT_ACTIONS)

    eintraege = await a.read()
    assert {e.action for e in eintraege} == set(AUDIT_ACTIONS)
    for e in eintraege:
        assert e.account == "ops"
        assert e.target == "t-1"
        assert e.ts == 100.0


async def test_ein_fehlschlag_wird_als_versuch_festgehalten() -> None:
    """Sonst liest sich eine abgelehnte Stornierung wie eine vollzogene."""
    s = FakeStream()
    a = AuditLog(s)
    await a.record("task_cancel", account="ops", target="t-1", outcome="abgelehnt",
                   detail="der Task lief bereits", now=100.0)
    (eintrag,) = await a.read()
    assert eintrag.outcome == "abgelehnt"
    assert eintrag.detail == "der Task lief bereits"


async def test_fehlanmeldungen_erscheinen_im_audit() -> None:
    """R22: "der Betreiber sieht Fehlanmeldungen im Audit"."""
    s = FakeStream()
    a = AuditLog(s)
    await a.record("login", account="ops", outcome="abgewiesen", target="10.0.0.1", now=100.0)
    (eintrag,) = await a.read()
    assert eintrag.action == "login"
    assert eintrag.outcome == "abgewiesen"
    assert eintrag.target == "10.0.0.1"


async def test_der_detailtext_ist_gedeckelt() -> None:
    """Der Audit-Strom hat weder die Rechtezusage aus R7a noch die Trimmung
    des Verzeichnisses -- was hier hineingeraet, steht dauerhaft und offen.

    **Was** hineingeraet, kann dieses Modul nicht beurteilen: es
    unterscheidet ein Passwort nicht von einem Tasknamen. Die Zusage gehoert
    an den Aufrufer, und der hat seinen eigenen Test (siehe test_login.py).
    Hier wird nur die Laenge gedeckelt -- damit ein versehentlich
    durchgereichter Koerper nicht den ganzen Strom fuellt.
    """
    from swarmq_dashboard.audit import MAX_DETAIL

    s = FakeStream()
    a = AuditLog(s)
    await a.record("login", account="ops", detail="x" * 10_000, now=100.0)
    (eintrag,) = await a.read()
    assert eintrag.detail is not None
    assert len(eintrag.detail) == MAX_DETAIL


# --- Zyklus 2 -----------------------------------------------------------


async def test_ohne_strom_meldet_das_festhalten_einen_fehlschlag() -> None:
    """KTD8: die Anmeldung muss gehen, wenn Valkey weg ist. Ein
    Audit-Schreibfehler darf sie also nicht aufhalten -- aber er verschwindet
    auch nicht schweigend.
    """
    a = AuditLog(FakeStream(kaputt=True))
    assert await a.record("login", account="ops", now=100.0) is False
    assert await a.record("task_cancel", account="ops", now=100.0) is False
    assert a.lost == 2

    ohne = AuditLog(None)
    assert await ohne.record("login", account="ops", now=100.0) is False
    assert ohne.lost == 1


# --- Zyklus 3 -----------------------------------------------------------


async def test_der_audit_liegt_unter_eigenem_schluessel() -> None:
    from swarmq_dashboard.events import TASK_STREAM

    assert AUDIT_STREAM != TASK_STREAM
    s = FakeStream()
    a = AuditLog(s)
    await a.record("login", account="ops", now=100.0)
    await a.trim(older_than=50.0)
    assert s.getrimmt == [(AUDIT_STREAM, 50_000)]


async def test_der_laengendeckel_ist_eigenstaendig() -> None:
    """R9 ausdruecklich: das Budget des Task-Stroms begrenzt den Audit nicht.
    Bei hunderttausend Tasks am Tag schnitte es ihn binnen Minuten weg.
    """
    from swarmq_dashboard.config import DEFAULT_BACKLOG_CAP

    s = FakeStream()
    a = AuditLog(s)
    await a.record("login", account="ops", now=100.0)
    assert s.deckel[0] != DEFAULT_BACKLOG_CAP
    assert s.deckel[0] >= 10_000, f"der Deckel ist zu klein fuer sieben Tage: {s.deckel[0]}"


# --- Zyklus 4 -----------------------------------------------------------


async def test_lesen_in_zeitreihenfolge_und_gefiltert() -> None:
    s = FakeStream()
    a = AuditLog(s)
    await a.record("login", account="ops", now=100.0)
    await a.record("task_cancel", account="dev", target="t-9", now=200.0)
    await a.record("login", account="dev", now=300.0)

    alle = await a.read()
    assert [e.ts for e in alle] == [100.0, 200.0, 300.0]
    assert [e.account for e in await a.read(account="dev")] == ["dev", "dev"]
    assert [e.ts for e in await a.read(action="login")] == [100.0, 300.0]
    assert await a.read(account="dev", action="login") == [
        AuditEntry(action="login", ts=300.0, account="dev", target=None,
                   outcome="ok", detail=None)
    ]


async def test_eine_unbekannte_aktionsart_wird_abgewiesen() -> None:
    """Sonst schleicht sich ein Tippfehler ein, und der Filter findet die
    Aktion spaeter nicht -- ohne dass es jemand merkt.
    """
    a = AuditLog(FakeStream())
    with pytest.raises(ValueError):
        await a.record("gibtesnicht", account="ops", now=100.0)


# --- Zyklus 5 -----------------------------------------------------------


async def test_workflow_cancel_ist_eine_aktionsart() -> None:
    """R15: das Stornieren eines Workflows ist ein Audit-Ereignis. Die Liste
    waechst erst nach diesem roten Test (KTD12)."""
    s = FakeStream()
    a = AuditLog(s)
    assert await a.record("workflow_cancel", account="ops", target="wf-1",
                          outcome="lief bereits", now=100.0) is True
    (eintrag,) = await a.read(action="workflow_cancel")
    assert eintrag.target == "wf-1"
    assert eintrag.outcome == "lief bereits"


# --- Zyklus 6 -----------------------------------------------------------


async def test_die_obergrenze_liefert_die_neuesten_eintraege() -> None:
    a = AuditLog(FakeStream())
    for i in range(5):
        await a.record("login", account="ops", now=100.0 + i)

    assert [e.ts for e in await a.read(limit=2)] == [103.0, 104.0]


async def test_ein_filter_findet_auch_einen_spaeten_eintrag() -> None:
    a = AuditLog(FakeStream())
    for i in range(6):
        await a.record("login", account="ops", now=100.0 + i)
    await a.record("workflow_cancel", account="georg", target="wf-1", now=200.0)

    (eintrag,) = await a.read(action="workflow_cancel", limit=5)
    assert eintrag.target == "wf-1"


async def test_auch_hinter_der_ersten_leseseite(monkeypatch: pytest.MonkeyPatch) -> None:
    import swarmq_dashboard.audit as audit

    monkeypatch.setattr(audit, "_SEITE", 2)
    a = AuditLog(FakeStream())
    for i in range(5):
        await a.record("login", account="ops", now=100.0 + i)
    await a.record("workflow_cancel", account="georg", target="wf-2", now=200.0)

    (eintrag,) = await a.read(action="workflow_cancel", limit=1)
    assert eintrag.target == "wf-2"
    assert [e.ts for e in await a.read(limit=3)] == [103.0, 104.0, 200.0]
