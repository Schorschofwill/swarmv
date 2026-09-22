"""Erzeugt das Sinnbild (Favicon) der Oberflaeche als eingebettetes PNG.

Das Ergebnis steht in ``src/swarmq_dashboard/web/templates/sinnbild.html``.
Aendert sich die Marke, laeuft dieses Skript neu und ersetzt dort die
Datenadresse -- von Hand ist eine Base64-Zeile nicht zu pflegen.

Ein PNG und **kein** SVG: ein eingebettetes SVG traegt zwingend
``xmlns="http://www.w3.org/2000/svg"``, und der Waechter gegen fremde Netze
(KTD7) prueft stumpf auf ``http``. Die Ausnahme waere sachlich richtig -- der
Namensraum wird nie geholt -- und genau solche Ausnahmen verdecken spaeter
den echten Verweis nach aussen.

    python docs/reference/sinnbild.py
"""

from __future__ import annotations

import base64
import struct
import zlib

N = 32
BG = (0x2F, 0x6F, 0x4E)  # --brand der hellen Fassung
FG = (0xFF, 0xFF, 0xFF)
#: Sechs Punkte im Kreis: der Schwarm.
PUNKTE = [(16, 8), (23, 12), (23, 20), (16, 24), (9, 20), (9, 12)]
RADIUS, ECKE = 3.0, 7.0


def _anteil(x: int, y: int, treffer: object) -> float:
    """Vierfache Abtastung eines Pixels -- ohne sie sind die Raender stufig."""
    n = 0
    for dx in (0.25, 0.75):
        for dy in (0.25, 0.75):
            if treffer(x + dx, y + dy):  # type: ignore[operator]
                n += 1
    return n / 4.0


def _punkt(px: float, py: float) -> bool:
    return any((px - cx) ** 2 + (py - cy) ** 2 <= RADIUS * RADIUS for cx, cy in PUNKTE)


def _flaeche(px: float, py: float) -> bool:
    cx = min(max(px, ECKE), N - ECKE)
    cy = min(max(py, ECKE), N - ECKE)
    return (px - cx) ** 2 + (py - cy) ** 2 <= ECKE * ECKE


def _stueck(art: bytes, nutz: bytes) -> bytes:
    return (struct.pack(">I", len(nutz)) + art + nutz
            + struct.pack(">I", zlib.crc32(art + nutz) & 0xFFFFFFFF))


def png() -> bytes:
    roh = bytearray()
    for y in range(N):
        roh.append(0)  # Filtertyp "none" je Zeile
        for x in range(N):
            deckung = _anteil(x, y, _punkt)
            farbe = tuple(round(BG[i] * (1 - deckung) + FG[i] * deckung) for i in range(3))
            roh += bytes(farbe) + bytes([round(255 * _anteil(x, y, _flaeche))])
    return (b"\x89PNG\r\n\x1a\n"
            + _stueck(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0))
            + _stueck(b"IDAT", zlib.compress(bytes(roh), 9))
            + _stueck(b"IEND", b""))


if __name__ == "__main__":
    bild = png()
    print(f'<link rel="icon" type="image/png" href="data:image/png;base64,'
          f'{base64.b64encode(bild).decode()}">')
