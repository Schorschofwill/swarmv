"""Seitengeruest, Stylesheet und statische Dateien (U11, R24-R26).

Die Gestaltung ist freigegeben (docs/reference/oberflaeche.md); hier wird
nicht ueber Aussehen geurteilt, sondern ueber die wenigen Zusagen, die sich
pruefen lassen und die beim Bauen leicht verlorengehen:

* **Kein Zugriff nach aussen** (KTD7). Eine Seite, die eine Schrift oder ein
  Skript aus einem fremden Netz holt, verraet jeden Aufruf des Dashboards an
  Dritte -- und faellt aus, sobald der Dashboard-Host keinen Ausgang hat. Das
  ist bei einem Ops-Werkzeug im Vorfall der Normalfall, nicht die Ausnahme.
* **Genau drei Bereiche** (R24, R25). Ein vierter Eintrag waere eine Zusage,
  die es nicht gibt.
* **Ein benannter Leerzustand** (R33). Eine leere Tabelle liest sich wie
  "nichts los", auch wenn in Wahrheit der Broker fehlt.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Das Geruest ===
1. Die Grundvorlage rendert mit den drei Navigationseintraegen -- und ohne
   einen vierten.
2. Die Kopfzeile stellt alle drei Zustaende dar, auch den nicht erreichbaren.
3. Jede Zustandsmarke ist Punkt **und** Wort, nie Farbe allein.

=== ZYKLUS 2: Kein Zugriff nach aussen (KTD7) ===
4. Keine ausgelieferte Datei verweist auf ein fremdes Netz.
5. Die Schriften und Skripte liegen im Paket.

=== ZYKLUS 3: Leerzustaende und Formulare ===
6. Ein Bereich ohne Daten nennt die Ursache statt einer leeren Tabelle.
7. Jedes schreibende Formular traegt ein nicht leeres CSRF-Feld.

=== ZYKLUS 4: Die Kopie bleibt die Vorlage ===
8. app.css und oberflaeche.css laufen nicht auseinander.
9. Jede vom Stylesheet verlangte Schriftdatei liegt da.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from swarmq_dashboard.web.templates import (
    BEREICHE,
    STATIC_DIR,
    TEMPLATE_DIR,
    render,
    statische_dateien,
)

_STATIK = Path(__file__).parents[3] / "src" / "swarmq_dashboard" / "web" / "static"


def _zustand(**kw: object) -> dict[str, object]:
    grund = {
        "broker": {"ok": True, "reason": None},
        "backend": {"ok": True, "reason": None},
        "stream": {"ok": True, "reason": None, "stale": False, "last_event_at": 1_700_000_000.0,
                   "retention_days": 7},
        "directory": {"ok": True, "reason": None, "rebuilding": False, "behind": False,
                      "gaps": 0, "rejected": 0, "dropped": 0, "erasure_pending": False,
                      "oldest_entry": 1_700_000_000.0},
        "accounts": {"ok": True, "reason": None},
        "audit": {"lost": 0},
        "maintenance": {"refresh": None, "purge": None},
    }
    grund.update(kw)
    return grund


# --- Zyklus 1 -----------------------------------------------------------


def test_drei_bereiche_und_kein_vierter() -> None:
    """Covers R24, R25."""
    assert [b.slug for b in BEREICHE] == ["worker", "tasks", "dlq"]
    html = render("base.html", status=_zustand(), account="ops", aktiv="worker")
    for bereich in BEREICHE:
        assert bereich.titel in html, f"{bereich.titel} fehlt in der Navigation"
    treffer = re.findall(r'class="nav-link"', html)
    assert len(treffer) == 3, f"{len(treffer)} Navigationseintraege statt drei"
    # Der aktive Bereich traegt ``aria-current`` statt einer eigenen Klasse:
    # das Stylesheet haengt seine Hervorhebung daran, und ein Vorleseprogramm
    # bekommt die Auskunft damit gleich mit.
    assert html.count('aria-current="page"') == 1, "der aktive Bereich ist nicht ausgezeichnet"


def test_die_kopfzeile_stellt_alle_drei_zustaende_dar() -> None:
    """Covers R26."""
    html = render("base.html", status=_zustand(), account="ops", aktiv="worker")
    for wort in ("Broker", "Backend", "Strom"):
        assert wort in html, f"{wort} fehlt in der Kopfzeile"

    weg = _zustand(broker={"ok": False, "reason": "Verbindung abgelehnt"})
    html = render("base.html", status=weg, account="ops", aktiv="worker")
    assert "Verbindung abgelehnt" in html, "der Grund wird verschwiegen"
    assert "nicht erreichbar" in html


def test_jede_zustandsmarke_traegt_ein_wort() -> None:
    """Die eine Regel der Gestaltung: Farbe allein traegt nie die Aussage --
    schon wegen der Farbfehlsichtigkeit, und weil ein Bildschirmfoto im
    Ticket seine Farben verliert.
    """
    weg = _zustand(broker={"ok": False, "reason": "weg"},
                   stream={"ok": True, "reason": None, "stale": True,
                           "last_event_at": None, "retention_days": 7})
    html = render("base.html", status=weg, account="ops", aktiv="worker")
    # ``tag-dot``, nicht ``dot``: beim Umbau auf das Vokabular des
    # Stylesheets blieb hier der alte Name stehen, und die Schleife lief ins
    # Leere -- nachgemessen null Treffer bei vier Marken auf der Seite. Eine
    # Zusicherung, die nie zu einer Pruefung kommt, ist keine.
    treffer = list(re.finditer(r'<span class="tag-dot"></span>\s*([^<]*)', html))
    assert treffer, "keine einzige Zustandsmarke gefunden -- die Probe laeuft leer"
    for punkt in treffer:
        assert punkt.group(1).strip(), f"eine Zustandsmarke ohne Wort: {punkt.group(0)!r}"


# --- Zyklus 2 -----------------------------------------------------------


@pytest.mark.parametrize("datei", sorted(_STATIK.rglob("*.css")) + sorted(_STATIK.rglob("*.js")))
def test_keine_ausgelieferte_datei_greift_nach_aussen(datei: Path) -> None:
    """KTD7. Ein Verweis nach aussen verraet jeden Aufruf des Dashboards an
    Dritte -- und faellt aus, sobald der Host keinen Ausgang hat. Bei einem
    Ops-Werkzeug ist das im Vorfall der Normalfall.
    """
    text = datei.read_text(encoding="utf-8", errors="replace")
    treffer = re.findall(r"https?://[^\s\"')]+", text)
    erlaubt = [t for t in treffer if t.startswith("http://www.w3.org/")]  # SVG-Namensraum
    assert not set(treffer) - set(erlaubt), f"{datei.name} verweist nach aussen: {treffer[:3]}"


def test_die_vorlage_holt_nichts_aus_fremden_netzen() -> None:
    html = render("base.html", status=_zustand(), account="ops", aktiv="worker")
    treffer = re.findall(r"https?://[^\s\"')]+", html)
    assert not treffer, f"die Seite verweist nach aussen: {treffer}"


def test_schriften_und_skripte_liegen_im_paket() -> None:
    assert (_STATIK / "app.css").is_file()
    assert (_STATIK / "htmx.min.js").is_file()
    assert (_STATIK / "idiomorph-ext.min.js").is_file()
    schriften = list((_STATIK / "fonts").glob("*.woff2"))
    assert len(schriften) >= 4, f"nur {len(schriften)} Schriftdateien im Paket"


def test_der_statische_router_ist_von_der_anmeldepflicht_ausgenommen() -> None:
    router = statische_dateien()
    assert router.opt.get("public") is True, "die statischen Dateien verlangen eine Sitzung"
    assert router.opt.get("no_csrf") is True


# --- Zyklus 3 -----------------------------------------------------------


def test_ein_leerer_bereich_nennt_die_ursache() -> None:
    """Covers R33. Eine leere Tabelle liest sich wie "nichts los", auch wenn
    in Wahrheit der Broker fehlt.
    """
    html = render(
        "leerzustand.html",
        titel="Keine Worker bekannt",
        ursache="Der Broker ist nicht erreichbar: Verbindung abgelehnt",
    )
    assert "Keine Worker bekannt" in html
    assert "Verbindung abgelehnt" in html


def test_ein_schreibendes_formular_traegt_das_merkmal() -> None:
    html = render("base.html", status=_zustand(), account="ops", aktiv="worker",
                  csrf_token="abc123")
    formulare = re.findall(r"<form[^>]*method=[\"']?post.*?</form>", html, re.S | re.I)
    assert formulare, "die Grundvorlage hat kein schreibendes Formular (Abmelden fehlt)"
    for f in formulare:
        treffer = re.search(r'name="_csrf_token"\s+value="([^"]*)"', f)
        assert treffer, f"ein Formular ohne Merkmal: {f[:120]}"
        assert treffer.group(1), "das Merkmal ist leer"


def test_das_stylesheet_ist_die_freigegebene_vorlage() -> None:
    """``oberflaeche.md`` sagt: aendert sich die Gestaltung, aendert sie sich
    **dort und hier**. Ohne diese Zusicherung laufen die beiden auseinander,
    und das Mockup verliert seine Rolle als Vorlage -- unbemerkt.
    """
    wurzel = Path(__file__).parents[3]
    vorlage = (wurzel / "docs" / "reference" / "oberflaeche.css").read_text(encoding="utf-8")
    geliefert = (_STATIK / "app.css").read_text(encoding="utf-8")
    assert vorlage in geliefert, (
        "app.css und oberflaeche.css sind auseinandergelaufen -- die Kopie traegt "
        "nur einen Kopfkommentar mehr"
    )


def test_jede_vom_stylesheet_verlangte_schrift_liegt_da() -> None:
    """Fehlt eine Datei, aendert sich nur die Schrift -- aber ein Verweis ins
    Leere ist ein Aufruf, der bei jedem Seitenaufbau fehlschlaegt.
    """
    css = (_STATIK / "app.css").read_text(encoding="utf-8")
    verlangt = set(re.findall(r"url\(['\"]?(fonts/[^'\")]+)", css))
    assert verlangt, "das Stylesheet verlangt gar keine Schrift"
    fehlend = [f for f in verlangt if not (_STATIK / f).is_file()]
    assert not fehlend, f"verlangt, aber nicht vorhanden: {fehlend}"


# --- Vorlagen gegen das Stylesheet --------------------------------------


def _css_klassen() -> set[str]:
    return set(re.findall(r"\.([a-zA-Z][\w-]*)", (STATIC_DIR / "app.css").read_text(encoding="utf-8")))


def _vorlagen_klassen() -> dict[str, set[str]]:
    """Alle **festen** Klassennamen aus den Vorlagen, je Datei.

    Namen mit Jinja-Anteil bleiben aussen vor; sie kommen aus
    ``zustandsart`` und ``marke`` und werden unten eigens geprueft.
    """
    gefunden: dict[str, set[str]] = {}
    for p in sorted(TEMPLATE_DIR.rglob("*.html")):
        for wert in re.findall(r'class="([^"]*)"', p.read_text(encoding="utf-8")):
            if "{" in wert:
                continue
            for name in wert.split():
                gefunden.setdefault(name, set()).add(p.name)
    return gefunden


def test_jede_klasse_der_vorlagen_hat_eine_regel() -> None:
    """Sonst ist die Seite funktionsfaehig und trotzdem ungestaltet.

    Genau das war sie: 33 von 41 Klassennamen hatten keine Regel, weil die
    Vorlagen ein eigenes Vokabular erfanden statt das freigegebene zu
    benutzen. Alle vier Tore standen dabei auf gruen -- niemand hielt die
    beiden Seiten gegeneinander. Dieser Test tut es.
    """
    vorhanden = _css_klassen()
    fehlend = {k: v for k, v in _vorlagen_klassen().items() if k not in vorhanden}
    assert not fehlend, "ohne Regel im Stylesheet: " + ", ".join(
        f".{k} ({', '.join(sorted(v))})" for k, v in sorted(fehlend.items())
    )


def test_jede_zustandsart_hat_eine_regel() -> None:
    """Die Zustandsarten stehen nicht im Klartext in der Vorlage, sondern
    kommen aus ``zustandsart`` und ``marke`` -- der Test oben sieht sie
    darum nicht. Ohne Regel bliebe die Marke farblos, und Farbe ist hier
    die halbe Aussage."""
    from swarmq_dashboard.web.templates import _ZUSTANDSART

    vorhanden = _css_klassen()
    for art in set(_ZUSTANDSART.values()) | {"is-ok", "is-wait", "is-fail"}:
        assert art in vorhanden, f"die Zustandsart {art!r} hat keine Regel"


def test_die_kopfzeile_nennt_die_zahl_der_luecken() -> None:
    """R4a. Die festgehaltene Zahl, nicht der laufende Rueckstand.

    ``behind`` sagt, ob der Leser **gerade** hinterherhaengt; nach einem
    Neustart mit eingeholtem Strom ist es wieder falsch. Die Luecke von
    gestern bleibt trotzdem eine, und die Kopfzeile muss sie kennen.
    """
    verzeichnis = {"ok": True, "reason": None, "rebuilding": False, "behind": False,
                   "gaps": 2, "rejected": 0, "dropped": 0, "erasure_pending": False,
                   "oldest_entry": 1_700_000_000.0}
    html = render("base.html", status=_zustand(directory=verzeichnis),
                  account="ops", aktiv="tasks")
    # Die Marke selbst, nicht die Seite: "2" steht sonst schon im Datum
    # 2023-11-14, und die Zahl durfte aus der Marke verschwinden, ohne dass
    # etwas kippte.
    marke = re.search(r'<span class="tag is-wait"><span class="tag-dot"></span>\s*([^<]*Lücke[^<]*)', html)
    assert marke, "die Kopfzeile schweigt ueber die Luecken"
    assert marke.group(1).strip().startswith("2 "), (
        f"die Zahl der Luecken fehlt in der Marke: {marke.group(1)!r}"
    )

    # Triangulieren: eine andere Zahl muss eine andere Marke ergeben.
    verzeichnis3 = {**verzeichnis, "gaps": 3}
    html3 = render("base.html", status=_zustand(directory=verzeichnis3),
                   account="ops", aktiv="tasks")
    marke3 = re.search(r'<span class="tag is-wait"><span class="tag-dot"></span>\s*([^<]*Lücke[^<]*)', html3)
    assert marke3 and marke3.group(1).strip().startswith("3 ")


def test_jede_seite_hat_genau_eine_h1() -> None:
    """Bedienbarkeit mit Vorleseprogramm.

    Gezaehlt im gerenderten HTML hatte keine Seite eine ``h1``; die
    Ueberschriftenfolge begann bei ``h2``, und die Tasks-Liste -- die
    meistgenutzte Seite -- hatte gar keine Ueberschrift. Wer per
    Ueberschriftenliste navigiert, findet so nicht, wo er ist.
    """
    html = render("base.html", status=_zustand(), account="ops", aktiv="worker")
    assert html.count("<h1") == 1, f"{html.count('<h1')} h1-Elemente statt einer"
    titel = re.search(r"<h1[^>]*>([^<]*)", html)
    assert titel and titel.group(1).strip() == "Worker", (
        f"die h1 nennt nicht den Bereich: {titel.group(1) if titel else None!r}"
    )


def test_der_sichtbare_text_schreibt_umlaute() -> None:
    """Die freigegebene Gestaltung schreibt durchgehend Umlaute.

    Die Vorlagen schrieben "Luecke", "verzoegert", "endgueltig",
    "unvollstaendig" -- die Oberflaeche las sich wie ein Rohentwurf, und ein
    Vorleseprogramm spricht "loeschen" falsch aus. Bezeichner bleiben
    englisch und ASCII; das hier betrifft nur, was ein Mensch liest.
    """
    verzeichnis = {"ok": True, "reason": None, "rebuilding": True, "behind": True,
                   "gaps": 2, "rejected": 1, "dropped": 1, "erasure_pending": True,
                   "oldest_entry": 1_700_000_000.0}
    html = render("base.html",
                  status=_zustand(directory=verzeichnis,
                                  stream={"ok": True, "reason": None, "stale": True,
                                          "last_event_at": None, "retention_days": 7}),
                  account="ops", aktiv="tasks")
    # Nur der sichtbare Text, ohne Auszeichnung und ohne Attribute.
    sichtbar = re.sub(r"<[^>]*>", " ", html)
    for falsch in ("Luecke", "verzoegert", "laeuft", "Loeschung"):
        assert falsch not in sichtbar, (
            f"{falsch!r} steht im sichtbaren Text -- die Gestaltung schreibt Umlaute"
        )


def test_kein_ascii_ersatz_im_sichtbaren_text_der_vorlagen() -> None:
    """Der Test darueber rendert nur ``base.html``.

    Von seinen fuenf Woertern kann eines dort in keinem Zweig auftreten --
    die Zusicherung kam nie zu einer Pruefung, und der
    Unvollstaendigkeits-Hinweis der Dead-Letter-Seite durfte still
    zurueckfallen. Hier laufen alle Vorlagen und alle Textkonstanten der
    Routen durch **eine** Probe.

    Geprueft wird auch der Text in ``title``-Attributen: er ist beim
    Ueberfahren sichtbar, und beim ersten Durchgang wurde er uebergangen.
    """
    import ast

    verdaechtig = re.compile(
        r"\b\w*(?:ae|oe|ue)\w*\b", re.IGNORECASE)
    erlaubt = {"queue", "queues", "neu", "true", "false", "value", "values",
               "duration", "requeue", "enqueued", "aktuell", "steuerung",
               "quellenname", "zeitraeume", "uebersicht",
               # Deutsche Woerter, die die Buchstabenfolge echt enthalten.
               "quelle", "quellen", "neue", "neuen", "neuer"}

    treffer: list[str] = []
    for datei in TEMPLATE_DIR.rglob("*.html"):
        text = datei.read_text(encoding="utf-8")
        # Jinja-Kommentare und Auszeichnung entfernen, ``title``-Texte behalten.
        ohne_kommentar = re.sub(r"\{#-?.*?-?#\}", " ", text, flags=re.S)
        # Jinja-Ausdruecke und -Bloecke sind Code, kein Text: dort stehen
        # Bezeichner wie ``enqueued_at``, und die bleiben ASCII.
        ohne_kommentar = re.sub(r"\{\{.*?\}\}|\{%-?.*?-?%\}", " ",
                                ohne_kommentar, flags=re.S)
        titel = " ".join(re.findall(r'title="([^"]*)"', ohne_kommentar))
        sichtbar = re.sub(r"<[^>]*>", " ", ohne_kommentar) + " " + titel
        for wort in verdaechtig.findall(sichtbar):
            if wort.lower() not in erlaubt:
                treffer.append(f"{datei.name}: {wort}")

    for pfad in (TEMPLATE_DIR.parent / "routes").glob("*.py"):
        baum = ast.parse(pfad.read_text(encoding="utf-8"))
        doks = {id(k.body[0].value) for k in ast.walk(baum)
                if isinstance(k, ast.Module | ast.ClassDef | ast.FunctionDef
                              | ast.AsyncFunctionDef)
                and ast.get_docstring(k) is not None and k.body
                and isinstance(k.body[0], ast.Expr)}
        for knoten in ast.walk(baum):
            if (isinstance(knoten, ast.Constant) and isinstance(knoten.value, str)
                    and id(knoten) not in doks and " " in knoten.value):
                for wort in verdaechtig.findall(knoten.value):
                    if wort.lower() not in erlaubt:
                        treffer.append(f"{pfad.name}: {wort}")

    assert not treffer, (
        "ASCII-Ersatz im sichtbaren Text:\n  " + "\n  ".join(sorted(set(treffer)))
    )


def test_eine_unbekannte_variable_wirft_statt_zu_schweigen() -> None:
    """Sonst rendert eine Vorlage still Leerraum, wo eine Aussage stehen soll.

    Genau das ist passiert: ein Umbau verschob einen Variablennamen, und der
    stehende Unvollstaendigkeits-Hinweis der Dead-Letter-Seite verschwand --
    ohne Fehler, ohne roten Test. Die Seite behauptete damit
    Vollstaendigkeit, weil eine Vorlage schwieg.

    Geprueft wird die Umgebung, die die **Anwendung** benutzt, nicht die der
    Hilfsfunktion ``render``: nur in der ersten ist der Fehler aufgetreten.
    """
    from litestar.plugins.jinja import JinjaTemplateEngine

    from swarmq_dashboard.web.templates import TEMPLATE_DIR, template_config

    config = template_config()
    engine = JinjaTemplateEngine(directory=TEMPLATE_DIR)
    assert config.engine_callback is not None
    config.engine_callback(engine)

    vorlage = engine.engine.from_string("[{{ gibtesnicht }}]")
    with pytest.raises(Exception, match="(?i)undefined"):
        vorlage.render()


#: Bausteine, die eine Karte als unmittelbares Kind haben darf. Jeder bringt
#: seine eigene Polsterung mit -- die Karte selbst hat keine.
KARTEN_KINDER = {"card-head", "card-meta", "card-section", "card-body",
                 "card-table", "card-foot", "empty", "fields", "args", "trail"}


def test_keine_nackte_ueberschrift_in_einer_karte() -> None:
    """Die Karte hat **keine** Polsterung -- das ist Absicht.

    Im Stylesheet tragen ihre Bausteine sie: ``.card-head``,
    ``.card-section``, ``.card-meta``. Der Reset setzt darum ``h1,h2,h3,p,dd``
    auf ``margin: 0``. Wer eine Ueberschrift oder einen Absatz **direkt** in
    eine Karte legt, bekommt sie bündig an der Kante -- genau so sah es im
    Browser aus, und genau das hat ein Mensch als Erstes bemerkt.

    Geprueft wird die Struktur, nicht das Aussehen: jedes unmittelbare Kind
    einer Karte muss einer der polsternden Bausteine sein.
    """
    import re as _re

    fehler: list[str] = []
    for datei in sorted(TEMPLATE_DIR.rglob("*.html")):
        text = datei.read_text(encoding="utf-8")
        text = _re.sub(r"\{#-?.*?-?#\}", " ", text, flags=_re.S)
        for stueck in _re.findall(r'<section class="card"[^>]*>(.*?)</section>',
                                  text, _re.S):
            # Nur die aeusserste Ebene ansehen.
            tiefe = 0
            for treffer in _re.finditer(r"<(/?)(\w+)([^>]*)>", stueck):
                zu, tag, rest = treffer.groups()
                if tiefe == 0 and not zu and tag in ("h1", "h2", "h3", "h4", "p",
                                                     "table", "form", "dl", "pre"):
                    klassen = set(
                        (_re.search(r'class="([^"]*)"', rest) or
                         _re.match("", "")).group(1).split()
                        if _re.search(r'class="([^"]*)"', rest) else []
                    )
                    if not (klassen & KARTEN_KINDER):
                        fehler.append(f"{datei.name}: <{tag}> ohne polsternden Baustein")
                if not zu and tag not in ("input", "br", "img", "hr", "meta", "link"):
                    tiefe += 1
                elif zu:
                    tiefe -= 1
    assert not fehler, (
        "unmittelbar in einer Karte, ohne eigene Polsterung:\n  "
        + "\n  ".join(sorted(set(fehler)))
    )


def test_ein_feldpaar_bleibt_zusammen() -> None:
    """``.fields`` ist ein dreispaltiges Raster.

    In einem Raster wird **jedes** ``dt`` und **jedes** ``dd`` eine eigene
    Zelle: "Name" landet in Spalte eins, "reports" in Spalte zwei, das
    naechste Label in Spalte drei. Welches Feld zu welchem Wert gehoert, ist
    dann nicht mehr erkennbar -- so stand es im Browser.

    Ein Paar muss darum in einem gemeinsamen Behaelter stecken, wie im
    freigegebenen Mockup.
    """
    import re as _re

    text = (TEMPLATE_DIR / "tasks" / "detail.html").read_text(encoding="utf-8")
    raster = _re.search(r'<dl class="fields">(.*?)</dl>', text, _re.S)
    assert raster, "die Feldliste fehlt"
    inhalt = _re.sub(r"\{[#%].*?[#%]\}", " ", raster.group(1), flags=_re.S)
    # Kein ``dt`` darf unmittelbares Kind der Liste sein.
    ohne_behaelter = _re.findall(r"^\s*<dt", inhalt, _re.M)
    assert not ohne_behaelter, (
        f"{len(ohne_behaelter)} Feldpaare stehen ohne gemeinsamen Behaelter im "
        "Raster -- Bezeichnung und Wert fallen dann auseinander"
    )


def test_die_tabellenkoepfe_haben_abstand_nach_oben() -> None:
    """Drei Runden hat dieser eine Wert gekostet.

    ``th`` stand auf ``padding: 0 14px 7px`` -- oben null. Der einzige
    Abstand kam aus dem Tabellenbehaelter, und der Kopf klebte sichtbar an
    der Kante. Gesucht habe ich in der Zwischenzeit bei Spaltenbreiten,
    Zeilenhoehe und Umbruchschutz.

    Die Zusicherung ist stumpf und genau deshalb richtig: sie prueft die
    Zahl, an der es lag.
    """
    import re as _re

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    regel = _re.search(r"\n  th \{(.*?)\}", css, _re.S)
    assert regel, "keine th-Regel im Stylesheet"
    polster = _re.search(r"padding:\s*([0-9.]+)px", regel.group(1))
    assert polster, f"kein padding an th: {regel.group(1)}"
    assert float(polster.group(1)) >= 10, (
        f"der Tabellenkopf hat {polster.group(1)}px Abstand nach oben -- "
        "er klebt dann an der Kante"
    )


async def test_statische_dateien_werden_nachgeprueft() -> None:
    """Ohne Cache-Anweisung entscheidet der Browser selbst.

    Gefunden, als eine Stylesheet-Aenderung im Browser dreimal hintereinander
    nicht ankam -- der Dienst lieferte die neue Datei aus, der Browser zeigte
    die alte aus seinem Speicher. Bei einem Ops-Werkzeug heisst das: nach
    einem Deploy sieht das Team tagelang das alte Stylesheet und das alte
    ``live.js``, ohne es zu merken.

    ``no-cache`` heisst nicht "nicht speichern", sondern "vor dem Benutzen
    nachfragen". Mit ETag kostet das eine Anfrage ohne Koerper -- und eine
    geaenderte Datei kommt garantiert an.
    """
    import tempfile
    from pathlib import Path as _Path

    from argon2 import PasswordHasher
    from litestar.testing import AsyncTestClient

    from swarmq_dashboard.config import DashboardConfig
    from swarmq_dashboard.web.app import create_app
    d = _Path(tempfile.mkdtemp())
    config = DashboardConfig(
        broker_url="amqp://guest:guest@127.0.0.1:1/", backend_url="valkey://127.0.0.1:1/0",
        stream_url="valkey://127.0.0.1:1/1", retention_days=7, unconfirmed_after_s=15.0,
        disconnected_after_s=40.0, session_idle_s=1800.0, backlog_cap=300,
        stream_stale_after_s=120.0, data_dir=d / "sqd-data",
    )
    app = create_app(config, hasher=PasswordHasher(time_cost=2, memory_cost=1024,
                                                   parallelism=1))
    async with AsyncTestClient(app=app, base_url="https://t.local") as c:
        for pfad in ("/static/app.css", "/static/live.js"):
            antwort = await c.get(pfad)
            assert antwort.status_code == 200, pfad
            steuerung = antwort.headers.get("cache-control", "")
            assert "no-cache" in steuerung, (
                f"{pfad} traegt keine Cache-Anweisung ({steuerung!r}) -- eine "
                "geaenderte Datei kommt dann nicht zuverlaessig an"
            )


# --- Zyklus 6: was der Browser misst, nicht was die Vorlage behauptet ----


def test_ein_verborgenes_element_bleibt_verborgen() -> None:
    """``hidden`` verliert gegen jede Klasse, die ein ``display`` setzt.

    Gemessen im Browser: die Marke fuer den Zustand des Live-Kanals traegt
    ``hidden`` **und** ``.tag``. ``.tag`` setzt ``display: inline-flex``, und
    das schlaegt die Vorgabe des Browsers fuer ``[hidden]``. Die Folge war ein
    leeres rotes Kaestchen von 18 px in der Kopfzeile **jeder** Seite -- eine
    Stoerungsmarke ohne Stoerung, genau die Art Falschaussage, die dieses
    Dashboard nicht machen darf.
    """
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    regel = re.search(r"\[hidden\]\s*\{([^}]*)\}", css)
    assert regel, "das Stylesheet holt [hidden] nicht ein"
    assert re.search(r"display:\s*none\s*!important", regel.group(1)), (
        "ohne !important verliert [hidden] gegen jede Klasse mit display: "
        f"{regel.group(1)}"
    )

    html = render("base.html", status=_zustand(), account="ops", aktiv="worker")
    marke = re.search(r'<span class="tag[^"]*"[^>]*data-live-zustand[^>]*>', html)
    assert marke and "hidden" in marke.group(0), (
        "die Marke des Live-Kanals traegt kein hidden mehr -- dann prueft "
        "dieser Test die falsche Stelle"
    )


def test_jede_seite_nennt_ihr_sinnbild() -> None:
    """Ohne ``rel="icon"`` fragt der Browser ``/favicon.ico`` -- und bekommt
    bei jedem Aufruf eine 404 im Zugriffsprotokoll. Das Sinnbild liegt im
    Dokument, nicht in einem fremden Netz (KTD7).
    """
    for vorlage, kontext in (
        ("base.html", {"status": _zustand(), "account": "ops", "aktiv": "worker"}),
        ("anmeldung.html", {"csrf_token": "t", "fehler": ""}),
        ("fehler.html", {"code": 404, "titel": "Nicht gefunden", "detail": "weg"}),
    ):
        html = render(vorlage, **kontext)
        assert re.search(r'<link[^>]+rel="icon"', html), f"{vorlage} hat kein Sinnbild"


def test_die_kopfzeile_zaehlt_im_singular() -> None:
    """"1 Audit-Einträge verloren" und "1 Lücke(n)".

    Eine Zahl, die man nicht liest, ohne ueber die Form zu stolpern, kostet
    genau die Sekunde, in der man im Vorfall etwas anderes tun sollte.
    """
    eins = _zustand(
        directory={"ok": True, "reason": None, "rebuilding": False, "behind": False,
                   "gaps": 1, "rejected": 1, "dropped": 1, "erasure_pending": False,
                   "oldest_entry": 1_700_000_000.0},
        audit={"lost": 1},
    )
    html = render("base.html", status=eins, account="ops", aktiv="worker")
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    for falsch in ("1 Lücke(n)", "1 Audit-Einträge", "1 Einträge", "1 Ereignisse"):
        assert falsch not in text, f"die Kopfzeile sagt {falsch!r}"
    for richtig in ("1 Lücke im Strom", "1 Audit-Eintrag verloren",
                    "1 Eintrag nicht lesbar", "1 Ereignis abgewiesen"):
        assert richtig in text, f"{richtig!r} fehlt: {text[:400]}"

    viele = _zustand(
        directory={"ok": True, "reason": None, "rebuilding": False, "behind": False,
                   "gaps": 2, "rejected": 2, "dropped": 2, "erasure_pending": False,
                   "oldest_entry": 1_700_000_000.0},
        audit={"lost": 2},
    )
    text = " ".join(re.sub(r"<[^>]+>", " ", render(
        "base.html", status=viele, account="ops", aktiv="worker")).split())
    for richtig in ("2 Lücken im Strom", "2 Audit-Einträge verloren",
                    "2 Einträge nicht lesbar", "2 Ereignisse abgewiesen"):
        assert richtig in text, f"{richtig!r} fehlt: {text[:400]}"


def test_jeder_tabellenkopf_nennt_seine_richtung() -> None:
    """``<th>`` ohne ``scope`` laesst ein Vorleseprogramm raten, ob die Zelle
    ihre Spalte oder ihre Zeile beschreibt. Bei acht Spalten sagt es dann in
    jeder Zelle den falschen Namen an.
    """
    fehlend: list[str] = []
    for pfad in sorted(TEMPLATE_DIR.rglob("*.html")):
        for kopf in re.findall(r"<th\b[^>]*>", pfad.read_text(encoding="utf-8")):
            if "scope=" not in kopf:
                fehlend.append(f"{pfad.name}: {kopf}")
    assert not fehlend, "Tabellenkoepfe ohne scope: " + ", ".join(fehlend)


def test_jede_seite_beginnt_mit_einem_sprungverweis() -> None:
    """Vor dem Inhalt stehen drei Navigationsverweise und eine Kopfzeile mit
    bis zu neun Marken. Wer mit der Tastatur arbeitet, laeuft sie auf jeder
    Seite neu durch, bevor er die Tabelle erreicht.
    """
    html = render("base.html", status=_zustand(), account="ops", aktiv="worker")
    verweis = re.search(r'<a class="skip"[^>]*href="#([^"]+)"', html)
    assert verweis, "kein Sprungverweis"
    assert f'id="{verweis.group(1)}"' in html, "der Sprungverweis zeigt ins Leere"
    assert html.index(verweis.group(0)) < html.index('class="nav"'), (
        "der Sprungverweis steht hinter der Navigation -- dann ueberspringt "
        "er sie nicht"
    )


def test_eine_breite_tabelle_schiebt_die_seite_nicht_auseinander() -> None:
    """Gemessen bei 1024 px: die Worker-Tabelle ist 792 px breit, ihre Karte
    745 -- die Tabelle stand 50 px ueber dem Kartenrand, und das Dokument war
    1035 px breit bei 1009 px Fenster. Die ganze Seite liess sich waagerecht
    schieben, Navigation und Kopfzeile mit.

    ``.card-table`` ist der Behaelter, der das auffangen soll; ohne
    ``overflow-x`` faengt er nichts auf.
    """
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    regel = re.search(r"\n  \.card-table \{([^}]*)\}", css)
    assert regel, "keine .card-table-Regel im Stylesheet"
    assert re.search(r"overflow-x:\s*auto", regel.group(1)), (
        f"die Tabelle laeuft aus ihrer Karte heraus: {regel.group(1)}"
    )


# --- Zyklus 7: Lesbarkeit als Zahl, nicht als Gefuehl --------------------

#: Vorder- und Hintergrund jeder Paarung, die in der Oberflaeche wirklich
#: vorkommt -- und die Stelle, an der sie steht. Die Werte sind Namen von
#: Farbtoken aus ``@layer tokens``.
_PAARE = [
    ("--ink", "--surface", "Fliesstext auf der Karte"),
    ("--ink-2", "--surface", "zweite Schriftstufe"),
    ("--muted", "--surface", "Meta-Zeile der Karte"),
    ("--dim", "--surface", "Quelle eines Workers, \"nicht gemessen\""),
    ("--muted", "--ground", "Meta-Zeile auf dem Seitengrund"),
    ("--dim", "--ground", "gedaempftes auf dem Seitengrund"),
    ("--ok-text", "--ok-bg", "Marke \"verbunden\""),
    ("--run-text", "--run-bg", "Marke \"laufend\""),
    ("--wait-text", "--wait-bg", "Marke \"wiederholung\""),
    ("--fail-text", "--fail-bg", "Marke \"gescheitert\""),
    # Der Grund einer Stoerung steht als ``.muted`` **in** der roten Marke.
    # Gemessen von axe-core: 4,15:1 -- unter der Schwelle, und ausgerechnet
    # an dem Satz, der im Vorfall sagt, was kaputt ist.
    ("--muted", "--fail-bg", "Fehlgrund in der Stoerungsmarke"),
    ("--muted", "--wait-bg", "Zusatz in der Wartemarke"),
    ("--muted", "--ok-bg", "Zusatz in der Gutmarke"),
    ("--on-brand", "--brand", "aktiver Navigationseintrag"),
]
#: WCAG AA fuer Text unter 18,5 px. Die Oberflaeche schreibt 11,5 bis 14 px.
_SCHWELLE = 4.5


def _token(css: str, name: str) -> tuple[str, str]:
    """Die helle und die dunkle Seite eines ``light-dark()``-Tokens."""
    treffer = re.search(
        rf"{re.escape(name)}:\s*light-dark\((#[0-9A-Fa-f]{{6}}),\s*(#[0-9A-Fa-f]{{6}})\)",
        css,
    )
    assert treffer, f"{name} ist kein light-dark()-Token mehr"
    return treffer.group(1), treffer.group(2)


def _kontrast(vorn: str, hinten: str) -> float:
    def hell(farbe: str) -> float:
        teile = []
        for i in (1, 3, 5):
            c = int(farbe[i:i + 2], 16) / 255
            teile.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * teile[0] + 0.7152 * teile[1] + 0.0722 * teile[2]

    a, b = hell(vorn), hell(hinten)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def test_jede_farbpaarung_der_oberflaeche_ist_lesbar() -> None:
    """Farbe ist in dieser Oberflaeche nie Schmuck: sie traegt den Zustand.

    Eine Paarung unter 4,5:1 heisst, dass ein Teil der Leser die Aussage
    nicht bekommt -- und zwar genau im Vorfall, wenn die roten Marken
    auftauchen. Geprueft werden **beide** Fassungen: die dunkle ist nicht
    weniger echt, seit ``data-theme`` sie nicht mehr abschaltet.
    """
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    schwach: list[str] = []
    for vorn, hinten, wo in _PAARE:
        for fassung, i in (("hell", 0), ("dunkel", 1)):
            v, h = _token(css, vorn)[i], _token(css, hinten)[i]
            wert = _kontrast(v, h)
            if wert < _SCHWELLE:
                schwach.append(f"{wo} ({fassung}, {vorn} auf {hinten}): {wert:.2f}:1")
    assert not schwach, "unter 4,5:1 -- " + "; ".join(schwach)


# --- Zyklus 8: Saetze, Zahlen und was die Rueckfrage kostet -------------


async def test_die_rueckfrage_kostet_keinen_koerper() -> None:
    """``no-cache`` heisst "vor dem Benutzen nachfragen".

    Die Zusage im Kommentar daneben lautet, dass diese Rueckfrage eine
    Antwort ohne Koerper kostet. Ohne ETag stimmt sie nicht: dann liefert der
    Dienst bei **jedem** Seitenaufruf das ganze Stylesheet, die beiden
    Skripte und die Schriften neu -- gemessen 186 KB, fuer eine Seite, die
    ein Team den ganzen Tag offen hat und deren Live-Kanal sie regelmaessig
    neu holt.
    """
    import tempfile
    from pathlib import Path as _Path

    from argon2 import PasswordHasher
    from litestar.testing import AsyncTestClient

    from swarmq_dashboard.config import DashboardConfig
    from swarmq_dashboard.web.app import create_app

    d = _Path(tempfile.mkdtemp())
    config = DashboardConfig(
        broker_url="amqp://guest:guest@127.0.0.1:1/", backend_url="valkey://127.0.0.1:1/0",
        stream_url="valkey://127.0.0.1:1/1", retention_days=7, unconfirmed_after_s=15.0,
        disconnected_after_s=40.0, session_idle_s=1800.0, backlog_cap=300,
        stream_stale_after_s=120.0, data_dir=d / "sqd-data",
    )
    app = create_app(config, hasher=PasswordHasher(time_cost=2, memory_cost=1024,
                                                   parallelism=1))
    async with AsyncTestClient(app=app, base_url="https://t.local") as c:
        for pfad in ("/static/app.css", "/static/live.js"):
            erste = await c.get(pfad)
            marke = erste.headers.get("etag", "")
            assert marke, f"{pfad} kommt ohne ETag -- die Rueckfrage kostet alles"
            zweite = await c.get(pfad, headers={"If-None-Match": marke})
            assert zweite.status_code == 304, (
                f"{pfad}: {zweite.status_code} statt 304, "
                f"{len(zweite.content)} Bytes Koerper"
            )
            assert not zweite.content, f"{pfad}: 304 mit Koerper"


def test_ein_task_ohne_verzeichnis_bekommt_keinen_zeitpunkt_angedichtet() -> None:
    """"Die Suche reicht zurück bis noch nie."

    ``zeitpunkt(None)`` gibt "noch nie" -- richtig als Zeitpunkt, falsch in
    diesem Satz. Ein leeres Verzeichnis hat keine Reichweite, und der Satz
    muss das sagen, statt eine zu behaupten.
    """
    html = render(
        "tasks/liste.html", status=_zustand(), account="ops", aktiv="tasks",
        filter=type("F", (), {"text": "", "status": "", "task_name": "", "worker_id": ""})(),
        zeitraeume=[(None, "beliebig")], seit="", reichweite=None,
        herkunft_hinweis="H", hinweis="", zeilen=[], weiter=None,
        leer_titel="T", leer_ursache="U", csrf_token="t",
    )
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    assert "reicht zurück bis noch nie" not in text, text[:300]
    assert "Das Verzeichnis ist leer" in text, text[:300]


def test_ein_mehrzeiliger_fehler_bleibt_mehrzeilig() -> None:
    """Ein Traceback ist das Erste, was man auf der Detailseite sucht.

    ``<dd class="mono">`` faltet jede Zeile zu einer einzigen -- aus zwanzig
    Zeilen Traceback wird ein Fliesstextabsatz ohne Einrueckung, in dem die
    Zeile mit dem Dateinamen nicht mehr zu finden ist.
    """
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    regel = re.search(r"\n  \.fehlertext \{([^}]*)\}", css)
    assert regel, "keine .fehlertext-Regel im Stylesheet"
    assert re.search(r"white-space:\s*pre-wrap", regel.group(1)), regel.group(1)
    quelle = (TEMPLATE_DIR / "tasks" / "detail.html").read_text(encoding="utf-8")
    assert "fehlertext" in quelle, "die Detailseite benutzt die Regel nicht"


def test_gleichnamige_knoepfe_nennen_ihr_ziel() -> None:
    """Zwoelf Knoepfe "Sichten" untereinander.

    Wer die Seite per Knopfliste abgeht -- ein Vorleseprogramm bietet genau
    das an -- hoerte zwoelfmal dasselbe Wort und wusste bei keinem, welche
    Queue er damit anfasst. Der Zusatz steht in ``.sr-only``: sichtbar
    beantwortet die Zeile die Frage schon.

    Geprueft wird die **gerenderte** Seite, nicht die Vorlage: erst beim
    Rendern entstehen aus einer Schleife zwoelf gleichlautende Knoepfe. Eine
    erste Fassung dieses Tests suchte in der Quelle nach ``sr-only`` in der
    Naehe und liess sich vom Kommentar darueber taeuschen.
    """
    class _Stats:
        def __init__(self, tiefe: int) -> None:
            self.exists, self.error, self.dead_letter = True, None, tiefe

    html = render(
        "dlq/uebersicht.html", status=_zustand(), account="ops", aktiv="dlq",
        csrf_token="t", meldung="", gesichtet=None, hinweis="", weiter=None,
        tasks=[], sicht_warnung="W", unvollstaendig_hinweis="U",
        leer_titel="T", leer_ursache="U", queues_leer_ursache="Q",
        queues=[{"queue": f"q{i}", "stats": _Stats(i), "busy": False} for i in range(3)],
    )
    namen = [
        " ".join(re.sub(r"<[^>]+>", " ", inhalt).split())
        for inhalt in re.findall(r"<button\b[^>]*>(.*?)</button>", html, re.S)
    ]
    doppelt = sorted({n for n in namen if namen.count(n) > 1})
    assert not doppelt, (
        "mehrere Knoepfe mit demselben Namen -- welcher welche Queue "
        f"anfasst, ist nicht zu hoeren: {doppelt}"
    )


def test_grosse_zahlen_sind_auf_einen_blick_lesbar() -> None:
    """``1200`` und ``12000`` unterscheiden sich um eine Stelle.

    Der Rueckstau einer Queue ist die Zahl, auf die im Vorfall zuerst
    geschaut wird, und "ist das jetzt zwoelfhundert oder zwoelftausend"
    kostet genau dort eine Sekunde. Deutsche Schreibung: Punkt als
    Tausendertrennung.
    """
    from swarmq_dashboard.web.templates import zahl

    assert zahl(0) == "0"
    assert zahl(999) == "999"
    assert zahl(1200) == "1.200"
    assert zahl(1234567) == "1.234.567"
    # Kein Wert ist **nicht** null: eine Null, wo eine Auskunft fehlt, ist
    # die gefaehrlichste stille Falschaussage dieser Oberflaeche.
    assert zahl(None) == "--"

    # ``Gruppe.belegt()`` gibt "2 von 4" zurueck, nicht 2. Der erste Einbau
    # schickte das durch ``zahl`` -- die Worker-Seite antwortete mit 500,
    # und die Meldung sprach von Formatangaben. Wer sie liest, soll lesen,
    # was wirklich los ist.
    with pytest.raises(TypeError, match="ganze Zahl"):
        zahl("2 von 4")  # type: ignore[arg-type]

    class _Stats:
        exists, error, depth, consumers, dead_letter = True, None, 12000, 3, 4200

    html = render(
        "worker/uebersicht.html", status=_zustand(), account="ops", aktiv="worker",
        csrf_token="t", gruppen=[], queues=[{"name": "reports", "stats": _Stats(),
                                             "ohne_hook": 0, "bekannt": True}],
        jetzt=1_700_000_000.0, quellenname={}, ohne_kontrollkanal="K",
        leer_titel="T", leer_ursache="U",
    )
    assert ">12.000<" in html, "der Rueckstau steht ungetrennt da"


def test_fremdsprachiger_text_ist_als_solcher_ausgezeichnet() -> None:
    """"Error 111 connecting to 127.0.0.1:1" in einer Seite mit ``lang="de"``.

    Ein Vorleseprogramm spricht den Satz dann mit deutscher Aussprache --
    aus "connecting" wird etwas, das niemand wiedererkennt. Die Gruende
    kommen aus aio-pika und valkey und sind englisch.
    """
    weg = _zustand(broker={"ok": False, "reason": "Error 111 connecting to 127.0.0.1"})
    html = render("base.html", status=weg, account="ops", aktiv="worker")
    treffer = re.search(r'<span class="muted"[^>]*>Error 111', html)
    assert treffer, f"der Grund steht nicht mehr in .muted: {html[:200]}"
    assert 'lang="en"' in treffer.group(0), (
        f"der englische Grund ist nicht ausgezeichnet: {treffer.group(0)}"
    )
