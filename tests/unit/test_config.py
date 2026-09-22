"""Einstellungen des Dienstes und der Hooks.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Pflichtangaben des Dienstes ===
1. Fehlt eine Pflicht-Umgebungsvariable, wirft der Dienst einen benannten Fehler.
2. Der Fehler nennt den Namen der fehlenden Variable, nicht nur "fehlt etwas".
3. Sind alle Pflichtangaben da, entsteht die Einstellung ohne Fehler.

=== ZYKLUS 2: Hooks sind nachsichtig, der Dienst ist streng ===
4. Dieselbe fehlende Variable laesst die Hook-Einstellung ohne Ausnahme zurueckkehren.
5. Die Hook-Einstellung meldet, dass sie unvollstaendig ist, statt still zu schweigen.

=== ZYKLUS 3: Eigener Valkey-Datenbankindex (KTD9) ===
6. Der Strom liegt standardmaessig auf einem anderen Index als das Backend.
7. Zeigen Strom und Backend auf denselben Index, wird das als Warnung benannt.

=== ZYKLUS 4: Vorgaben und Ueberschreibungen ===
8. Aufbewahrungsdauer und Fristen haben die im Plan festgelegten Vorgaben.
9. Jede davon laesst sich einzeln ueberschreiben, ohne die anderen zu veraendern.
"""

from __future__ import annotations

import pytest

from swarmq_dashboard.config import ConfigError, DashboardConfig, HookConfig


@pytest.mark.usefixtures("clean_env")
def test_fehlende_pflichtvariable_wirft_benannten_fehler() -> None:
    with pytest.raises(ConfigError) as exc:
        DashboardConfig.from_env()
    assert "SWARMQ_BROKER_URL" in str(exc.value)


@pytest.mark.usefixtures("clean_env")
def test_fehler_nennt_jede_fehlende_pflichtvariable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5672/")
    with pytest.raises(ConfigError) as exc:
        DashboardConfig.from_env()
    assert "SWARMQ_BACKEND_URL" in str(exc.value)
    assert "SWARMQ_BROKER_URL" not in str(exc.value)


@pytest.mark.usefixtures("clean_env")
def test_vollstaendige_umgebung_ergibt_einstellung(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")
    cfg = DashboardConfig.from_env()
    assert cfg.broker_url == "amqp://localhost:5699/"
    assert cfg.backend_url == "valkey://localhost:6399/0"


# --- Zyklus 2: Hooks sind nachsichtig -----------------------------------


@pytest.mark.usefixtures("clean_env")
def test_hook_einstellung_wirft_nie() -> None:
    hook = HookConfig.from_env()
    assert hook.stream_url is None


@pytest.mark.usefixtures("clean_env")
def test_hook_einstellung_meldet_unvollstaendigkeit() -> None:
    assert HookConfig.from_env().usable is False
    assert "SWARMQ_BACKEND_URL" in (HookConfig.from_env().reason or "")


@pytest.mark.usefixtures("clean_env")
def test_vollstaendige_hook_einstellung_ist_brauchbar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")
    hook = HookConfig.from_env()
    assert hook.usable is True
    assert hook.reason is None
    assert hook.stream_url == "valkey://localhost:6399/1"


# --- Zyklus 3: eigener Valkey-Datenbankindex (KTD9) ---------------------


@pytest.mark.usefixtures("clean_env")
def test_strom_liegt_standardmaessig_auf_anderem_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")
    cfg = DashboardConfig.from_env()
    assert cfg.stream_url == "valkey://localhost:6399/1"
    assert cfg.stream_url != cfg.backend_url
    assert cfg.shares_backend_db is False


@pytest.mark.usefixtures("clean_env")
def test_gleicher_index_wird_als_warnung_benannt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")
    monkeypatch.setenv("SQD_STREAM_URL", "valkey://localhost:6399/0")
    cfg = DashboardConfig.from_env()
    assert cfg.shares_backend_db is True
    assert "FLUSHDB" in (cfg.shared_db_warning or "")


# --- Zyklus 4: Vorgaben und Ueberschreibungen ---------------------------


@pytest.mark.usefixtures("clean_env")
def test_vorgaben_entsprechen_dem_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")
    cfg = DashboardConfig.from_env()
    assert cfg.retention_days == 7                 # KD7
    assert cfg.unconfirmed_after_s == 15.0         # R17: zwei Abtastintervalle plus Nachsicht
    assert cfg.disconnected_after_s == 40.0        # R19: Gnadenfrist 30 + 5 + 5
    assert cfg.session_idle_s == 1800.0            # R21
    assert cfg.backlog_cap == 300                  # R13a: gerechnet fuer 100k Tasks/Tag


@pytest.mark.usefixtures("clean_env")
def test_jede_vorgabe_einzeln_ueberschreibbar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")
    monkeypatch.setenv("SQD_RETENTION_DAYS", "3")
    cfg = DashboardConfig.from_env()
    assert cfg.retention_days == 3
    assert cfg.unconfirmed_after_s == 15.0         # unberuehrt
    assert cfg.disconnected_after_s == 40.0        # unberuehrt


@pytest.mark.usefixtures("clean_env")
def test_unbrauchbare_zahl_wirft_benannt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")
    monkeypatch.setenv("SQD_RETENTION_DAYS", "sieben")
    with pytest.raises(ConfigError) as exc:
        DashboardConfig.from_env()
    assert "SQD_RETENTION_DAYS" in str(exc.value)


# === Befunde aus der Codepruefung Stufe A ================================
#
# 10. Eine Unix-Socket-Adresse ueberlebt die Ableitung des Strom-Index.
# 11. Der FLUSHDB-Hinweis erkennt dieselbe Datenbank trotz Zugangsdaten.
# 12. Der FLUSHDB-Hinweis erkennt dieselbe Datenbank ohne ausgeschriebenen Index.


@pytest.mark.usefixtures("clean_env")
def test_unix_socket_ueberlebt_die_ableitung(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "unix:///var/run/valkey.sock?db=0")
    cfg = DashboardConfig.from_env()
    assert "/var/run/valkey.sock" in cfg.stream_url, f"Socketpfad verloren: {cfg.stream_url}"


@pytest.mark.usefixtures("clean_env")
def test_gleiche_datenbank_trotz_zugangsdaten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")
    monkeypatch.setenv("SQD_STREAM_URL", "valkey://:geheim@localhost:6399/0")
    cfg = DashboardConfig.from_env()
    assert cfg.shares_backend_db is True, "Zugangsdaten haben den Vergleich ausgehebelt"


@pytest.mark.usefixtures("clean_env")
def test_gleiche_datenbank_ohne_ausgeschriebenen_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost:6399/0")
    monkeypatch.setenv("SQD_STREAM_URL", "valkey://localhost:6399")
    cfg = DashboardConfig.from_env()
    assert cfg.shares_backend_db is True, "fehlender Index wurde nicht als 0 gelesen"


# 13. Der FLUSHDB-Hinweis erkennt dieselbe Datenbank trotz fehlendem Standardport.
# 14. Auch `valkey+unix` behaelt den Socketpfad.


@pytest.mark.usefixtures("clean_env")
def test_gleiche_datenbank_trotz_fehlendem_standardport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://localhost/0")
    monkeypatch.setenv("SQD_STREAM_URL", "valkey://localhost:6379/0")
    cfg = DashboardConfig.from_env()
    assert cfg.shares_backend_db is True, "der weggelassene Standardport hat den Vergleich ausgehebelt"


@pytest.mark.usefixtures("clean_env")
def test_valkey_unix_behaelt_den_socketpfad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://localhost:5699/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey+unix:///run/valkey.sock?db=0")
    cfg = DashboardConfig.from_env()
    assert "/run/valkey.sock" in cfg.stream_url, f"Socketpfad verloren: {cfg.stream_url}"
