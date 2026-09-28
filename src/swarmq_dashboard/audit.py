"""Audit-Strom: wer hat was getan (R27).

Der Zweck ist eng: die Frage "wer hat das storniert" muss sich ohne
Protokolldateien beantworten lassen. Drei Entscheidungen folgen daraus, und
jede hat einen Test:

1. **Ein eigener Strom mit eigenem Deckel und eigener Frist.** R9 sagt
   ausdruecklich, dass das Budget des Task-Stroms den Audit nicht begrenzt.
   Bei hunderttausend Tasks am Tag schnitte der Laengendeckel des Task-Stroms
   den Audit binnen Minuten weg -- und gerade die seltenen Aktionen eines
   Teammitglieds sind die, die man Wochen spaeter sucht.

2. **Ein Schreibfehler haelt keine Aktion auf.** Die Anmeldung muss gehen,
   wenn Valkey weg ist (KTD8); ein nicht geschriebener Audit-Eintrag darf sie
   also nicht verhindern. Er verschwindet aber nicht schweigend: ``record``
   sagt, ob geschrieben wurde, und ``lost`` zaehlt mit.

3. **Die Aktionsarten sind eine feste Liste.** Ein Tippfehler waere sonst
   eine Aktion, die der Filter spaeter nicht findet -- ohne dass es jemand
   merkt.
"""

from __future__ import annotations

from typing import Any, Protocol

import msgspec

from swarmq_dashboard.events import AUDIT_STREAM

#: Die Aktionsarten aus R27. Feste Liste, damit ein Tippfehler auffaellt.
AUDIT_ACTIONS = (
    "login",
    "logout",
    "task_cancel",
    "setpoint_set",
    "setpoint_clear",
    "dlq_action",
    "worker_remove",
    "probe_send",
    "workflow_cancel",
)
#: Laengendeckel des Audit-Stroms -- eigenstaendig (R9). Gerechnet fuer sieben
#: Tage: selbst bei tausend Aktionen am Tag bleibt reichlich Luft, und ein
#: Audit-Eintrag ist ein Bruchteil der Groesse eines Task-Ereignisses.
AUDIT_MAXLEN = 100_000
#: Laengste Zeichenkette, die ein Detailtext mitfuehren darf.
MAX_DETAIL = 200


class AuditEntry(msgspec.Struct, frozen=True, omit_defaults=True):
    """Ein Eintrag im Audit-Strom."""

    action: str
    ts: float
    account: str
    target: str | None = None
    #: "ok", "abgelehnt", "abgewiesen", "fehlgeschlagen" -- das Ergebnis, nicht
    #: die Absicht. Eine abgelehnte Stornierung liest sich sonst wie eine
    #: vollzogene.
    outcome: str = "ok"
    detail: str | None = None


_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder(AuditEntry)


class AuditSink(Protocol):
    """Was der Audit vom Strom braucht."""

    async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None: ...

    async def read_after(self, key: str, last_id: str, count: int) -> list[tuple[str, bytes]]: ...

    async def trim_by_min_id(self, key: str, min_ms: int) -> int: ...


class AuditLog:
    """Haelt Aktionen fest und liest sie wieder."""

    def __init__(self, sink: AuditSink | Any | None, *, key: str = AUDIT_STREAM) -> None:
        self._sink = sink
        self._key = key
        #: Eintraege, die nicht geschrieben werden konnten. Kein stiller
        #: Verlust: die Kopfzeile kann die Zahl nennen.
        self.lost = 0

    # -- Schreiben -------------------------------------------------------

    async def record(
        self,
        action: str,
        *,
        account: str,
        target: str | None = None,
        outcome: str = "ok",
        detail: str | None = None,
        now: float | None = None,
    ) -> bool:
        """Eine Aktion festhalten. ``False`` heisst: nicht geschrieben.

        Wirft nur bei einer unbekannten Aktionsart -- das ist ein Fehler im
        Aufrufer, kein Betriebszustand. Alles andere, insbesondere ein
        fehlender oder gestoerter Strom, ergibt ``False``: die Aktion selbst
        darf daran nicht scheitern (KTD8).
        """
        if action not in AUDIT_ACTIONS:
            raise ValueError(
                f"unbekannte Aktionsart {action!r} -- erlaubt sind {', '.join(AUDIT_ACTIONS)}"
            )
        import time

        eintrag = AuditEntry(
            action=action,
            ts=time.time() if now is None else now,
            account=account,
            target=target,
            outcome=outcome,
            detail=_gekuerzt(detail),
        )
        if self._sink is None:
            self.lost += 1
            return False
        try:
            await self._sink.xadd_batch(self._key, [_encoder.encode(eintrag)], AUDIT_MAXLEN)
        except Exception:
            # Kein Protokollieren der Nutzlast: ein Detailtext kann eine
            # Kundenkennung tragen, und ein Protokoll erreicht weder die
            # Rechtezusage aus R7a noch die Trimmung aus R9.
            self.lost += 1
            return False
        return True

    # -- Lesen -----------------------------------------------------------

    async def read(
        self,
        *,
        account: str | None = None,
        action: str | None = None,
        limit: int = 1000,
    ) -> list[AuditEntry]:
        """Eintraege in Zeitreihenfolge, gefiltert.

        Die Reihenfolge ist die des Stroms -- er hat eine Uhr, und die
        Eintraege entstehen alle im selben Dienst.
        """
        if self._sink is None:
            return []
        roh = await self._sink.read_after(self._key, "", limit)
        eintraege = []
        for _, wert in roh:
            try:
                eintraege.append(_decoder.decode(wert))
            except Exception:
                continue  # ein unlesbarer Eintrag haelt die Auskunft nicht an
        if account is not None:
            eintraege = [e for e in eintraege if e.account == account]
        if action is not None:
            eintraege = [e for e in eintraege if e.action == action]
        return eintraege

    # -- Pflege ----------------------------------------------------------

    async def trim(self, *, older_than: float) -> int:
        """Alles vor ``older_than`` entfernen -- die eigene Frist aus R27."""
        if self._sink is None:
            return 0
        return int(await self._sink.trim_by_min_id(self._key, int(older_than * 1000)))


def _gekuerzt(detail: str | None) -> str | None:
    """Detailtext beschneiden.

    Der Audit-Strom hat weder die Rechtezusage aus R7a noch die Trimmung des
    Verzeichnisses -- was hier hineingeraet, steht dauerhaft und offen. Der
    Aufrufer traegt die Verantwortung fuer den Inhalt; die Laenge deckelt
    dieses Modul.
    """
    if detail is None:
        return None
    return detail[:MAX_DETAIL]
