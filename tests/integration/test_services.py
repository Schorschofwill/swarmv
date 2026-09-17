"""Die Vorrichtung selbst: erreicht sie die Dienste, und trifft sie den richtigen Index?

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Vorrichtung ===
1. Die Einstellung aus der Testumgebung ist vollstaendig und zeigt auf die Testdienste.
2. Der Strom liegt auf einem anderen Valkey-Index als das Backend (KTD9).
"""

from __future__ import annotations

import pytest

from swarmq_dashboard.config import DashboardConfig

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("stream_env")
def test_testumgebung_ergibt_vollstaendige_einstellung(broker_url: str, backend_url: str) -> None:
    cfg = DashboardConfig.from_env()
    assert cfg.broker_url == broker_url
    assert cfg.backend_url == backend_url


@pytest.mark.usefixtures("stream_env")
def test_strom_trifft_nicht_den_index_der_bibliothek() -> None:
    cfg = DashboardConfig.from_env()
    assert cfg.shares_backend_db is False, cfg.shared_db_warning
