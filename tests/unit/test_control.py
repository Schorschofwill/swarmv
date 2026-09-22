"""U16 -- Sollwert-Steuerung: Zustand, Pruefung, Gruppenbild.

Testliste (AGENTS.md). Erledigtes bleibt stehen, damit die Reihenfolge der
Zyklen nachvollziehbar ist.

Zustandsableitung aus der Rueckmeldung (KTD13, R17) -- zuerst, weil sich hier
entscheidet, ob Ablehnung und Warten auseinandergehen:
  1. [x] source "setpoint", setpoint == gesendet, rejection None -> bestaetigt
  2. [x] rejection gesetzt -> abgelehnt, Grund im Wortlaut
  3. [x] setpoint noch der alte Wert, innerhalb der Frist -> gesendet
  4. [x] setpoint noch der alte Wert, Frist abgelaufen -> unbestaetigt
  5. [x] rejection schlaegt die Frist: abgelehnt bleibt abgelehnt
  6. [x] gar keine Meldung (TTL abgelaufen) -> unbestaetigt nach Frist
  7. [x] store "unread" -> Backend nicht erreichbar, nicht "bestaetigt"
  8. [x] unlesbarer Inhalt: setpoint null, rejection "unreadable: ..." -> abgelehnt

Gruppenbild ueber mehrere Supervisoren (R15a, R15b):
  9. [x] Spielraum, Slots und Worker summieren sich ueber die Supervisoren
 10. [x] zwei Supervisoren, einer bestaetigt -> Verhaeltnis statt "bestaetigt"
 11. [x] verschiedener Spielraum -> uneinheitlich
 12. [x] verschiedene Autoskalierung -> uneinheitlich
 13. [x] kein meldender Supervisor -> wirkungsunbekannt

Pruefung vor dem Schreiben (R15b, R15):
 14. [x] Wert ueber dem kleinsten Spielraum der Gruppe -> abgelehnt, ungeschrieben
 15. [x] Bereich auf einer Gruppe ohne Autoskalierung -> abgelehnt
 16. [x] Untergrenze unter 1 -> abgelehnt
 17. [x] verdrehte Grenzen -> abgelehnt
 18. [x] ohne meldenden Supervisor wird geschrieben, aber wirkungsunbekannt
 19. [x] gueltiger Wert auf einheitlicher Gruppe -> angenommen

Gesamtzahl je Gruppe (R15a, AE4):
 20. [x] "je Supervisor" mal Zahl der Supervisoren ergibt die Gesamtzahl

Der Kanal selbst (U16 Punkt 1 und 2):
 21. [x] fehlender Kontrollkanal wird benannt statt geworfen (AE15)
 22. [x] die Steuerung sammelt die Meldungen einer Gruppe
 23. [x] setzen schreibt und merkt den Sendezeitpunkt
 24. [x] ein abgelehnter Wert erreicht den Kanal gar nicht
 25. [x] eine Ablehnung raeumt den Sollwert aus dem Kanal (R16a)
 26. [x] ohne gemerkten Sendezeitpunkt gibt es kein "unbestaetigt"
 27. [x] aufheben loescht Sollwert und Zeitpunkt
 28. [x] ohne Kontrollkanal bleibt die Steuerung stumm statt zu werfen
 29. [x] Supervisoren werden ueber den Statusschluesselraum gefunden
 30. [x] ein Gruppenname mit Sternchen sammelt keine fremden Supervisoren
 31. [x] der Sollwert wird in den Typ der Bibliothek uebersetzt
 32. [x] ein unlesbarer Sollwert im Kanal liest sich als keiner
 33. [x] ein nicht erreichbarer Kanal wird zur Auskunft, nicht zur Ausnahme
 34. [x] ein toter Kanal wird nicht bei jedem Aufruf neu gewaehlt
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from swarmq_dashboard.control import (
    SPERRE_S,
    Sollwert,
    Steuergruppe,
    Steuerung,
    SwarmQSetpointStore,
    Zustand,
    bewerten,
    kontrollkanal_grund,
)
from swarmq_dashboard.stream import StreamUnavailable


def test_bestaetigt_wenn_der_supervisor_den_gesendeten_wert_als_wirksam_meldet() -> None:
    """Lesart aus der Schnittstelle: setpoint == gesendet, source "setpoint", keine Ablehnung."""
    meldung = {
        "group": "demo",
        "supervisor": "demo:p1",
        "setpoint": {"min": 4, "max": 4},
        "effective": {"min": 4, "max": 4},
        "source": "setpoint",
        "rejection": None,
        "store": "present",
    }
    ergebnis = bewerten(meldung, gesendet=Sollwert(4, 4), gesendet_at=100.0, now=101.0)
    assert ergebnis.zustand is Zustand.BESTAETIGT


def test_abgelehnt_wenn_die_rueckmeldung_einen_grund_traegt() -> None:
    """R16a: eine Ablehnung ist ein eigener, dauerhafter Zustand -- keine ausstehende Bestaetigung."""
    meldung = {
        "setpoint": {"min": 2, "max": 12},
        "effective": {"min": 2, "max": 4},
        "source": "startup",
        "rejection": "max 12 ueber dem Spielraum 8",
        "store": "present",
    }
    ergebnis = bewerten(meldung, gesendet=Sollwert(2, 12), gesendet_at=100.0, now=101.0)
    assert ergebnis.zustand is Zustand.ABGELEHNT
    # Im Wortlaut (R17): eine eigene Formulierung verloere genau die Zahl,
    # um die es geht.
    assert ergebnis.grund == "max 12 ueber dem Spielraum 8"


def test_gesendet_solange_der_supervisor_noch_den_alten_wert_meldet() -> None:
    """Der Supervisor tastet alle 5 s ab; innerhalb der Frist ist Warten normal."""
    meldung = {
        "setpoint": {"min": 2, "max": 4},
        "effective": {"min": 2, "max": 4},
        "source": "setpoint",
        "rejection": None,
        "store": "present",
    }
    ergebnis = bewerten(meldung, gesendet=Sollwert(2, 8), gesendet_at=100.0, now=103.0)
    assert ergebnis.zustand is Zustand.GESENDET


def test_unbestaetigt_wenn_die_frist_ohne_bestaetigung_verstreicht() -> None:
    """AE6a: nach der Frist markiert die Ansicht den Wert als unbestaetigt und nennt den Sendezeitpunkt."""
    meldung = {
        "setpoint": {"min": 2, "max": 4},
        "source": "setpoint",
        "rejection": None,
        "store": "present",
    }
    ergebnis = bewerten(meldung, gesendet=Sollwert(2, 8), gesendet_at=100.0, now=130.0)
    assert ergebnis.zustand is Zustand.UNBESTAETIGT
    assert ergebnis.gesendet_at == 100.0


def test_ablehnung_bleibt_ablehnung_auch_nach_der_frist() -> None:
    """KTD13: Ablehnung ist dauerhaft, Unbestaetigt voruebergehend.

    Teilten sie sich eine Darstellung, bekaeme das Teammitglied nach der
    Frist ein Angebot zum erneuten Senden auf einen Wert, den der Supervisor
    bereits verworfen hat -- die Wiederholungsschleife, gegen die KTD13 steht.
    """
    meldung = {"setpoint": {"min": 2, "max": 12}, "rejection": "max 12 ueber dem Spielraum 8"}
    ergebnis = bewerten(meldung, gesendet=Sollwert(2, 12), gesendet_at=100.0, now=900.0)
    assert ergebnis.zustand is Zustand.ABGELEHNT


def test_ohne_jede_meldung_gilt_dieselbe_frist() -> None:
    """Der Statusschluessel verfaellt mit ``ttl_s``; danach gibt es gar keine Meldung mehr."""
    innerhalb = bewerten(None, gesendet=Sollwert(2, 8), gesendet_at=100.0, now=103.0)
    danach = bewerten(None, gesendet=Sollwert(2, 8), gesendet_at=100.0, now=130.0)
    assert innerhalb.zustand is Zustand.GESENDET
    assert danach.zustand is Zustand.UNBESTAETIGT


def test_unerreichbares_backend_beim_supervisor_wird_benannt() -> None:
    """``store == "unread"``: der Supervisor kam an den Sollwert gar nicht heran.

    Ohne diesen Hinweis wartet das Teammitglied auf eine Bestaetigung, die
    nicht kommen kann, und sucht den Fehler bei seinem Wert.
    """
    meldung = {"setpoint": {"min": 2, "max": 4}, "rejection": None, "store": "unread"}
    ergebnis = bewerten(meldung, gesendet=Sollwert(2, 8), gesendet_at=100.0, now=103.0)
    assert ergebnis.zustand is Zustand.GESENDET
    assert ergebnis.hinweis is not None
    assert "Backend" in ergebnis.hinweis


def test_bestaetigt_verlangt_dass_der_wert_auch_wirksam_ist() -> None:
    """Die Lesart der Schnittstelle nennt drei Bedingungen, nicht eine.

    Der Supervisor hat den Wert **gelesen** (``setpoint``), wirksam sind aber
    die Startargumente (``source == "startup"``). "Bestaetigt" zu melden,
    weil die Zahlen zufaellig uebereinstimmen, waere eine Aussage, die die
    Rueckmeldung nicht deckt.
    """
    meldung = {
        "setpoint": {"min": 2, "max": 8},
        "effective": {"min": 2, "max": 4},
        "source": "startup",
        "rejection": None,
        "store": "present",
    }
    ergebnis = bewerten(meldung, gesendet=Sollwert(2, 8), gesendet_at=100.0, now=103.0)
    assert ergebnis.zustand is Zustand.GESENDET


def test_unlesbarer_sollwert_ist_eine_ablehnung() -> None:
    """Der Kern meldet ``unreadable: ...`` als Ablehnungsgrund und ``setpoint: null``."""
    meldung = {"setpoint": None, "rejection": "unreadable: expected object, got str", "store": "present"}
    ergebnis = bewerten(meldung, gesendet=Sollwert(2, 8), gesendet_at=100.0, now=103.0)
    assert ergebnis.zustand is Zustand.ABGELEHNT
    assert ergebnis.grund == "unreadable: expected object, got str"


def test_zahlen_der_gruppe_summieren_sich_ueber_die_supervisoren() -> None:
    """R15a: Spielraum, Slots und Worker einer Gruppe gelten ueber alle meldenden Supervisoren."""
    bild = Steuergruppe(
        "orders-eu",
        [
            {"supervisor": "a:p1", "headroom": 6, "slots": {"size": 6, "occupied": 4}, "workers": 4},
            {"supervisor": "b:p1", "headroom": 8, "slots": {"size": 8, "occupied": 2}, "workers": 2},
        ],
    )
    assert bild.supervisoren == 2
    assert bild.spielraum == 14
    assert bild.belegt == 6
    assert bild.worker == 6


def test_bei_zwei_supervisoren_zaehlt_die_ansicht_die_bestaetigungen() -> None:
    """R17: "bestaetigt" erst, wenn alle bestaetigt haben -- sonst das Verhaeltnis.

    Ein Sollwert, den nur einer von zwei Supervisoren angewendet hat, fuehrt
    die halbe Gruppe mit alten Grenzen. "Bestaetigt" zu melden verdeckte
    genau das.
    """
    bild = Steuergruppe(
        "orders-eu",
        [
            {"supervisor": "a:p1", "setpoint": {"min": 2, "max": 8}, "source": "setpoint", "rejection": None},
            {"supervisor": "b:p1", "setpoint": {"min": 2, "max": 4}, "source": "setpoint", "rejection": None},
        ],
    )
    stand = bild.stand(gesendet=Sollwert(2, 8), gesendet_at=100.0, now=103.0)
    assert stand.zustand is Zustand.GESENDET
    assert stand.bestaetigt_von == 1
    assert stand.von == 2


def test_verschiedener_spielraum_macht_die_gruppe_uneinheitlich() -> None:
    """R15b: eine Gruppe, deren Supervisoren verschiedenen Spielraum melden, wird als solche ausgewiesen.

    Der Sollwert gilt je Supervisor. Bei ungleichem Spielraum nimmt ihn der
    eine an und der andere lehnt ihn ab -- die Gruppe liefe dann dauerhaft
    halb auf alten Grenzen.
    """
    bild = Steuergruppe(
        "orders-eu",
        [
            {"supervisor": "a:p1", "headroom": 6},
            {"supervisor": "b:p1", "headroom": 8},
        ],
    )
    assert bild.uneinheitlich is not None
    assert "Spielraum" in bild.uneinheitlich


def test_verschiedene_autoskalierung_macht_die_gruppe_uneinheitlich() -> None:
    """R15b: gleicher Spielraum genuegt nicht -- die Form des Sollwerts haengt an der Autoskalierung.

    Ein Bereich 2..8 wird vom autoskalierenden Supervisor angenommen und vom
    anderen abgelehnt; eine feste Zahl schaltet dem einen die Autoskalierung
    ab. Es gibt keinen Wert, der fuer beide richtig ist.
    """
    bild = Steuergruppe(
        "orders-eu",
        [
            {"supervisor": "a:p1", "headroom": 8, "autoscaling": True},
            {"supervisor": "b:p1", "headroom": 8, "autoscaling": False},
        ],
    )
    assert bild.uneinheitlich is not None
    assert "Autoskalierung" in bild.uneinheitlich


def test_ohne_meldenden_supervisor_ist_die_wirkung_unbekannt() -> None:
    """R15b: schreiben ist erlaubt, aber die Ansicht sagt, dass niemand bestaetigen kann.

    Der Fall ist nicht exotisch: nach einem Deploy meldet sich der erste
    Supervisor erst nach seinem Takt, und ein Sollwert vor dem Start ist
    genau der Weg, den R16 vorsieht.
    """
    bild = Steuergruppe("orders-eu", [])
    assert bild.supervisoren == 0
    assert bild.wirkungsunbekannt is True
    assert bild.uneinheitlich is None


def test_wert_ueber_dem_kleinsten_spielraum_wird_vor_dem_schreiben_abgelehnt() -> None:
    """R15b: das Dashboard prueft gegen die Angaben der Supervisoren, bevor es schreibt.

    Ungeprueft geschrieben, laendet der Wert im Kontrollkanal, der Supervisor
    lehnt ihn ab, und das Teammitglied bekommt eine Ablehnung fuer etwas, das
    die Oberflaeche vorher wusste.
    """
    bild = Steuergruppe(
        "orders-eu",
        [{"supervisor": "a:p1", "headroom": 8, "autoscaling": True},
         {"supervisor": "b:p1", "headroom": 8, "autoscaling": True}],
    )
    ergebnis = bild.pruefen(Sollwert(2, 12))
    assert ergebnis.erlaubt is False
    assert ergebnis.grund is not None
    assert "12" in ergebnis.grund and "8" in ergebnis.grund


def test_bereich_auf_einer_gruppe_ohne_autoskalierung_wird_abgelehnt() -> None:
    """R15: Gruppen ohne Autoskalierung nehmen nur die feste Form N..N an."""
    bild = Steuergruppe("cpu-pool", [{"supervisor": "a:p1", "headroom": 8, "autoscaling": False}])
    bereich = bild.pruefen(Sollwert(2, 6))
    fest = bild.pruefen(Sollwert(4, 4))
    assert bereich.erlaubt is False
    assert bereich.grund is not None and "Autoskalierung" in bereich.grund
    assert fest.erlaubt is True


def test_unmoegliche_grenzen_werden_abgelehnt_auch_ohne_meldenden_supervisor() -> None:
    """R15: ganze Zahlen, ``min >= 1``, ``min <= max``.

    Diese Regeln haengen nicht an einer Rueckmeldung: ein Wert 0..5 oder 8..2
    ist unabhaengig davon falsch, wer gerade meldet. Ohne die Pruefung an
    dieser Stelle laege er im Kontrollkanal und wartete auf einen Supervisor,
    der ihn ablehnt.
    """
    leer = Steuergruppe("orders-eu", [])
    zu_klein = leer.pruefen(Sollwert(0, 5))
    verdreht = leer.pruefen(Sollwert(8, 2))
    assert zu_klein.erlaubt is False
    assert zu_klein.grund is not None and "1" in zu_klein.grund
    assert verdreht.erlaubt is False
    assert verdreht.grund is not None


def test_ohne_meldenden_supervisor_wird_geschrieben_aber_gekennzeichnet() -> None:
    """R15b: das Schreiben ist erlaubt -- die Wirkung ist unbekannt, und das steht dran."""
    ergebnis = Steuergruppe("orders-eu", []).pruefen(Sollwert(2, 8))
    assert ergebnis.erlaubt is True
    assert ergebnis.wirkungsunbekannt is True


def test_gesamtzahl_ist_der_wert_je_supervisor_mal_die_meldenden() -> None:
    """AE4: die Eingabe ist "je Supervisor" beschriftet und nennt die Gesamtzahl.

    Zwei Supervisoren, 2 bis 8 je Supervisor: die Gruppe faehrt zwischen 4
    und 16 Prozessen. Wer die Eingabe fuer die Gruppenzahl haelt, setzt die
    Haelfte dessen, was er will -- oder das Doppelte.
    """
    bild = Steuergruppe(
        "orders-eu",
        [{"supervisor": "a:p1", "headroom": 8, "autoscaling": True},
         {"supervisor": "b:p1", "headroom": 8, "autoscaling": True}],
    )
    assert bild.gesamt(Sollwert(2, 8)) == Sollwert(4, 16)


def test_fehlender_kontrollkanal_wird_benannt_statt_zu_werfen() -> None:
    """AE15, R33: eine Bibliothek ohne Kontrollkanal schaltet den Bereich ab und begruendet ihn.

    Der Dienst muss mit ihr trotzdem hochfahren -- die Worker-Uebersicht ist
    die Startseite und traegt alles andere weiterhin.
    """
    def ohne() -> object:
        raise ImportError("No module named 'swarmq.contracts.control'")

    grund = kontrollkanal_grund(laden=ohne)
    assert grund is not None
    assert "swarmq" in grund


class FakeStore:
    """Der Kontrollkanal ohne Valkey -- die Naht, an der U16 pruefbar wird."""

    def __init__(self, meldungen: dict[str, list[dict[str, object]]] | None = None) -> None:
        self.meldungen = meldungen or {}
        self.geschrieben: list[tuple[str, Sollwert]] = []
        self.geloescht: list[str] = []
        self.sollwerte: dict[str, Sollwert] = {}

    async def status_aller(self, gruppe: str) -> list[dict[str, object]]:
        return self.meldungen.get(gruppe, [])

    async def schreiben(self, gruppe: str, wert: Sollwert) -> None:
        self.geschrieben.append((gruppe, wert))
        self.sollwerte[gruppe] = wert

    async def loeschen(self, gruppe: str) -> None:
        self.geloescht.append(gruppe)
        self.sollwerte.pop(gruppe, None)

    async def gelesen(self, gruppe: str) -> Sollwert | None:
        return self.sollwerte.get(gruppe)


async def test_die_steuerung_sammelt_die_meldungen_einer_gruppe() -> None:
    """U16 Punkt 2: Supervisoren gibt es nur ueber den Statusschluesselraum, nicht als Liste."""
    store = FakeStore({"orders-eu": [
        {"supervisor": "a:p1", "headroom": 8, "autoscaling": True},
        {"supervisor": "b:p1", "headroom": 8, "autoscaling": True},
    ]})
    steuerung = Steuerung(store)
    bild = await steuerung.gruppe("orders-eu")
    assert bild.name == "orders-eu"
    assert bild.supervisoren == 2
    assert bild.spielraum == 16


async def test_setzen_schreibt_den_sollwert_und_merkt_den_sendezeitpunkt() -> None:
    """R15, R17: der Wert geht in den Kanal, der Zeitpunkt bleibt beim Dienst.

    Den Sendezeitpunkt kennt nur das Dashboard -- der Kontrollkanal traegt
    ihn nicht. Ohne ihn gaebe es den Zustand "unbestaetigt seit" nicht.
    """
    store = FakeStore({"orders-eu": [{"supervisor": "a:p1", "headroom": 8, "autoscaling": True}]})
    steuerung = Steuerung(store)
    ergebnis = await steuerung.setzen("orders-eu", Sollwert(2, 8), now=100.0)
    assert ergebnis.erlaubt is True
    assert store.geschrieben == [("orders-eu", Sollwert(2, 8))]
    assert steuerung.gesendet_at("orders-eu") == 100.0


async def test_ein_abgelehnter_wert_erreicht_den_kanal_gar_nicht() -> None:
    """R15b: geprueft wird vor dem Schreiben -- sonst liegt der Wert da und wartet auf seine Ablehnung."""
    store = FakeStore({"orders-eu": [{"supervisor": "a:p1", "headroom": 8, "autoscaling": True}]})
    ergebnis = await Steuerung(store).setzen("orders-eu", Sollwert(2, 12), now=100.0)
    assert ergebnis.erlaubt is False
    assert store.geschrieben == []


async def test_eine_ablehnung_raeumt_den_sollwert_aus_dem_kanal() -> None:
    """R16a, KTD13, AE6: ein abgelehnter Wert darf nicht liegen bleiben.

    Bliebe er stehen, wendete ihn ein spaeter startender Supervisor mit
    groesserem Spielraum an -- eine Zahl, die niemand mehr auf dem Schirm
    hat, weil die Ansicht sie als abgelehnt fuehrt.
    """
    store = FakeStore({"orders-eu": [{
        "supervisor": "a:p1", "headroom": 8, "autoscaling": True,
        "setpoint": {"min": 2, "max": 12}, "source": "startup",
        "rejection": "max 12 über dem Spielraum 8",
    }]})
    store.sollwerte["orders-eu"] = Sollwert(2, 12)
    steuerung = Steuerung(store)
    bild = await steuerung.bild("orders-eu", now=200.0)
    assert bild.stand is not None
    assert bild.stand.zustand is Zustand.ABGELEHNT
    assert bild.stand.grund == "max 12 über dem Spielraum 8"
    assert store.geloescht == ["orders-eu"]


async def test_nach_einem_neustart_laeuft_keine_frist_gegen_einen_alten_sollwert() -> None:
    """Der Sendezeitpunkt lebt im Arbeitsspeicher, der Sollwert im Kanal -- ein Neustart trennt beides.

    Ohne diesen Fall rechnet die Frist gegen den Zeitpunkt 0 und faellt
    sofort auf "unbestaetigt seit 1970": eine seit Wochen laufende Gruppe
    saehe im Dashboard nach jedem Neustart aus, als warte sie auf ihren
    Supervisor.
    """
    store = FakeStore({"orders-eu": [{
        "supervisor": "a:p1", "headroom": 8, "autoscaling": True,
        "setpoint": {"min": 2, "max": 8}, "source": "setpoint", "rejection": None,
    }]})
    store.sollwerte["orders-eu"] = Sollwert(2, 8)
    bild = await Steuerung(store).bild("orders-eu", now=1_700_000_000.0)
    assert bild.stand is not None
    assert bild.stand.zustand is Zustand.BESTAETIGT


async def test_ohne_gemerkten_sendezeitpunkt_gibt_es_kein_unbestaetigt() -> None:
    """R17 verlangt beim Zustand "unbestaetigt" den Zeitpunkt des Sendens.

    Nach einem Neustart des Dienstes gibt es ihn nicht. Dann ist "seit
    1970 unbestaetigt" die Antwort, die der Code ohne diesen Test gibt --
    eine Zahl, die nichts bedeutet, an der teuersten Stelle der Ansicht.
    """
    store = FakeStore({"orders-eu": [{
        "supervisor": "a:p1", "headroom": 8, "autoscaling": True,
        "setpoint": None, "source": "startup", "rejection": None,
    }]})
    store.sollwerte["orders-eu"] = Sollwert(2, 8)
    bild = await Steuerung(store).bild("orders-eu", now=1_700_000_000.0)
    assert bild.stand is not None
    assert bild.stand.zustand is not Zustand.UNBESTAETIGT
    assert bild.stand.hinweis is not None


async def test_aufheben_loescht_den_sollwert_und_den_gemerkten_zeitpunkt() -> None:
    """R15: nach dem Aufheben gelten wieder die Startargumente des Deployments."""
    store = FakeStore({"orders-eu": [{"supervisor": "a:p1", "headroom": 8, "autoscaling": True}]})
    steuerung = Steuerung(store)
    await steuerung.setzen("orders-eu", Sollwert(2, 8), now=100.0)
    await steuerung.aufheben("orders-eu")
    assert store.geloescht == ["orders-eu"]
    assert steuerung.gesendet_at("orders-eu") is None
    bild = await steuerung.bild("orders-eu", now=110.0)
    assert bild.sollwert is None
    assert bild.stand is None


async def test_ohne_kontrollkanal_bleibt_die_steuerung_stumm_statt_zu_werfen() -> None:
    """AE15, R33: der Bereich ist abgeschaltet und begruendet -- die Seite traegt weiter.

    Keine Ausnahme, kein halber Zustand: die Worker-Uebersicht ist die
    Startseite, und sie muss auch gegen eine Bibliothek ohne Kontrollkanal
    vollstaendig laden.
    """
    steuerung = Steuerung(None, grund="Steuerung nicht verfügbar: ...")
    assert steuerung.verfuegbar is False
    bild = await steuerung.bild("orders-eu", now=100.0)
    assert bild.gruppe.supervisoren == 0
    assert bild.sollwert is None
    # Wirft nicht, schreibt nichts.
    await steuerung.aufheben("orders-eu")
    ergebnis = await steuerung.setzen("orders-eu", Sollwert(2, 8), now=100.0)
    assert ergebnis.wirkungsunbekannt is True


def valkey_glob(muster: str) -> re.Pattern[str]:
    """Das Mustern von SCAN, einschliesslich der Maskierung mit Backslash.

    ``fnmatch`` kennt keine Maskierung -- ein Fake damit haette den Fehler,
    um den es hier geht, gar nicht zeigen koennen.
    """
    teile: list[str] = []
    i = 0
    while i < len(muster):
        z = muster[i]
        if z == "\\" and i + 1 < len(muster):
            teile.append(re.escape(muster[i + 1]))
            i += 2
            continue
        teile.append(".*" if z == "*" else "." if z == "?" else re.escape(z))
        i += 1
    return re.compile("".join(teile) + r"\Z")


class FakeValkey:
    """Nur so viel Valkey, wie der SCAN ueber den Statusschluesselraum braucht."""

    def __init__(self, schluessel: list[str]) -> None:
        self.schluessel = schluessel
        self.gesucht: list[str] = []

    async def scan_iter(self, match: str = "*", count: int | None = None) -> AsyncIterator[bytes]:
        self.gesucht.append(match)
        treffer = valkey_glob(match)
        for k in self.schluessel:
            if treffer.match(k):
                yield k.encode()


class FakeKernStore:
    """Steht fuer ``ValkeySetpointStore`` aus swarmq."""

    def __init__(self, schluessel: list[str], status: dict[str, dict[str, object]]) -> None:
        self.client = FakeValkey(schluessel)
        self._status = status
        self.geschrieben: list[tuple[str, object]] = []
        self.geloescht: list[str] = []
        self.sollwerte: dict[str, object] = {}

    async def read_status(self, group: str, supervisor_id: str) -> dict[str, object] | None:
        return self._status.get(supervisor_id)

    async def write_setpoint(self, group: str, setpoint: object) -> None:
        self.geschrieben.append((group, setpoint))

    async def clear_setpoint(self, group: str) -> None:
        self.geloescht.append(group)

    async def read_setpoint(self, group: str) -> object:
        return self.sollwerte.get(group)


async def test_status_aller_findet_die_supervisoren_ueber_den_schluesselraum() -> None:
    """U16 Punkt 2: eine Auflistung gibt es im Kern nicht -- nur den SCAN.

    Die Kennung wechselt bei jedem Neustart (sie traegt die PID), eine
    gemerkte Kennung waere also nach dem naechsten Deploy falsch.
    """
    kern = FakeKernStore(
        ["sq:control:status:orders-eu:a:p1", "sq:control:status:orders-eu:b:p2",
         "sq:control:status:billing:c:p3"],
        {"a:p1": {"supervisor": "a:p1", "headroom": 8}, "b:p2": {"supervisor": "b:p2", "headroom": 8}},
    )
    store = SwarmQSetpointStore("valkey://localhost:6399/0", store=kern)
    meldungen = await store.status_aller("orders-eu")
    assert [m["supervisor"] for m in meldungen] == ["a:p1", "b:p2"]


async def test_ein_gruppenname_mit_sternchen_sammelt_keine_fremden_supervisoren() -> None:
    """Der Gruppenname geht in ein SCAN-Muster -- ungemaskiert ist er dort Code, nicht Text.

    Eine Gruppe ``a*`` laese die Rueckmeldungen aller Gruppen, die mit "a"
    beginnen: fremder Spielraum, fremde Slots, und eine Pruefung nach R15b
    gegen Zahlen, die zu einer anderen Gruppe gehoeren.
    """
    kern = FakeKernStore(
        ["sq:control:status:a*:x:p1", "sq:control:status:ab:y:p1"],
        {"x:p1": {"supervisor": "x:p1", "headroom": 4}, "y:p1": {"supervisor": "y:p1", "headroom": 99}},
    )
    store = SwarmQSetpointStore("valkey://localhost:6399/0", store=kern)
    meldungen = await store.status_aller("a*")
    assert [m["supervisor"] for m in meldungen] == ["x:p1"]


async def test_der_sollwert_wird_in_den_typ_der_bibliothek_uebersetzt() -> None:
    """Das Dashboard rechnet in ``Sollwert``, der Kanal in ``Setpoint`` -- die Naht liegt hier."""
    kern = FakeKernStore([], {})
    store = SwarmQSetpointStore("valkey://localhost:6399/0", store=kern)
    await store.schreiben("orders-eu", Sollwert(2, 8))
    gruppe, gesendet = kern.geschrieben[0]
    assert gruppe == "orders-eu"
    assert (gesendet.low, gesendet.high) == (2, 8)


async def test_ein_unlesbarer_sollwert_im_kanal_liest_sich_als_keiner() -> None:
    """Der Kern gibt ``SetpointUnreadable`` zurueck statt zu werfen (R10 dort).

    Fuer die Ansicht ist das kein Wert: der Supervisor lehnt ihn ab, und die
    Ablehnung raeumt ihn weg. Ihn als Zahl zu deuten hiesse, eine Grenze zu
    zeigen, die nirgends gilt.
    """
    from swarmq.contracts.control import Setpoint, SetpointUnreadable

    kern = FakeKernStore([], {})
    store = SwarmQSetpointStore("valkey://localhost:6399/0", store=kern)
    kern.sollwerte["kaputt"] = SetpointUnreadable(reason="setpoint 'nope' is not {...}")
    kern.sollwerte["gut"] = Setpoint(low=3, high=3)
    assert await store.gelesen("kaputt") is None
    assert await store.gelesen("gut") == Sollwert(3, 3)
    assert await store.gelesen("fehlt") is None


async def test_ein_nicht_erreichbarer_kanal_wird_zur_auskunft_nicht_zur_ausnahme() -> None:
    """R33: die Worker-Uebersicht ist die Startseite -- gerade im Vorfall.

    Ein Backend, das nicht antwortet, ist der Fall, fuer den das Dashboard
    gebaut wurde. Faellt die Seite dabei mit 500 um, ist sie genau dann weg,
    wenn sie gebraucht wird.
    """
    class ToterKanal:
        async def status_aller(self, gruppe: str) -> list[Mapping[str, Any]]:
            raise StreamUnavailable("der Kontrollkanal ist nicht erreichbar: Error 111")

        async def schreiben(self, gruppe: str, wert: Sollwert) -> None: ...

        async def loeschen(self, gruppe: str) -> None: ...

        async def gelesen(self, gruppe: str) -> Sollwert | None:
            return None

    bild = await Steuerung(ToterKanal()).bild("orders-eu", now=100.0)
    assert bild.gruppe.supervisoren == 0
    assert bild.fehler is not None
    assert "111" in bild.fehler


async def test_ein_toter_kanal_wird_nicht_bei_jedem_aufruf_neu_gewaehlt() -> None:
    """Gemessen 3,2 s je Gruppe gegen ein totes Backend -- bei zehn Gruppen eine halbe Minute.

    Und zwar bei **jedem** Seitenaufruf, auf der Startseite, im Vorfall. Ein
    Fehlversuch gilt darum eine Weile: der Grund bleibt stehen, die Seite
    kommt sofort, und nach der Sperrfrist wird wieder gewaehlt.
    """
    versuche = 0
    jetzt = [0.0]

    async def verbinden() -> object:
        nonlocal versuche
        versuche += 1
        raise OSError("Error 111 connecting to 127.0.0.1:1")

    store = SwarmQSetpointStore(
        "valkey://127.0.0.1:1/0", verbinden=verbinden, uhr=lambda: jetzt[0]
    )
    for _ in range(3):
        with pytest.raises(StreamUnavailable):
            await store.status_aller("orders-eu")
    assert versuche == 1, "der tote Kanal wurde mehrfach gewaehlt"

    jetzt[0] = SPERRE_S + 1.0
    with pytest.raises(StreamUnavailable):
        await store.status_aller("orders-eu")
    assert versuche == 2, "nach der Sperrfrist wird nicht wieder gewaehlt"
