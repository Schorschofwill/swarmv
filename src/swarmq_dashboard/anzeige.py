"""Reine Formatierungen fuer die Anzeige -- ohne Jinja, ohne Litestar.

Vorlagen (``web.templates``) und Diagramm (``workflow_diagramm``) teilen sie;
das Diagramm ist rein und darf die Vorlagen nicht nachziehen.
"""

from __future__ import annotations

from swarmq_dashboard.workflow_baum import (
    GRUND_GESCHEITERT,
    GRUND_STORNIERT,
    GRUND_STORNO_GLIED,
    GRUND_VORGAENGER_FERTIG,
    GRUND_WARTET,
)


def dauer(sekunden: float | None) -> str:
    """Eine Zeitspanne lesbar machen: ``245.0`` -> ``4 min 05 s``.

    ``None`` und eine negative Spanne -- zwei Hosts mit getrennten Uhren --
    ergeben "--": eine erfundene Zeit waere eine Falschaussage.
    """
    if sekunden is None or sekunden < 0:
        return "--"
    if sekunden < 1:
        return f"{sekunden * 1000:.0f} ms"
    if sekunden < 60:
        return f"{sekunden:.1f} s".replace(".", ",")
    ganz = int(sekunden)
    if ganz < 3600:
        return f"{ganz // 60} min {ganz % 60:02d} s"
    return f"{ganz // 3600} h {ganz % 3600 // 60:02d} min"


def zahl(wert: int | None) -> str:
    """Eine Zahl mit deutscher Tausendertrennung. ``None`` heisst "kein Wert".

    ``1200`` und ``12000`` unterscheiden sich um eine Stelle, und der
    Rueckstau einer Queue ist die Zahl, auf die im Vorfall zuerst geschaut
    wird. "Ist das jetzt zwoelfhundert oder zwoelftausend" kostet genau dort
    eine Sekunde.

    ``None`` wird **nicht** zu ``0``: eine Null, wo eine Auskunft fehlt, ist
    die gefaehrlichste stille Falschaussage dieser Oberflaeche.
    """
    if wert is None:
        return "--"
    if not isinstance(wert, int) or isinstance(wert, bool):
        # ``Gruppe.belegt()`` gibt "2 von 4" zurueck, nicht 2. Ohne diese
        # Pruefung antwortete die Worker-Seite mit 500 und einer Meldung
        # ueber Formatangaben -- die sagt nicht, was los ist.
        raise TypeError(f"zahl() erwartet eine ganze Zahl oder None, nicht {wert!r}")
    return f"{wert:,}".replace(",", ".")


#: Status-Wert -> Zustandsart der Gestaltung. Die Zuordnung steht hier und
#: nicht in der Vorlage, damit sie an **einer** Stelle liegt: eine Vorlage,
#: die sie nachbaut, laeuft irgendwann auseinander.
_ZUSTANDSART = {
    "fertig": "is-ok",
    "laufend": "is-run",
    "wartend": "is-idle",
    "wiederholung": "is-wait",
    "storniert": "is-idle",
    "gescheitert": "is-fail",
    "dead_letter": "is-fail",
    "abgelaufen": "is-fail",
    "abgewiesen": "is-fail",
    # Workflow-Anzeige-Codes (``Statusanzeige.code``, U4). "fertig",
    # "gescheitert" und "storniert" teilen sich Wort und Art mit den
    # Task-Status oben; die uebrigen sind eigenes Vokabular.
    "laeuft": "is-run",
    "angefordert": "is-wait",
    "angefordert_nichts_aktiv": "is-wait",
    "verfallen": "is-idle",
    "nie_gesehen": "is-idle",
    # Gruende eines nicht eingereihten Schritts (R11): Warten ist Warten,
    # "nie gestartet" ist endgueltig und neutral wie "storniert".
    GRUND_WARTET: "is-wait",
    GRUND_VORGAENGER_FERTIG: "is-wait",
    GRUND_STORNO_GLIED: "is-wait",
    GRUND_GESCHEITERT: "is-idle",
    GRUND_STORNIERT: "is-idle",
}


def zustandsart(status: str | None) -> str:
    """Die Zustandsart zu einem Task-Status. Unbekanntes ist neutral.

    Neutral und nicht "fehler": ein Status, den diese Fassung nicht kennt, ist
    keine Stoerung -- er ist eine Aussage, die wir noch nicht einordnen.
    """
    return _ZUSTANDSART.get(status or "", "is-idle")
