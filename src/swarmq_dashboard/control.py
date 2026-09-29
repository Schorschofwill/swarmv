"""Sollwert-Steuerung ueber den Kontrollkanal (U16, R15--R17).

Die eine Entscheidung, an der diese Datei haengt: **Ablehnung und Warten sind
verschiedene Zustaende** (KTD13). Eine Ablehnung ist dauerhaft -- der
Supervisor hat den Wert gesehen und verworfen; ein erneutes Senden aendert
daran nichts und dreht das Teammitglied in eine Schleife. Ein Warten ist
voruebergehend -- der Supervisor tastet alle fuenf Sekunden ab und hat den
Wert noch nicht gelesen.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from swarmq_dashboard.config import DEFAULT_UNCONFIRMED_AFTER_S
from swarmq_dashboard.zugang import Zugang

#: Wenn der Dienst nach einem Neustart einen Sollwert im Kanal findet, den
#: er nicht selbst gesendet hat. Die Frist aus R17 braucht einen Anfang;
#: ohne ihn gibt es kein "unbestaetigt seit", sondern diese Auskunft.
OHNE_SENDEZEITPUNKT = (
    "Dieser Sollwert lag schon im Kontrollkanal, als der Dienst startete; "
    "seit wann er dort wartet, weiß das Dashboard nicht."
)

#: Was ``store == "unread"`` fuer das wartende Teammitglied bedeutet.
UNREAD_HINWEIS = (
    "Der Supervisor erreicht das Backend nicht und hat den Sollwert beim "
    "letzten Versuch nicht gelesen; seine Meldung kann veraltet sein."
)


def _kontrollkanal_laden() -> object:
    """Die Typen des Kontrollkanals holen. Wirft ``ImportError``, wenn er fehlt."""
    from swarmq.backend.valkey.control import ValkeySetpointStore

    return ValkeySetpointStore


def kontrollkanal_grund(*, laden: Callable[[], object] | None = None) -> str | None:
    """``None``, wenn die installierte Bibliothek den Kontrollkanal mitbringt -- sonst der Grund.

    Die Pruefung gehoert an den Start und nicht an die erste Aktion (R33):
    ein Bereich, der erst beim Klicken zugibt, dass es ihn nicht gibt, ist
    schlechter als einer, der gar nicht erst anbietet.
    """
    try:
        (laden or _kontrollkanal_laden)()
    except Exception as exc:
        return (
            "Steuerung nicht verfügbar: die installierte swarmq-Fassung bringt den "
            f"Kontrollkanal nicht mit ({exc})."
        )
    return None


class Zustand(enum.Enum):
    """Die vier Zustaende eines gesendeten Sollwerts (R17, KTD13)."""

    GESENDET = "gesendet"
    BESTAETIGT = "bestaetigt"
    ABGELEHNT = "abgelehnt"
    UNBESTAETIGT = "unbestaetigt"


@dataclass(frozen=True, slots=True)
class Sollwert:
    """Unter- und Obergrenze **je Supervisor** (R15a)."""

    unten: int
    oben: int


@dataclass(frozen=True, slots=True)
class Stand:
    """Was eine Rueckmeldung ueber den gesendeten Sollwert aussagt."""

    zustand: Zustand
    grund: str | None = None
    #: Wann der Wert gesendet wurde. Die Ansicht nennt ihn beim Zustand
    #: "unbestaetigt" (R17) -- ohne ihn stuende dort ein Warten ohne Mass.
    gesendet_at: float = 0.0
    #: Warum das Warten nicht am Wert liegt -- gesetzt, wenn der Supervisor
    #: selbst nicht an den Sollwert herankam. Getrennt von ``grund``: das
    #: ist der Wortlaut einer Ablehnung, dies eine Auskunft ueber die
    #: Leitung dazwischen.
    hinweis: str | None = None
    #: Wie viele Supervisoren der Gruppe den Wert angewendet haben, und wie
    #: viele melden (R17). Bei einem einzelnen Supervisor 1 von 1.
    bestaetigt_von: int = 0
    von: int = 0


def bewerten(
    meldung: Mapping[str, Any] | None,
    *,
    gesendet: Sollwert,
    gesendet_at: float,
    now: float,
    frist_s: float = DEFAULT_UNCONFIRMED_AFTER_S,
) -> Stand:
    """Den Zustand eines gesendeten Sollwerts aus der Rueckmeldung ableiten."""
    gemeldet = (meldung or {}).get("rejection")
    if gemeldet:
        return Stand(Zustand.ABGELEHNT, grund=str(gemeldet), gesendet_at=gesendet_at)
    if _gelesen(meldung) == gesendet and (meldung or {}).get("source") == "setpoint":
        return Stand(Zustand.BESTAETIGT, gesendet_at=gesendet_at)
    hinweis = UNREAD_HINWEIS if (meldung or {}).get("store") == "unread" else None
    if now - gesendet_at >= frist_s:
        return Stand(Zustand.UNBESTAETIGT, gesendet_at=gesendet_at, hinweis=hinweis)
    return Stand(Zustand.GESENDET, gesendet_at=gesendet_at, hinweis=hinweis)


def _gelesen(meldung: Mapping[str, Any] | None) -> Sollwert | None:
    """Der Sollwert, den der Supervisor zuletzt gelesen hat -- oder ``None``."""
    roh = (meldung or {}).get("setpoint")
    if not isinstance(roh, Mapping):
        return None
    try:
        return Sollwert(int(roh["min"]), int(roh["max"]))
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Pruefung:
    """Was aus einem Sollwert wird, **bevor** er in den Kontrollkanal geht (R15b)."""

    erlaubt: bool
    grund: str | None = None
    #: Geschrieben, aber niemand kann bestaetigen -- kein Fehler, eine Auskunft.
    wirkungsunbekannt: bool = False


@dataclass(frozen=True, slots=True)
class Steuergruppe:
    """Alle meldenden Supervisoren einer Worker-Gruppe (R15a).

    Der Sollwert gilt **je Supervisor**. Die Anzeige summiert trotzdem --
    "wie viele Prozesse laufen in dieser Gruppe" ist die Frage, die im
    Vorfall gestellt wird -- und beschriftet die Eingabe getrennt davon.
    """

    name: str
    meldungen: list[Mapping[str, Any]]

    @property
    def supervisoren(self) -> int:
        return len(self.meldungen)

    @property
    def spielraum(self) -> int:
        """Summierter Spielraum aller meldenden Supervisoren."""
        return sum(int(m.get("headroom") or 0) for m in self.meldungen)

    @property
    def spielraeume(self) -> list[int]:
        """Der Spielraum jedes einzelnen Supervisors -- die Groesse, gegen die geprueft wird."""
        return [int(m.get("headroom") or 0) for m in self.meldungen]

    @property
    def herkunft(self) -> str | None:
        """Woher die wirksamen Werte stammen: Sollwert oder Startargumente (R14).

        Melden die Supervisoren Verschiedenes, steht das da statt einer der
        beiden Antworten -- bei ungleicher Herkunft laeuft die halbe Gruppe
        auf anderen Grenzen als die andere.
        """
        quellen = {str(m.get("source") or "") for m in self.meldungen}
        if not quellen:
            return None
        if quellen == {"setpoint"}:
            return "Sollwert"
        if quellen == {"startup"}:
            return "Startargumente"
        return "gemischt: Sollwert und Startargumente"

    @property
    def wirkungsunbekannt(self) -> bool:
        """Kein Supervisor meldet -- ein Sollwert laesst sich schreiben, aber nicht bestaetigen (R15b)."""
        return not self.meldungen

    @property
    def autoskalierung(self) -> bool:
        """Skaliert die Gruppe automatisch? Nur wahr, wenn **alle** es melden."""
        return bool(self.meldungen) and all(bool(m.get("autoscaling")) for m in self.meldungen)

    @property
    def uneinheitlich(self) -> str | None:
        """Melden die Supervisoren dieser Gruppe Verschiedenes? (R15b)

        Zwei Merkmale entscheiden, und beide einzeln: gleicher Spielraum bei
        verschiedener Autoskalierung laesst trotzdem keinen Wert zu, der fuer
        alle richtig waere.
        """
        if len(set(self.spielraeume)) > 1:
            gereiht = ", ".join(str(h) for h in sorted(set(self.spielraeume)))
            return f"Die Supervisoren melden verschiedenen Spielraum ({gereiht})."
        if len({bool(m.get("autoscaling")) for m in self.meldungen}) > 1:
            return "Die Supervisoren melden verschiedene Autoskalierung."
        return None

    @property
    def belegt(self) -> int:
        """Summe der belegten Slots."""
        return sum(int((m.get("slots") or {}).get("occupied") or 0) for m in self.meldungen)

    @property
    def worker(self) -> int:
        """Summe der Kindprozesse, einschliesslich der sanft endenden."""
        return sum(int(m.get("workers") or 0) for m in self.meldungen)

    def stand(self, *, gesendet: Sollwert, gesendet_at: float, now: float,
              frist_s: float = DEFAULT_UNCONFIRMED_AFTER_S) -> Stand:
        """Der Zustand des Sollwerts ueber die ganze Gruppe (R17).

        Die Rangfolge ist nicht beliebig: **eine** Ablehnung schlaegt alles
        andere durch. Sie ist dauerhaft, und ein Teammitglied, dem die
        Gruppe als "gesendet" erscheint, weil der zweite Supervisor noch
        schweigt, wartet auf etwas, das nie eintritt.
        """
        einzeln = [
            bewerten(m, gesendet=gesendet, gesendet_at=gesendet_at, now=now, frist_s=frist_s)
            for m in self.meldungen
        ]
        bestaetigt = [s for s in einzeln if s.zustand is Zustand.BESTAETIGT]
        abgelehnt = [s for s in einzeln if s.zustand is Zustand.ABGELEHNT]
        ja, alle = len(bestaetigt), len(einzeln)
        if abgelehnt:
            return Stand(Zustand.ABGELEHNT, grund=abgelehnt[0].grund,
                         gesendet_at=gesendet_at, bestaetigt_von=ja, von=alle)
        if bestaetigt and ja == alle:
            return Stand(Zustand.BESTAETIGT, gesendet_at=gesendet_at, bestaetigt_von=ja, von=alle)
        offen = [s for s in einzeln if s.zustand is not Zustand.BESTAETIGT]
        hinweis = next((s.hinweis for s in offen if s.hinweis), None)
        wartend = Zustand.UNBESTAETIGT if now - gesendet_at >= frist_s else Zustand.GESENDET
        return Stand(wartend, gesendet_at=gesendet_at, hinweis=hinweis,
                     bestaetigt_von=ja, von=alle)

    def pruefen(self, wert: Sollwert) -> Pruefung:
        """Einen Sollwert gegen die Angaben der meldenden Supervisoren pruefen (R15b)."""
        if wert.unten < 1:
            return Pruefung(False, grund="Die Untergrenze muss mindestens 1 sein.")
        if wert.unten > wert.oben:
            return Pruefung(
                False,
                grund=f"Die Untergrenze {wert.unten} liegt über der Obergrenze {wert.oben}.",
            )
        if self.meldungen and not self.autoskalierung and wert.unten != wert.oben:
            return Pruefung(
                False,
                grund=("Diese Gruppe läuft ohne Autoskalierung und nimmt nur eine feste "
                       "Prozesszahl an, keinen Bereich."),
            )
        kleinster = min(self.spielraeume, default=0)
        if self.meldungen and wert.oben > kleinster:
            return Pruefung(
                False,
                grund=f"Obergrenze {wert.oben} liegt über dem Spielraum {kleinster} dieser Gruppe.",
            )
        return Pruefung(True, wirkungsunbekannt=self.wirkungsunbekannt)

    def gesamt(self, wert: Sollwert) -> Sollwert:
        """Was der Wert je Supervisor fuer die ganze Gruppe bedeutet (R15a, AE4)."""
        n = self.supervisoren
        return Sollwert(wert.unten * n, wert.oben * n)


@dataclass(frozen=True, slots=True)
class Steuerbild:
    """Was die Ansicht ueber die Steuerung einer Gruppe braucht."""

    gruppe: Steuergruppe
    #: Der Sollwert, der im Kanal liegt -- oder ``None``: dann gelten die
    #: Startargumente des Deployments (R15, R16).
    sollwert: Sollwert | None = None
    #: Sein Zustand. ``None``, wenn gar kein Sollwert gesetzt ist.
    stand: Stand | None = None
    #: Warum das Bild leer ist -- der Kanal war nicht erreichbar. Eine
    #: Auskunft, keine Ausnahme: die Startseite muss auch dann laden (R33).
    fehler: str | None = None


class SetpointStore(Protocol):
    """Was die Steuerung vom Kontrollkanal braucht.

    Bewusst in den Worten des Dashboards und nicht in denen der Bibliothek:
    die Suche nach den Supervisoren ist ein SCAN ueber einen Schluesselraum
    (eine Auflistung gibt es im Kern nicht), und das gehoert hinter diese
    Naht, nicht in die Ansicht.
    """

    async def status_aller(self, gruppe: str) -> list[Mapping[str, Any]]: ...

    async def schreiben(self, gruppe: str, wert: Sollwert) -> None: ...

    async def loeschen(self, gruppe: str) -> None: ...

    async def gelesen(self, gruppe: str) -> Sollwert | None: ...


class Steuerung:
    """Sollwerte lesen, setzen und aufheben (U16).

    Fehlt der Kontrollkanal, ist ``verfuegbar`` falsch und ``grund`` sagt
    warum; die Seite bleibt benutzbar (R33, AE15).
    """

    def __init__(self, store: SetpointStore | None, *, grund: str | None = None) -> None:
        self._store = store
        self._grund = grund
        #: Wann welche Gruppe zuletzt einen Sollwert bekam. Nur hier, weil
        #: der Kontrollkanal keinen Zeitstempel des Sendens traegt. Ein
        #: Neustart vergisst ihn -- dann gibt es den Wert im Kanal noch,
        #: aber keine Frist mehr, und die Ansicht sagt das statt zu raten.
        self._gesendet: dict[str, float] = {}
        #: Der Grund des letzten gescheiterten Lesens, fuer das Steuerbild.
        self._letzter_fehler: str | None = None

    @property
    def verfuegbar(self) -> bool:
        return self._store is not None and self._grund is None

    @property
    def grund(self) -> str | None:
        return self._grund

    async def gruppe(self, name: str) -> Steuergruppe:
        """Das Bild einer Gruppe aus den Rueckmeldungen ihrer Supervisoren.

        Wirft nicht: ein nicht erreichbarer Kanal ist eine leere Gruppe, und
        der Grund steht im Steuerbild.
        """
        if self._store is None:
            return Steuergruppe(name, [])
        try:
            return Steuergruppe(name, list(await self._store.status_aller(name)))
        except Exception as exc:
            self._letzter_fehler = str(exc)
            return Steuergruppe(name, [])

    def gesendet_at(self, gruppe: str) -> float | None:
        """Wann zuletzt ein Sollwert fuer diese Gruppe gesendet wurde."""
        return self._gesendet.get(gruppe)

    async def setzen(self, gruppe: str, wert: Sollwert, *, now: float) -> Pruefung:
        """Einen Sollwert pruefen und schreiben (R15, R15b).

        Geprueft wird **vor** dem Schreiben: ein Wert, den die Oberflaeche
        schon als unmoeglich erkennt, gehoert nicht in den Kanal, wo er auf
        einen Supervisor wartet, der ihn ablehnt.
        """
        bild = await self.gruppe(gruppe)
        ergebnis = bild.pruefen(wert)
        if not ergebnis.erlaubt or self._store is None:
            return ergebnis
        await self._store.schreiben(gruppe, wert)
        self._gesendet[gruppe] = now
        return ergebnis

    async def bild(self, gruppe: str, *, now: float) -> Steuerbild:
        """Gruppe, Sollwert und sein Zustand -- und das Aufraeumen nach einer Ablehnung.

        Das Loeschen sitzt hier und nicht in der Ansicht, weil es an der
        **Feststellung** der Ablehnung haengt: wer den Zustand liest, hat die
        Pflicht, den verworfenen Wert aus dem Kanal zu nehmen (R16a, KTD13).
        """
        self._letzter_fehler = None
        bild = await self.gruppe(gruppe)
        if self._store is None or self._letzter_fehler is not None:
            return Steuerbild(bild, fehler=self._letzter_fehler)
        try:
            wert = await self._store.gelesen(gruppe)
        except Exception as exc:
            return Steuerbild(bild, fehler=str(exc))
        if wert is None:
            return Steuerbild(bild)
        gesendet_at = self._gesendet.get(gruppe)
        # Ohne gemerkten Sendezeitpunkt darf keine Frist laufen: sie
        # rechnete gegen 0 und faende jeden Wert sofort unbestaetigt.
        stand = bild.stand(
            gesendet=wert,
            gesendet_at=gesendet_at if gesendet_at is not None else now,
            now=now,
        )
        if gesendet_at is None and stand.zustand is Zustand.GESENDET:
            stand = replace(stand, hinweis=OHNE_SENDEZEITPUNKT, gesendet_at=0.0)
        if stand.zustand is Zustand.ABGELEHNT:
            try:
                await self._store.loeschen(gruppe)
                self._gesendet.pop(gruppe, None)
            except Exception as exc:
                # Der Zustand stimmt trotzdem -- nur das Aufraeumen fiel aus,
                # und das gehoert gesagt: der verworfene Wert liegt noch da.
                return Steuerbild(bild, sollwert=wert, stand=stand, fehler=str(exc))
        return Steuerbild(bild, sollwert=wert, stand=stand)

    async def aufheben(self, gruppe: str) -> None:
        """Den Sollwert aus dem Kanal nehmen (R15).

        Danach gelten wieder die Startargumente des Deployments -- nicht der
        vorige Sollwert. Ein Verlauf der Werte gehoert in den Audit-Strom,
        nicht in den Kanal.
        """
        if self._store is None:
            return
        await self._store.loeschen(gruppe)
        self._gesendet.pop(gruppe, None)


#: Praefix der Rueckmeldungen. Steht hier und nicht in der Bibliothek, weil
#: der Kern keine Auflistung anbietet -- das Dashboard muss den
#: Schluesselraum selbst absuchen (U16 Punkt 2).
STATUS_PREFIX = "sq:control:status:"
#: Wie lange ein Fehlversuch gilt, bevor der Kanal wieder gewaehlt wird.
#: Gemessen kostete ein Versuch gegen ein totes Backend 3,2 s **je Gruppe**;
#: ohne Sperre bezahlte das jeder Seitenaufruf neu -- auf der Startseite,
#: waehrend des Vorfalls. Kuerzer als der Pflegetakt der Erreichbarkeit
#: (``DEFAULT_MAINTENANCE_S``) waere sie wirkungslos, viel laenger liesse
#: die Rueckkehr des Backends zu lange unbemerkt.
SPERRE_S = 30.0
#: Frist fuer den Aufbau und fuer jede einzelne Abfrage. Kurz: die Antwort
#: "das Backend ist weg" ist auf dieser Seite so viel wert wie jede andere,
#: und sie muss sofort kommen.
CONNECT_TIMEOUT_S = 2.0


def _maskiert(gruppe: str) -> str:
    """Den Gruppennamen im SCAN-Muster als Text lesen lassen, nicht als Muster.

    Die Gruppe ist frei waehlbar (``--group LABEL``) und faellt sonst
    ungeprueft in die Mustersprache von SCAN.
    """
    for zeichen in ("\\", "*", "?", "[", "]"):
        gruppe = gruppe.replace(zeichen, "\\" + zeichen)
    return gruppe


class SwarmQSetpointStore(Zugang):
    """Der Kontrollkanal ueber den Sollwert-Speicher der Bibliothek.

    Die Verbindung entsteht **traege**, wie beim Stornieren und bei der
    Dead-Letter-Bedienung: die Worker-Uebersicht ist die Startseite, und wer
    sie nur oeffnet, soll keine zusaetzliche Backend-Verbindung bezahlen.
    Riegel, Sperrfrist und Schliessen kommen aus
    :class:`~swarmq_dashboard.zugang.Zugang`. Anders als die beiden Leser
    sperrt der Speicher nach einem Fehler mitten im Aufruf **nicht**: der
    Fehler geht an die ``Steuerung``, die ihn im Steuerbild nennt.
    """

    SPERRE_S = SPERRE_S
    CONNECT_TIMEOUT_S = CONNECT_TIMEOUT_S
    SPERRTEXT = "der Kontrollkanal ist nicht erreichbar: {exc}"

    def __init__(
        self,
        backend_url: str,
        *,
        store: Any | None = None,
        verbinden: Callable[[], Awaitable[Any]] | None = None,
        uhr: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(backend_url, kern=store, verbinden=verbinden, uhr=uhr)
        #: Ein eingesetzter Speicher gehoert dem Aufrufer: ``close()`` laesst
        #: ihn offen und erhalten.
        self._eigen = store is None

    async def _kern_auf(self, client: Any) -> Any:
        """Den Sollwert-Speicher der Bibliothek auf dem **eigenen** Klienten.

        ``ValkeySetpointStore(client=...)`` uebernimmt einen fertigen
        Klienten mitsamt dessen Einstellungen -- und schliesst ihn nicht,
        deshalb schliesst ``close()`` ihn selbst. Geprueft wird ueber den
        ``ping()`` des Speichers, damit ein Fehlschlag in dessen Worten
        erscheint.
        """
        from swarmq.backend.valkey.control import ValkeySetpointStore

        store = ValkeySetpointStore(client=client)
        await store.ping()
        return store

    async def connect(self) -> None:
        """Verbindung aufbauen. Idempotent. Wirft benannt.

        Nach einem Fehlversuch bleibt die Wahl fuer ``SPERRE_S`` aus: sonst
        bezahlt jeder Seitenaufruf denselben Zeitablauf noch einmal.
        """
        await super().connect()

    async def _kern_schliessen(self) -> None:
        if self._eigen:
            await super()._kern_schliessen()

    async def _verbunden(self) -> Any:
        # ``connect()`` liefert hier nichts (R2); der Speicher kommt aus der Basis.
        return await super().connect()

    async def status_aller(self, gruppe: str) -> list[Mapping[str, Any]]:
        """Die Rueckmeldungen aller Supervisoren dieser Gruppe (U16 Punkt 2)."""
        store = await self._verbunden()
        muster = f"{STATUS_PREFIX}{_maskiert(gruppe)}:*"
        vorn = len(f"{STATUS_PREFIX}{gruppe}:")
        gefunden: list[Mapping[str, Any]] = []
        async for roh in store.client.scan_iter(match=muster):
            schluessel = roh.decode(errors="replace") if isinstance(roh, bytes) else str(roh)
            kennung = schluessel[vorn:]
            meldung = await store.read_status(gruppe, kennung)
            if meldung is not None:
                gefunden.append(meldung)
        return gefunden

    async def schreiben(self, gruppe: str, wert: Sollwert) -> None:
        """Den Sollwert in den Kanal setzen (R15). Ersetzt den vorigen vollstaendig."""
        store = await self._verbunden()
        from swarmq.contracts.control import Setpoint

        await store.write_setpoint(gruppe, Setpoint(low=wert.unten, high=wert.oben))

    async def loeschen(self, gruppe: str) -> None:
        """Den Sollwert aus dem Kanal nehmen (R15, R16a)."""
        store = await self._verbunden()
        await store.clear_setpoint(gruppe)

    async def gelesen(self, gruppe: str) -> Sollwert | None:
        """Was im Kanal liegt. Unlesbares ist ``None`` -- der Supervisor lehnt es ohnehin ab."""
        store = await self._verbunden()
        roh = await store.read_setpoint(gruppe)
        unten, oben = getattr(roh, "low", None), getattr(roh, "high", None)
        if isinstance(unten, int) and isinstance(oben, int):
            return Sollwert(unten, oben)
        return None
