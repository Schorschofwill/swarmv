"""Anwendungs-Hook: jede unmittelbare Einreihung mit ihren Argumenten melden.

Nur dieser Hook kennt einen Task ab der Einreihung -- mit Argumenten, mit
Einreihungszeitpunkt und als wartend stornierbar (KD12, R3a). Scheduler- und
verzoegerte Einreihungen passieren ihn nie; die faengt der Worker-Hook beim
Aufgreifen (R3b, R31).

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Aufnahme ===
1. Eine Einreihung erzeugt ein Ereignis mit Kennung, Name, Queue und Argumenten.
2. Die Herkunft ist "einreihung", nicht "aufgreifen" (R31).
3. Ein Workflow-Schritt traegt zusaetzlich Workflow-Kennung und Schrittpfad.

=== ZYKLUS 2: Das Einreihen bleibt unberuehrt (R4) ===
4. Ein unerreichbarer Strom laesst das Einreihen gelingen.
5. install() wirft nie, auch bei unvollstaendiger Einstellung.
6. Bei unvollstaendiger Einstellung wird nichts registriert.

=== ZYKLUS 3: Ein Puffer je Prozess ===
7. Laeuft der Worker-Hook im selben Prozess, wird sein Puffer geteilt.
8. Zweimal installiert ergibt kein doppeltes Ereignis.
"""

from __future__ import annotations

import pytest
from swarmq import RabbitMQBroker, SwarmQ, ValkeyBackend

from swarmq_dashboard.events import Origin
from swarmq_dashboard.hooks.app import install
from swarmq_dashboard.hooks.worker import setup as worker_setup


def _app() -> SwarmQ:
    return SwarmQ(
        broker=RabbitMQBroker("amqp://localhost:5699/"),
        backend=ValkeyBackend("valkey://localhost:6399/0"),
    )


def _ctx(**extra: object) -> dict[str, object]:
    return {
        "task_id": "a41f8c2e",
        "task_name": "render_pdf",
        "queue": "reports",
        "args": [],
        "kwargs": {"report_id": 812, "locale": "de_AT"},
        "delivery_mode": "at_least_once",
        **extra,
    }


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")


# --- Zyklus 1 -----------------------------------------------------------


@pytest.mark.usefixtures("env")
async def test_einreihung_erzeugt_ereignis_mit_argumenten() -> None:
    app = _app()
    install(app)
    await app.middleware_chain.run_post_enqueue(_ctx())

    ereignisse = app.hook_buffer.peek()  # type: ignore[attr-defined]
    assert len(ereignisse) == 1
    p = ereignisse[0].payload
    assert p["task_id"] == "a41f8c2e"
    assert p["task_name"] == "render_pdf"
    assert p["queue"] == "reports"
    assert p["kwargs"] == {"report_id": 812, "locale": "de_AT"}


@pytest.mark.usefixtures("env")
async def test_herkunft_ist_einreihung() -> None:
    app = _app()
    install(app)
    await app.middleware_chain.run_post_enqueue(_ctx())
    assert app.hook_buffer.peek()[0].origin is Origin.ENQUEUE  # type: ignore[attr-defined]


@pytest.mark.usefixtures("env")
async def test_workflow_schritt_traegt_kennung_und_pfad() -> None:
    app = _app()
    install(app)
    await app.middleware_chain.run_post_enqueue(
        _ctx(workflow_id="wf-77", step_path="chain.2")
    )
    p = app.hook_buffer.peek()[0].payload  # type: ignore[attr-defined]
    assert p["workflow_id"] == "wf-77"
    assert p["step_path"] == "chain.2"


# --- Zyklus 2 -----------------------------------------------------------


@pytest.mark.usefixtures("env")
async def test_unerreichbarer_strom_laesst_einreihen_gelingen() -> None:
    app = _app()
    install(app)
    # Kein Verbindungsaufbau, kein Flusher: der Strom ist tot.
    await app.middleware_chain.run_post_enqueue(_ctx())
    assert app.hook_buffer.counters.buffered == 1  # type: ignore[attr-defined]


@pytest.mark.usefixtures("clean_env")
def test_install_wirft_nicht_ohne_einstellung() -> None:
    app = _app()
    install(app)


@pytest.mark.usefixtures("clean_env")
async def test_ohne_einstellung_wird_nichts_registriert() -> None:
    app = _app()
    install(app)
    assert app.hook_buffer is None  # type: ignore[attr-defined]
    await app.middleware_chain.run_post_enqueue(_ctx())  # darf nicht werfen


# --- Zyklus 3 -----------------------------------------------------------


@pytest.mark.usefixtures("env")
async def test_puffer_wird_mit_dem_worker_hook_geteilt() -> None:
    app = _app()
    worker_setup(app)
    vorher = app.hook_buffer  # type: ignore[attr-defined]
    install(app)
    assert app.hook_buffer is vorher, "der Anwendungs-Hook hat einen zweiten Puffer angelegt"


@pytest.mark.usefixtures("env")
async def test_zweimal_installiert_ergibt_kein_doppeltes_ereignis() -> None:
    app = _app()
    install(app)
    install(app)
    await app.middleware_chain.run_post_enqueue(_ctx())
    assert len(app.hook_buffer.peek()) == 1, "die Einreihung wurde doppelt gemeldet"  # type: ignore[attr-defined]
