"""Live-Schicht: neue Ereignisse in offenen Ansichten (R13, R13a, R32).

**Warum Folgenummern und nicht der Deckel der Kanal-Verteilung.** KTD6 sagt
woertlich: "Laeuft sie ueber, wird nicht still verworfen, sondern die Ansicht
zum Neuladen aufgefordert." Die Ueberlaufbehandlung des Rahmens leistet genau
das nicht -- nachgelesen und nachgemessen an litestar 2.24:
``ChannelsPlugin._sub_worker`` ruft ``subscriber.put_nowait(payload)`` und
**verwirft den Rueckgabewert**; ``Subscriber.put_nowait`` meldet einen vollen
Puffer nur als ``False``. Mit Deckel 3 und zehn gesendeten Nachrichten liefert
``backoff`` n0 n1 n2 und ``dropleft`` n7 n8 n9 -- beide Male ohne jedes Signal
an die betroffene Verbindung. Waere die Live-Schicht darauf gebaut, staende
die Ansicht auf einem stillen Teilstand und hielte ihn fuer aktuell. Das ist
die Fehlerklasse, gegen die dieses ganze Dashboard gebaut wird.

Jede Nachricht traegt darum eine fortlaufende Nummer, und die Verbindung
vergleicht sie mit der zuletzt weitergereichten. Eine Luecke heisst Verlust --
**gleich wo er entstand**: im Puffer der Verteilung, im Backend oder bei einer
Wiederverbindung. Der Deckel bleibt; er verhindert das unbegrenzte Wachsen
einer stehengebliebenen Ansicht, worum es KTD6 zuerst geht. Er ist nur nicht
mehr die Stelle, an der der Verlust bemerkt wird.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncGenerator, Callable
from typing import Any

import msgspec
from litestar import Request, get
from litestar.channels import ChannelsPlugin
from litestar.channels.subscriber import Subscriber
from litestar.response import ServerSentEvent
from litestar.response.sse import ServerSentEventMessage

from swarmq_dashboard.web.auth import SESSION_ACCOUNT_KEY

#: Der eine Kanal. Mehr braucht es nicht: die Ansichten unterscheiden sich in
#: dem, was sie zeigen, nicht in dem, was sie hoeren wollen.
LIVE_CHANNEL = "live"
#: Art der Nachricht, die die Ansicht zum Neuladen bewegt (R13a).
RELOAD_KIND = "reload"
#: Art des Herzschlags. Er haelt zwischengeschaltete Proxys davon ab, die
#: Verbindung abzuraeumen -- eine abgeraeumte Verbindung sieht fuer die
#: Ansicht aus wie ein stilles System.
HEARTBEAT_KIND = "heartbeat"
#: Letzte Nachricht, wenn Sitzung oder Konto wegfallen (R32).
SESSION_ENDED_KIND = "session_ended"
#: Takt des Herzschlags und der Sitzungspruefung, in Sekunden.
DEFAULT_HEARTBEAT_S = 15.0

_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder(dict)


class LivePublisher:
    """Veroeffentlicht Ereignisse mit fortlaufender Nummer.

    Die Nummer ist prozessweit, nicht je Verbindung: nur so sieht jede
    Verbindung dieselbe Folge, und eine Luecke bei **einer** von ihnen ist
    ihr eigener Verlust.
    """

    def __init__(self, plugin: ChannelsPlugin, *, channel: str = LIVE_CHANNEL) -> None:
        self._plugin = plugin
        self._channel = channel
        self._seq = itertools.count(1)
        self._vergeben = 0

    @property
    def current_seq(self) -> int:
        """Die zuletzt vergebene Nummer -- der Anfangswert einer neuen Ansicht.

        Ohne ihn waere ein Ueberlauf **vor** der ersten zugestellten Nachricht
        unsichtbar: die erste Nummer setzte den Massstab, und alles davor
        Verworfene faende nie jemand. Eine Ansicht, die sich gerade verbunden
        hat, ist aber genau die, die am ehesten hinterherhinkt.
        """
        return self._vergeben

    async def publish(self, kind: str, payload: dict[str, Any]) -> int:
        """Ein Ereignis veroeffentlichen. Gibt seine Folgenummer zurueck."""
        seq = next(self._seq)
        self._vergeben = seq
        self._plugin.publish(
            _encoder.encode({"seq": seq, "kind": kind, "payload": payload}),
            self._channel,
        )
        return seq


async def live_events(
    subscriber: Subscriber,
    *,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    still_valid: Callable[[], bool] | None = None,
    start_seq: int | None = None,
) -> AsyncGenerator[dict[str, Any]]:
    """Der Strom einer offenen Ansicht.

    Drei Dinge zugleich, und alle drei im selben Takt:

    * **Ereignisse weiterreichen** und dabei die Folgenummern pruefen. Eine
      Luecke ergibt eine Neuladen-Nachricht (R13a) -- die Ansicht laedt den
      betroffenen Bereich vollstaendig neu, statt auf einem stillen
      Teilstand stehen zu bleiben.
    * **Herzschlag im Leerlauf**, damit zwischengeschaltete Proxys nicht
      abraeumen.
    * **Sitzung und Kontosperre pruefen** (R32). Faellt eine weg, geht eine
      letzte Nachricht hinaus und der Strom endet. Ohne das bleibt die Ansicht
      nach dem Sitzungsablauf offen und bekommt weiter Daten -- eine
      eingefrorene Seite, die aktuell aussieht.
    """
    letzte: int | None = start_seq
    strom = subscriber.iter_events()
    # Das Warten laeuft ueber eine **aufgehobene** Aufgabe, nicht ueber
    # ``wait_for(anext(...))``: dessen Zeitueberschreitung bricht den
    # ``anext``-Aufruf ab, und ein abgebrochener Generator ist danach
    # geschlossen. In einer ersten Fassung kam deshalb genau ein Herzschlag
    # und dann nichts mehr.
    warten: asyncio.Task[bytes] | None = None
    try:
        while True:
            if still_valid is not None and not still_valid():
                yield {"kind": SESSION_ENDED_KIND, "seq": letzte, "payload": {}}
                return
            if warten is None:
                warten = asyncio.ensure_future(anext(strom))
            fertig, _ = await asyncio.wait({warten}, timeout=heartbeat_s)
            if not fertig:
                # Kein Ereignis in dieser Frist: Herzschlag hinaus, die Aufgabe
                # laeuft weiter, und im naechsten Durchlauf wird die Sitzung
                # geprueft.
                yield {"kind": HEARTBEAT_KIND, "seq": letzte, "payload": {}}
                continue
            try:
                roh = warten.result()
            except StopAsyncIteration:
                return
            finally:
                warten = None

            try:
                nachricht = _decoder.decode(roh)
            except Exception:
                # Eine unlesbare Nachricht ist ein Verlust wie jeder andere.
                yield _neuladen(letzte)
                continue

            seq = nachricht.get("seq")
            if not isinstance(seq, int):
                yield _neuladen(letzte)
                continue
            if letzte is not None and seq != letzte + 1:
                # **Hier** wird der Verlust bemerkt -- nicht am Fuellstand des
                # Puffers, den der Rahmen ohne Rueckmeldung leert.
                yield _neuladen(letzte)
            letzte = seq
            yield nachricht
    finally:
        if warten is not None:
            warten.cancel()


def _neuladen(letzte: int | None) -> dict[str, Any]:
    return {
        "kind": RELOAD_KIND,
        "seq": letzte,
        "payload": {"reason": "Es sind Ereignisse verlorengegangen."},
    }


def sse_payload(nachricht: dict[str, Any]) -> ServerSentEventMessage:
    """Eine Nachricht als Server-Sent-Event.

    Die Art wandert in das ``event``-Feld: die Ansicht haengt ihre Behandlung
    daran, statt den Koerper zu durchsuchen -- und die Neuladen-Nachricht
    braucht eine eigene Behandlung, keinen Sonderfall im Datenpfad.
    """
    return ServerSentEventMessage(
        event=str(nachricht.get("kind", "message")),
        data=_encoder.encode(nachricht.get("payload") or {}).decode(),
        id=str(nachricht.get("seq") or ""),
    )


def _letzte_kennung(request: Request[Any, Any, Any]) -> int | None:
    """Die Folgenummer, bei der die Ansicht stehengeblieben ist.

    Aus ``Last-Event-ID`` (der Browser schickt sie bei der Wiederverbindung)
    oder aus ``?last`` fuer die erste Verbindung nach einem Neuladen. Alles,
    was keine Zahl ist, wird verworfen -- der Wert kommt von aussen, und
    eine unbrauchbare Angabe darf nicht dazu fuehren, dass die Ansicht
    dauerhaft einen Verlust meldet.
    """
    roh = request.headers.get("last-event-id") or request.query_params.get("last") or ""
    try:
        wert = int(roh)
    except (TypeError, ValueError):
        return None
    return wert if wert >= 0 else None


@get("/live", opt={"no_csrf": True})
async def live(request: Request[Any, Any, Any]) -> ServerSentEvent:
    """Der Live-Strom einer offenen Ansicht (R13, R13a, R32).

    Die Sitzungspflicht gilt hier wie ueberall: R23 nimmt die Live-Verbindung
    ausdruecklich **nicht** aus -- sie traegt alles, was die Seiten zeigen,
    und zwar fortlaufend. Das CSRF-Merkmal entfaellt, weil sie nur liest.
    """
    services = request.app.state.services
    konto = request.session.get(SESSION_ACCOUNT_KEY) if request.session else None

    def gilt_noch() -> bool:
        """Sitzung und Kontosperre im Takt nachsehen (R32).

        Eine offene Verbindung haelt eine Sitzung **nicht** am Leben -- sie
        liest den Zustand nur nach. Sonst bliebe eine vergessene Ansicht
        beliebig lange angemeldet.
        """
        speicher = getattr(services, "accounts", None)
        if speicher is None or not isinstance(konto, str):
            return False
        zustand = speicher.state_of(konto)
        return zustand is not None and not zustand.locked

    # ``Last-Event-ID`` schickt der Browser bei jeder Wiederverbindung von
    # selbst -- ``EventSource`` verbindet nach einem Abriss allein neu. Ohne
    # sie begann der Strom bei der **gerade aktuellen** Folgenummer: alles,
    # was waehrend der Trennung veroeffentlicht wurde, war weder zugestellt
    # noch als Luecke gemeldet. Gemessen: 25 s getrennt, zwei Ereignisse
    # still verschluckt. Der Docstring unten verspricht ausdruecklich das
    # Gegenteil -- "gleich wo er entstand: ... oder bei einer
    # Wiederverbindung".
    letzte = _letzte_kennung(request)
    start = letzte if letzte is not None else services.publisher.current_seq

    async def strom() -> AsyncGenerator[ServerSentEventMessage]:
        async with services.live.start_subscription([LIVE_CHANNEL]) as abo:
            async for nachricht in live_events(
                abo,
                heartbeat_s=DEFAULT_HEARTBEAT_S,
                still_valid=gilt_noch,
                start_seq=start,
            ):
                yield sse_payload(nachricht)

    return ServerSentEvent(strom())
