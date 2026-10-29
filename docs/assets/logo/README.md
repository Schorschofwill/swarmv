# SwarmV – Logo

Teil der gemeinsamen Logo-Familie von SwarmQ, SwarmV und Loadsuite: konzentrische
Bögen in Navy und Gold mit Ring-Knoten, das Projekt erkennt man am Linienbündel
(zwei Bündel bilden ein V). Alle Dateien haben einen transparenten Hintergrund.

| Datei | Verwendung |
|---|---|
| `swarmv-stacked-*.svg` | Zeichen über dem Namen (Titelseite, Präsentation) |
| `swarmv-horizontal-*.svg` | Zeichen neben dem Namen (README-Kopf, Navigationsleiste) |
| `swarmv-mark-*.svg` | nur das Zeichen, ab etwa 64 px |
| `swarmv-icon-*.svg` | vereinfachtes Zeichen für 16–48 px |
| `favicon/swarmv-favicon.svg` | Favicon, schaltet im Dunkelmodus selbst auf hell |
| `favicon/swarmv-favicon.ico` | Favicon 16/32/48 px für ältere Browser |
| `favicon/swarmv-apple-touch-icon.png`, `swarmv-icon-192.png`, `swarmv-icon-512.png` | Homescreen und Web-Manifest |
| `png/` | alle SVG-Varianten als PNG |

Varianten: `light` (für helle Hintergründe), `dark` (für dunkle: Navy wird
`#E8ECF4`), `mono-black`, `mono-white`.

Farben: Navy `#1F2B47`, Gold als Verlauf `#B4842F` → `#E8C766` → `#BF8F36`.
Schrift: Outfit Bold (OFL), in Pfade umgewandelt.

Erzeugt mit `docs/assets/logo/gen_logos.py` im swarmq-Repo; die PNGs sind daraus
mit Inkscape exportiert.

## Wo das Logo eingebunden ist

- README-Kopf: `swarmv-horizontal-*.svg` je Farbschema.
- Dashboard: Favicon als eingebettetes PNG in
  `src/swarmq_dashboard/web/templates/sinnbild.html`, erzeugt mit
  `docs/reference/sinnbild.py` aus `favicon/swarmv-apple-touch-icon.png`.
- Nach einer Änderung am Logo das Skript neu laufen lassen.

```html
<link rel="icon" href="favicon/swarmv-favicon.svg" type="image/svg+xml">
<link rel="icon" href="favicon/swarmv-favicon.ico" sizes="48x48">
<link rel="apple-touch-icon" href="favicon/swarmv-apple-touch-icon.png">
```
