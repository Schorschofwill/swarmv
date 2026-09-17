"""Worker-Hook: Signale spiegeln und Argumente beim Aufgreifen melden.

Die harte Zusage dieser Einheit: das Ziel wirft nie und wartet nie. swarmq
huellt jede Ausnahme des --configure-Ziels in einen Konfigurationsfehler und
beendet den Worker mit einem Konfigurations-Exitcode, statt ihn neu zu starten
-- ein vergessener Umgebungswert wuerde die ganze Flotte am Hochfahren
hindern.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Das Ziel wirft nie ===
1. Eine unvollstaendige Einstellung laesst setup() ohne Ausnahme zurueckkehren.
2. Bei unvollstaendiger Einstellung wird nichts registriert -- der Worker laeuft wie ohne Hook (R2).
3. setup() stellt keine Verbindung her; ein Aufruf vor dem Verbinden wirft nicht.

=== ZYKLUS 2: Signale vollstaendig spiegeln (R3) ===
4. Ein Signal mit undokumentiertem Zusatzfeld landet vollstaendig im Puffer.
5. Ein Signal ohne ein erwartetes Feld ergibt ein Ereignis, keinen Fehler.
6. Jedes Signal der Bibliothek ist registriert -- keines faellt still durch.

=== ZYKLUS 3: Argumente beim Aufgreifen (R3b, R31) ===
7. pre_execute erzeugt ein Ereignis mit Argumenten und Herkunft "aufgreifen".

=== ZYKLUS 4: Der Ausfuehrungspfad bleibt unberuehrt (R4, AE11a) ===
8. Covers AE11a. Ein blockierender Strom verlaengert den Handler nicht messbar.

=== ZYKLUS 5: Lebenszyklus ===
9. Beim Signal "Worker ist oben" geht ein Stammdaten-Ereignis hinaus (R14, R18).
10. Die Stammdaten tragen Queue-Menge samt Prioritaetsvarianten und Gruppe.
11. Das Signal "Worker geht herunter" beendet den Flusher nicht.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from swarmq import RabbitMQBroker, Signal, SwarmQ, ValkeyBackend

from swarmq_dashboard.events import Origin
from swarmq_dashboard.hooks.worker import META_EVENT, setup


def _app(**cfg: object) -> SwarmQ:
    return SwarmQ(
        broker=RabbitMQBroker("amqp://localhost:5699/"),
        backend=ValkeyBackend("valkey://localhost:6399/0"),
        **cfg,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")


# --- Zyklus 1 -----------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
def test_unvollstaendige_einstellung_wirft_nicht() -> None:
    app = _app()
    setup(app)  # darf unter keinen Umstaenden werfen


@pytest.mark.usefixtures("clean_env")
def test_unvollstaendige_einstellung_registriert_nichts() -> None:
    app = _app()
    setup(app)
    assert app.hook_buffer is None  # type: ignore[attr-defined]


@pytest.mark.usefixtures("env")
def test_setup_stellt_keine_verbindung_her() -> None:
    app = _app()
    setup(app)
    puffer = app.hook_buffer  # type: ignore[attr-defined]
    assert puffer is not None
    assert puffer.counters.buffered == 0


# --- Zyklus 2 -----------------------------------------------------------


@pytest.mark.usefixtures("env")
async def test_signal_mit_zusatzfeld_landet_vollstaendig() -> None:
    app = _app()
    setup(app)
    # queue ist bei TASK_STARTED undokumentiert, wird aber verschickt.
    await app._signal_dispatcher.dispatch(
        Signal.TASK_STARTED, task_id="a41f", task_name="render_pdf", queue="reports"
    )
    ereignisse = app.hook_buffer.peek()  # type: ignore[attr-defined]
    assert len(ereignisse) == 1
    assert ereignisse[0].type == "task_started"
    assert ereignisse[0].payload["queue"] == "reports"


@pytest.mark.usefixtures("env")
async def test_signal_ohne_erwartetes_feld_ergibt_ereignis() -> None:
    app = _app()
    setup(app)
    await app._signal_dispatcher.dispatch(Signal.TASK_LOCKED, task_id="x")
    ereignisse = app.hook_buffer.peek()  # type: ignore[attr-defined]
    assert len(ereignisse) == 1
    assert ereignisse[0].payload["task_id"] == "x"


@pytest.mark.usefixtures("env")
def test_jedes_signal_ist_registriert() -> None:
    app = _app()
    setup(app)
    registriert = app._signal_dispatcher._handlers
    fehlend = [s.name for s in Signal if s not in registriert]
    assert not fehlend, f"nicht gespiegelt: {fehlend}"


# --- Zyklus 3 -----------------------------------------------------------


@pytest.mark.usefixtures("env")
async def test_pre_execute_meldet_argumente_mit_herkunft_aufgreifen() -> None:
    app = _app()
    setup(app)
    await app.middleware_chain.run_pre_execute(
        {"task_id": "a41f", "task_name": "render_pdf", "queue": "reports",
         "args": [], "kwargs": {"report_id": 812}, "delivery_mode": "at_least_once"}
    )
    ereignisse = [e for e in app.hook_buffer.peek() if e.origin is Origin.PICKUP]  # type: ignore[attr-defined]
    assert ereignisse, "pre_execute hat nichts gemeldet"
    assert ereignisse[-1].payload["kwargs"] == {"report_id": 812}
    assert ereignisse[-1].payload["task_id"] == "a41f"


# --- Zyklus 4 -----------------------------------------------------------


@pytest.mark.usefixtures("env")
async def test_blockierender_strom_verlaengert_den_handler_nicht() -> None:
    """Covers AE11a."""
    app = _app()
    setup(app)
    t0 = time.perf_counter()
    for i in range(2_000):
        await app._signal_dispatcher.dispatch(
            Signal.TASK_COMPLETED, task_id=f"t{i}", task_name="x", queue="q", duration_ms=1.0
        )
    dauer = time.perf_counter() - t0
    assert dauer < 0.5, f"2.000 Signale brauchten {dauer:.3f}s — der Handler wartet auf etwas"


# --- Zyklus 5 -----------------------------------------------------------


@pytest.mark.usefixtures("env")
async def test_stammdaten_beim_hochfahren(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQD_WORKER_GROUP", "orders-eu")
    app = _app(queues=["orders", "billing"], concurrency=4)
    setup(app)
    await app._signal_dispatcher.dispatch(Signal.WORKER_ONLINE, worker_id="phx-w1:0")

    meta = [e for e in app.hook_buffer.peek() if e.type == META_EVENT]  # type: ignore[attr-defined]
    assert len(meta) == 1, "genau ein Stammdaten-Ereignis erwartet"
    p = meta[0].payload
    assert p["worker_id"] == "phx-w1:0"
    assert p["group"] == "orders-eu"
    assert p["concurrency"] == 4
    assert set(p["queues"]) == {"orders", "billing"}
    assert set(p["all_queues"]) == {
        "orders", "orders.high", "orders.low",
        "billing", "billing.high", "billing.low",
    }


@pytest.mark.usefixtures("env")
async def test_gruppenschluessel_faellt_auf_queue_menge_zurueck() -> None:
    app = _app(queues=["orders", "billing"])
    setup(app)
    await app._signal_dispatcher.dispatch(Signal.WORKER_ONLINE, worker_id="phx-w1:0")
    meta = [e for e in app.hook_buffer.peek() if e.type == META_EVENT]  # type: ignore[attr-defined]
    assert meta[0].payload["group"] == "billing,orders"


@pytest.mark.usefixtures("env")
async def test_herunterfahr_signal_beendet_den_flusher_nicht() -> None:
    app = _app()
    setup(app)
    puffer = app.hook_buffer  # type: ignore[attr-defined]
    await puffer.start()
    try:
        await app._signal_dispatcher.dispatch(Signal.WORKER_SHUTDOWN, worker_id="phx-w1:0")
        assert puffer.running is True, (
            "der Flusher wurde beendet — die Ausgaenge auslaufender Tasks gingen verloren"
        )
        await app._signal_dispatcher.dispatch(
            Signal.TASK_COMPLETED, task_id="spaet", task_name="x", queue="q", duration_ms=1.0
        )
        assert any(e.payload.get("task_id") == "spaet" for e in puffer.peek())
    finally:
        await puffer.stop()


# === Befunde aus der Codepruefung Stufe A ================================
#
# 12. setup() zweimal aufgerufen ergibt keine doppelten Ereignisse.
# 13. setup() uebernimmt einen vorhandenen Puffer, statt ihn zu ersetzen.
# 14. Der Herunterfahr-Handler wartet nicht auf Netz.


@pytest.mark.usefixtures("env")
async def test_setup_zweimal_ergibt_keine_doppelten_ereignisse() -> None:
    """Befund 7: nur der Anwendungs-Hook war gegen Doppelinstallation geschuetzt."""
    app = _app()
    setup(app)
    setup(app)
    await app._signal_dispatcher.dispatch(
        Signal.TASK_STARTED, task_id="a41f", task_name="render_pdf", queue="reports"
    )
    ereignisse = [e for e in app.hook_buffer.peek() if e.type == "task_started"]  # type: ignore[attr-defined]
    assert len(ereignisse) == 1, f"{len(ereignisse)} Ereignisse fuer ein Signal"


@pytest.mark.usefixtures("env")
async def test_setup_uebernimmt_vorhandenen_puffer() -> None:
    """Befund 4: bei umgekehrter Reihenfolge entstand ein zweiter, toter Puffer."""
    from swarmq_dashboard.hooks.app import install

    app = _app()
    install(app)                      # Anwendungs-Hook zuerst
    erster = app.hook_buffer          # type: ignore[attr-defined]
    setup(app)                        # dann der Worker-Hook
    assert app.hook_buffer is erster, "der Worker-Hook hat den Puffer ersetzt"

    await app.middleware_chain.run_post_enqueue(
        {"task_id": "x", "task_name": "t", "queue": "q", "args": [], "kwargs": {},
         "delivery_mode": "at_least_once"}
    )
    assert len(erster.peek()) == 1, "die Einreihung landete in einem anderen Puffer"


@pytest.mark.usefixtures("env")
async def test_herunterfahr_handler_wartet_nicht_auf_netz() -> None:
    """Befund 6: WORKER_SHUTDOWN laeuft mit 10 s Zeitgrenze, die Gnadenfrist tickt."""
    import time as _t

    app = _app()
    setup(app)
    puffer = app.hook_buffer  # type: ignore[attr-defined]

    class Haengend:
        async def connect(self) -> None: ...
        async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
            await asyncio.sleep(30)

    puffer._client = Haengend()
    puffer.record(_ev_worker())

    t0 = _t.perf_counter()
    await app._signal_dispatcher.dispatch(Signal.WORKER_SHUTDOWN, worker_id="phx-w1:0")
    dauer = _t.perf_counter() - t0
    assert dauer < 0.2, f"der Handler wartete {dauer:.2f}s auf den Strom"


def _ev_worker() -> object:
    from swarmq_dashboard.events import Event, Origin

    return Event(type="task_started", ts=1.0, source="phx-w1:0",
                 origin=Origin.PICKUP, payload={"task_id": "x"})


# 15. Der Herunterfahr-Weckruf bringt die gepufferten Ereignisse wirklich hinaus.
#     (Der aeltere Test dazu war wertlos: er lief ohne Flusher, also konnte
#     gar nichts blockieren, und er prueft nur die Dauer.)


@pytest.mark.usefixtures("env")
async def test_herunterfahr_weckruf_bringt_ereignisse_hinaus() -> None:
    geschrieben: list[int] = []

    class Langsam:
        async def connect(self) -> None: ...
        async def xadd_batch(self, key: str, events: list[bytes], maxlen: int) -> None:
            await asyncio.sleep(0.1)
            geschrieben.append(len(events))

    app = _app()
    setup(app)
    puffer = app.hook_buffer  # type: ignore[attr-defined]
    puffer._client = Langsam()
    puffer._interval = 5.0
    await puffer.start()
    await asyncio.sleep(0.02)

    puffer.record(_ev_worker())
    import time as _t
    t0 = _t.perf_counter()
    await app._signal_dispatcher.dispatch(Signal.WORKER_SHUTDOWN, worker_id="phx-w1:0")
    dauer = _t.perf_counter() - t0
    assert dauer < 0.2, f"der Handler wartete {dauer:.2f}s"

    await asyncio.sleep(0.4)
    vor_stop = len(geschrieben)
    await puffer.stop()
    assert vor_stop >= 1, "der Weckruf hat nichts ausgeloest; die Ereignisse blieben liegen"
