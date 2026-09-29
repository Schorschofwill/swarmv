"""U1/U2 (Workflow-Diagramm) -- Anordnung, Zustaende und Ziele, rein.

Das Modul bekommt einen :class:`Baum` aus ``workflow_baum.py`` und liefert
Geometrie: Knoten mit Kennung, Rechteck, Zustand, Beschriftung und Link,
Linien mit Punkten und Zustand, dazu Breite und Hoehe (KTD1). Keine
Datenbank, keine Vorlage, keine Uhr: ``jetzt`` kommt als Parameter.

Jeder Fall laeuft zusaetzlich durch :func:`pruefe_geometrie`: keine zwei
Rechtecke ueberlappen, jede Linie beginnt am rechten Rand ihres Quell- und
endet am linken Rand ihres Zielknotens, keine Linie schneidet ein fremdes
Rechteck, Breite und Hoehe umschliessen alles, Kennungen sind eindeutig.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Blatt und Startpunkt (U1) ===
1. Ein einzelner Schritt ergibt Startpunkt, einen Knoten und eine Linie.
2. (gestrichen: "Breite und Hoehe umschliessen alle Knoten" prueft
   pruefe_geometrie in jedem Fall; ein eigener Test waere nie rot gewesen)

=== ZYKLUS 2: Kette und Gruppe (U1) ===
3. Eine Kette steht hintereinander, von links nach rechts.
4. Covers AE3. chain(a, group(b, c, d), e): a links, b/c/d gleich weit
   rechts untereinander, e rechts davon, sieben Linien.
5. (gestrichen: "Gruppe als Wurzel" deckt Fall 6 ab, der Kopf ist dort
   die erste Gruppe hinter dem Startpunkt)

=== ZYKLUS 3: Faltung und Chord (U1) ===
6. Covers AE1. Chord mit 47 fertigen und 3 auffaelligen Gliedern: vier
   Knoten im Kopf, oben der Sammelknoten "47 fertig", vier Linien zum Body.
   (Die Beschriftung "47 fertig" kam in Zyklus 9 dazu.)
7. (gestrichen: "ganz fertige Gruppe" -- faellt aus Fall 6 ohne neuen
   Code; Fall 17 nutzt sie fuer den Zustand des Sammelknotens)

=== ZYKLUS 4: Knotenpunkt (U1) ===
8. Eine Gruppe, die auf eine Gruppe folgt, bekommt einen Knotenpunkt statt
   einer Linienmatrix.

=== ZYKLUS 5: Verschachtelung (U1) ===
9. Eine Kette in einer Gruppe in einem Chord rueckt nach rechts, ohne dass
   sich Rechtecke ueberschneiden.
10. chord(group(chain(b, c), d), e): d ist bis an den rechten Rand des
    Kopfs verlaengert, keine Linie schneidet ein fremdes Rechteck.
   (9 und 10 waren sofort gruen -- Reihe und Stapel aus Zyklus 2 tragen
   sie; per Mutation belegt, dass 10 und pruefe_geometrie ohne die
   Verlaengerung bzw. mit ueberlappenden Gliedern rot werden.)

=== ZYKLUS 6: Ohne Topologie und Randfaelle (U1) ===
11. Flacher Baum: die bekannten Schritte als Spalte, ohne Startpunkt und
    ohne erfundene Verbindungen.
12. Flacher Baum ohne jede Zeile: leeres Diagramm mit Rand. (sofort gruen)
13. (sofort gruen) Stabile Kennungen nach KTD4 (dk-<pfad>, dk-start, dk-<gruppe>-fertig,
    dk-<pfad>-ein, dk-kante-<von>-<nach>).
14. Linienpfad als SVG-Pfaddaten: gerade Verlaengerung, dann eine Kurve.
14a. Grosse Koordinaten erscheinen ohne Exponent und ohne Rundungsverlust.

=== ZYKLUS 7: Zeichen, Wort und Klasse (U2) ===
15. Jeder Blattstatus aus workflow_baum.py (auch storniert, dead_letter,
    abgelaufen, abgewiesen) hat Zeichen, Wort und Klasse.
16. Ein unbekannter Status bleibt als Wort stehen und ist neutral.
    (sofort gruen)
17. Der Sammelknoten ist fertig: Zeichen, Wort, Klasse.

=== ZYKLUS 8: Beschriftung (U2) ===
18. Ein langer Task-Name wird gekuerzt, der volle Name bleibt erhalten.
19. Ohne Task-Namen steht die Kennung da.
20. Dauer: beendet (Spanne), laufend (laeuft seit), wartend (wartet seit),
    ohne Zeile keine Angabe -- mit uebergebenem jetzt.

=== ZYKLUS 9: Ziele (U2) ===
21. Ein Schritt-Knoten verweist auf die Task-Ansicht mit kodierter Kennung.
22. Der Sammelknoten verweist auf die Glieder-Seite seiner Gruppe.
23. Ein nicht eingereihter Knoten hat keinen Link und nennt seinen Grund.
24. Ein eingereihter Knoten ohne Zeile verweist auf seine Kennung.
    (sofort gruen)

=== ZYKLUS 10: Warten (U2) ===
25. Der Body eines laufenden Chords zeigt "wartet auf N".
26. Ein Chord, dessen Body eine Kette ist, zeigt "wartet auf N" am ersten
    Schritt der Kette.

=== ZYKLUS 11: Linienzustaende (U2, KTD6) ===
27. Covers AE2. Gescheiterter Chord: gestrichelte Linien zum Body, Grund
    "nie gestartet, Workflow gescheitert" am Body.
28. Linien zu einem laufenden, einem wiederholten und einem eingereihten
    Schritt tragen drei verschiedene Klassen und sind hervorgehoben.
29. Eine Linie zu einem Schritt, der auf Vorgaenger wartet, ist nicht
    gestrichelt; eine zu einem fertigen nicht hervorgehoben. (sofort gruen)
30. Die Linie in einen Knotenpunkt uebernimmt die auffaelligste Klasse der
    Linien aus ihm; gestrichelt nur, wenn alle aus ihm gestrichelt sind.

=== Belastung ===
31. Drei tief verschachtelte Formen bleiben ueberschneidungsfrei.
    (sofort gruen, Absicherung der Stop-Bedingung)
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import pytest

from swarmq_dashboard.workflow_baum import (
    Baum,
    Taskzeile,
    Workflowkopie,
    baue_baum,
)
from swarmq_dashboard.workflow_diagramm import NAME_MAX, Diagramm, Linie, anordnen

WF = "0b6c3c6e-9f36-4c2e-8f7e-3d1f2a4b5c6d"
JETZT = 1000.0


# --- Hilfen wie in test_workflow_baum.py -------------------------------------


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


def baum_aus(topologie: dict[str, Any] | None, zeilen: list[Taskzeile] | None = None,
             **rest: Any) -> Baum:
    return baue_baum(kopie(topologie, **rest), zeilen or [])


def diagramm(baum: Baum) -> Diagramm:
    """Anordnen und die allgemeine Pruefung ueber jeden Fall laufen lassen."""
    d = anordnen(baum, workflow_id=WF, jetzt=JETZT)
    pruefe_geometrie(d)
    return d


# --- Allgemeine Pruefung ------------------------------------------------------


def _innen(px: float, py: float, k: Any) -> bool:
    """Liegt der Punkt echt im Inneren des Rechtecks (Rand zaehlt nicht)?"""
    eps = 0.01
    return (k.x + eps < px < k.x + k.b - eps) and (k.y + eps < py < k.y + k.h - eps)


def _abtasten(punkte: tuple[tuple[float, float], ...]) -> list[tuple[float, float]]:
    """Gerade Stuecke bis zum vorletzten Punkt, dann die Kurve zum letzten."""
    proben: list[tuple[float, float]] = []
    for (x0, y0), (x1, y1) in pairwise(punkte[:-1]):
        proben += [(x0 + (x1 - x0) * i / 50, y0 + (y1 - y0) * i / 50) for i in range(51)]
    (x0, y0), (x3, y3) = punkte[-2], punkte[-1]
    xm = (x0 + x3) / 2
    for i in range(101):
        t = i / 100
        u = 1 - t
        x = u**3 * x0 + 3 * u**2 * t * xm + 3 * u * t**2 * xm + t**3 * x3
        y = u**3 * y0 + 3 * u**2 * t * y0 + 3 * u * t**2 * y3 + t**3 * y3
        proben.append((x, y))
    return proben


def pruefe_geometrie(d: Diagramm) -> None:
    kennungen = [k.kennung for k in d.knoten] + [li.kennung for li in d.linien]
    assert len(kennungen) == len(set(kennungen)), "Kennungen doppelt"
    nach_kennung = {k.kennung: k for k in d.knoten}
    for i, a in enumerate(d.knoten):
        assert a.x >= 0 and a.y >= 0
        assert a.x + a.b <= d.breite and a.y + a.h <= d.hoehe, f"{a.kennung} ragt hinaus"
        for b in d.knoten[i + 1:]:
            ueberlappt = (a.x < b.x + b.b and b.x < a.x + a.b
                          and a.y < b.y + b.h and b.y < a.y + a.h)
            assert not ueberlappt, f"{a.kennung} ueberlappt {b.kennung}"
    for li in d.linien:
        von, nach = nach_kennung[li.von], nach_kennung[li.nach]
        (sx, sy), (ex, ey) = li.punkte[0], li.punkte[-1]
        assert sx == von.x + von.b and von.y <= sy <= von.y + von.h, f"{li.kennung} Start"
        assert ex == nach.x and nach.y <= ey <= nach.y + nach.h, f"{li.kennung} Ende"
        fremde = [k for k in d.knoten if k.kennung not in (li.von, li.nach)]
        for px, py in _abtasten(li.punkte):
            assert 0 <= px <= d.breite and 0 <= py <= d.hoehe
            for k in fremde:
                assert not _innen(px, py, k), f"{li.kennung} schneidet {k.kennung}"


# === ZYKLUS 1: Blatt und Startpunkt (U1) ===


def test_einzelner_schritt_ergibt_startpunkt_knoten_und_linie() -> None:
    d = diagramm(baum_aus(sig("a")))

    assert [k.art for k in d.knoten] == ["start", "schritt"]
    start, a = d.knoten
    assert len(d.linien) == 1
    assert (d.linien[0].von, d.linien[0].nach) == (start.kennung, a.kennung)
    assert start.x + start.b < a.x


def schritte(d: Diagramm) -> dict[str, Any]:
    """Die Knoten eines Diagramms nach Kennung, ohne Startpunkt."""
    return {k.kennung: k for k in d.knoten if k.art != "start"}


def paare(d: Diagramm) -> set[tuple[str, str]]:
    return {(li.von, li.nach) for li in d.linien}


def mitte_y(k: Any) -> float:
    return float(k.y + k.h / 2)


# === ZYKLUS 2: Kette und Gruppe (U1) ===


def test_kette_steht_hintereinander_von_links_nach_rechts() -> None:
    d = diagramm(baum_aus(kette(sig("a"), sig("b"), sig("c"))))

    k = schritte(d)
    a, b, c = k["dk-0"], k["dk-1"], k["dk-2"]
    assert a.x + a.b < b.x and b.x + b.b < c.x
    assert mitte_y(a) == mitte_y(b) == mitte_y(c)
    assert paare(d) == {("dk-start", "dk-0"), ("dk-0", "dk-1"), ("dk-1", "dk-2")}


def test_kette_mit_gruppe_faechert_auf_und_fuehrt_zusammen() -> None:
    """Covers AE3."""
    d = diagramm(baum_aus(kette(sig("a"), gruppe(sig("b"), sig("c"), sig("d")), sig("e"))))

    k = schritte(d)
    a, b, c, dd, e = (k[n] for n in ("dk-0", "dk-1-g0", "dk-1-g1", "dk-1-g2", "dk-2"))
    assert a.x + a.b < b.x
    assert b.x == c.x == dd.x
    assert b.y + b.h < c.y and c.y + c.h < dd.y
    assert dd.x + dd.b < e.x
    assert len(d.linien) == 7
    assert paare(d) == {
        ("dk-start", "dk-0"),
        ("dk-0", "dk-1-g0"), ("dk-0", "dk-1-g1"), ("dk-0", "dk-1-g2"),
        ("dk-1-g0", "dk-2"), ("dk-1-g1", "dk-2"), ("dk-1-g2", "dk-2"),
    }


# === ZYKLUS 3: Faltung und Chord (U1) ===


def test_chord_faltet_fertige_glieder_in_einen_sammelknoten() -> None:
    """Covers AE1."""
    topologie = chord(gruppe(*[sig(f"bericht-{i}") for i in range(50)]), sig("zusammenfassen"))
    statusse = ["fertig"] * 47 + ["laufend", "laufend", "wiederholung"]
    zeilen = [zeile(f"/h/g{i}", s) for i, s in enumerate(statusse)]

    d = diagramm(baum_aus(topologie, zeilen))

    kopf = [k for k in d.knoten if k.kennung.startswith("dk-h")]
    assert [k.kennung for k in kopf] == ["dk-h-fertig", "dk-h-g47", "dk-h-g48", "dk-h-g49"]
    assert kopf[0].art == "sammel"
    assert (kopf[0].titel, kopf[0].kurzname) == ("47 fertig", "47 fertig")
    assert all(a.y + a.h < b.y for a, b in pairwise(kopf))
    assert {li.von for li in d.linien if li.nach == "dk-b"} == {k.kennung for k in kopf}


# === ZYKLUS 4: Knotenpunkt (U1) ===


def test_gruppe_nach_gruppe_bekommt_einen_knotenpunkt() -> None:
    d = diagramm(baum_aus(kette(gruppe(sig("a"), sig("b")), gruppe(sig("c"), sig("d"), sig("e")))))

    punkt = next(k for k in d.knoten if k.art == "punkt")
    assert punkt.kennung == "dk-1-ein"
    k = schritte(d)
    assert k["dk-0-g0"].x + k["dk-0-g0"].b < punkt.x
    assert punkt.x + punkt.b < k["dk-1-g0"].x
    assert paare(d) == {
        ("dk-start", "dk-0-g0"), ("dk-start", "dk-0-g1"),
        ("dk-0-g0", "dk-1-ein"), ("dk-0-g1", "dk-1-ein"),
        ("dk-1-ein", "dk-1-g0"), ("dk-1-ein", "dk-1-g1"), ("dk-1-ein", "dk-1-g2"),
    }


# === ZYKLUS 5: Verschachtelung (U1) ===


def test_kette_in_gruppe_in_chord_rueckt_nach_rechts() -> None:
    d = diagramm(baum_aus(chord(gruppe(kette(sig("a"), sig("b")), sig("c")), sig("d"))))

    k = schritte(d)
    a, b, c, body = k["dk-h-g0-0"], k["dk-h-g0-1"], k["dk-h-g1"], k["dk-b"]
    assert a.x == c.x
    assert a.x + a.b < b.x
    assert b.x + b.b < body.x
    assert paare(d) >= {("dk-h-g0-0", "dk-h-g0-1"), ("dk-h-g0-1", "dk-b"), ("dk-h-g1", "dk-b")}


def test_kuerzeres_glied_wird_bis_an_den_rand_des_kopfs_verlaengert() -> None:
    d = diagramm(baum_aus(chord(gruppe(kette(sig("b"), sig("c")), sig("d")), sig("e"))))

    k = schritte(d)
    c, dd = k["dk-h-g0-1"], k["dk-h-g1"]
    linie = next(li for li in d.linien if li.von == "dk-h-g1")
    assert linie.punkte[0] == (dd.x + dd.b, mitte_y(dd))
    assert linie.punkte[1] == (c.x + c.b, mitte_y(dd))
    # Drei Glieder: ohne Verlaengerung liefe die Kurve von d quer durch y.
    diagramm(baum_aus(chord(gruppe(kette(sig("b"), sig("c")), kette(sig("x"), sig("y")),
                                   sig("d")), sig("e"))))


# === ZYKLUS 6: Ohne Topologie und Randfaelle (U1) ===


def test_flacher_baum_ist_eine_spalte_ohne_erfundene_verbindungen() -> None:
    zeilen = [zeile("/0", "fertig", first_seen_at=1.0),
              zeile(None, "laufend", task_id="fremd", first_seen_at=2.0)]

    d = diagramm(baum_aus(None, zeilen, status=None, sichtbarkeit="nie_gesehen"))

    assert [k.kennung for k in d.knoten] == ["dk-0", "dk-task-fremd"]
    assert d.linien == ()
    a, b = d.knoten
    assert a.x == b.x and a.y + a.h < b.y


def test_flacher_baum_ohne_zeilen_ist_leer_mit_rand() -> None:
    """Sofort gruen: faellt aus Fall 11 (Randfall, bewusst festgehalten)."""
    d = diagramm(baum_aus(None, [], status=None, sichtbarkeit="nie_gesehen"))

    assert (d.knoten, d.linien) == ((), ())
    assert d.breite > 0 and d.hoehe > 0


def test_kennungen_sind_stabil_nach_ktd4() -> None:
    """Sofort gruen: festgehalten, weil idiomorph an genau diesen Namen
    abgleicht (R9) -- eine Umbenennung waere ein Bruch fuer U3."""
    d = diagramm(baum_aus(kette(sig("a"), sig("b"))))
    assert {li.kennung for li in d.linien} == {"dk-kante-start-0", "dk-kante-0-1"}

    einfach = diagramm(baum_aus(sig("a")))
    assert [k.kennung for k in einfach.knoten] == ["dk-start", "dk"]
    assert [li.kennung for li in einfach.linien] == ["dk-kante-start-dk"]


def test_linienpfad_ist_gerade_verlaengerung_dann_kurve() -> None:
    d = diagramm(baum_aus(chord(gruppe(kette(sig("b"), sig("c")), sig("d")), sig("e"))))

    linie = next(li for li in d.linien if li.von == "dk-h-g1")
    (x0, y0), (x1, _), (x3, y3) = linie.punkte
    xm = (x1 + x3) / 2
    assert linie.pfad == f"M {x0:g} {y0:g} L {x1:g} {y0:g} C {xm:g} {y0:g} {xm:g} {y3:g} {x3:g} {y3:g}"

    einfach = diagramm(baum_aus(sig("a"))).linien[0]
    (a0, b0), (a3, b3) = einfach.punkte
    am = (a0 + a3) / 2
    assert einfach.pfad == f"M {a0:g} {b0:g} C {am:g} {b0:g} {am:g} {b3:g} {a3:g} {b3:g}"


def test_linienpfad_schreibt_grosse_koordinaten_ohne_exponent() -> None:
    """Eine Kette aus einigen tausend Schritten ist ueber eine Million breit;
    ``1.23457e+06`` waere fuer SVG ein anderer Punkt."""
    linie = Linie("dk-kante-a-b", "dk-a", "dk-b", ((1234567.25, 10.5), (1234623.25, 20.0)))

    assert linie.pfad == "M 1234567.25 10.5 C 1234595.25 10.5 1234595.25 20 1234623.25 20"


# === ZYKLUS 7: Zeichen, Wort und Klasse (U2) ===


def blatt(status: str, **rest: Any) -> Any:
    """Der eine Schritt-Knoten eines einfachen Workflows mit *status*."""
    if status == "eingereiht":
        baum = baum_aus(sig("a"), step_tasks={"": f"{WF}#"})
    elif status == "nicht_eingereiht":
        baum = baum_aus(sig("a"), **rest)
    else:
        baum = baum_aus(sig("a"), [zeile("", status, task_name="a")], **rest)
    return next(k for k in diagramm(baum).knoten if k.art == "schritt")


@pytest.mark.parametrize(("status", "zeichen", "wort", "klasse"), [
    ("fertig", "✓", "fertig", "is-ok"),
    ("laufend", "▶", "läuft", "is-run"),
    ("wartend", "○", "wartet", "is-idle"),
    ("wiederholung", "↻", "Wiederholung", "is-wait"),
    ("storniert", "○", "storniert", "is-idle"),
    ("gescheitert", "✗", "gescheitert", "is-fail"),
    ("dead_letter", "✗", "Dead Letter", "is-fail"),
    ("abgelaufen", "✗", "abgelaufen", "is-fail"),
    ("abgewiesen", "✗", "abgewiesen", "is-fail"),
    ("eingereiht", "○", "eingereiht", "is-idle"),
    # Ein nicht eingereihter Schritt nimmt die Klasse seines Grunds, wie
    # in der Liste: hier "wartet auf Vorgänger".
    ("nicht_eingereiht", "↻", "nicht eingereiht", "is-wait"),
])
def test_jeder_blattstatus_hat_zeichen_wort_und_klasse(
    status: str, zeichen: str, wort: str, klasse: str
) -> None:
    k = blatt(status)

    assert (k.zeichen, k.wort, k.klasse) == (zeichen, wort, klasse)


def test_unbekannter_status_bleibt_als_wort_und_ist_neutral() -> None:
    """Sofort gruen: folgt aus zustandsart() und dem Wort-Rueckfall."""
    k = blatt("verschollen")

    assert (k.zeichen, k.wort, k.klasse) == ("○", "verschollen", "is-idle")


def test_sammelknoten_ist_fertig() -> None:
    """Auch eine ganz fertige Gruppe steht als ein Sammelknoten (Fall 7)."""
    d = diagramm(baum_aus(gruppe(sig("a"), sig("b")),
                          [zeile("/g0", "fertig"), zeile("/g1", "fertig")]))

    (sammel,) = (k for k in d.knoten if k.art != "start")
    assert (sammel.kennung, sammel.art) == ("dk-fertig", "sammel")
    assert (sammel.zeichen, sammel.wort, sammel.klasse) == ("✓", "fertig", "is-ok")


# === ZYKLUS 8: Beschriftung (U2) ===


def test_langer_task_name_wird_gekuerzt_der_volle_bleibt() -> None:
    lang = "berichte.erzeugen_und_versenden_an_alle_kunden"
    d = diagramm(baum_aus(kette(sig(lang), sig("kurz"))))

    k = schritte(d)
    assert k["dk-0"].titel == lang
    assert k["dk-0"].kurzname == lang[:NAME_MAX - 1] + "…"
    assert len(k["dk-0"].kurzname) == NAME_MAX
    assert (k["dk-1"].titel, k["dk-1"].kurzname) == ("kurz", "kurz")


def test_ohne_task_namen_steht_die_kennung_und_ohne_beides_ein_strich() -> None:
    """Wie ``k.task_name or k.task_id`` und ``or '--'`` in _baum.html."""
    zeilen = [zeile(None, "laufend", task_id="nur-kennung", first_seen_at=1.0)]
    (k,) = diagramm(baum_aus(None, zeilen, status=None, sichtbarkeit="nie_gesehen")).knoten
    assert (k.titel, k.kurzname) == ("nur-kennung", "nur-kennung")

    ohne = diagramm(baum_aus({"kind": "signature", "args": []}))
    (leer,) = (k for k in ohne.knoten if k.art == "schritt")
    assert (leer.titel, leer.kurzname) == ("--", "--")


@pytest.mark.parametrize(("felder", "text"), [
    ({"started_at": 10.0, "ended_at": 12.5}, "2,5 s"),
    ({"ended_at": 12.5}, "--"),
    ({"started_at": JETZT - 60}, "läuft seit 1 min 00 s"),
    ({"first_seen_at": JETZT - 5}, "wartet seit 5,0 s"),
])
def test_dauer_oder_wartezeit_wie_in_der_liste(felder: dict[str, float], text: str) -> None:
    d = diagramm(baum_aus(sig("a"), [zeile("", "laufend", **felder)]))

    (k,) = (k for k in d.knoten if k.art == "schritt")
    assert k.dauer_text == text


def test_ohne_zeile_keine_dauer() -> None:
    assert blatt("eingereiht").dauer_text is None


# === ZYKLUS 9: Ziele (U2) ===


def test_schritt_verweist_auf_die_task_ansicht_mit_kodierter_kennung() -> None:
    """Die Route ist ``/tasks/{kennung:path}``: "/" bleibt, "#" nicht."""
    d = diagramm(baum_aus(kette(sig("a"), sig("b")), [zeile("/0", "laufend")]))

    assert schritte(d)["dk-0"].link == f"/tasks/{WF}%23/0"


def test_sammelknoten_verweist_auf_die_glieder_seite_seiner_gruppe() -> None:
    """Pfad kodiert wie ``|urlencode`` in _baum.html; die Wurzelgruppe hat ""."""
    topologie = chord(gruppe(sig("a"), sig("b")), sig("c"))
    d = diagramm(baum_aus(topologie, [zeile("/h/g0", "fertig"), zeile("/h/g1", "laufend")]))
    assert schritte(d)["dk-h-fertig"].link == f"/workflows/{WF}/glieder?gruppe=/h"

    wurzel = diagramm(baum_aus(gruppe(sig("a")), [zeile("/g0", "fertig")]))
    assert schritte(wurzel)["dk-fertig"].link == f"/workflows/{WF}/glieder?gruppe="


def test_nicht_eingereihter_knoten_hat_keinen_link_und_nennt_seinen_grund() -> None:
    d = diagramm(baum_aus(kette(sig("a"), sig("b")), [zeile("/0", "laufend")]))

    b = schritte(d)["dk-1"]
    assert b.link is None
    assert b.grund == "wartet auf Vorgänger"
    assert schritte(d)["dk-0"].grund is None


def test_eingereihter_knoten_ohne_zeile_verweist_auf_seine_kennung() -> None:
    """Sofort gruen: die Kennung aus step_tasks ist schon task_id des Blatts."""
    assert blatt("eingereiht").link == f"/tasks/{WF}%23"


# === ZYKLUS 10: Warten (U2) ===


def test_body_eines_laufenden_chords_wartet_auf_n() -> None:
    zeilen = [zeile("/h/g0", "fertig"), zeile("/h/g1", "laufend"), zeile("/h/g2", "wiederholung")]
    d = diagramm(baum_aus(chord(gruppe(sig("x"), sig("y"), sig("z")), sig("body")), zeilen))

    k = schritte(d)
    assert k["dk-b"].wartet_auf == 2
    assert k["dk-h-g1"].wartet_auf is None


def test_body_als_kette_zeigt_wartet_auf_n_am_ersten_schritt() -> None:
    zeilen = [zeile("/h/g0", "laufend")]
    d = diagramm(baum_aus(chord(gruppe(sig("x")), kette(sig("p"), sig("q"))), zeilen))

    k = schritte(d)
    assert (k["dk-b-0"].wartet_auf, k["dk-b-1"].wartet_auf) == (1, None)

    als_gruppe = diagramm(baum_aus(chord(gruppe(sig("x")), gruppe(sig("p"), sig("q"))), zeilen))
    k = schritte(als_gruppe)
    assert (k["dk-b-g0"].wartet_auf, k["dk-b-g1"].wartet_auf) == (1, 1)


# === ZYKLUS 11: Linienzustaende (U2, KTD6) ===


def linien_nach(d: Diagramm, kennung: str) -> list[Any]:
    return [li for li in d.linien if li.nach == kennung]


def test_gescheiterter_chord_strichelt_die_linien_zum_body() -> None:
    """Covers AE2."""
    zeilen = [zeile(f"/h/g{i}", "fertig") for i in range(3)]
    zeilen.append(zeile("/h/g3", "gescheitert", ended_at=5.0))
    topologie = chord(gruppe(*[sig("laden") for _ in range(4)]), sig("zusammenfassen"))

    d = diagramm(baum_aus(topologie, zeilen, status="failed"))

    assert schritte(d)["dk-b"].grund == "nie gestartet, Workflow gescheitert"
    zum_body = linien_nach(d, "dk-b")
    assert {li.von for li in zum_body} == {"dk-h-fertig", "dk-h-g3"}
    assert all(li.gestrichelt for li in zum_body)
    assert not any(li.gestrichelt for li in linien_nach(d, "dk-h-g3"))


def test_linien_zu_laufend_wiederholung_und_eingereiht_sind_unterscheidbar() -> None:
    zeilen = [zeile("/0", "fertig"), zeile("/1/g0", "laufend"), zeile("/1/g1", "wiederholung")]
    d = diagramm(baum_aus(kette(sig("a"), gruppe(sig("b"), sig("c"), sig("e"))), zeilen,
                          step_tasks={"/1/g2": f"{WF}#/1/g2"}))

    (lauf,), (wdh,), (ein,) = (linien_nach(d, n) for n in ("dk-1-g0", "dk-1-g1", "dk-1-g2"))
    assert (lauf.klasse, wdh.klasse, ein.klasse) == ("is-run", "is-wait", "is-idle")
    assert lauf.hervorgehoben and wdh.hervorgehoben and ein.hervorgehoben


def test_warten_ist_nicht_gestrichelt_und_fertig_nicht_hervorgehoben() -> None:
    """Sofort gruen: folgt aus dem Nachlauf von Fall 27/28."""
    d = diagramm(baum_aus(kette(sig("a"), sig("b"), sig("c")),
                          [zeile("/0", "fertig"), zeile("/1", "laufend")]))

    (zu_a,), (zu_c,) = linien_nach(d, "dk-0"), linien_nach(d, "dk-2")
    assert (zu_a.klasse, zu_a.hervorgehoben, zu_a.gestrichelt) == ("is-ok", False, False)
    assert (zu_c.klasse, zu_c.gestrichelt) == ("is-wait", False)


def test_linie_in_den_knotenpunkt_folgt_den_linien_aus_ihm() -> None:
    topologie = kette(gruppe(sig("a"), sig("b")), gruppe(sig("c"), sig("d")))
    kopf = [zeile("/0/g0", "fertig"), zeile("/0/g1", "gescheitert")]

    gemischt = diagramm(baum_aus(topologie, [*kopf, zeile("/1/g0", "gescheitert")],
                                 status="failed"))
    rein = linien_nach(gemischt, "dk-1-ein")
    assert {(li.klasse, li.gestrichelt) for li in rein} == {("is-fail", False)}
    assert schritte(gemischt)["dk-1-ein"].klasse == "is-fail"

    nie = diagramm(baum_aus(topologie, kopf, status="failed"))
    rein = linien_nach(nie, "dk-1-ein")
    assert {(li.klasse, li.gestrichelt) for li in rein} == {("is-idle", True)}

    laufend = diagramm(baum_aus(topologie, [*kopf[:1], zeile("/1/g0", "laufend"),
                                            zeile("/1/g1", "wiederholung")]))
    rein = linien_nach(laufend, "dk-1-ein")
    assert {(li.klasse, li.hervorgehoben) for li in rein} == {("is-wait", True)}


# === Belastung der allgemeinen Pruefung ===


@pytest.mark.parametrize("topologie", [
    kette(gruppe(kette(gruppe(sig("a"), sig("b")), sig("c")), sig("d")),
          chord(gruppe(sig("e"), kette(sig("f"), sig("g"), sig("h"))), gruppe(sig("i"), sig("j")))),
    chord(gruppe(chord(gruppe(sig("a"), sig("b")), sig("c")), sig("d"), kette(sig("e"), sig("f"))),
          kette(gruppe(sig("g"), sig("h")), gruppe(sig("i"), sig("j"), sig("k")), sig("l"))),
    gruppe(kette(sig("a"), gruppe(sig("b"), sig("c"), sig("d"))), sig("e"),
           kette(gruppe(sig("f"), sig("g")), gruppe(sig("h"), sig("i")))),
])
def test_verschachtelte_formen_bleiben_ueberschneidungsfrei(topologie: dict[str, Any]) -> None:
    """Sofort gruen: Absicherung der Stop-Bedingung aus der Goal Capsule --
    keine Form darf Rechtecke ueberlappen oder von Linien schneiden lassen."""
    d = diagramm(baum_aus(topologie))

    assert len([k for k in d.knoten if k.art == "schritt"]) >= 9
