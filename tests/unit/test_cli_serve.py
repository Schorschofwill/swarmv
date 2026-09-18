"""Der Dienst laesst sich starten (U18).

Der Befund, der diese Datei ausgeloest hat: ``swarmq-dashboard --help`` sagte
"Konten verwalten **und den Dienst fahren**" -- fahren liess er sich nicht. Es
gab ``account`` und ``audit``, sonst nichts. Granian stand als Abhaengigkeit
im Paket und wurde von keiner Zeile gerufen.

Das erklaert zugleich, warum eine Pruefung sechs nicht angeschlossene Naehte
fand und ein Nachlauf danach sieben Fehler in den Korrekturen: **nichts an
diesem Projekt ist je gelaufen.** Alle Belege kamen aus gelesenem Code,
Attrappen und Messskripten. Ein einziger echter Start haette die sechs Naehte
in fuenf Minuten gezeigt.

Test-Liste (AGENTS.md):

=== ZYKLUS 1: Es gibt einen Startweg ===
1. ``serve`` steht im Parser.
2. Der Befehl startet wirklich einen Server -- und zwar mit **einem**
   Arbeiter (KTD5).
3. Adresse und Port kommen aus der Kommandozeile.

=== ZYKLUS 2: Was der Betreiber wissen muss ===
4. Ohne TLS und ohne oertliche Adresse sagt der Befehl, warum die Anmeldung
   so nicht gehen kann.
5. Mit Zertifikat und Schluessel reicht er beide durch.
6. Ein Zertifikat ohne Schluessel wird abgewiesen, statt still ohne TLS zu
   starten.

=== ZYKLUS 3: Die Anwendung entsteht aus der Umgebung ===
7. Die Fabrik baut eine Anwendung.
8. Eine unbrauchbare Umgebung wird benannt, nicht als Traceback geworfen.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, ClassVar

import pytest

from swarmq_dashboard.cli.__main__ import main


class FakeServer:
    """Statt Granian. Schreibt mit, womit er gestartet worden waere."""

    letzte: ClassVar[dict[str, Any]] = {}

    def __init__(self, target: str, **kw: Any) -> None:
        FakeServer.letzte = {"target": target, **kw}
        self.gestartet = False

    def serve(self) -> None:
        FakeServer.letzte["serve"] = True


@pytest.fixture(autouse=True)
def _frische_attrappe() -> None:
    """Der Mitschrieb ist ein Klassenattribut -- ohne Ruecksetzen sieht ein
    Test den Start des vorigen und haelt ihn fuer seinen eigenen."""
    FakeServer.letzte = {}


def _umgebung(tmp_path: Path) -> dict[str, str]:
    """Eine brauchbare Umgebung. ``main`` nimmt sie als Naht entgegen -- der
    Arbeitsprozess liest spaeter ``os.environ``, aber die **Vorpruefung** vor
    dem Start soll sich hier nachstellen lassen."""
    return {
        "SWARMQ_BROKER_URL": "amqp://guest:guest@127.0.0.1:5699/",
        "SWARMQ_BACKEND_URL": "valkey://127.0.0.1:6399/0",
        "SQD_STREAM_URL": "valkey://127.0.0.1:6399/1",
        "SQD_DATA_DIR": str(tmp_path),
    }


# --- Zyklus 1 -----------------------------------------------------------


def test_serve_steht_im_parser() -> None:
    from swarmq_dashboard.cli.__main__ import _build_parser

    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["serve", "--gibtesnicht"])
    args = p.parse_args(["serve"])
    assert args.befehl == "serve"


def test_der_befehl_startet_einen_server_mit_einem_arbeiter(tmp_path: Path) -> None:
    """KTD5, und die Zahl ist nicht Geschmack.

    Der prozessinterne Riegel der Dead-Letter-Bedienung und die
    Kanal-Verteilung der Live-Schicht leben **im Prozess**. Mit zwei
    Arbeitern traegt beides nicht mehr, und zwar still: zwei Teammitglieder
    koennten dieselbe Queue gleichzeitig sichten, ohne dass es jemand merkt.
    """
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve"], out=aus, server=FakeServer,
                umgebung=_umgebung(tmp_path))
    assert code == 0, aus.getvalue()
    assert FakeServer.letzte.get("serve"), "es wurde kein Server gestartet"
    assert FakeServer.letzte["workers"] == 1, (
        f"gestartet mit {FakeServer.letzte['workers']} Arbeitern -- "
        "der DLQ-Riegel und die Live-Schicht leben im Prozess (KTD5)"
    )
    assert FakeServer.letzte["interface"] == "asgi"


def test_adresse_und_port_kommen_aus_der_kommandozeile(tmp_path: Path) -> None:
    aus = io.StringIO()
    main(["--data-dir", str(tmp_path), "serve", "--host", "10.0.0.5", "--port", "9443",
          "--tls-cert", str(tmp_path / "c.pem"), "--tls-key", str(tmp_path / "k.pem")],
         out=aus, server=FakeServer, umgebung=_umgebung(tmp_path))
    assert FakeServer.letzte["address"] == "10.0.0.5"
    assert FakeServer.letzte["port"] == 9443


# --- Zyklus 2 -----------------------------------------------------------


def test_ohne_tls_auf_fremder_adresse_sagt_der_befehl_warum(tmp_path: Path) -> None:
    """Die Sitzungscookies tragen ``secure``. Ueber eine Klartextverbindung
    schickt der Browser sie nicht mit -- die Anmeldung kann dann gar nicht
    gelingen, und zwar ohne jede Fehlermeldung im Dienst.

    Das ist die unangenehmste Art Fehlschlag: alles laeuft, nichts geht.
    """
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--host", "0.0.0.0"],
                out=aus, server=FakeServer)
    text = aus.getvalue()
    assert code != 0, f"der Start lief durch: {text}"
    assert "secure" in text.lower() or "TLS" in text, text
    assert not FakeServer.letzte.get("serve"), "es wurde trotzdem gestartet"


def test_auf_der_oertlichen_adresse_geht_es_ohne_tls(tmp_path: Path) -> None:
    """Fuer die Entwicklung und hinter einem vorgelagerten Abschluss."""
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--host", "127.0.0.1"],
                out=aus, server=FakeServer, umgebung=_umgebung(tmp_path))
    assert code == 0, aus.getvalue()
    assert FakeServer.letzte.get("serve")


def test_zertifikat_ohne_schluessel_wird_abgewiesen(tmp_path: Path) -> None:
    """Still ohne TLS zu starten waere die gefaehrlichste Antwort: der
    Betreiber glaubt, es sei verschluesselt."""
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--host", "0.0.0.0",
                 "--tls-cert", str(tmp_path / "c.pem")], out=aus, server=FakeServer)
    assert code != 0
    assert "schluessel" in aus.getvalue().lower() or "key" in aus.getvalue().lower()
    assert not FakeServer.letzte.get("serve")


def test_mit_zertifikat_und_schluessel_wird_beides_durchgereicht(tmp_path: Path) -> None:
    zertifikat, schluessel = tmp_path / "c.pem", tmp_path / "k.pem"
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--host", "0.0.0.0",
                 "--tls-cert", str(zertifikat), "--tls-key", str(schluessel)],
                out=aus, server=FakeServer, umgebung=_umgebung(tmp_path))
    assert code == 0, aus.getvalue()
    assert FakeServer.letzte["ssl_cert"] == zertifikat
    assert FakeServer.letzte["ssl_key"] == schluessel


# --- Zyklus 3 -----------------------------------------------------------


def test_die_fabrik_baut_eine_anwendung(tmp_path: Path, monkeypatch: Any) -> None:
    """Granian bekommt einen Modulpfad und ruft ihn selbst auf -- die Fabrik
    ist damit die eigentliche Naht zwischen Kommandozeile und Dienst."""
    from litestar import Litestar

    from swarmq_dashboard.web.app import aus_der_umgebung

    monkeypatch.setenv("SQD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SWARMQ_BROKER_URL", "amqp://guest:guest@127.0.0.1:1/")
    monkeypatch.setenv("SWARMQ_BACKEND_URL", "valkey://127.0.0.1:1/0")
    monkeypatch.setenv("SQD_STREAM_URL", "valkey://127.0.0.1:1/1")
    assert isinstance(aus_der_umgebung(), Litestar)


def test_eine_unbrauchbare_umgebung_wird_benannt(tmp_path: Path, monkeypatch: Any) -> None:
    """Ein Traceback beim Start sieht aus, als waere das Werkzeug kaputt --
    dabei fehlt nur eine Einstellung."""
    monkeypatch.delenv("SWARMQ_BROKER_URL", raising=False)
    monkeypatch.delenv("SWARMQ_BACKEND_URL", raising=False)
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve"], out=aus, server=FakeServer,
                umgebung={})
    assert code != 0
    assert "SWARMQ_BROKER_URL" in aus.getvalue(), aus.getvalue()
    assert not FakeServer.letzte.get("serve")
