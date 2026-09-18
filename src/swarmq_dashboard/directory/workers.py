"""Worker-Register: wer laeuft, seit wann, und woher wir das wissen.

**Die Quelle gehoert zur Aussage** (KTD10). Die Verbundenheit eines Workers
laesst sich aus drei Quellen ableiten, und sie sind verschieden verlaesslich:

1. **Supervisor-Rueckmeldung** -- kommt im Fuenf-Sekunden-Takt und meldet auch
   Spielraum und Slot-Kapazitaet. Sie setzt aber den Kontrollkanal voraus, und
   der ist in der installierten Bibliothek noch nicht veroeffentlicht.
2. **Lebenszeichen-Signal** -- setzt einen Metrik-Port **und** ein optionales
   Paket voraus und schweigt sonst **ohne Fehlermeldung**. Genau deshalb darf
   sein Ausbleiben nicht als "getrennt" gelesen werden.
3. **Letztes Signal ueberhaupt** -- gibt es immer, sobald ein Worker
   ueberhaupt etwas tut. Dafuer sagt es nichts ueber einen ruhigen Worker.

Daraus folgt die zweite Eigenheit: **die Frist haengt an der Quelle**.
Vierzig Sekunden ohne Rueckmeldung sind ein Ausfall; vierzig Sekunden ohne
irgendein Signal sind bei einem ruhigen Worker der Normalfall. Eine
einheitliche Frist meldete entweder falsche Ausfaelle oder verschwiege echte.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from dataclasses import dataclass, field

#: Frist ohne Supervisor-Rueckmeldung: Gnadenfrist 30 s + Terminierung 5 s +
#: Abtastung 5 s.
SUPERVISOR_STALE_S = 40.0
#: Frist ohne Lebenszeichen: dessen Takt plus Nachsicht.
HEARTBEAT_STALE_S = 35.0
#: Frist ohne **irgendein** Signal. Bewusst lang: ein Worker, der gerade
#: nichts zu tun hat, ist nicht getrennt. Lieber spaet melden als falsch.
SIGNAL_STALE_S = 900.0
#: Wie eine Gruppe heisst, die es nicht gibt. Nicht leer und nicht geraten --
#: eine geratene Gruppe waere eine Falschaussage.
KEINE_GRUPPE = "ohne Gruppe"


class Quelle(enum.IntEnum):
    """Woher die Aussage ueber die Verbundenheit stammt (KTD10).

    Als Zahl, damit "die beste gewinnt" ein Vergleich ist und keine
    Fallunterscheidung -- und damit eine schwaechere Quelle die festgehaltene
    nie senkt.
    """

    SIGNAL = 1
    HEARTBEAT = 2
    SUPERVISOR = 3


_FRIST = {
    Quelle.SIGNAL: SIGNAL_STALE_S,
    Quelle.HEARTBEAT: HEARTBEAT_STALE_S,
    Quelle.SUPERVISOR: SUPERVISOR_STALE_S,
}

#: Ereignisarten, die eine bestimmte Quelle belegen.
_QUELLE_JE_ART = {
    "sqd_worker_meta": Quelle.SIGNAL,
    "worker_heartbeat": Quelle.HEARTBEAT,
    "worker_online": Quelle.SIGNAL,
    "worker_shutdown": Quelle.SIGNAL,
    "supervisor_report": Quelle.SUPERVISOR,
}


@dataclass(frozen=True, slots=True)
class Worker:
    """Ein Worker, wie die Uebersicht ihn zeigt."""

    worker_id: str
    group: str | None
    slot: int | None
    queues: list[str] = field(default_factory=list)
    all_queues: list[str] = field(default_factory=list)
    concurrency: int | None = None
    pool: str | None = None
    started_at: float | None = None
    last_seen_at: float = 0.0
    source: Quelle = Quelle.SIGNAL

    def connected(self, *, now: float) -> bool:
        """Gilt der Worker als verbunden? Die Frist haengt an der Quelle."""
        return (now - self.last_seen_at) <= _FRIST[self.source]

    @property
    def stale_after_s(self) -> float:
        """Die Frist, die fuer diesen Worker gilt -- die Ansicht nennt sie."""
        return _FRIST[self.source]


class WorkerRegistry:
    """Lesender Zugang zum Worker-Register."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con
        self._con.row_factory = sqlite3.Row

    def all(self) -> list[Worker]:
        """Alle bekannten Worker, nach Kennung geordnet."""
        return [_zu_worker(z) for z in
                self._con.execute("select * from worker order by worker_id")]

    def get(self, worker_id: str) -> Worker:
        """Einen Worker. Wirft, wenn es ihn nicht gibt."""
        zeile = self._con.execute(
            "select * from worker where worker_id=?", (worker_id,)
        ).fetchone()
        if zeile is None:
            raise KeyError(worker_id)
        return _zu_worker(zeile)

    def by_group(self) -> dict[str, list[Worker]]:
        """Nach Gruppe gebuendelt. Worker ohne Gruppe kommen unter einem
        benannten Schluessel, nicht unter einem leeren."""
        gruppen: dict[str, list[Worker]] = {}
        for w in self.all():
            gruppen.setdefault(w.group or KEINE_GRUPPE, []).append(w)
        return gruppen

    def load(self) -> dict[str, tuple[int, int]]:
        """Je Worker: laufende und erledigte Tasks (R14).

        Beides steht schon im Verzeichnis -- der Kontrollkanal wird dafuer
        nicht gebraucht. "Belegt" ist die Zahl der laufenden Tasks dieses
        Workers; die Kapazitaet daneben kommt aus den Stammdaten.

        **Erledigt ist keine Lebenszeit-Zahl.** Sie reicht genau so weit wie
        das Verzeichnis, und nach einem Ablauf faellt sie. Die Ansicht muss
        das dazusagen, sonst liest sich "12" als "dieser Worker hat zwoelf
        Tasks gemacht" -- und der Fall nach dem Ablauf als Ausfall.
        """
        zahlen: dict[str, tuple[int, int]] = {}
        for zeile in self._con.execute(
            "select worker_id, "
            "sum(case when status = 'laufend' then 1 else 0 end) as laufend, "
            "sum(case when status in "
            "  ('fertig','gescheitert','storniert','dead_letter','abgelaufen','abgewiesen') "
            "  then 1 else 0 end) as erledigt "
            "from task where worker_id is not null group by worker_id"
        ):
            zahlen[str(zeile["worker_id"])] = (int(zeile["laufend"]),
                                               int(zeile["erledigt"]))
        return zahlen

    def known_queues(self) -> list[str]:
        """Alle Queues, von denen das Verzeichnis weiss.

        **Nicht nur die der bekannten Worker**: R18 schliesst den Fall
        ausdruecklich ein, dass der Strom gar keinen Worker kennt -- dann sind
        alle Konsumenten ohne Hook, und genau dann braeuchte man die Auskunft
        am dringendsten. Die Queues der Tasks kommen darum dazu; sie belegen,
        dass dort ueberhaupt gearbeitet wird.
        """
        aus_workern = {q for w in self.all() for q in (w.queues or [])}
        aus_tasks = {
            str(z[0]) for z in self._con.execute(
                "select distinct queue from task where queue is not null"
            )
        }
        return sorted(aus_workern | aus_tasks)

    def remove(self, worker_id: str) -> bool:
        """Einen Worker entfernen (R19). ``False`` heisst: gab es nicht.

        Ein Aufraeumen, kein Bann: kommt der Worker zurueck, legt ihn das
        naechste Signal wieder an. Alles andere waere ein Zustand, den niemand
        mehr sieht und den niemand aufhebt.
        """
        cur = self._con.execute("delete from worker where worker_id=?", (worker_id,))
        self._con.commit()
        return bool(cur.rowcount)


def _zu_worker(z: sqlite3.Row) -> Worker:
    return Worker(
        worker_id=str(z["worker_id"]),
        group=z["worker_group"],
        slot=z["slot"],
        queues=json.loads(z["queues"]) if z["queues"] else [],
        all_queues=json.loads(z["all_queues"]) if z["all_queues"] else [],
        concurrency=z["concurrency"],
        pool=z["pool"],
        started_at=z["started_at"],
        last_seen_at=float(z["last_seen_at"]),
        source=Quelle(int(z["source"])),
    )


def quelle_fuer(event_type: str) -> Quelle:
    """Welche Quelle eine Ereignisart belegt. Unbekanntes ist die schwaechste."""
    return _QUELLE_JE_ART.get(event_type, Quelle.SIGNAL)
