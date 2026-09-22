"""Eine Probe losschicken (ausserhalb des urspruenglichen Plans).

Der Zweck ist eng und der Umfang darum auch: pruefen, ob der Weg steht.
Reiht das Dashboard ein, greift ein Worker auf, laeuft der Task, kommt das
Ergebnis an, wandert ein Fehlschlag ins Dead-Letter?

Das Formular bietet genau das an, was die Bibliothek **je Einreihung**
entgegennimmt -- Queue, Verzoegerung oder Zeitpunkt, Entdoppelung -- und die
Argumente des Probe-Tasks. Alles andere (Fristen, Wiederholungen,
Ratenbegrenzung, Sperren, Verfall, Priorität) gehoert zur Registrierung des
Tasks und laesst sich beim Einreihen gar nicht setzen; das sagt die Seite
auch, statt Felder anzubieten, die nichts bewirken.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from litestar import Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Template

from swarmq_dashboard.probe import MAX_DAUER_S, PROBE_NAME, PROBE_QUEUES
from swarmq_dashboard.web.auth import SESSION_ACCOUNT_KEY
from swarmq_dashboard.web.probe import (
    MAX_ANZAHL,
    ProbeAuftrag,
    ProbeFehleingabe,
)


def _kontext(request: Request[Any, Any, Any], **extra: Any) -> dict[str, Any]:
    services = request.app.state.services
    return {
        "queues": PROBE_QUEUES,
        "max_anzahl": MAX_ANZAHL,
        "max_dauer": MAX_DAUER_S,
        "task_name": PROBE_NAME,
        "status": services.status(),
        "account": request.session.get(SESSION_ACCOUNT_KEY, ""),
        "aktiv": "probe",
        "csrf_token": request.session.get("csrf", ""),
        **extra,
    }


@get("/probe")
async def formular(request: Request[Any, Any, Any]) -> Template:
    """Das Formular. Die Rueckmeldung der letzten Einreihung steht in der Adresse."""
    return Template(
        "probe/formular.html",
        context=_kontext(
            request,
            hinweis=request.query_params.get("hinweis"),
            fehler=request.query_params.get("fehler"),
        ),
    )


def _zahl(wert: Any, vorgabe: float = 0.0) -> float:
    try:
        return float(str(wert).strip().replace(",", "."))
    except (TypeError, ValueError):
        return vorgabe


def _ganz(wert: Any, vorgabe: int = 0) -> int:
    try:
        return int(str(wert).strip())
    except (TypeError, ValueError):
        return vorgabe


@post("/probe")
async def senden(
    request: Request[Any, Any, Any],
    data: Annotated[dict[str, Any], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Redirect:
    """Proben einreihen (R27: die Aktion steht im Audit-Strom).

    Das Komma wird als Dezimaltrennzeichen angenommen: ein deutsches
    Formular bekommt "0,5" haeufiger als "0.5", und daraus stillschweigend
    0 zu machen waere die schlechteste aller Deutungen.
    """
    zeitpunkt = str(data.get("eta") or "").strip()
    eta: datetime | None = None
    if zeitpunkt:
        try:
            eta = datetime.fromisoformat(zeitpunkt)
            if eta.tzinfo is None:
                eta = eta.replace(tzinfo=UTC)
        except ValueError:
            return _zurueck(fehler=f"Der Zeitpunkt {zeitpunkt!r} ist keine gültige Angabe.")

    verzug = str(data.get("delay_s") or "").strip()
    auftrag = ProbeAuftrag(
        queue=str(data.get("queue") or "io"),
        anzahl=_ganz(data.get("anzahl"), 1),
        sleep_s=_zahl(data.get("sleep_s")),
        burn_s=_zahl(data.get("burn_s")),
        fail=str(data.get("fail", "")) == "ja",
        note=str(data.get("note") or "")[:120],
        delay_s=_zahl(verzug) if verzug else None,
        eta=eta,
        unique_for=_ganz(data.get("unique_for")) or None,
        unique_until=str(data.get("unique_until") or "") or None,
    )

    services = request.app.state.services
    konto = str(request.session.get(SESSION_ACCOUNT_KEY, "unbekannt"))
    try:
        kennungen = await services.probe_sender.senden(auftrag)
    except ProbeFehleingabe as exc:
        await services.audit.record(
            "probe_send", account=konto, target=auftrag.queue,
            outcome="abgelehnt", detail=str(exc)[:200],
        )
        return _zurueck(fehler=str(exc))
    except Exception as exc:
        await services.audit.record(
            "probe_send", account=konto, target=auftrag.queue,
            outcome="fehlgeschlagen", detail=str(exc)[:200],
        )
        return _zurueck(fehler=f"Nicht eingereiht: {exc}")

    await services.audit.record(
        "probe_send", account=konto, target=auftrag.queue, outcome="ok",
        detail=(f"{len(kennungen)}x {PROBE_NAME}, sleep={auftrag.sleep_s:g}s, "
                f"burn={auftrag.burn_s:g}s" + (", scheitert" if auftrag.fail else "")),
    )
    zeit = time.strftime("%H:%M:%S")
    return _zurueck(hinweis=f"{len(kennungen)} Probe(n) um {zeit} in die Queue "
                            f"{auftrag.queue} eingereiht.")


def _zurueck(*, hinweis: str = "", fehler: str = "") -> Redirect:
    teil = f"?hinweis={quote(hinweis)}" if hinweis else f"?fehler={quote(fehler)}"
    return Redirect(f"/probe{teil}", status_code=303)
