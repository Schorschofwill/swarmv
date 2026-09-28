"""Aufraeumen U3 -- die gemeinsame Basis der traege verbundenen Valkey-Klienten.

Ergebnisleser, Workflowleser und Sollwert-Speicher bauen ihre Verbindung
gleich auf: erst beim ersten Aufruf, hinter einem Riegel, mit einem eigenen
Klienten, der erst nach dem ``ping()`` behalten wird, und mit einer
Sperrfrist nach jedem Fehlversuch (KTD2). Die Basis traegt das einmal; die
Unterklassen liefern nur den Kern-Aufbau, den Sperrtext und ``SPERRE_S``.

Getestet wird ueber eine kleine Unterklasse, wie die drei Klienten sie
bilden.

Testliste (AGENTS.md). Erledigtes bleibt stehen, damit die Reihenfolge der
Zyklen nachvollziehbar ist.

Verbinden (R1):
  1. [x] ein eingesetzter Kern wird geliefert, ohne zu verbinden
  2. [x] der erste ``connect()`` verbindet, der zweite nicht mehr
  3. [x] zwei gleichzeitige erste ``connect()`` bauen genau eine Verbindung

Sperrfrist (R2):
  4. [x] gescheitertes Verbinden -> ``StreamUnavailable`` mit dem Sperrtext
         der Unterklasse; innerhalb der Frist wird nicht erneut gewaehlt
  5. [x] nach Ablauf von ``SPERRE_S`` der Unterklasse wird wieder versucht
  6. [x] ``_ausfall`` schliesst und sperrt mit demselben Sperrtext

Eigener Klient (R1):
  7. [x] der Kern entsteht auf einem eigenen Klienten mit der Frist der
         Unterklasse; er wird behalten und von ``close()`` geschlossen
  8. [x] scheitert der Aufbau auf dem Klienten, wird er genau einmal
         geschlossen und nicht behalten
  8a. [x] wirft dabei auch das Schliessen, bleibt der Grund der des Aufbaus

Schliessen (R2):
  9. [x] ``close()`` schliesst einen synchronen wie einen asynchronen Kern
         genau einmal und wirft nie
 10. [x] eine Unterklasse kann den Kern beim Schliessen behalten
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from swarmq_dashboard.stream import StreamUnavailable
from swarmq_dashboard.zugang import Zugang


class _Kern:
    """Was die Unterklasse auf dem Klienten baut."""

    def __init__(self, *, wirft: BaseException | None = None) -> None:
        self._wirft = wirft
        self.rufe = 0
        self.geschlossen = 0

    async def tu(self) -> str:
        self.rufe += 1
        if self._wirft is not None:
            raise self._wirft
        return "getan"

    async def close(self) -> None:
        self.geschlossen += 1


class _Probe(Zugang):
    """Die kleinste Unterklasse: ein Kern auf dem eigenen Klienten."""

    SPERRTEXT = "die Probe ist nicht erreichbar: {exc}"

    async def _kern_auf(self, client: Any) -> Any:
        await client.ping()
        return _Kern()

    async def benutzen(self) -> str:
        """Ein Aufruf wie ``lesen``: jeder Leitungsfehler wird zur Auskunft."""
        try:
            kern = await self.connect()
        except StreamUnavailable as exc:
            return str(exc)
        try:
            return str(await kern.tu())
        except Exception as exc:
            return await self._ausfall(exc)


@pytest.mark.timeout(5)
async def test_ein_eingesetzter_kern_wird_ohne_verbinden_geliefert() -> None:
    kern = _Kern()
    versuche = 0

    async def verbinden() -> Any:
        nonlocal versuche
        versuche += 1
        return _Kern()

    zugang = _Probe("valkey://x", kern=kern, verbinden=verbinden)

    assert await zugang.connect() is kern
    assert versuche == 0


class _Uhr:
    """Eine Uhr, die nur weitergeht, wenn der Test es sagt."""

    def __init__(self) -> None:
        self.jetzt = 1000.0

    def __call__(self) -> float:
        return self.jetzt


class _Verbinden:
    """Ein Verbindungsaufbau, der zaehlt und nach Wunsch scheitert."""

    def __init__(self, kern: Any | None = None) -> None:
        self.kern = kern
        self.versuche = 0

    async def __call__(self) -> Any:
        self.versuche += 1
        if self.kern is None:
            raise ConnectionError("Port 6399 zu")
        return self.kern


@pytest.mark.timeout(5)
async def test_der_erste_connect_verbindet_der_zweite_nicht_mehr() -> None:
    kern = _Kern()
    verbinden = _Verbinden(kern)
    zugang = _Probe("valkey://x", verbinden=verbinden, uhr=_Uhr())

    assert await zugang.connect() is kern
    assert await zugang.connect() is kern
    assert verbinden.versuche == 1


class _WartendesVerbinden:
    """Ein Verbindungsaufbau, der haengt, bis der Test ihn freigibt."""

    def __init__(self, kern: Any) -> None:
        self._kern = kern
        self.versuche = 0
        self.begonnen = asyncio.Event()
        self.frei = asyncio.Event()

    async def __call__(self) -> Any:
        self.versuche += 1
        self.begonnen.set()
        await self.frei.wait()
        return self._kern


@pytest.mark.timeout(5)
async def test_zwei_gleichzeitige_erste_connect_bauen_eine_verbindung() -> None:
    """R1: Ohne Riegel baute jeder erste Aufruf einen eigenen Klienten, und
    ``close()`` schloesse nur den letzten -- die anderen leckten."""
    kern = _Kern()
    verbinden = _WartendesVerbinden(kern)
    zugang = _Probe("valkey://x", verbinden=verbinden, uhr=_Uhr())

    erster = asyncio.create_task(zugang.connect())
    zweiter = asyncio.create_task(zugang.connect())
    await verbinden.begonnen.wait()
    verbinden.frei.set()

    assert await asyncio.gather(erster, zweiter) == [kern, kern]
    assert verbinden.versuche == 1


@pytest.mark.timeout(5)
async def test_nach_gescheitertem_verbinden_wird_innerhalb_der_frist_nicht_gewaehlt() -> None:
    """R2: Der Sperrtext kommt von der Unterklasse, und waehrend der Frist
    bezahlt kein Aufruf denselben Zeitablauf noch einmal."""
    verbinden = _Verbinden()
    zugang = _Probe("valkey://x", verbinden=verbinden, uhr=_Uhr())

    with pytest.raises(StreamUnavailable) as erster:
        await zugang.connect()
    with pytest.raises(StreamUnavailable) as zweiter:
        await zugang.connect()

    assert str(erster.value) == "die Probe ist nicht erreichbar: Port 6399 zu"
    assert str(zweiter.value) == str(erster.value)
    assert isinstance(erster.value.__cause__, ConnectionError)
    assert verbinden.versuche == 1


class _KurzeProbe(_Probe):
    """Eine Unterklasse mit eigener Sperrfrist, wie jedes Modul sie hat."""

    SPERRE_S = 5.0


@pytest.mark.timeout(5)
async def test_nach_ablauf_der_sperrfrist_der_unterklasse_wird_wieder_versucht() -> None:
    """R2: Jedes Modul behaelt sein eigenes ``SPERRE_S``. Die Sperre ist eine
    Pause, kein Urteil."""
    uhr = _Uhr()
    verbinden = _Verbinden()
    zugang = _KurzeProbe("valkey://x", verbinden=verbinden, uhr=uhr)
    with pytest.raises(StreamUnavailable):
        await zugang.connect()

    uhr.jetzt += 4.9
    with pytest.raises(StreamUnavailable):
        await zugang.connect()
    assert verbinden.versuche == 1

    uhr.jetzt += 0.2
    verbinden.kern = _Kern()
    assert await zugang.connect() is verbinden.kern
    assert verbinden.versuche == 2


@pytest.mark.timeout(5)
async def test_ein_ausfall_mitten_im_aufruf_schliesst_und_sperrt() -> None:
    """R2: Stirbt das Backend nach dem Verbinden, kehrte ``connect()`` sonst
    weiter sofort zurueck, und jeder Aufruf liefe in die volle Frist."""
    kern = _Kern(wirft=TimeoutError("keine Antwort"))
    verbinden = _Verbinden(kern)
    zugang = _Probe("valkey://x", verbinden=verbinden, uhr=_Uhr())

    erster = await zugang.benutzen()
    zweiter = await zugang.benutzen()

    assert erster == "die Probe ist nicht erreichbar: keine Antwort"
    assert zweiter == erster
    assert kern.rufe == 1
    assert kern.geschlossen == 1
    assert verbinden.versuche == 1


class _Klient:
    """Ein Valkey-Klient, dessen ``ping()`` nach Wunsch scheitert."""

    def __init__(
        self,
        *,
        ping_wirft: BaseException | None = None,
        aclose_wirft: BaseException | None = None,
    ) -> None:
        self._ping_wirft = ping_wirft
        self._aclose_wirft = aclose_wirft
        self.geschlossen = 0

    async def ping(self) -> None:
        if self._ping_wirft is not None:
            raise self._ping_wirft

    async def aclose(self) -> None:
        self.geschlossen += 1
        if self._aclose_wirft is not None:
            raise self._aclose_wirft


class _GeduldigeProbe(_Probe):
    """Eine Unterklasse mit eigener Frist fuer den Aufbau."""

    CONNECT_TIMEOUT_S = 0.5


@pytest.mark.timeout(5)
async def test_der_kern_entsteht_auf_einem_eigenen_klienten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1: Der Klient, den die Bibliothek sich sonst baut, ist fuer den
    Worker-Pfad eingestellt -- fuenf Versuche mit Backoff. Die Basis baut
    einen eigenen mit der Frist der Unterklasse und schliesst ihn selbst."""
    import valkey.asyncio

    klient = _Klient()
    gebaut: list[tuple[str, dict[str, Any]]] = []

    def from_url(url: str, **optionen: Any) -> _Klient:
        gebaut.append((url, optionen))
        return klient

    monkeypatch.setattr(valkey.asyncio.Valkey, "from_url", from_url)
    zugang = _GeduldigeProbe("valkey://localhost:6399/0", uhr=_Uhr())

    assert isinstance(await zugang.connect(), _Kern)
    assert gebaut == [(
        "valkey://localhost:6399/0",
        {"socket_connect_timeout": 0.5, "socket_timeout": 0.5},
    )]
    assert klient.geschlossen == 0

    await zugang.close()
    assert klient.geschlossen == 1


@pytest.mark.timeout(5)
async def test_ein_gescheiterter_aufbau_schliesst_den_eigenen_klienten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1: Behalten wird der Klient erst, wenn der Aufbau auf ihm durchkommt.
    Sonst schloesse ihn niemand -- und ``close()`` schliesst ihn danach nicht
    ein zweites Mal."""
    import valkey.asyncio

    klient = _Klient(ping_wirft=ConnectionError("Port 6399 zu"))
    monkeypatch.setattr(valkey.asyncio.Valkey, "from_url", lambda *a, **k: klient)
    zugang = _Probe("valkey://x", uhr=_Uhr())

    with pytest.raises(StreamUnavailable, match="Port 6399 zu"):
        await zugang.connect()
    assert klient.geschlossen == 1

    await zugang.close()
    assert klient.geschlossen == 1


@pytest.mark.timeout(5)
async def test_ein_werfendes_schliessen_verdeckt_den_grund_des_aufbaus_nicht(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Die Karte soll sagen, warum das Backend weg ist -- nicht, dass sich
    ein halb gebauter Klient nicht schliessen liess."""
    import valkey.asyncio

    klient = _Klient(
        ping_wirft=ConnectionError("Port 6399 zu"),
        aclose_wirft=RuntimeError("schon zu"),
    )
    monkeypatch.setattr(valkey.asyncio.Valkey, "from_url", lambda *a, **k: klient)
    zugang = _Probe("valkey://x", uhr=_Uhr())

    with pytest.raises(StreamUnavailable) as fehler:
        await zugang.connect()

    assert str(fehler.value) == "die Probe ist nicht erreichbar: Port 6399 zu"


class _SyncKern:
    """Ein Kern mit synchronem ``close``, der nach Wunsch wirft."""

    def __init__(self, *, wirft: bool = False) -> None:
        self._wirft = wirft
        self.geschlossen = 0

    def close(self) -> None:
        self.geschlossen += 1
        if self._wirft:
            raise RuntimeError("schon zu")


class _AsyncKern(_SyncKern):
    """Derselbe Kern mit asynchronem ``close``."""

    async def close(self) -> None:  # type: ignore[override]
        super().close()


@pytest.mark.timeout(5)
@pytest.mark.parametrize("art", [_SyncKern, _AsyncKern], ids=["synchron", "asynchron"])
@pytest.mark.parametrize("wirft", [False, True], ids=["sauber", "wirft"])
async def test_close_schliesst_kern_und_klienten_einmal_und_wirft_nie(
    monkeypatch: pytest.MonkeyPatch, art: type[_SyncKern], wirft: bool,
) -> None:
    """R2: ``close()`` gehoert in die Schliessliste von ``Services.shutdown()``.
    Ein Fehler beim Schliessen darf das Herunterfahren nicht abbrechen, und
    ein zweites ``close()`` schliesst nichts noch einmal."""
    import valkey.asyncio

    klient = _Klient(aclose_wirft=RuntimeError("schon zu") if wirft else None)
    kern = art(wirft=wirft)

    class _MitKern(_Probe):
        async def _kern_auf(self, client: Any) -> Any:
            return kern

    monkeypatch.setattr(valkey.asyncio.Valkey, "from_url", lambda *a, **k: klient)
    zugang = _MitKern("valkey://x", uhr=_Uhr())
    await zugang.connect()

    await zugang.close()
    await zugang.close()

    assert kern.geschlossen == 1
    assert klient.geschlossen == 1


class _BehaltendeProbe(_Probe):
    """Wie der Sollwert-Speicher mit ``store=``: der Kern gehoert dem Aufrufer."""

    async def _kern_schliessen(self) -> None:
        pass


@pytest.mark.timeout(5)
async def test_eine_unterklasse_kann_den_kern_beim_schliessen_behalten() -> None:
    """R2: Ein eingesetzter Kern bleibt offen und erhalten; der naechste
    Aufruf nutzt ihn weiter, ohne neu zu verbinden."""
    kern = _Kern()
    verbinden = _Verbinden(_Kern())
    zugang = _BehaltendeProbe("valkey://x", kern=kern, verbinden=verbinden, uhr=_Uhr())

    await zugang.close()

    assert await zugang.connect() is kern
    assert kern.geschlossen == 0
    assert verbinden.versuche == 0
