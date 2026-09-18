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
    for punkt in re.finditer(r'<span class="dot[^"]*"[^>]*></span>\s*([^<]*)', html):
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
    assert "Luecke" in html, "die Kopfzeile schweigt ueber die Luecken"
    assert "2" in html
