"""U4 -- Baum, Zaehler und Gruende eines Workflows, rein und ohne I/O.

Das Modul bekommt zwei Dinge: die Kopie des Workflow-Zustands (Topologie im
Format von ``node.to_dict()`` der Bibliothek, ``step_tasks``, Status,
Sichtbarkeit, eigener Storno-Vermerk, Fehlertext) und die Task-Zeilen des
Workflows aus dem Verzeichnis. Daraus entsteht alles, was Liste und
Detailansicht zeigen (KTD7). Keine Datenbank, kein Netz: jede Anzeige aus
R8 bis R12 und R16 laesst sich hier aus einem Testfall ablesen.

Zwei Grenzen, die die Tests festhalten:

* **Kein Endstatus aus Schritten (R6).** Fertig, gescheitert und storniert
  kommen nur aus dem Status der Kopie. Die Schritte liefern Zaehler, Gruende
  und "nichts mehr aktiv" -- nie einen Endstatus.
* **Eingereiht ist, was in ``step_tasks`` steht ODER eine Zeile hat.** Die
  Bibliothek veroeffentlicht zuerst und vermerkt danach; die Zeile kann vor
  dem Vermerk da sein, und der Vermerk vor der Zeile (KTD7).

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Knoten der Topologie ===
1. Die einfache Wurzel mit Pfad "" ergibt einen Baum mit genau einem Blatt.
2. Eine Kette bekommt die Pfade /0, /1, /2 mit Task-Namen aus der Topologie.
3. Ein Chord bekommt Kopf /h (Gruppe) mit Gliedern /h/g<i> und Body /b.
4. Ein Blatt in step_tasks ohne Zeile ist "eingereiht, noch nicht gemeldet".
5. Die Zeile wird ueber die Kennung aus step_tasks gefunden, auch ohne
   step_path an der Zeile.

=== ZYKLUS 2: Fortschritt und Zaehler ===
6. Covers AE1. Chord mit 50 Downloads, 47 fertig: Fortschritt 47/51, "läuft".
7. Jede Gruppe und der Kopf zaehlen ihre Blaetter je Status.

=== ZYKLUS 3: Gruende fuer nicht eingereihte Schritte ===
8. Covers AE3. Kopf laeuft: Body "wartet auf Vorgänger", wartet auf N Glieder.
9. Covers AE4. Kette mit gescheitertem Mittelschritt: Fehlertext oben,
   letzter Schritt "nie gestartet, Workflow gescheitert".
10. Stornierter Workflow: "nie gestartet, Workflow storniert".
11. Storniertes Kopf-Glied: Body "wartet auf storniertes Glied, startet nie",
    Status bleibt "läuft", kein "wartet auf N".
12. Hinter einem Glied, das nie startet, startet auch der Nachfolger nie.
13. Alle Glieder fertig, Body nicht in step_tasks, "running":
    "Vorgänger fertig, nicht eingereiht", kein "wartet auf 0".
14. Body mit laufender Zeile, der in step_tasks fehlt, laeuft ohne Grund.

=== ZYKLUS 4: Faltung ===
15. Covers AE2. Gruppe 498 fertig, 1 laufend, 1 gescheitert: zwei offene
    Zeilen, eine gefaltete mit 498.
16. Ein zusammengesetztes Glied faltet nur, wenn alle seine Blaetter fertig
    sind; Glieder einer Kette falten nie.

=== ZYKLUS 5: Angezeigter Status (KTD6) ===
17. Endstatus "completed" mit Storno-Vermerk ergibt "fertig".
18. Covers AE11. Vermerk, alle Glieder storniert, "running":
    "Stornierung angefordert, nichts mehr aktiv", nicht stornierbar.
19. Ein Blatt in step_tasks ohne Zeile zaehlt als aktiv: nur "angefordert".
20. Verfallen nach "nichts mehr aktiv": "Zustand verfallen, zuletzt:
    Stornierung angefordert, nichts mehr aktiv", mit Zeitpunkt der Lesung.
21. Verfallen ohne bekannten letzten Stand: "zuletzt: läuft" (R17).
22. Ohne jede Lesung: "Zustand nie gesehen", kein Endstatus.
23. Ein Endstatus schlaegt auch "verfallen".
24. Stornierbar sind nur "läuft" und "Stornierung angefordert" (AE8, AE9).

=== ZYKLUS 6: Ergebnis-Kennung der Kopf-Glieder (KTD8, R18) ===
25. Blatt-Glied: seine Kennung; Ketten-Glied: die seines letzten Blatts;
    Gruppen-Glied: nicht einzeln anzeigbar.
26. chain(a, group(b, c)) als Glied ist nicht anzeigbar; ein Chord-Glied
    folgt seinem Body.
27. Nicht-Kopf-Knoten tragen keinen Ergebnisverweis.

=== ZYKLUS 7: Gescheiterte Schritte (R12) ===
28. Mehrere gescheiterte Schritte, frueheste zuerst (Dead Letter zaehlt mit).
29. Kein gescheiterter Schritt: nur der Fehlertext.
30. Ein laufender Workflow nennt keine gescheiterten Schritte oben.

=== ZYKLUS 8: Ohne Topologie (R7) ===
31. Nie gesehen: flacher Baum aus den bekannten Schritten, Fortschritt nur
    ueber diese.
32. Eine Zeile ohne step_path bekommt den Pfad aus dem Muster <wf>#<pfad>.

=== ZYKLUS 9: Fuer die Liste (R2) ===
33. Form der Wurzel und Name des ersten Schritts.
"""

from __future__ import annotations

from typing import Any

from swarmq_dashboard.directory.writer import SICHT_NIE_GESEHEN
from swarmq_dashboard.workflow_baum import (
    Ergebnisverweis,
    Taskzeile,
    Workflowkopie,
    baue_baum,
)

WF = "0b6c3c6e-9f36-4c2e-8f7e-3d1f2a4b5c6d"


def sig(name: str) -> dict[str, Any]:
    """Ein Blatt im Format von ``Signature.to_dict()``."""
    return {"kind": "signature", "task": name, "args": [], "kwargs": {}, "immutable": False}


def kette(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "chain", "steps": list(steps)}


def gruppe(*members: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "group", "members": list(members)}


def chord(header: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "chord", "header": header, "body": body}


def kopie(
    topologie: dict[str, Any] | None,
    *,
    step_tasks: dict[str, str] | None = None,
    status: str | None = "running",
    sichtbarkeit: str = "gesehen",
    **rest: Any,
) -> Workflowkopie:
    return Workflowkopie(
        workflow_id=WF,
        topologie=topologie,
        step_tasks=step_tasks or {},
        status=status,
        sichtbarkeit=sichtbarkeit,
        **rest,
    )


def zeile(pfad: str | None, status: str, **rest: Any) -> Taskzeile:
    return Taskzeile(
        task_id=rest.pop("task_id", f"{WF}#{pfad}"),
        step_path=pfad,
        status=status,
        **rest,
    )


# === ZYKLUS 1: Knoten der Topologie ===


def test_einfache_wurzel_ergibt_genau_ein_blatt() -> None:
    baum = baue_baum(
        kopie(sig("dl"), step_tasks={"": f"{WF}#"}), [zeile("", "fertig")]
    )

    assert baum.wurzel.pfad == ""
    assert baum.wurzel.art == "schritt"
    assert baum.wurzel.kinder == ()
    assert baum.wurzel.status == "fertig"
    assert [b.pfad for b in baum.blaetter] == [""]


def test_kette_bekommt_pfade_und_task_namen() -> None:
    baum = baue_baum(
        kopie(kette(sig("a"), sig("b"), sig("c")), step_tasks={"/0": f"{WF}#/0"}),
        [zeile("/0", "laufend")],
    )

    assert baum.wurzel.art == "kette"
    assert [k.pfad for k in baum.wurzel.kinder] == ["/0", "/1", "/2"]
    assert [k.task_name for k in baum.wurzel.kinder] == ["a", "b", "c"]
    assert baum.wurzel.kinder[0].status == "laufend"
    assert [b.pfad for b in baum.blaetter] == ["/0", "/1", "/2"]


def test_chord_bekommt_kopf_mit_gliedern_und_body() -> None:
    baum = baue_baum(kopie(chord(gruppe(sig("dl"), sig("dl")), sig("merge"))), [])

    kopf, body = baum.wurzel.kinder
    assert baum.wurzel.art == "chord"
    assert (kopf.pfad, kopf.art, kopf.rolle) == ("/h", "gruppe", "kopf")
    assert [g.pfad for g in kopf.kinder] == ["/h/g0", "/h/g1"]
    assert (body.pfad, body.art, body.rolle) == ("/b", "schritt", "body")
    assert baum.wurzel.rolle is None


def test_blatt_in_step_tasks_ohne_zeile_ist_eingereiht_noch_nicht_gemeldet() -> None:
    baum = baue_baum(
        kopie(kette(sig("a"), sig("b")), step_tasks={"/0": f"{WF}#/0"}), []
    )

    a = baum.wurzel.kinder[0]
    assert a.status == "eingereiht"
    assert a.task_id == f"{WF}#/0"
    assert a.zeile is None


def test_zeile_wird_ueber_die_kennung_aus_step_tasks_gefunden() -> None:
    """Ein Schritt, den nur das Muster erkannt hat, traegt noch keinen
    step_path an der Zeile -- die Kennung aus step_tasks findet ihn trotzdem."""
    baum = baue_baum(
        kopie(kette(sig("a"), sig("b")), step_tasks={"/0": "eigene-kennung"}),
        [zeile(None, "fertig", task_id="eigene-kennung")],
    )

    a = baum.wurzel.kinder[0]
    assert a.status == "fertig"
    assert a.task_id == "eigene-kennung"


# === ZYKLUS 2: Fortschritt und Zaehler ===


def download_chord(anzahl: int = 50) -> dict[str, Any]:
    return chord(gruppe(*[sig("download") for _ in range(anzahl)]), sig("verarbeiten"))


def kopf_zeilen(*statusse: str) -> list[Taskzeile]:
    return [zeile(f"/h/g{i}", s) for i, s in enumerate(statusse)]


def test_chord_mit_47_von_50_fertig_ergibt_47_von_51_und_laeuft() -> None:
    """Covers AE1."""
    zeilen = kopf_zeilen(*(["fertig"] * 47 + ["laufend"] * 2 + ["wiederholung"]))

    baum = baue_baum(kopie(download_chord()), zeilen)

    assert (baum.fortschritt.fertig, baum.fortschritt.gesamt) == (47, 51)
    assert baum.status.text == "läuft"


def test_gruppe_und_kopf_zaehlen_ihre_blaetter_je_status() -> None:
    """Gezaehlt werden Blaetter, auch unter zusammengesetzten Gliedern; ein
    nie eingereihtes Blatt zaehlt als "nicht_eingereiht"."""
    topologie = chord(
        gruppe(sig("a"), sig("b"), kette(sig("c"), sig("d"))), sig("body")
    )
    zeilen = [zeile("/h/g0", "fertig"), zeile("/h/g1", "laufend"), zeile("/h/g2/0", "fertig")]

    baum = baue_baum(kopie(topologie), zeilen)

    kopf = baum.wurzel.kinder[0]
    assert kopf.zaehler == {"fertig": 2, "laufend": 1, "nicht_eingereiht": 1}


# === ZYKLUS 3: Gruende fuer nicht eingereihte Schritte ===


def test_laufender_kopf_laesst_den_body_auf_vorgaenger_warten() -> None:
    """Covers AE3. Der Body steht im Baum, obwohl ihn kein Ereignis meldete."""
    zeilen = kopf_zeilen("fertig", "laufend", "wartend")

    baum = baue_baum(kopie(download_chord(3)), zeilen)

    body = baum.wurzel.kinder[1]
    assert body.status == "nicht_eingereiht"
    assert body.grund == "wartet auf Vorgänger"
    assert body.wartet_auf == 2


def test_gescheiterter_mittelschritt_steht_oben_und_der_rest_startet_nie() -> None:
    """Covers AE4."""
    topologie = kette(sig("a"), sig("b"), sig("c"))
    zeilen = [zeile("/0", "fertig"), zeile("/1", "gescheitert", ended_at=20.0)]

    baum = baue_baum(
        kopie(topologie, status="failed", fehler="ValueError: kaputt"), zeilen
    )

    assert baum.status.text == "gescheitert"
    assert baum.fehlertext == "ValueError: kaputt"
    assert [k.pfad for k in baum.fehlschlaege] == ["/1"]
    c = baum.wurzel.kinder[2]
    assert (c.status, c.grund) == ("nicht_eingereiht", "nie gestartet, Workflow gescheitert")


def test_stornierter_workflow_laesst_offene_schritte_nie_starten() -> None:
    baum = baue_baum(
        kopie(kette(sig("a"), sig("b")), status="cancelled"), [zeile("/0", "fertig")]
    )

    assert baum.status.text == "storniert"
    assert baum.wurzel.kinder[1].grund == "nie gestartet, Workflow storniert"


def test_storniertes_kopf_glied_laesst_den_body_nie_starten() -> None:
    """Der Workflow-Status bleibt, was der Zustand traegt (R6, R11)."""
    zeilen = kopf_zeilen("fertig", "storniert", "laufend")

    baum = baue_baum(kopie(download_chord(3)), zeilen)

    body = baum.wurzel.kinder[1]
    assert body.grund == "wartet auf storniertes Glied, startet nie"
    assert body.wartet_auf is None
    assert baum.status.text == "läuft"


def test_hinter_einem_glied_das_nie_startet_startet_auch_der_nachfolger_nie() -> None:
    baum = baue_baum(
        kopie(kette(sig("a"), sig("b"), sig("c"))), [zeile("/0", "storniert")]
    )

    b, c = baum.wurzel.kinder[1:]
    assert b.grund == "wartet auf storniertes Glied, startet nie"
    assert c.grund == "wartet auf storniertes Glied, startet nie"


def test_alle_glieder_fertig_body_nicht_eingereiht_heisst_vorgaenger_fertig() -> None:
    """KTD7: kein "wartet auf 0 Glieder"."""
    baum = baue_baum(kopie(download_chord(2)), kopf_zeilen("fertig", "fertig"))

    body = baum.wurzel.kinder[1]
    assert body.grund == "Vorgänger fertig, nicht eingereiht"
    assert body.wartet_auf is None


def test_body_mit_laufender_zeile_ohne_vermerk_laeuft_ohne_grund() -> None:
    """Die Bibliothek veroeffentlicht zuerst und vermerkt danach (KTD7)."""
    zeilen = [*kopf_zeilen("fertig", "fertig"), zeile("/b", "laufend")]

    baum = baue_baum(kopie(download_chord(2)), zeilen)

    body = baum.wurzel.kinder[1]
    assert (body.status, body.grund, body.wartet_auf) == ("laufend", None, None)


# === ZYKLUS 4: Faltung ===


def test_gruppe_mit_498_fertigen_zeigt_zwei_offene_und_eine_gefaltete_zeile() -> None:
    """Covers AE2."""
    topologie = gruppe(*[sig("dl") for _ in range(500)])
    statusse = ["fertig"] * 250 + ["laufend"] + ["fertig"] * 248 + ["gescheitert"]
    zeilen = [zeile(f"/g{i}", s) for i, s in enumerate(statusse)]

    baum = baue_baum(kopie(topologie), zeilen)

    assert [k.pfad for k in baum.wurzel.offene_kinder] == ["/g250", "/g499"]
    assert len(baum.wurzel.gefaltete_kinder) == 498


def test_zusammengesetztes_glied_faltet_nur_ganz_fertig_und_ketten_falten_nie() -> None:
    topologie = kette(
        sig("start"),
        gruppe(kette(sig("a"), sig("b")), kette(sig("c"), sig("d"))),
    )
    zeilen = [
        zeile("/0", "fertig"),
        zeile("/1/g0/0", "fertig"),
        zeile("/1/g0/1", "fertig"),
        zeile("/1/g1/0", "fertig"),
    ]

    baum = baue_baum(kopie(topologie), zeilen)

    assert baum.wurzel.gefaltete_kinder == ()
    assert len(baum.wurzel.offene_kinder) == 2
    innere = baum.wurzel.kinder[1]
    assert [k.pfad for k in innere.gefaltete_kinder] == ["/1/g0"]
    assert [k.pfad for k in innere.offene_kinder] == ["/1/g1"]


# === ZYKLUS 5: Angezeigter Status (KTD6) ===


def test_endstatus_schlaegt_den_eigenen_storno_vermerk() -> None:
    baum = baue_baum(
        kopie(sig("a"), status="completed", storno_angefordert_at=5.0),
        [zeile("", "fertig")],
    )

    assert baum.status.code == "fertig"
    assert baum.status.text == "fertig"
    assert baum.status.endstatus is True


def test_vermerk_und_kein_schritt_aktiv_heisst_nichts_mehr_aktiv() -> None:
    """Covers AE11. Kein Endstatus, und kein erneutes Stornieren."""
    zeilen = kopf_zeilen(*["storniert"] * 50)

    baum = baue_baum(kopie(download_chord(), storno_angefordert_at=5.0), zeilen)

    assert baum.status.code == "angefordert_nichts_aktiv"
    assert baum.status.text == "Stornierung angefordert, nichts mehr aktiv"
    assert baum.status.endstatus is False


def test_eingereiht_ohne_zeile_zaehlt_als_aktiv() -> None:
    baum = baue_baum(
        kopie(
            download_chord(2),
            step_tasks={"/h/g0": f"{WF}#/h/g0", "/h/g1": f"{WF}#/h/g1"},
            storno_angefordert_at=5.0,
        ),
        [zeile("/h/g0", "storniert")],
    )

    assert baum.wurzel.kinder[0].kinder[1].status == "eingereiht"
    assert baum.status.code == "angefordert"
    assert baum.status.text == "Stornierung angefordert"


def test_verfallen_nach_nichts_mehr_aktiv_nennt_den_letzten_stand() -> None:
    baum = baue_baum(
        kopie(
            download_chord(2),
            sichtbarkeit="verfallen",
            zuletzt_angezeigt="angefordert_nichts_aktiv",
            gelesen_at=1234.0,
            storno_angefordert_at=5.0,
        ),
        kopf_zeilen("storniert", "storniert"),
    )

    assert baum.status.code == "verfallen"
    assert baum.status.zuletzt == "angefordert_nichts_aktiv"
    assert baum.status.text == (
        "Zustand verfallen, zuletzt: Stornierung angefordert, nichts mehr aktiv"
    )
    assert baum.status.gelesen_at == 1234.0
    assert baum.status.endstatus is False


def test_verfallen_ohne_bekannten_letzten_stand_heisst_zuletzt_laeuft() -> None:
    """R17: ein zuletzt laufend gesehener Zustand, der verschwand."""
    baum = baue_baum(
        kopie(kette(sig("a"), sig("b")), sichtbarkeit="verfallen"), [zeile("/0", "fertig")]
    )

    assert baum.status.text == "Zustand verfallen, zuletzt: läuft"


# === ZYKLUS 8: Ohne Topologie (R7) ===


def test_ohne_topologie_entsteht_ein_flacher_baum_aus_den_bekannten_schritten() -> None:
    zeilen = [
        zeile("/1", "laufend", first_seen_at=20.0),
        zeile("/0", "fertig", first_seen_at=10.0),
    ]

    baum = baue_baum(kopie(None, status=None, sichtbarkeit=SICHT_NIE_GESEHEN), zeilen)

    assert baum.wurzel.art == "unbekannt"
    assert [k.pfad for k in baum.wurzel.kinder] == ["/0", "/1"]
    assert [k.status for k in baum.wurzel.kinder] == ["fertig", "laufend"]
    assert (baum.fortschritt.fertig, baum.fortschritt.gesamt) == (1, 2)


def test_zeile_ohne_step_path_bekommt_den_pfad_aus_dem_muster() -> None:
    """KTD5: die erste Front ist ohne Anwendungs-Hook nur ueber das Muster
    ``<workflow_id>#<step_path>`` zu erkennen; eine Kennung ohne das Muster
    hat keinen bekannten Pfad."""
    zeilen = [
        zeile(None, "fertig", task_id=f"{WF}#/g0", first_seen_at=1.0),
        zeile(None, "fertig", task_id="eigene-kennung", first_seen_at=2.0),
    ]

    baum = baue_baum(kopie(None, status=None, sichtbarkeit=SICHT_NIE_GESEHEN), zeilen)

    assert [k.pfad for k in baum.wurzel.kinder] == ["/g0", None]


def test_im_baum_findet_das_muster_eine_zeile_ohne_pfad_und_ohne_vermerk() -> None:
    """Veroeffentlicht, noch nicht vermerkt, und nur ueber das Muster erkannt:
    die Zeile gehoert trotzdem an ihr Blatt (KTD5, KTD7)."""
    baum = baue_baum(
        kopie(gruppe(sig("a"), sig("b"))),
        [zeile(None, "laufend", task_id=f"{WF}#/g1")],
    )

    assert baum.wurzel.kinder[1].status == "laufend"


def test_ohne_jede_lesung_heisst_es_zustand_nie_gesehen() -> None:
    """Covers AE6. Auch wenn alle bekannten Schritte fertig sind: kein
    Endstatus aus Schritten (R6). Die Sichtbarkeit kommt so aus dem
    Verzeichnis, wie der Writer sie ablegt."""
    baum = baue_baum(
        kopie(None, status=None, sichtbarkeit=SICHT_NIE_GESEHEN),
        [zeile("/0", "fertig")],
    )

    assert baum.status.code == "nie_gesehen"
    assert baum.status.text == "Zustand nie gesehen"
    assert baum.status.endstatus is False


def test_endstatus_schlaegt_auch_verfallen() -> None:
    """Covers AE5. Die Kopie traegt "fertig", der Zustand in Valkey ist weg."""
    baum = baue_baum(
        kopie(sig("a"), status="completed", sichtbarkeit="verfallen"),
        [zeile("", "fertig")],
    )

    assert baum.status.text == "fertig"


def test_stornierbar_sind_nur_laeuft_und_stornierung_angefordert() -> None:
    """AE8: eine bestehende Anforderung bietet den Knopf weiter an; AE9 und
    AE11: nach Endstatus und bei "nichts mehr aktiv" nicht. Ohne lesbaren
    Zustand gibt es nichts zu stornieren (KTD9)."""
    from swarmq_dashboard.workflow_baum import Statusanzeige

    stornierbar = {
        code: Statusanzeige(code).stornierbar
        for code in (
            "laeuft",
            "angefordert",
            "angefordert_nichts_aktiv",
            "fertig",
            "gescheitert",
            "storniert",
            "verfallen",
            "nie_gesehen",
        )
    }

    assert {c for c, ja in stornierbar.items() if ja} == {"laeuft", "angefordert"}


# === ZYKLUS 6: Ergebnis-Kennung der Kopf-Glieder (KTD8, R18) ===


def test_kopf_glieder_verweisen_auf_die_kennung_ihres_ergebnisses() -> None:
    topologie = chord(
        gruppe(sig("a"), kette(sig("b"), sig("c")), gruppe(sig("d"), sig("e"))),
        sig("body"),
    )
    step_tasks = {"/h/g0": "kennung-a", "/h/g1/1": "kennung-c"}

    baum = baue_baum(kopie(topologie, step_tasks=step_tasks), [])

    blatt, kette_, gruppe_ = baum.wurzel.kinder[0].kinder
    assert blatt.ergebnis is not None and blatt.ergebnis.task_id == "kennung-a"
    assert kette_.ergebnis is not None and kette_.ergebnis.task_id == "kennung-c"
    assert gruppe_.ergebnis is not None and gruppe_.ergebnis.anzeigbar is False
    assert gruppe_.ergebnis.task_id is None


def test_kette_auf_gruppe_hat_keinen_verweis_und_ein_chord_glied_folgt_dem_body() -> None:
    topologie = chord(
        gruppe(
            kette(sig("a"), gruppe(sig("b"), sig("c"))),
            chord(gruppe(sig("d")), sig("e")),
        ),
        sig("body"),
    )
    step_tasks = {"/h/g1/b": "kennung-e"}

    baum = baue_baum(kopie(topologie, step_tasks=step_tasks), [])

    kette_auf_gruppe, chord_glied = baum.wurzel.kinder[0].kinder
    assert kette_auf_gruppe.ergebnis == Ergebnisverweis(anzeigbar=False)
    assert chord_glied.ergebnis == Ergebnisverweis(anzeigbar=True, task_id="kennung-e")


def test_nur_kopf_glieder_tragen_einen_ergebnisverweis() -> None:
    baum = baue_baum(kopie(kette(gruppe(sig("a")), download_chord(1))), [])

    gruppe_, chord_ = baum.wurzel.kinder
    assert gruppe_.kinder[0].ergebnis is None
    assert chord_.kinder[1].ergebnis is None
    assert chord_.kinder[0].ergebnis is None


# === ZYKLUS 7: Gescheiterte Schritte (R12) ===


def test_gescheiterte_schritte_stehen_fruehester_zuerst() -> None:
    """Dead Letter zaehlt mit; ohne Ende zaehlt der erste Sichtkontakt."""
    zeilen = [
        zeile("/g0", "gescheitert", first_seen_at=1.0, ended_at=30.0),
        zeile("/g1", "fertig", first_seen_at=1.0, ended_at=5.0),
        zeile("/g2", "dead_letter", first_seen_at=1.0, ended_at=10.0),
        zeile("/g3", "gescheitert", first_seen_at=20.0),
    ]

    baum = baue_baum(
        kopie(gruppe(*[sig("x") for _ in range(4)]), status="failed", fehler="boom"),
        zeilen,
    )

    assert [k.pfad for k in baum.fehlschlaege] == ["/g2", "/g3", "/g0"]


def test_ohne_gescheiterten_schritt_steht_nur_der_fehlertext() -> None:
    """R12: etwa wenn schon die Einreihung scheiterte."""
    baum = baue_baum(
        kopie(kette(sig("a"), sig("b")), status="failed", fehler="publish kaputt"), []
    )

    assert baum.fehlertext == "publish kaputt"
    assert baum.fehlschlaege == ()


def test_ein_nicht_gescheiterter_workflow_nennt_oben_nichts() -> None:
    """Der Schritt ist schon gescheitert, die Kopie traegt noch "running":
    oben steht erst etwas, wenn der Zustand "gescheitert" traegt (R6, R12).
    Im Baum steht der Schritt trotzdem mit seinem Status."""
    baum = baue_baum(
        kopie(gruppe(sig("a"), sig("b"))), [zeile("/g0", "gescheitert")]
    )

    assert baum.fehlschlaege == ()
    assert baum.wurzel.kinder[0].status == "gescheitert"


# === ZYKLUS 9: Fuer die Liste (R2) ===


def test_form_der_wurzel_und_name_des_ersten_schritts() -> None:
    baum = baue_baum(kopie(download_chord(2)), [])
    flach = baue_baum(
        kopie(None, status=None, sichtbarkeit=SICHT_NIE_GESEHEN),
        [zeile("/1", "laufend", task_name="spaeter", first_seen_at=2.0),
         zeile("/0", "fertig", task_name="zuerst", first_seen_at=1.0)],
    )

    assert (baum.form, baum.erster_schritt) == ("chord", "download")
    assert (flach.form, flach.erster_schritt) == (None, "zuerst")


# === ZYKLUS 10: Randfaelle ===


def test_ohne_topologie_und_ohne_zeilen_ist_der_baum_leer() -> None:
    baum = baue_baum(kopie(None, status=None, sichtbarkeit=SICHT_NIE_GESEHEN), [])

    assert baum.wurzel.kinder == ()
    assert (baum.fortschritt.fertig, baum.fortschritt.gesamt) == (0, 0)
    assert baum.erster_schritt is None


def test_verfallen_mit_unbekanntem_letzten_stand_raet_keinen() -> None:
    """Ein Code, den diese Fassung nicht kennt (oder ein Endstatus, der dort
    nie hingehoert), wird nicht zu "läuft" umgedeutet und wirft nicht."""
    baum = baue_baum(
        kopie(sig("a"), sichtbarkeit="verfallen", zuletzt_angezeigt="kaputt"), []
    )

    assert baum.status.text == "Zustand verfallen"
