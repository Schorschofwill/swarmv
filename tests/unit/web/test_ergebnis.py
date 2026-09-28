"""U1 -- der Ergebnis-Leser: eine Auskunft, kein geworfener Fehler.

Der Leser holt das Ergebnis eines Tasks ueber die oeffentliche API der
Bibliothek und gibt es als **Zustand** zurueck. Das ist der ganze Zuschnitt:
``get_result`` wirft bei Storno und Fehler, und genau das ist hier die
Auskunft, kein Fehlerfall (KTD2).

Testliste (AGENTS.md). Erledigtes bleibt stehen, damit die Reihenfolge der
Zyklen nachvollziehbar ist.

Das Lesen selbst (U1 Punkte 2, 3, 5):
  1. [x] ein vorliegendes Ergebnis -> zustand "da", art "fertig", Klartext
  2. [x] ResultNotReadyError -> zustand "fehlt", keine Ausnahme
  3. [x] Darstellung als JSON wie in der Argumente-Karte, nicht als repr
  4. [x] was JSON nicht kodieren kann, faellt auf repr zurueck
  5. [x] ein Objekt, dessen repr wirft, ergibt trotzdem eine Auskunft
  6. [x] ueber der Grenze: abgeschnitten=True, groesse = volle UTF-8-Laenge

Ausnahmen auf Zustaende abbilden (U1 Punkte 4, 4a, 6):
  7. [x] TaskError -> art "gescheitert" mit dem gespeicherten Text
  8. [x] TaskCancelledError -> art "storniert", obwohl Unterklasse von TaskError
  9. [x] ResultSerializationError -> zustand "nicht_lesbar", nicht "gescheitert"
 10. [x] Verbindungsfehler -> zustand "nicht_erreichbar" mit Grund
 11. [x] ein gescheiterter Task mit uebergrossem Text behaelt art "gescheitert"

Sperrfrist (KTD1):
 12. [x] nach gescheitertem Verbinden wird innerhalb der Frist nicht gewaehlt
 13. [x] nach gescheitertem **Lesen** wird gar nicht mehr zugegriffen
 14. [x] nach Ablauf der Frist wird wieder versucht

Gemeinsame Basis (Aufraeumen U3; R1, R2):
 15. [x] zwei gleichzeitige erste Lesungen bauen genau eine Verbindung
 16. [x] scheitert der ``ping()`` des eigenen Klienten, wird er genau einmal
         geschlossen und nicht behalten
 17. [x] nach gescheitertem Aufbau steht der bisherige Sperrtext da
 18. [x] ``close()`` vertraegt eine App mit synchronem und mit asynchronem
         ``close``

Zwischenspeicher fuer beendete Ergebnisse (Aufraeumen U4; R3, KTD3):
 19. [x] zweimal dieselbe fertige Kennung -> ein ``get_result``, zweimal
         dasselbe Bild
 20. [x] storniert wird ebenso gespeichert
 21. [x] gescheitert wird bei jeder Anfrage neu gelesen (DLQ-Requeue)
 22. [x] fehlt, nicht_lesbar und nicht_erreichbar werden neu gelesen
 23. [x] ein fertiges Ergebnis, das danach im Backend verschwindet, bleibt
         fertig (die benannte Ausnahme der Product authority)
 24. [x] ueber die Obergrenze faellt der am laengsten ungenutzte Eintrag
         heraus und wird bei Bedarf neu gelesen; ein Treffer frischt auf
 25. [x] ein Treffer verbindet nicht -- auch nicht waehrend der Sperre
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swarmq_dashboard.web.ergebnis import SwarmQErgebnisleser


class _App:
    """Was der Leser von der Bibliothek braucht: ``get_result``.

    Entweder ein Wert oder eine Ausnahme -- die Bibliothek tut beides, und
    beides ist hier eine Auskunft.
    """

    def __init__(self, wert: Any = None, *, wirft: BaseException | None = None) -> None:
        self._wert = wert
        self._wirft = wirft
        self.rufe: list[tuple[str, float]] = []

    async def get_result(self, task_id: str, timeout: float = 10) -> Any:
        self.rufe.append((task_id, timeout))
        if self._wirft is not None:
            raise self._wirft
        return self._wert


@pytest.mark.timeout(5)
async def test_ein_vorliegendes_ergebnis_kommt_als_da() -> None:
    """Der einfachste Fall: es liegt etwas vor, und es steht da."""
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=_App({"pid": 42}))

    bild = await leser.lesen("t1")

    assert bild.zustand == "da"
    assert bild.art == "fertig"
    assert "42" in bild.text


@pytest.mark.timeout(5)
async def test_ein_fehlendes_ergebnis_ist_kein_fehler() -> None:
    """``ResultNotReadyError`` heisst hier nicht "noch nicht", sondern "weg".

    Mit ``timeout=0`` wartet die Bibliothek nicht: sie holt einmal und wirft,
    wenn nichts da ist. Fuer einen **beendeten** Task -- und nur fuer den wird
    gelesen -- bedeutet das, dass das Ergebnis abgelaufen ist oder nie
    abgelegt wurde. Beides sagt die Seite; entscheiden kann sie es nicht.
    """
    from swarmq.exceptions import ResultNotReadyError

    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=_App(wirft=ResultNotReadyError("t1")))

    bild = await leser.lesen("t1")

    assert bild.zustand == "fehlt"
    assert bild.art is None


@pytest.mark.timeout(5)
async def test_die_darstellung_ist_json_wie_bei_den_argumenten() -> None:
    """Dieselbe Form wie die Karte darueber, nicht Python-Quelltext.

    Die Argumente werden in ``directory/writer.py`` mit
    ``json.dumps(..., ensure_ascii=False, sort_keys=True)`` abgelegt. Stuende
    das Ergebnis als ``repr`` darunter, zeigte dieselbe Seite zweimal
    dieselbe Art Daten in zwei Schreibweisen -- einfache Anfuehrungszeichen,
    ``True`` statt ``true``, unsortierte Schluessel.
    """
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=_App({"pid": 42, "ok": True, "note": "grün"}))

    bild = await leser.lesen("t1")

    assert bild.text == '{"note": "grün", "ok": true, "pid": 42}'


class _Eigen:
    """Etwas, das JSON nicht kennt -- msgpack-Ext-Typen der Bibliothek etwa."""

    def __repr__(self) -> str:
        return "<Eigen: 7>"


@pytest.mark.timeout(5)
async def test_was_json_nicht_kodieren_kann_faellt_auf_repr_zurueck() -> None:
    """Die Rueckfallebene, nicht der Regelfall.

    Die Bibliothek entpackt zwar nur Builtins und ihre eigenen Ext-Typen,
    aber ein Ext-Typ ist eben kein JSON-Wert. Lieber ``repr`` als eine leere
    Karte.
    """
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=_App(_Eigen()))

    bild = await leser.lesen("t1")

    assert bild.text == "<Eigen: 7>"
    assert bild.zustand == "da"


class _ReprWirft:
    """Ein Objekt, das sich selbst nicht beschreiben kann."""

    def __repr__(self) -> str:
        raise RuntimeError("kein repr")


@pytest.mark.timeout(5)
async def test_ein_objekt_dessen_repr_wirft_ergibt_trotzdem_eine_auskunft() -> None:
    """Die letzte Rueckfallebene: der Typname.

    Sonst wuerde die Detailseite an der Darstellung scheitern -- eine
    Fehlerseite statt der Auskunft, wegen der sie geoeffnet wurde.
    """
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=_App(_ReprWirft()))

    bild = await leser.lesen("t1")

    assert bild.zustand == "da"
    assert "_ReprWirft" in bild.text


@pytest.mark.timeout(5)
async def test_ueber_der_grenze_wird_abgeschnitten_und_die_laenge_genannt() -> None:
    """Die genannte Zahl ist die Laenge der **Darstellung** (KTD3).

    Nicht die des im Backend abgelegten Pakets: ``get_result`` gibt das
    entpackte Objekt zurueck, an die Rohbytes kommt das Dashboard nach KD2
    nicht heran. Wer die Zahl als Speicherbelegung liest, liest sie falsch --
    deshalb steht das so auch auf der Seite.
    """
    from swarmq_dashboard.web.ergebnis import GRENZE_B

    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=_App("ä" * 20_000))

    bild = await leser.lesen("t1")

    assert bild.abgeschnitten is True
    assert len(bild.text.encode("utf-8")) <= GRENZE_B
    # 20000 Umlaute zu je zwei Bytes, dazu die beiden Anfuehrungszeichen.
    assert bild.groesse == 40_002


@pytest.mark.timeout(5)
async def test_ein_gescheiterter_task_zeigt_seinen_gespeicherten_fehlertext() -> None:
    """``_unpack`` wirft ``TaskError`` -- **eine feste Klasse** (KTD2).

    Nicht die Ausnahme des Tasks: die Bibliothek legt nur deren Text ab und
    baut daraus beim Lesen ein ``TaskError``. Wer die Testdaten um den
    eigenen Ausnahmetyp herum baut, prueft gegen eine Form, die die
    Bibliothek nie erzeugt -- das war der schwerste Befund im Review des
    Plans.

    Der Task hat ein Ergebnis, es ist nur ein schlechtes: ``zustand`` bleibt
    "da", die Art traegt die Aussage.
    """
    from swarmq.exceptions import TaskError

    leser = SwarmQErgebnisleser(
        "amqp://x", "valkey://x", app=_App(wirft=TaskError("ZeroDivisionError: division by zero")),
    )

    bild = await leser.lesen("t1")

    assert bild.zustand == "da"
    assert bild.art == "gescheitert"
    assert "division by zero" in bild.text


@pytest.mark.timeout(5)
async def test_ein_stornierter_task_verschwindet_nicht_im_fehlerzweig() -> None:
    """``TaskCancelledError`` ist **Unterklasse** von ``TaskError`` (KTD2).

    Wer den allgemeinen Fehlerfall zuerst faengt, verliert den Storno-Zustand
    still: Die Seite zeigte "gescheitert", wo der Task auf Anforderung
    abgebrochen wurde -- eine Falschaussage ueber die Anwendung. Die
    Bibliothek selbst gibt dem Storno Vorrang, auch wenn zugleich ein Fehler
    vermerkt ist.
    """
    from swarmq.exceptions import TaskCancelledError, TaskError

    assert issubclass(TaskCancelledError, TaskError), "die Falle ist weggefallen"

    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=_App(wirft=TaskCancelledError("t1")))

    bild = await leser.lesen("t1")

    assert bild.art == "storniert"


@pytest.mark.timeout(5)
async def test_ein_nicht_entpackbares_ergebnis_ist_keine_aussage_ueber_den_task() -> None:
    """``ResultSerializationError`` ist weder Task- noch Verbindungsfehler.

    Sie faellt sonst in einen der beiden Sammelfaenge, und die Seite
    behauptete "der Task ist gescheitert" oder "Backend nicht erreichbar" --
    obwohl der Task erfolgreich war und das Backend antwortete. Die Aussage
    gilt der **Ablage**, nicht der Anwendung.

    Erreichbar ueber Versionsversatz: Das Dashboard haengt an seiner eigenen
    swarmq-Fassung, waehrend die Anwendung die Ergebnisse schreibt, und die
    Bibliothek wirft bei unbekanntem Ext-Code bewusst laut.
    """
    from swarmq.exceptions import ResultSerializationError

    leser = SwarmQErgebnisleser(
        "amqp://x", "valkey://x", app=_App(wirft=ResultSerializationError("unknown ext code 7")),
    )

    bild = await leser.lesen("t1")

    assert bild.zustand == "nicht_lesbar"
    assert bild.art is None
    assert "ext code 7" in (bild.grund or "")


@pytest.mark.timeout(5)
async def test_ein_totes_backend_ist_eine_auskunft_keine_fehlerseite() -> None:
    """R5: Ein Fehlschlag beim Nachschlagen haelt die Seite nicht auf.

    Die Detailseite traegt Argumente, Signalfolge und den Stornier-Knopf --
    sie muss vollstaendig laden, auch wenn das Backend weg ist. Der Grund
    steht in der Karte, nicht in einer Fehlerseite.
    """
    async def verbinden() -> Any:
        raise OSError("connection refused")

    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", verbinden=verbinden)

    bild = await leser.lesen("t1")

    assert bild.zustand == "nicht_erreichbar"
    assert "refused" in (bild.grund or "")


@pytest.mark.timeout(5)
async def test_abschneiden_verdraengt_die_art_nicht() -> None:
    """AE4 und AE5 zugleich -- der Fall, an dem der Plan zerbrach.

    Solange ``zu_gross`` ein **Zustand** neben ``art`` war, galten fuer einen
    gescheiterten Task mit langem Traceback beide zugleich und keine
    Vorrangregel. Wer "wartend" waehlte, zeigte einen beendeten Fehlschlag als
    wartend; wer "gescheitert" waehlte, verlor die Abschneide-Kennzeichnung.
    Abschneiden ist darum eine Eigenschaft des Textes, kein Zustand.
    """
    from swarmq.exceptions import TaskError

    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=_App(wirft=TaskError("x" * 20_000)))

    bild = await leser.lesen("t1")

    assert bild.art == "gescheitert"
    assert bild.abgeschnitten is True
    assert bild.groesse == 20_000


class _Uhr:
    """Eine Uhr, die nur vorgestellt wird -- kein ``sleep`` im Test."""

    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


@pytest.mark.timeout(5)
async def test_nach_gescheitertem_verbinden_wird_innerhalb_der_frist_nicht_gewaehlt() -> None:
    """KTD1: Sonst bezahlt jeder Seitenaufruf denselben Zeitablauf neu.

    Gemessen kostete ein Versuch gegen ein totes Backend Sekunden -- waehrend
    des Vorfalls, auf der Seite, die man dann oeffnet.
    """
    versuche: list[int] = []

    async def verbinden() -> Any:
        versuche.append(1)
        raise OSError("connection refused")

    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())

    await leser.lesen("t1")
    zweites = await leser.lesen("t2")

    assert len(versuche) == 1, "das tote Backend wurde erneut gewaehlt"
    assert zweites.zustand == "nicht_erreichbar"


@pytest.mark.timeout(5)
async def test_nach_gescheitertem_lesen_wird_gar_nicht_mehr_zugegriffen() -> None:
    """Der Fall, den das Review am Plan fand -- und der haeufigste im Betrieb.

    Stirbt das Backend, **nachdem** der Leser einmal verbunden war, kehrt
    ``connect()`` weiterhin sofort zurueck: Die Verbindung ist ja gesetzt. Die
    Sperre greift dann nie, und jeder einzelne Seitenaufruf laeuft erneut in
    die volle Frist -- genau in dem Vorfall, fuer den das Dashboard gebaut
    ist. Ein gescheitertes Lesen muss die Verbindung darum verwerfen und
    dieselbe Sperre setzen wie ein gescheiterter Verbindungsaufbau.
    """
    app = _App(wirft=OSError("connection lost"))
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=app, uhr=_Uhr())

    erstes = await leser.lesen("t1")
    zweites = await leser.lesen("t2")

    assert erstes.zustand == "nicht_erreichbar"
    assert "connection lost" in (erstes.grund or "")
    assert len(app.rufe) == 1, "das tote Backend wurde erneut gelesen"
    assert zweites.zustand == "nicht_erreichbar"


@pytest.mark.timeout(5)
async def test_nach_ablauf_der_frist_wird_wieder_versucht() -> None:
    """Die Sperre daempft, sie schaltet nicht ab.

    Kaeme das Backend zurueck und die Seite bliebe bei ihrer alten Auskunft,
    waere das genau die stille Falschaussage, gegen die R33 steht.
    """
    from swarmq_dashboard.web.ergebnis import SPERRE_S

    versuche: list[int] = []
    uhr = _Uhr()

    async def verbinden() -> Any:
        versuche.append(1)
        if len(versuche) == 1:
            raise OSError("connection refused")
        return _App({"pid": 7})

    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=uhr)

    assert (await leser.lesen("t1")).zustand == "nicht_erreichbar"
    uhr.jetzt += SPERRE_S + 1
    bild = await leser.lesen("t2")

    assert len(versuche) == 2, "nach Ablauf der Frist wurde nicht erneut gewaehlt"
    assert bild.zustand == "da"


class _WartendesVerbinden:
    """Ein Verbindungsaufbau, der haengt, bis der Test ihn freigibt."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.versuche = 0
        self.begonnen = asyncio.Event()
        self.frei = asyncio.Event()

    async def __call__(self) -> Any:
        self.versuche += 1
        self.begonnen.set()
        await self.frei.wait()
        return self._app


@pytest.mark.timeout(5)
async def test_zwei_gleichzeitige_erste_lesungen_bauen_eine_verbindung() -> None:
    """R1: Workflow-Detail und Glieder-Seite lesen nebenlaeufig. Ohne Riegel
    baute jede erste Lesung einen eigenen Klienten, und ``close()``
    schloesse nur den letzten -- die anderen leckten."""
    verbinden = _WartendesVerbinden(_App({"pid": 1}))
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())

    erste = asyncio.create_task(leser.lesen("t1"))
    zweite = asyncio.create_task(leser.lesen("t2"))
    await verbinden.begonnen.wait()
    verbinden.frei.set()
    bilder = await asyncio.gather(erste, zweite)

    assert verbinden.versuche == 1
    assert [b.zustand for b in bilder] == ["da", "da"]


class _KlientOhnePing:
    """Ein Valkey-Klient, dessen ``ping()`` scheitert."""

    def __init__(self) -> None:
        self.geschlossen = 0

    async def ping(self) -> None:
        raise ConnectionError("Port 6399 zu")

    async def aclose(self) -> None:
        self.geschlossen += 1


@pytest.mark.timeout(5)
async def test_ein_gescheiterter_ping_schliesst_den_eigenen_klienten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1: Der Klient wird erst nach dem ``ping()`` behalten. Scheitert er,
    wird er sofort geschlossen -- und ``close()`` schliesst ihn nicht ein
    zweites Mal. Vorher blieb er offen liegen, bis zufaellig jemand
    ``close()`` rief."""
    import valkey.asyncio

    klient = _KlientOhnePing()
    monkeypatch.setattr(valkey.asyncio.Valkey, "from_url", lambda *a, **k: klient)
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", uhr=_Uhr())

    bild = await leser.lesen("t1")
    assert bild.zustand == "nicht_erreichbar"
    assert klient.geschlossen == 1

    await leser.close()
    assert klient.geschlossen == 1


@pytest.mark.timeout(5)
async def test_nach_gescheitertem_aufbau_steht_der_bisherige_sperrtext_da() -> None:
    """R2: Der Wortlaut des Sperrgrunds bleibt -- die Karte spricht vom
    Backend, beim ersten Versuch wie waehrend der Sperre."""
    async def verbinden() -> Any:
        raise OSError("connection refused")

    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())

    erstes = await leser.lesen("t1")
    zweites = await leser.lesen("t2")

    assert erstes.grund == "das Backend ist nicht erreichbar: connection refused"
    assert zweites.grund == erstes.grund


class _SyncSchliessbareApp(_App):
    """Eine App mit synchronem ``close``."""

    def __init__(self, *, wirft: bool = False) -> None:
        super().__init__({"pid": 1})
        self._close_wirft = wirft
        self.geschlossen = 0

    def close(self) -> None:
        self.geschlossen += 1
        if self._close_wirft:
            raise RuntimeError("schon zu")


class _AsyncSchliessbareApp(_SyncSchliessbareApp):
    """Eine App mit asynchronem ``close``."""

    async def close(self) -> None:  # type: ignore[override]
        super().close()


@pytest.mark.timeout(5)
@pytest.mark.parametrize("art", [_SyncSchliessbareApp, _AsyncSchliessbareApp],
                         ids=["synchron", "asynchron"])
@pytest.mark.parametrize("wirft", [False, True], ids=["sauber", "close_wirft"])
async def test_close_vertraegt_synchrones_und_asynchrones_close(
    art: type[_SyncSchliessbareApp], wirft: bool,
) -> None:
    """R2: ``close()`` gehoert in die Schliessliste von ``Services.shutdown()``
    und wirft nie -- gleich, ob die App synchron oder asynchron schliesst.
    Ein zweites ``close()`` schliesst nicht noch einmal."""
    app = art(wirft=wirft)
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", verbinden=lambda: _sofort(app), uhr=_Uhr())
    await leser.lesen("t1")

    await leser.close()
    await leser.close()

    assert app.geschlossen == 1


async def _sofort(app: Any) -> Any:
    return app


# --- Zwischenspeicher (Aufraeumen U4) ------------------------------------


class _Wechselapp(_App):
    """Eine App, deren Antwort der Test zwischen zwei Lesungen umstellt."""

    def umstellen(self, wert: Any = None, *, wirft: BaseException | None = None) -> None:
        self._wert = wert
        self._wirft = wirft


@pytest.mark.timeout(5)
async def test_ein_fertiges_ergebnis_wird_nur_einmal_gelesen() -> None:
    """R3: ein fertiges Ergebnis aendert sich nicht mehr. Jede Live-Meldung
    rendert die Detailseite neu -- ohne Speicher je Kopf-Glied eine Lesung."""
    app = _App({"pid": 42})
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=app)

    erstes = await leser.lesen("t1")
    zweites = await leser.lesen("t1")

    assert len(app.rufe) == 1
    assert zweites == erstes
    assert zweites.art == "fertig"


@pytest.mark.timeout(5)
async def test_ein_storniertes_ergebnis_wird_nur_einmal_gelesen() -> None:
    """R3: auch ein Storno ist endgueltig -- die Bibliothek reiht einen
    stornierten Task nicht wieder ein."""
    from swarmq.exceptions import TaskCancelledError

    app = _App(wirft=TaskCancelledError("t1"))
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=app)

    erstes = await leser.lesen("t1")
    zweites = await leser.lesen("t1")

    assert len(app.rufe) == 1
    assert zweites == erstes
    assert zweites.art == "storniert"


@pytest.mark.timeout(5)
async def test_ein_gescheitertes_ergebnis_wird_jedes_mal_gelesen() -> None:
    """KTD3: ein Requeue aus der Dead-Letter-Queue reiht dieselbe Kennung neu
    ein, und ein erfolgreicher Lauf ueberschreibt das Fehlerergebnis. Stuende
    "gescheitert" im Speicher, zeigte die Seite den alten Fehler weiter."""
    from swarmq.exceptions import TaskError

    app = _Wechselapp(wirft=TaskError("ZeroDivisionError: division by zero"))
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=app)

    erstes = await leser.lesen("t1")
    app.umstellen({"pid": 7})
    zweites = await leser.lesen("t1")

    assert erstes.art == "gescheitert"
    assert len(app.rufe) == 2
    assert zweites.art == "fertig"


def _ausnahme(name: str) -> BaseException:
    """Die Ausnahme, mit der die Bibliothek bzw. die Leitung antwortet."""
    from swarmq.exceptions import ResultNotReadyError, ResultSerializationError

    return {
        "fehlt": ResultNotReadyError("t1"),
        "nicht_lesbar": ResultSerializationError("unknown ext code 7"),
        "nicht_erreichbar": OSError("connection lost"),
    }[name]


@pytest.mark.timeout(5)
@pytest.mark.parametrize("zustand", ["fehlt", "nicht_lesbar", "nicht_erreichbar"])
async def test_ohne_endgueltiges_ergebnis_wird_jedes_mal_gelesen(zustand: str) -> None:
    """R3: nur ein Ergebnis, das sich nicht mehr aendert, steht im Speicher.
    Ein fehlendes kann noch kommen, ein unlesbares nach einem Update lesbar
    werden, und ein totes Backend kehrt zurueck."""
    from swarmq_dashboard.web.ergebnis import SPERRE_S

    app = _Wechselapp(wirft=_ausnahme(zustand))
    uhr = _Uhr()
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", verbinden=lambda: _sofort(app), uhr=uhr)

    erstes = await leser.lesen("t1")
    app.umstellen({"pid": 7})
    # Ueber eine etwaige Sperre hinaus -- sie ist nicht, was hier geprueft wird.
    uhr.jetzt += SPERRE_S + 1
    zweites = await leser.lesen("t1")

    assert erstes.zustand == zustand
    assert len(app.rufe) == 2
    assert zweites.art == "fertig"


@pytest.mark.timeout(5)
async def test_ein_verfallenes_fertiges_ergebnis_bleibt_fertig() -> None:
    """Die benannte Ausnahme der Product authority (KTD3): verfaellt ein
    gelesenes fertiges Ergebnis in Valkey, zeigt der Speicher es weiter --
    bis zum Neustart des Dienstes, der dann "abgelaufen" zeigt."""
    from swarmq.exceptions import ResultNotReadyError

    app = _Wechselapp({"pid": 42})
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=app)

    erstes = await leser.lesen("t1")
    app.umstellen(wirft=ResultNotReadyError("t1"))
    zweites = await leser.lesen("t1")

    assert zweites == erstes
    assert zweites.zustand == "da" and zweites.art == "fertig"


@pytest.mark.timeout(5)
async def test_ueber_der_obergrenze_faellt_der_aelteste_eintrag_heraus() -> None:
    """R3: der Speicher ist nach Eintraegen begrenzt -- ein langlebiger Dienst
    saehe sonst jede je gelesene Kennung. Was herausfaellt, wird bei Bedarf
    wieder gelesen."""
    app = _App({"pid": 1})
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=app, speicher_n=2)

    for kennung in ("t1", "t2", "t3", "t2", "t3", "t1"):
        await leser.lesen(kennung)

    assert [k for k, _ in app.rufe] == ["t1", "t2", "t3", "t1"]


@pytest.mark.timeout(5)
async def test_ein_treffer_haelt_seinen_eintrag_im_speicher() -> None:
    """R3, KTD3 (LRU): wer gerade gelesen wird, faellt nicht heraus. Die
    Kopf-Glieder einer offenen Detailseite werden bei jeder Live-Meldung
    getroffen; ein FIFO verdraengte sie trotzdem, sobald andere Seiten genug
    neue Kennungen lesen."""
    app = _App({"pid": 1})
    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", app=app, speicher_n=2)

    for kennung in ("t1", "t2", "t1", "t3", "t1", "t2"):
        await leser.lesen(kennung)

    assert [k for k, _ in app.rufe] == ["t1", "t2", "t3", "t2"]


@pytest.mark.timeout(5)
async def test_ein_treffer_verbindet_nicht_auch_nicht_waehrend_der_sperre() -> None:
    """KTD3: der Speicher sitzt **vor** dem Verbindungsaufbau. Faellt das
    Backend aus, bleiben die schon gelesenen fertigen Glieder sichtbar, statt
    mit "nicht erreichbar" zu verschwinden -- und kein Treffer waehlt neu."""
    versuche: list[int] = []
    app = _Wechselapp({"pid": 42})

    async def verbinden() -> Any:
        versuche.append(1)
        return app

    leser = SwarmQErgebnisleser("amqp://x", "valkey://x", verbinden=verbinden, uhr=_Uhr())

    erstes = await leser.lesen("t1")
    app.umstellen(wirft=OSError("connection lost"))
    assert (await leser.lesen("t2")).zustand == "nicht_erreichbar"
    zweites = await leser.lesen("t1")

    assert zweites == erstes
    assert len(versuche) == 1
