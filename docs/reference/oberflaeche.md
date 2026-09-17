# Oberfläche des Dashboards (freigegeben 2026-09-17)

Verbindlich für den Bau. Die sichtbare Wahrheit ist das Mockup in
`docs/mockup/` (`index.html` öffnen); dieses Dokument nennt die Regeln
dahinter, das Stylesheet `oberflaeche.css` liefert sie als Code.

Beim Bau wandert `oberflaeche.css` nach `src/swarmq_dashboard/static/app.css`.
Ändert sich die Gestaltung, ändert sie sich dort **und** im Mockup — sonst
verliert das Mockup seine Rolle als Vorlage.

## Die eine Regel

**Farbe trägt Bedeutung, nie Marke.**

Gehäuse — Navigation, Kopfzeile, Karten, Knöpfe — ist grafit und papierfarben.
Farbe erscheint ausschließlich als Zustand. Wo im Bild Farbe ist, ist etwas los;
das macht die Seite auf einen Blick lesbar und ist der Gegenentwurf zum bunten
Flower. Eine Markenfarbe wurde erwogen und verworfen.

| Bedeutung | hell | dunkel | wofür |
|---|---|---|---|
| ok | `#2E6F52` | `#6FBF95` | verbunden, fertig, Sollwert aktiv |
| läuft | `#2A5F8F` | `#7FB2E5` | Task in Arbeit |
| wartet | `#8A5E12` | `#D9A54A` | unbestätigter Sollwert, Stornierung angefordert, Wiederholung, Lücke im Strom, Konsument ohne Hook |
| Fehler | `#A33A31` | `#E8817A` | gescheitert, **abgelehnter Sollwert**, Dead Letter, getrennter Worker |
| neutral | `#6E7068` | `#9A9C94` | wartend, storniert, keine Aussage |

Jede Zustandsmarke ist Punkt **und** Wort — Farbe allein trägt nie die Aussage.

## Schrift

IBM Plex Sans für Fließendes, IBM Plex Mono für alles Technische: Kennungen,
`worker_id`, Queue-Namen, Gruppennamen, Zeitstempel, jede Zahl.

Alles Feste läuft mit `font-variant-numeric: tabular-nums`. Das ist keine
Kosmetik: SSE tauscht Tabellenzeilen im laufenden Betrieb aus, und ohne
gleich breite Ziffern springt die Spalte, sobald eine Zahl eine Stelle
gewinnt.

| Rolle | Größe | Gewicht |
|---|---|---|
| Seitentitel (Kopfzeile) | 16 px | 600 |
| Detailtitel | 21 px | 600, mono |
| Gruppenname | 14,5 px | 600, mono |
| Tabellenzelle, Fließtext | 13 px | 400 |
| Spaltenkopf | 10,5 px | 500, Versalien, `.07em` |
| Zustandsmarke | 11,5 px | 500 |
| Meta und Fußzeilen | 12–12,5 px | 400 |

Die Schriften werden **selbst gehostet** unter `static/fonts/` — kein CDN, das
Dashboard soll im geschlossenen Netz laufen. Fehlen sie, greift der
System-Fallback: Layout und Farben bleiben, nur die Typografie ändert sich.

## Aufbau

Linke Navigation 216 px mit Worker, Tasks, Dead Letter, Audit; Kopfzeile 56 px
mit Seitentitel, einer Meta-Zeile und rechts dem Zustand von Broker, Backend
und Strom (R26). Darunter die Seite mit 20/24 px Rand.

Startseite ist die Worker-Übersicht (R24). Je Bereich eine Tabellenseite,
Details als eigene Seite. Keine Kacheln, keine Kennzahlenfelder (R25) — die
Gruppen-Metadaten stehen als schmale Textzeile unter dem Gruppenkopf, nicht
als Kachelfeld.

## Geometrie

| | |
|---|---|
| Karte | Radius 6 px, 1 px Linie |
| Bedienelement | Radius 4 px |
| Zustandsmarke | Höhe 21 px, Radius 3 px, Punkt 6 px |
| Knopf | 32 px (27 px in Tabellenzeilen, 36 px im Dialog, 38–40 px im Formular) |
| Tabellenzelle | 8 px / 14 px |
| Dialog | 452 px breit, Radius 8 px |

## Dunkle Fassung

Ein Stylesheet, zwei Fassungen. Die Marken sind über `light-dark()` definiert,
`color-scheme: light dark` steht auf `:root`. Ohne Zutun folgt die Oberfläche
dem Betriebssystem; `data-theme="light"` und `data-theme="dark"` auf `<html>`
überschreiben das, wenn ein Umschalter dazukommt.

Das Mockup zeigt beide Fassungen als getrennte Seiten (`Main.html`,
`WorkerDark.html`), weil statisches HTML keinen Umschalter hat. Im Bau ist es
eine Seite.

## Was die Zustände zeigen müssen

Diese Fälle sind im Mockup ausgestaltet und gehören in die Umsetzung, nicht
erst in eine zweite Runde:

- **Sollwert unbestätigt** (R17) — Zeitpunkt des Sendens und „erneut senden",
  nicht nur ein Ladezeichen.
- **Sollwert abgelehnt** (R16a, R17) — ein eigener, dauerhafter Zustand mit dem
  Grund im Wortlaut. Kein „erneut senden": Ablehnung ist endgültig, Unbestätigt
  ist vorübergehend, und wer beides gleich darstellt, schickt das Teammitglied
  in eine Wiederholungsschleife.
- **Sollwert je Supervisor** (R15a) — die Eingabe ist so beschriftet, nennt die
  Zahl der meldenden Supervisoren und die daraus folgende Gesamtzahl. Wer „4"
  als Gruppenzahl liest und die Gruppe läuft auf drei Hosts, bekommt zwölf.
- **Stornierung angefordert** (R12a) — zwischen „läuft" und dem Ausgang; ein
  rechnender Task bemerkt die Anforderung möglicherweise nie.
- **Konsument ohne Hook** (R18) — als Hinweisleiste in der Gruppe, mit den
  beiden Zahlen, aus denen die Differenz entsteht.
- **Worker getrennt** (R19) — Zeile bleibt stehen, gedämpft, mit dem Zeitpunkt
  des letzten Lebenszeichens.
- **Reichweite des Verzeichnisses** (R8) — dauerhaft sichtbar, sobald der Strom
  kürzer als sieben Tage ist.
- **Lücke im Strom** (R4a) — mit Zeitspanne und Zahl der verlorenen Ereignisse.
- **Strom verzögert** (R26) — Kopfzeile wechselt auf „wartet" und nennt den
  Zeitpunkt des letzten Signals.
- **Nicht mehr stornierbar** (AE3) — Knopf gesperrt **und** ein Satz, der sagt
  warum.
- **Leeren** (R30, AE12) — die Bestätigung nennt Queue und Nachrichtenzahl, und
  der Knopf wiederholt die Zahl.
- **Dead Letter sichten** (R28a) — die Seite sagt vor der Handlung, was sie
  kostet: Umreihung, zurückgesetzter Zustellzähler, möglicher Verlust bei
  einem Absturz. Die dauerhafte Liste kommt aus dem Verzeichnis und ist als
  möglicherweise unvollständig beschriftet; sie kostet nichts.

## Bewusste Abweichungen

**Knöpfe sind 32 px statt der üblichen 44 px Mindestgröße für Touch.** Das
Dashboard ist ein dichtes Schreibtisch-Werkzeug auf 1360 px Breite und
ausdrücklich nicht für Touch ausgelegt (R26). Tastaturbedienung und
Fokusrahmen bleiben davon unberührt und sind Pflicht.

## Was das Stylesheet nicht regelt

Die Klassennamen in `oberflaeche.css` sind englisch wie die Bezeichner im
swarmq-Kern, die Kommentare deutsch. Zustände hängen als eigene Klasse am
Baustein (`.tag.is-wait`, `.notice.is-fail`, `tr.is-gone`), damit eine Vorlage
nur eine Klasse tauschen muss. Die Namen sind ein Vorschlag, kein Vertrag: der
Bau darf sie umbenennen, solange Marken, Größen und Zustände bleiben.

Nicht entschieden und im Implementierungsplan zu klären: wie HTMX die
Fragmente schneidet (ganze Tabelle, Gruppe oder Zeile), welche `id` eine Zeile
für den Austausch per `hx-swap-oob` bekommt, und ob die Sollwert-Bedienung ein
Dialog bleibt oder inline in der Gruppenzeile editierbar wird.
