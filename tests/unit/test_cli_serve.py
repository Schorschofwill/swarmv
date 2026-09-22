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
4. Ohne TLS sagt der Befehl, warum die Anmeldung so nicht gehen kann.
5. Mit Zertifikat und Schluessel reicht er beide durch.

=== ZYKLUS 4: Klartext-HTTP ist eine Sackgasse ===
7. Ohne TLS wird auch oertlich abgewiesen -- die Cookies tragen
   'Secure', und niemand kaeme hinein.
8. Mit --insecure-http laeuft es oertlich, und die Cookies verlieren
   ihr 'Secure'.
9. --insecure-http gilt nur oertlich.
6. Ein Zertifikat ohne Schluessel wird abgewiesen, statt still ohne TLS zu
   starten.

=== ZYKLUS 3: Die Anwendung entsteht aus der Umgebung ===
7. Die Fabrik baut eine Anwendung.
8. Eine unbrauchbare Umgebung wird benannt, nicht als Traceback geworfen.
"""

from __future__ import annotations

import io
import os
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
    code = main(["--data-dir", str(tmp_path), "serve", "--insecure-http"],
                out=aus, server=FakeServer, umgebung=_umgebung(tmp_path))
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
    code = main(["--data-dir", str(tmp_path), "serve", "--insecure-http"],
                out=aus, server=FakeServer, umgebung={})
    assert code != 0
    assert "SWARMQ_BROKER_URL" in aus.getvalue(), aus.getvalue()
    assert not FakeServer.letzte.get("serve")


# --- Zyklus 4: Klartext-HTTP ist eine Sackgasse -------------------------


def test_ohne_tls_wird_auch_oertlich_abgewiesen(tmp_path: Path) -> None:
    """Gefunden beim **ersten echten Start**, nicht beim Lesen.

    Die Sitzungs- und Merkmalscookies tragen ``Secure``. Kein Klient schickt
    sie ueber ``http`` -- auch nicht an ``127.0.0.1``. Nachgemessen: die
    Anmeldung antwortet mit 403 "Das Anmeldeformular ist abgelaufen", weil
    der POST ohne Cookies ankommt und damit ohne Sitzung.

    Der Befehl liess das bis dahin durch und schrieb sogar eine
    ``http://``-Adresse hin -- ein Versprechen, das er nicht halten kann.
    Genau das Bild, vor dem seine eigene Warnung fuer fremde Adressen
    schuetzt: alles laeuft, nichts geht.
    """
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--host", "127.0.0.1"],
                out=aus, server=FakeServer, umgebung=_umgebung(tmp_path))
    assert code != 0, aus.getvalue()
    assert "--insecure-http" in aus.getvalue(), aus.getvalue()
    assert not FakeServer.letzte.get("serve")


def test_mit_insecure_http_laeuft_es_oertlich(tmp_path: Path) -> None:
    """Der ausdrueckliche Weg fuer die Entwicklung.

    Er muss ausdruecklich sein: waere er die Vorgabe, liefe irgendwann ein
    Dashboard ohne TLS im Netz, und niemand haette es entschieden.
    """
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--host", "127.0.0.1",
                 "--insecure-http"], out=aus, server=FakeServer,
                umgebung=_umgebung(tmp_path))
    assert code == 0, aus.getvalue()
    assert FakeServer.letzte.get("serve")
    assert "nur fuer die Entwicklung" in aus.getvalue().lower() or \
        "entwicklung" in aus.getvalue().lower(), aus.getvalue()


def test_insecure_http_gilt_nur_oertlich(tmp_path: Path) -> None:
    """Sonst waere die Ausnahme der bequemste Weg, TLS ganz wegzulassen."""
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--host", "0.0.0.0",
                 "--insecure-http"], out=aus, server=FakeServer,
                umgebung=_umgebung(tmp_path))
    assert code != 0, aus.getvalue()
    assert not FakeServer.letzte.get("serve")


def test_die_anwendung_kennt_den_unterschied(tmp_path: Path) -> None:
    """Der Schalter muss bis zum Cookie durchschlagen -- sonst ist er ein
    Schild ohne Wirkung."""
    from litestar.middleware.session.server_side import ServerSideSessionConfig

    from swarmq_dashboard.config import DashboardConfig
    from swarmq_dashboard.web.app import create_app

    config = DashboardConfig(
        broker_url="amqp://guest:guest@127.0.0.1:1/", backend_url="valkey://127.0.0.1:1/0",
        stream_url="valkey://127.0.0.1:1/1", retention_days=7, unconfirmed_after_s=15.0,
        disconnected_after_s=40.0, session_idle_s=1800.0, backlog_cap=300,
        stream_stale_after_s=120.0, data_dir=tmp_path / "sqd-data",
    )

    def sicher(app: Any) -> bool:
        """Bis zum Cookie durchgereicht -- ueber den Rueckhalt der Sitzung,
        den Litestar in der Zwischenschicht haelt."""
        for m in app.middleware:
            rueckhalt = getattr(m, "kwargs", {}).get("backend")
            cfg = getattr(rueckhalt, "config", None)
            if isinstance(cfg, ServerSideSessionConfig):
                return bool(cfg.secure)
        raise AssertionError("keine Sitzungseinstellung gefunden")

    assert sicher(create_app(config)) is True, "die Vorgabe ist nicht sicher"
    assert sicher(create_app(config, secure_cookies=False)) is False, (
        "der Schalter erreicht das Cookie nicht"
    )


# --- Zyklus 5: --data-dir wirkt auch fuer den Dienst ---------------------


def test_data_dir_erreicht_den_arbeitsprozess(tmp_path: Path, monkeypatch: Any) -> None:
    """Der Befund aus dem ersten echten Start mit eigenem Datenverzeichnis.

    ``--data-dir`` galt nur fuer ``account`` und ``audit``. ``serve`` reichte
    es nirgends hin: Granian ruft ``aus_der_umgebung()``, und die liest
    ``SQD_DATA_DIR`` oder faellt auf ``./sqd-data`` zurueck. Wer seine Konten
    unter ``--data-dir /var/lib/sqd`` anlegte und den Dienst genauso startete,
    bekam ein frisch angelegtes, leeres ``./sqd-data`` im Arbeitsverzeichnis
    -- und auf die Anmeldung "Konto oder Passwort stimmt nicht". Ohne einen
    Hinweis darauf, dass der Dienst woanders hinsieht.

    Die bestehenden Tests konnten das nicht finden: sie setzten
    ``SQD_DATA_DIR`` **und** ``--data-dir`` auf denselben Pfad.
    """
    monkeypatch.delenv("SQD_DATA_DIR", raising=False)
    eigenes = tmp_path / "eigenes-verzeichnis"
    eigenes.mkdir()
    umgebung = {k: v for k, v in _umgebung(tmp_path).items() if k != "SQD_DATA_DIR"}
    aus = io.StringIO()
    code = main(["--data-dir", str(eigenes), "serve", "--insecure-http"],
                out=aus, server=FakeServer, umgebung=umgebung)
    assert code == 0, aus.getvalue()
    assert os.environ.get("SQD_DATA_DIR") == str(eigenes), (
        "der Arbeitsprozess sieht das Datenverzeichnis der Kommandozeile nicht"
    )


def test_ein_gesetztes_sqd_data_dir_bleibt_ohne_ausdrueckliches_argument(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Die Vorgabe des Arguments darf die Umgebung nicht ueberschreiben.

    Sonst laege das Datenverzeichnis eines Dienstes, der seine Einstellungen
    aus der Umgebung bezieht, ploetzlich im Arbeitsverzeichnis der Unit --
    weil ``--data-dir`` einen Vorgabewert hat, den niemand angegeben hat.
    """
    aus_der_umgebung_pfad = str(tmp_path / "aus-der-umgebung")
    monkeypatch.setenv("SQD_DATA_DIR", aus_der_umgebung_pfad)
    umgebung = dict(_umgebung(tmp_path), SQD_DATA_DIR=aus_der_umgebung_pfad)
    aus = io.StringIO()
    code = main(["serve", "--insecure-http"], out=aus, server=FakeServer, umgebung=umgebung)
    assert code == 0, aus.getvalue()
    assert os.environ.get("SQD_DATA_DIR") == aus_der_umgebung_pfad


# --- Zyklus 6: hinter einem vorgelagerten TLS-Abschluss ------------------


def test_hinter_einem_tls_proxy_laeuft_der_dienst_auch_ohne_zertifikat(tmp_path: Path) -> None:
    """Der dritte Weg aus dem eigenen Hilfetext -- den es bisher nicht gab.

    "Hinter einem vorgelagerten TLS-Abschluss lauschen (der spricht dann
    https nach aussen)" stand als Moeglichkeit in der Fehlermeldung. Gangbar
    war sie nicht: ohne ``--tls-cert`` verlangt der Start ``--insecure-http``,
    und das gilt nur auf 127.0.0.1. In einem Container muss der Dienst aber
    auf 0.0.0.0 lauschen, sonst erreicht ihn kein Portmapping -- der
    haeufigste Fall ueberhaupt war damit ausgeschlossen.
    """
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--host", "0.0.0.0",
                 "--behind-tls-proxy"],
                out=aus, server=FakeServer, umgebung=_umgebung(tmp_path))
    assert code == 0, aus.getvalue()
    assert FakeServer.letzte.get("serve"), "der Dienst wurde nicht gestartet"
    assert FakeServer.letzte["address"] == "0.0.0.0"


def test_hinter_dem_proxy_behalten_die_cookies_ihr_secure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Der Unterschied zu ``--insecure-http``, und er ist der ganze Punkt.

    Bei ``--insecure-http`` spricht der Browser selbst Klartext, das Merkmal
    ``Secure`` waere dort eine Sperre gegen die eigene Anmeldung. Hinter
    einem Abschluss spricht er https -- das Cookie gehoert also gesetzt, und
    es wegzulassen gaebe es ueber jede Klartextstrecke preis, die der
    Browser sonst noch nimmt.
    """
    monkeypatch.delenv("SQD_INSECURE_HTTP", raising=False)
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--host", "0.0.0.0",
                 "--behind-tls-proxy"],
                out=aus, server=FakeServer, umgebung=_umgebung(tmp_path))
    assert code == 0, aus.getvalue()
    assert os.environ.get("SQD_INSECURE_HTTP") != "1", (
        "hinter dem Abschluss verlieren die Cookies ihr 'Secure'"
    )


def test_eigenes_tls_und_proxy_zugleich_wird_abgewiesen(tmp_path: Path) -> None:
    """Zwei Aussagen ueber dieselbe Strecke -- eine davon waere unwahr."""
    aus = io.StringIO()
    code = main(["--data-dir", str(tmp_path), "serve", "--behind-tls-proxy",
                 "--tls-cert", str(tmp_path / "c.pem"), "--tls-key", str(tmp_path / "k.pem")],
                out=aus, server=FakeServer, umgebung=_umgebung(tmp_path))
    assert code == 2
    assert "dieselbe Strecke" in aus.getvalue()
    assert not FakeServer.letzte.get("serve"), "es wurde trotzdem gestartet"
