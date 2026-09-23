"""``live.js`` wird ausgefuehrt, nicht gelesen (Frontend-Review).

Die Datei war bis hierher ausschliesslich per Zeichenkettenvergleich
geprueft -- ``assert 'addEventListener("reload"' in skript``. Genau deshalb
blieb der schwerste Fehler des Projekts unentdeckt: ``swap: "morph"``
bedeutet bei Idiomorph ``morphStyle: "outerHTML"``, das Ziel war ``<body>``,
und nach dem ersten Ereignis gab es keinen Koerper mehr. Der Aufruf ist
syntaktisch tadellos und zerbricht erst im DOM.

Hier laeuft die Datei unveraendert in einer Node-Umgebung gegen nachgebaute
``window``/``document``/``EventSource``/``htmx``-Objekte. Das faengt nicht
alles -- der Koerper-Fehler braucht ein echtes DOM --, aber es faengt die
Logik: welche Ereignisse anstossen, welche nicht, und womit getauscht wird.

Test-Liste (AGENTS.md):

1. Ein gewoehnliches Ereignis stoesst genau einen Nachladevorgang an.
2. Eine **unbekannte** Ereignisart ebenso -- die Liste ist offen.
3. Der Herzschlag stoesst nichts an.
4. Getauscht wird mit ``morph:innerHTML``, nie mit ``morph`` allein.
5. Waehrend ein Nachladen laeuft, beginnt kein zweites.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from swarmq_dashboard.web.templates import STATIC_DIR

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node ist nicht verfuegbar")

_TREIBER = textwrap.dedent("""
    const fs = require('fs'), vm = require('vm');
    const quelle = fs.readFileSync(process.argv[2], 'utf8');
    const rufe = [], hoerer = {};
    const el = { textContent: '', hidden: true, tagName: 'SPAN' };
    const ctx = {
      console,
      document: {
        hidden: false, activeElement: null,
        addEventListener: () => {},
        querySelector: () => el,
      },
      location: { href: 'http://x/tasks', reload: () => rufe.push('reload') },
      // Das Fenster nimmt Hoerer entgegen wie im Browser. Ohne das schlug
      // die Quelle beim Anmelden von ``pagehide`` fehl -- eine Luecke der
      // Attrappe, die aussah wie ein Fehler im Skript.
      addEventListener: () => {},
      setTimeout: (fn) => { fn(); return 1; },
      clearTimeout: () => {},
      EventSource: function () {
        this.readyState = 1;
        this.addEventListener = (art, fn) => { (hoerer[art] ||= []).push(fn); };
        this.dispatchEvent = () => true;
        this.close = () => {};
      },
      htmx: { ajax: (m, u, o) => { rufe.push('ajax:' + o.swap); return Promise.resolve(); } },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(quelle, ctx);
    // Die Quelle ersetzt dispatchEvent; darueber laeuft die Verteilung.
    const es = ctx.__es;
    const feuer = (art) => ctx.__quelle.dispatchEvent({ type: art });
    for (const art of JSON.parse(process.argv[3])) feuer(art);
    console.log(JSON.stringify(rufe));
""")


def _lauf(arten: list[str]) -> list[str]:
    """``live.js`` mit diesen Ereignisarten laufen lassen."""
    treiber = Path(__file__).parent / "_live_treiber.js"
    quelle = (STATIC_DIR / "live.js").read_text(encoding="utf-8")
    # Die Instanz nach aussen reichen, damit der Treiber sie ansprechen kann.
    quelle = quelle.replace("var quelle = new EventSource(\"/live\");",
                            "var quelle = new EventSource(\"/live\");\n  window.__quelle = quelle;")
    gepatcht = treiber.parent / "_live_gepatcht.js"
    gepatcht.write_text(quelle, encoding="utf-8")
    treiber.write_text(_TREIBER, encoding="utf-8")
    try:
        ergebnis = subprocess.run(
            [node or "node", str(treiber), str(gepatcht), json.dumps(arten)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return list(json.loads(ergebnis.stdout.strip().splitlines()[-1]))
    finally:
        treiber.unlink(missing_ok=True)
        gepatcht.unlink(missing_ok=True)


def test_ein_gewoehnliches_ereignis_stoesst_an() -> None:
    assert _lauf(["task_completed"]) == ["ajax:morph:{morphStyle:'innerHTML',"
                                         "callbacks:{beforeNodeMorphed:window.__sqdBewahreEingabe}}"]


def test_eine_unbekannte_art_stoesst_ebenfalls_an() -> None:
    """Die Liste war frueher eine geschlossene Aufzaehlung gegen eine offene
    Menge -- ``worker_online`` und ``worker_shutdown`` fehlten darin, und ein
    Worker, der hochkam, aktualisierte die Uebersicht nicht."""
    assert _lauf(["gibtesnochnicht"]), "eine neue Ereignisart wird ignoriert"


def test_der_herzschlag_stoesst_nichts_an() -> None:
    """Er traegt keine Aenderung -- er haelt nur die Leitung offen."""
    assert _lauf(["heartbeat"]) == []


def test_getauscht_wird_mit_innerhtml() -> None:
    """``morph`` allein ersetzt das Ziel. Das Ziel ist ``<body>``."""
    rufe = _lauf(["task_started"])
    assert rufe and "morphStyle:'innerHTML'" in rufe[0], rufe
    assert not any(r == "ajax:morph" for r in rufe), (
        "getauscht wird mit outerHTML -- das loescht den Koerper der Seite"
    )


_TREIBER_SEITENWECHSEL = textwrap.dedent("""
    const fs = require('fs'), vm = require('vm');
    const quelle = fs.readFileSync(process.argv[2], 'utf8');
    const rufe = [], fensterHoerer = {};
    const el = { textContent: '', hidden: true, tagName: 'SPAN' };
    const ctx = {
      console,
      document: {
        hidden: false, activeElement: null,
        addEventListener: () => {},
        querySelector: () => el,
      },
      location: { href: 'http://x/tasks', assign: () => {}, reload: () => {} },
      setTimeout: (fn) => 1, clearTimeout: () => {},
      addEventListener: (art, fn) => { (fensterHoerer[art] ||= []).push(fn); },
      EventSource: function () {
        this.readyState = 1;
        this.addEventListener = () => {};
        this.dispatchEvent = () => true;
        this.close = () => { rufe.push('close'); };
      },
      htmx: { ajax: () => Promise.resolve() },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(quelle, ctx);
    for (const fn of (fensterHoerer[process.argv[3]] || [])) fn({ persisted: true });
    console.log(JSON.stringify({ rufe, angemeldet: Object.keys(fensterHoerer) }));
""")


def _seitenwechsel(art: str) -> dict:
    """``live.js`` laden und dieses Fenster-Ereignis ausloesen."""
    treiber = Path(__file__).parent / "_live_treiber_wechsel.js"
    treiber.write_text(_TREIBER_SEITENWECHSEL, encoding="utf-8")
    quelle = Path(__file__).parent / "_live_quelle_wechsel.js"
    quelle.write_text((STATIC_DIR / "live.js").read_text(encoding="utf-8"), encoding="utf-8")
    try:
        ergebnis = subprocess.run(
            [node or "node", str(treiber), str(quelle), art],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return dict(json.loads(ergebnis.stdout.strip().splitlines()[-1]))
    finally:
        treiber.unlink(missing_ok=True)
        quelle.unlink(missing_ok=True)


def test_beim_verlassen_der_seite_wird_die_live_verbindung_geschlossen() -> None:
    """Im Betrieb gemeldet: eine Seite lud fast dreissig Sekunden.

    Ein Browser haelt je Host nur eine Handvoll gleichzeitiger
    HTTP/1.1-Verbindungen -- ueblich sechs. Eine Live-Verbindung belegt
    davon dauerhaft eine. Geht die Seite beim Zurueckspringen in den
    Back-Forward-Cache, **lebt sie samt ihrer ``EventSource`` weiter**, und
    ihre Verbindung bleibt belegt. Nach ein paar Wechseln sind alle Slots
    voll, und die naechste Seite wartet, bis eine frei wird.

    ``pagehide`` ist das Ereignis, das dabei zuverlaessig feuert --
    ``beforeunload`` und ``unload`` verhindern den Cache oder bleiben aus.
    """
    ergebnis = _seitenwechsel("pagehide")
    assert "pagehide" in ergebnis["angemeldet"], (
        f"kein pagehide-Hoerer angemeldet: {ergebnis['angemeldet']}"
    )
    assert "close" in ergebnis["rufe"], (
        "die Verbindung bleibt beim Verlassen der Seite offen"
    )
