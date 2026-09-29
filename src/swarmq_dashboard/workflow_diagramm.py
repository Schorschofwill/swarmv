"""Geometrie des Workflow-Diagramms (KTD1, KTD2) -- rein, ohne I/O.

Eingabe ist ein :class:`~swarmq_dashboard.workflow_baum.Baum`; Zustand,
Zaehler, Gruende und Faltung kommen unveraendert von dort. Dieses Modul
rechnet nur Positionen: Knoten als Rechtecke, Linien als Punktfolgen, dazu
Breite und Hoehe. Die Vorlage zeichnet daraus SVG, ohne selbst zu rechnen.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import pairwise
from urllib.parse import quote

from swarmq_dashboard.web.templates import dauer, zahl, zustandsart
from swarmq_dashboard.workflow_baum import (
    EINGEREIHT,
    GRUND_GESCHEITERT,
    GRUND_STORNIERT,
    GRUND_STORNO_GLIED,
    NICHT_EINGEREIHT,
    Baum,
    Taskzeile,
)
from swarmq_dashboard.workflow_baum import Knoten as Baumknoten

#: Rand um das ganze Diagramm.
RAND = 16.0
#: Waagerechter Abstand zwischen zwei Bloecken; darin verlaufen die Kurven.
ABSTAND_X = 56.0
#: Senkrechter Abstand zwischen den Gliedern einer Gruppe.
ABSTAND_Y = 16.0
#: Kasten eines Schritts. Breit genug fuer den laengsten Grund in der
#: zweiten Zeile ("wartet auf storniertes Glied, startet nie", 11 px Mono):
#: abgeschnitten saehe "startet nie" wie ein Warten aus.
KASTEN_B = 300.0
KASTEN_H = 40.0
#: Zeichen des Namens, die in einen Kasten passen; laenger wird gekuerzt,
#: der volle Name steht im Titel des Knotens (KTD3).
NAME_MAX = 22
#: Startpunkt links vor dem ersten Block.
START_B = 12.0
#: Knotenpunkt zwischen zwei Bloecken mit je mehreren Anschluessen.
PUNKT_B = 8.0


#: Zustandsklasse -> Zeichen (KTD3). Das Zeichen folgt der Klasse, damit
#: Farbe und Zeichen nie auseinanderlaufen; das Wort daneben unterscheidet
#: die Status innerhalb einer Klasse (R1).
_ZEICHEN = {
    "is-ok": "✓",
    "is-run": "▶",
    "is-wait": "↻",
    "is-fail": "✗",
    "is-idle": "○",
}

#: Blattstatus -> Wort. Ein Status, den diese Fassung nicht kennt, bleibt
#: als Code stehen -- ein leeres Feld hiesse "kein Zustand".
_WORT = {
    "fertig": "fertig",
    "laufend": "läuft",
    "wartend": "wartet",
    "wiederholung": "Wiederholung",
    "storniert": "storniert",
    "gescheitert": "gescheitert",
    "dead_letter": "Dead Letter",
    "abgelaufen": "abgelaufen",
    "abgewiesen": "abgewiesen",
    "eingereiht": "eingereiht",
    NICHT_EINGEREIHT: "nicht eingereiht",
}


#: Gruende, mit denen ein Schritt nie starten wird: "nie gestartet ..."
#: und "... startet nie" (KTD6).
_STARTET_NIE = frozenset({GRUND_GESCHEITERT, GRUND_STORNIERT, GRUND_STORNO_GLIED})

#: Blattstatus, die als aktiv gelten -- dieselben wie fuer "nichts mehr
#: aktiv" in ``workflow_baum.py``.
_AKTIV = frozenset({"wartend", "laufend", "wiederholung", EINGEREIHT})


def _zahl(wert: float) -> str:
    """Eine Koordinate fuer SVG: hoechstens zwei Nachkommastellen, nie mit
    Exponent -- ``:g`` machte aus 1234567 ein ``1.23457e+06``."""
    return f"{wert:.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Knoten:
    """Ein Rechteck des Diagramms."""

    kennung: str
    x: float
    y: float
    b: float
    h: float
    #: schritt, sammel, start oder punkt.
    art: str
    #: Zustandsart der Gestaltung (``is-ok``, ``is-run`` ...).
    klasse: str = "is-idle"
    #: Zustandszeichen und -wort: Farbe traegt den Zustand nie allein (R1).
    zeichen: str = ""
    wort: str = ""
    #: Der volle Name -- fuer ``<title>`` und ``aria-label``.
    titel: str = ""
    #: Der Name, wie er in den Kasten passt.
    kurzname: str = ""
    #: Dauer oder Wartezeit wie in der Liste; None ohne Task-Zeile.
    dauer_text: str | None = None
    #: Ziel des Knotens; None heisst: kein Link (R5).
    link: str | None = None
    #: Warum ein Schritt nicht eingereiht ist (R11 der Workflow-Ansicht).
    grund: str | None = None
    #: Auf wie viele Kopf-Glieder der Schritt noch wartet (R10 dort).
    wartet_auf: int | None = None
    #: Laeuft, wartet oder ist eingereiht -- Linien dorthin sind
    #: hervorgehoben. Eigenes Feld, weil "eingereiht" und "storniert"
    #: dieselbe Klasse haben.
    aktiv: bool = False


@dataclass(frozen=True)
class Linie:
    """Eine Verbindung vom rechten Rand von *von* zum linken Rand von *nach*."""

    kennung: str
    von: str
    nach: str
    #: Vom rechten Rand der Quelle waagerecht bis zum vorletzten Punkt, von
    #: dort als Kurve zum letzten, dem linken Rand des Ziels.
    punkte: tuple[tuple[float, float], ...]
    #: Zustandsart des Ziels (KTD6).
    klasse: str = "is-idle"
    #: Das Ziel wird nie starten (KTD6).
    gestrichelt: bool = False
    #: Das Ziel laeuft, wartet oder ist eingereiht (U2).
    hervorgehoben: bool = False

    @property
    def pfad(self) -> str:
        """Die Pfaddaten fuer SVG: Geraden, dann eine kubische Kurve, deren
        Stuetzpunkte waagerecht in der Mitte des Abstands liegen."""
        (x0, y0), *mitte, (x3, y3) = self.punkte
        teile = [f"M {_zahl(x0)} {_zahl(y0)}"]
        teile += [f"L {_zahl(x)} {_zahl(y)}" for x, y in mitte]
        xa, ya = mitte[-1] if mitte else (x0, y0)
        xm = _zahl((xa + x3) / 2)
        teile.append(f"C {xm} {_zahl(ya)} {xm} {_zahl(y3)} {_zahl(x3)} {_zahl(y3)}")
        return " ".join(teile)


@dataclass(frozen=True)
class Diagramm:
    """Alles, was die Vorlage zeichnet."""

    knoten: tuple[Knoten, ...]
    linien: tuple[Linie, ...]
    breite: float
    hoehe: float


@dataclass(frozen=True)
class _Anschluss:
    """Ein Ein- oder Ausgang eines Blocks.

    Beim Eingang ist ``x`` der linke Rand des Knotens, beim Ausgang sein
    rechter Rand -- dort beginnt die Linie. Der Ausgang liegt trotzdem am
    rechten Rand des Blocks: bis dahin laeuft die Linie waagerecht (KTD2).
    """

    kennung: str
    x: float
    y: float


@dataclass(frozen=True)
class _Block:
    """Ein Element der Topologie mit Groesse, Inhalt und Anschluessen.

    Gebaut wird jeder Block links oben bei 0, 0; :meth:`verschoben` setzt
    ihn samt Inhalt an seine Stelle.
    """

    b: float
    h: float
    #: Kennung des Topologie-Elements, fuer den Knotenpunkt davor (KTD4).
    kennung: str = ""
    knoten: tuple[Knoten, ...] = ()
    linien: tuple[Linie, ...] = ()
    eingaenge: tuple[_Anschluss, ...] = ()
    ausgaenge: tuple[_Anschluss, ...] = ()
    x: float = 0.0
    y: float = 0.0

    def verschoben(self, dx: float, dy: float) -> _Block:
        def punkt(p: tuple[float, float]) -> tuple[float, float]:
            return (p[0] + dx, p[1] + dy)

        return _Block(
            b=self.b, h=self.h, kennung=self.kennung, x=self.x + dx, y=self.y + dy,
            knoten=tuple(replace(k, x=k.x + dx, y=k.y + dy) for k in self.knoten),
            linien=tuple(replace(li, punkte=tuple(map(punkt, li.punkte)))
                         for li in self.linien),
            eingaenge=tuple(replace(a, x=a.x + dx, y=a.y + dy) for a in self.eingaenge),
            ausgaenge=tuple(replace(a, x=a.x + dx, y=a.y + dy) for a in self.ausgaenge),
        )


@dataclass(frozen=True)
class _Umfeld:
    """Was die Rekursion ausser dem Baum braucht."""

    workflow_id: str
    #: Die Uhr kommt von aussen: das Modul bleibt rein und testbar.
    jetzt: float


def _kennung(k: Baumknoten) -> str:
    """Stabile Kennung aus dem Topologie-Pfad (KTD4), wie ``knoten-...`` in
    ``_baum.html``; ein Schritt ohne bekannten Pfad heisst nach seiner Task."""
    if k.pfad is None:
        return f"dk-task-{k.task_id}"
    return "dk" + k.pfad.replace("/", "-")


def _name(kennung: str) -> str:
    """Der Teil einer Knotenkennung, der in eine Linienkennung eingeht."""
    return kennung.removeprefix("dk-")


def _linie(von: _Anschluss, rand_x: float, nach: _Anschluss) -> Linie:
    """Vom Ausgang *von* waagerecht bis *rand_x*, dann zum Eingang *nach*."""
    punkte = [(von.x, von.y)]
    if rand_x != von.x:
        punkte.append((rand_x, von.y))
    punkte.append((nach.x, nach.y))
    return Linie(
        f"dk-kante-{_name(von.kennung)}-{_name(nach.kennung)}",
        von.kennung, nach.kennung, tuple(punkte),
    )


def _kurz(name: str) -> str:
    if len(name) <= NAME_MAX:
        return name
    return name[:NAME_MAX - 1] + "…"


def _dauer_text(z: Taskzeile | None, jetzt: float) -> str | None:
    """Die Regel der Dauer-Spalte aus ``_baum.html``."""
    if z is None:
        return None
    if z.ended_at is not None:
        return dauer(z.ended_at - z.started_at) if z.started_at is not None else "--"
    if z.started_at is not None:
        return f"läuft seit {dauer(jetzt - z.started_at)}"
    return f"wartet seit {dauer(jetzt - z.first_seen_at)}"


def _blatt(k: Baumknoten, umfeld: _Umfeld) -> _Block:
    titel = k.task_name or k.task_id or "--"
    status = k.status or NICHT_EINGEREIHT
    # Nicht eingereiht nimmt die Klasse seines Grunds, wie in der Liste:
    # Warten ist Warten, "nie gestartet" ist neutral.
    klasse = zustandsart(k.grund if status == NICHT_EINGEREIHT else status)
    return _kasten(Knoten(
        _kennung(k), 0.0, 0.0, KASTEN_B, KASTEN_H, "schritt",
        klasse=klasse, zeichen=_ZEICHEN[klasse], wort=_WORT.get(status, status),
        titel=titel, kurzname=_kurz(titel), dauer_text=_dauer_text(k.zeile, umfeld.jetzt),
        link=f"/tasks/{quote(k.task_id, safe='/')}" if k.task_id else None,
        grund=k.grund, wartet_auf=k.wartet_auf, aktiv=status in _AKTIV,
    ))


def _sammel(gruppe: Baumknoten, umfeld: _Umfeld) -> _Block:
    """Die fertigen Glieder einer Gruppe als ein Knoten (R4), mit Verweis
    auf deren Seite -- kodiert wie ``|urlencode`` in ``_baum.html``."""
    link = (f"/workflows/{quote(umfeld.workflow_id, safe='/')}/glieder"
            f"?gruppe={quote(gruppe.pfad or '', safe='/')}")
    titel = f"{zahl(len(gruppe.gefaltete_kinder))} fertig"
    return _kasten(Knoten(
        _kennung(gruppe) + "-fertig", 0.0, 0.0, KASTEN_B, KASTEN_H, "sammel",
        klasse="is-ok", zeichen=_ZEICHEN["is-ok"], wort=_WORT["fertig"], link=link,
        titel=titel, kurzname=_kurz(titel),
    ))


def _kasten(knoten: Knoten) -> _Block:
    mitte = knoten.h / 2
    return _Block(
        b=knoten.b, h=knoten.h, kennung=knoten.kennung, knoten=(knoten,),
        eingaenge=(_Anschluss(knoten.kennung, 0.0, mitte),),
        ausgaenge=(_Anschluss(knoten.kennung, knoten.b, mitte),),
    )


def _verbinden(links: _Block, rechts: _Block, rand_x: float) -> tuple[Linie, ...]:
    """Die Linien zwischen zwei aufeinanderfolgenden Bloecken (KTD2)."""
    return tuple(
        _linie(aus, rand_x, ein) for aus in links.ausgaenge for ein in rechts.eingaenge
    )


def _punkt(vor: _Block) -> _Block:
    """Der Knotenpunkt vor *vor* (KTD2): ohne ihn entstuende eine
    Linienmatrix aus jedem Ausgang in jeden Eingang."""
    return _kasten(Knoten(vor.kennung + "-ein", 0.0, 0.0, PUNKT_B, PUNKT_B, "punkt"))


def _mit_knotenpunkten(bloecke: list[_Block]) -> list[_Block]:
    ergebnis: list[_Block] = []
    for block in bloecke:
        if ergebnis and len(ergebnis[-1].ausgaenge) > 1 and len(block.eingaenge) > 1:
            ergebnis.append(_punkt(block))
        ergebnis.append(block)
    return ergebnis


def _reihe(bloecke: list[_Block], kennung: str = "") -> _Block:
    """Bloecke nebeneinander, senkrecht mittig, jeweils verbunden."""
    bloecke = _mit_knotenpunkten(bloecke)
    h = max((b.h for b in bloecke), default=0.0)
    gesetzt: list[_Block] = []
    x = 0.0
    for block in bloecke:
        if gesetzt:
            x += ABSTAND_X
        gesetzt.append(block.verschoben(x, (h - block.h) / 2))
        x += block.b
    knoten = tuple(k for b in gesetzt for k in b.knoten)
    linien = [li for b in gesetzt for li in b.linien]
    for links, rechts in pairwise(gesetzt):
        linien += _verbinden(links, rechts, links.x + links.b)
    return _Block(
        b=x, h=h, kennung=kennung, knoten=knoten, linien=tuple(linien),
        eingaenge=gesetzt[0].eingaenge if gesetzt else (),
        ausgaenge=gesetzt[-1].ausgaenge if gesetzt else (),
    )


def _stapel(bloecke: list[_Block], kennung: str) -> _Block:
    """Bloecke untereinander, linksbuendig. Die Ausgaenge liegen alle am
    rechten Rand des Stapels; ein kuerzeres Glied reicht seine Linie
    waagerecht bis dorthin (KTD2)."""
    b = max((block.b for block in bloecke), default=0.0)
    gesetzt: list[_Block] = []
    y = 0.0
    for block in bloecke:
        if gesetzt:
            y += ABSTAND_Y
        gesetzt.append(block.verschoben(0.0, y))
        y += block.h
    return _Block(
        b=b, h=y, kennung=kennung,
        knoten=tuple(k for g in gesetzt for k in g.knoten),
        linien=tuple(li for g in gesetzt for li in g.linien),
        eingaenge=tuple(a for g in gesetzt for a in g.eingaenge),
        ausgaenge=tuple(a for g in gesetzt for a in g.ausgaenge),
    )


def _block(k: Baumknoten, umfeld: _Umfeld) -> _Block:
    if k.art == "schritt":
        return _blatt(k, umfeld)
    if k.art == "gruppe":
        # Der Sammelknoten oben, darunter die auffaelligen Glieder einzeln
        # in ihrer Reihenfolge (KTD2).
        oben = [_sammel(k, umfeld)] if k.gefaltete_kinder else []
        block = _stapel(oben + [_block(kind, umfeld) for kind in k.offene_kinder], _kennung(k))
    else:
        block = _reihe([_block(kind, umfeld) for kind in k.kinder], _kennung(k))
    if k.wartet_auf is not None:
        block = _wartend(block, k.wartet_auf)
    return block


def _wartend(block: _Block, anzahl: int) -> _Block:
    """Ein innerer Knoten hat keinen Kasten: sein "wartet auf N" zeigen die
    Knoten, in die der Block zuerst fuehrt (U2)."""
    eingang = {a.kennung for a in block.eingaenge}
    return replace(block, knoten=tuple(
        replace(k, wartet_auf=anzahl) if k.kennung in eingang else k for k in block.knoten
    ))


#: Wie auffaellig eine Zustandsart ist -- fuer die Linie in einen
#: Knotenpunkt, die fuer mehrere Ziele steht (KTD6). Was scheitert, faellt
#: vor dem auf, was sich wiederholt, das vor dem, was laeuft.
_RANG = {"is-fail": 4, "is-wait": 3, "is-run": 2, "is-idle": 1, "is-ok": 0}


@dataclass(frozen=True)
class _Linienzustand:
    """Was eine Linie von ihrem Ziel uebernimmt (KTD6)."""

    klasse: str
    gestrichelt: bool
    hervorgehoben: bool

    @staticmethod
    def zu(ziel: Knoten) -> _Linienzustand:
        return _Linienzustand(ziel.klasse, ziel.grund in _STARTET_NIE, ziel.aktiv)

    def an(self, linie: Linie) -> Linie:
        return replace(linie, klasse=self.klasse, gestrichelt=self.gestrichelt,
                       hervorgehoben=self.hervorgehoben)

    @staticmethod
    def zusammen(zustaende: list[_Linienzustand]) -> _Linienzustand:
        """Fuer die Linie in einen Knotenpunkt: die auffaelligste Klasse,
        gestrichelt nur, wenn alles dahinter nie startet."""
        return _Linienzustand(
            klasse=max((z.klasse for z in zustaende), key=lambda k: _RANG.get(k, 1),
                       default="is-idle"),
            gestrichelt=bool(zustaende) and all(z.gestrichelt for z in zustaende),
            hervorgehoben=any(z.hervorgehoben for z in zustaende),
        )


def _mit_zustand(
    knoten: tuple[Knoten, ...], linien: tuple[Linie, ...]
) -> tuple[tuple[Knoten, ...], tuple[Linie, ...]]:
    """Jede Linie traegt den Zustand ihres Ziels (KTD6); eine Linie in einen
    Knotenpunkt den der Linien aus ihm, und der Knotenpunkt mit ihr."""
    nach_kennung = {k.kennung: k for k in knoten}
    zustand = {
        li.kennung: _Linienzustand.zu(nach_kennung[li.nach])
        for li in linien if nach_kennung[li.nach].art != "punkt"
    }
    punkt = {
        k.kennung: _Linienzustand.zusammen(
            [zustand[li.kennung] for li in linien if li.von == k.kennung]
        )
        for k in knoten if k.art == "punkt"
    }
    for li in linien:
        if li.nach in punkt:
            zustand[li.kennung] = punkt[li.nach]
    return (
        tuple(replace(k, klasse=punkt[k.kennung].klasse) if k.kennung in punkt else k
              for k in knoten),
        tuple(zustand[li.kennung].an(li) for li in linien),
    )


def anordnen(baum: Baum, *, workflow_id: str, jetzt: float) -> Diagramm:
    """Die Geometrie des Diagramms zu *baum*."""
    umfeld = _Umfeld(workflow_id=workflow_id, jetzt=jetzt)
    if baum.wurzel.art == "unbekannt":
        # Ohne Topologie weiss niemand, was worauf folgt (R7): die bekannten
        # Schritte als Spalte, ohne Startpunkt und ohne Linien.
        spalte = _stapel([_block(k, umfeld) for k in baum.wurzel.kinder], "")
        spalte = spalte.verschoben(RAND, RAND)
        return Diagramm(
            knoten=spalte.knoten, linien=(),
            breite=spalte.b + 2 * RAND, hoehe=spalte.h + 2 * RAND,
        )
    block = _block(baum.wurzel, umfeld)
    start = _Block(
        b=START_B, h=START_B,
        knoten=(Knoten("dk-start", 0.0, 0.0, START_B, START_B, "start"),),
        ausgaenge=(_Anschluss("dk-start", START_B, START_B / 2),),
    )
    gesamt = _reihe([start, block]).verschoben(RAND, RAND)
    knoten, linien = _mit_zustand(gesamt.knoten, gesamt.linien)
    return Diagramm(
        knoten=knoten, linien=linien,
        breite=gesamt.b + 2 * RAND, hoehe=gesamt.h + 2 * RAND,
    )
