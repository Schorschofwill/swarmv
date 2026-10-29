"""Erzeugt das Sinnbild (Favicon) der Oberflaeche als eingebettetes PNG.

Das Ergebnis steht in ``src/swarmq_dashboard/web/templates/sinnbild.html``.
Aendert sich die Marke, laeuft dieses Skript neu und ersetzt dort die
Datenadresse -- von Hand ist eine Base64-Zeile nicht zu pflegen.

Quelle ist das SwarmV-Icon aus dem Logo-Set (``docs/assets/logo``), in der
Fassung mit heller Flaeche: Es bleibt auf heller wie dunkler Tab-Leiste
sichtbar. Verkleinert wird mit ImageMagick (``convert``).

Ein PNG und **kein** SVG: ein eingebettetes SVG traegt zwingend
``xmlns="http://www.w3.org/2000/svg"``, und der Waechter gegen fremde Netze
(KTD7) prueft stumpf auf ``http``. Die Ausnahme waere sachlich richtig -- der
Namensraum wird nie geholt -- und genau solche Ausnahmen verdecken spaeter
den echten Verweis nach aussen.

    python docs/reference/sinnbild.py
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

N = 32
QUELLE = Path(__file__).resolve().parents[1] / "assets/logo/favicon/swarmv-apple-touch-icon.png"


def png() -> bytes:
    return subprocess.run(
        ["convert", str(QUELLE), "-filter", "Lanczos", "-resize", f"{N}x{N}", "-strip", "png:-"],
        check=True,
        capture_output=True,
    ).stdout


if __name__ == "__main__":
    bild = png()
    print(f'<link rel="icon" type="image/png" href="data:image/png;base64,{base64.b64encode(bild).decode()}">')
