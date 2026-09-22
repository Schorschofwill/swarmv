# Betrieb: Dateien, Rechte, Konten

Was der Betreiber wissen muss, bevor der Dienst das erste Mal läuft. Der Teil
über die Dateien ist keine Empfehlung, sondern die Einlösung einer Zusage:
R7a verlangt, dass der Ort der Datei mit den Klartext-Argumenten
**dokumentiert** ist.

## Das Datenverzeichnis

Beide lokalen Dateien liegen in einem Verzeichnis, das mit `--data-dir`
gesetzt wird — für **alle** Unterbefehle, auch `serve`. Ohne das Argument
gilt `SQD_DATA_DIR`, sonst `./sqd-data` relativ zum Arbeitsverzeichnis des
Prozesses; bei einer systemd-Unit also relativ zu `WorkingDirectory`.

Die drei Wege müssen dasselbe Verzeichnis meinen. Wer Konten unter
`--data-dir /var/lib/sqd` anlegt und den Dienst ohne dieses Argument startet,
bekommt ein frisch angelegtes, leeres `./sqd-data` — und auf die Anmeldung
„Konto oder Passwort stimmt nicht", ohne einen Hinweis darauf, dass der Dienst
woanders hinsieht.

```
sqd-data/                 0700, gehört dem Dienstbenutzer
├── directory.sqlite3     0600  Task-Verzeichnis
└── accounts.sqlite3      0600  Konten (später auch Sitzungen und Bremszähler)
```

Dazu legt SQLite je Datei ein `-wal` und ein `-shm` an. Beide erben den Modus
der Hauptdatei; sie gehören zur Datei und dürfen im laufenden Betrieb nicht
einzeln angefasst werden.

Die Rechte werden bei **jedem** Start geprüft und nachgezogen, nicht nur beim
Anlegen — eine Datei aus einer Rücksicherung oder von einem `ExecStartPre`
brächte sonst ihre eigenen mit.

## Was gelöscht werden darf und was nicht

| Datei | Löschen? | Folge |
|---|---|---|
| `directory.sqlite3` | **ja** | Der Dienst baut sie beim nächsten Start aus dem Ereignisstrom neu auf und weist den Neuaufbau in der Kopfzeile aus (R8). Was älter ist als der Strom reicht, kommt nicht wieder. |
| `accounts.sqlite3` | **nein** | Konten stehen in keinem Strom. Ihr Verlust sperrt das ganze Team aus; ein neues Konto muss dann über die Kommandozeile angelegt werden. |

Deshalb liegen sie in zwei Dateien und nicht in einer (KTD8): der Neuaufbau
des Verzeichnisses ist ein vorgesehener Weg, und in einer gemeinsamen Datei
wäre er ein Aussperren.

Wird das Verzeichnis gelöscht, **alle drei** Dateien entfernen —
`directory.sqlite3`, `-wal` und `-shm`. Bleibt das `-wal` liegen, stehen die
Klartext-Argumente dort weiter, bis sich wieder jemand verbindet.

## Was in der Verzeichnisdatei steht

Die Task-Argumente im Klartext, sieben Tage lang (R7, KD11, KD16). Das ist
bewusst so entschieden: ohne die Argumente beantwortet das Dashboard die
Frage „welcher Kunde ist betroffen?" nicht, und genau dafür wird es benutzt.
Die Gegenleistung sind drei Zusagen:

1. Die Datei gehört dem Dienstbenutzer und ist nur für ihn lesbar.
2. Der Ablauf nach sieben Tagen löscht wirklich — `secure_delete` lässt keine
   Rohbytes stehen, und das Verdichten gibt den Platz zurück. Über alle
   Kombinationen gemessen: beide Wege tragen die Zusage je für sich.
3. Verhindert ein offener Leser das Zusammenführen des Journals, sagt der
   Dienst das in der Kopfzeile, statt die Löschung als erfolgt zu melden.

Wer das nicht will, muss die Aufbewahrung kürzen (`SQD_RETENTION_DAYS`) — es
gibt keinen Schalter, der die Argumente weglässt, ohne das Werkzeug zu
entwerten.

## Konten anlegen

```
$ swarmq-dashboard --data-dir /var/lib/sqd account create ops
Passwort fuer ops: 
Passwort wiederholen: 
Konto ops angelegt.
```

Der Befehl nimmt **kein** Passwort als Argument entgegen, weder als Option
noch als zweites Wort. Ein Passwort in der Kommandozeile stünde in der
Prozessliste, im Shell-Verlauf und in jedem Protokoll, das Kommandozeilen
mitschreibt — dauerhaft und für jeden auf dem Rechner sichtbar. Gefragt wird
verdeckt und mit Bestätigung.

Mindestlänge sind zwölf Zeichen. Zeichenklassen-Regeln gibt es bewusst nicht:
sie treiben nachweislich zu vorhersagbaren Mustern, während die Länge wirklich
trägt.

Weitere Unterbefehle:

```
swarmq-dashboard account list          # Name und Zustand, nie der Hashwert
swarmq-dashboard account passwd ops    # Passwort ersetzen
swarmq-dashboard account lock ops      # sperren -- das Konto bleibt bestehen
swarmq-dashboard account unlock ops
```

Sperren ist ein Zustand, kein Löschen. Ein gelöschtes Konto ließe sich unter
demselben Namen neu anlegen; ein gesperrtes nicht.

Passwörter liegen als argon2id. Werden die Verfahrensparameter später
angehoben, zieht der gespeicherte Wert beim nächsten erfolgreichen Anmelden
von selbst nach.

## Worker-Gruppen: `--group` und `SQD_WORKER_GROUP`

Der Kontrollkanal führt eine Worker-Gruppe unter dem Label aus `--group`; ohne
Label unter der sortierten, kommagetrennten Queue-Menge. Der Hook des
Dashboards läuft im **Kindprozess** und sieht dieses Argument nicht — er liest
`SQD_WORKER_GROUP` und fällt sonst ebenfalls auf die Queue-Menge zurück.

Beide müssen übereinstimmen, sonst führt das Dashboard dieselbe Gruppe unter
zwei Namen: die Übersicht unter dem einen, der Kontrollkanal unter dem
anderen. Die Steuerung findet dann nie einen Supervisor, und die Gruppe
erscheint dauerhaft als „kein Supervisor dieser Gruppe meldet sich".

```
$ export SQD_WORKER_GROUP=orders-eu
$ swarmq worker --module app.tasks --configure swarmq_dashboard.hooks.worker:setup \
    --queues orders --max-processes 16 --group orders-eu
```

Ohne `--group` genügt es, `SQD_WORKER_GROUP` wegzulassen: dann bilden beide
Seiten denselben Schlüssel aus den Queues.

`--max-processes` ist die zweite Voraussetzung. Ohne dieses Argument schaltet
der Supervisor den Kontrollkanal gar nicht ein: er meldet nichts, und ein
Sollwert erreicht ihn nicht. Die Worker-Übersicht sagt das an der betroffenen
Gruppe.

## Im Container

`Dockerfile` im Wurzelverzeichnis baut in zwei Stufen: die erste erzeugt das
Rad aus dem Quellbaum, die zweite installiert nur dieses Rad. Im
Laufzeit-Abbild liegt weder `uv` noch ein Compiler noch der Quellbaum.

Der Dienst läuft als eigener Benutzer `sqd`, das Datenverzeichnis ist
`/var/lib/sqd` und gehört ihm. Ohne Volume wäre die Kontendatei beim ersten
`docker rm` weg — sie steht in keinem Strom und käme nicht wieder.

### TLS im Container

Der voreingestellte Befehl lauscht auf `0.0.0.0:8080` mit
`--behind-tls-proxy`. Das ist kein Schlupfloch, sondern der zweite der drei
Wege aus der Startmeldung, und im Container der einzige gangbare: Auf
`127.0.0.1` zu lauschen erreichte kein Portmapping, und `--insecure-http`
gilt ausdrücklich nur örtlich.

Der Unterschied zwischen den beiden Schaltern ist das Sitzungscookie:

| Schalter | Cookie | wann |
|---|---|---|
| `--insecure-http` | ohne `Secure` | Entwicklung auf 127.0.0.1, sonst nirgends |
| `--behind-tls-proxy` | **mit** `Secure` | ein Abschluss davor spricht https nach außen |

Hinter dem Abschluss spricht der Browser https — das Merkmal gehört also
gesetzt. Klartext ist allein die Strecke zwischen Abschluss und Dienst; sie
abzusichern (eigenes Netz, mTLS, Unix-Socket) ist Sache des Betreibers.

Wer ohne Proxy prüfen will, reicht Zertifikate hinein und ersetzt den
Befehl:

```bash
docker run -d -p 8443:8443 -v /etc/ssl/sqd:/certs:ro \
  -e SWARMQ_BROKER_URL=... -e SWARMQ_BACKEND_URL=... -v sqd-data:/var/lib/sqd \
  swarmq-dashboard:0.1.0 serve --host 0.0.0.0 --port 8443 \
  --tls-cert /certs/cert.pem --tls-key /certs/key.pem
```

### Gesundheitsprüfung

Das Abbild fragt alle 30 Sekunden `/login` ab — eine öffentliche Seite.
`/api/status` wäre die genauere Auskunft, braucht aber eine Sitzung und
taugt darum nicht als Prüfung von außen.
