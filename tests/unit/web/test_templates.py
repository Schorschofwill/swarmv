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
