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

## Wo die Ergebnisse liegen — und wann sie verschwinden

Anders als die Argumente steht das **Ergebnis** eines Tasks nirgends im
Dashboard. Es liegt im Backend der Bibliothek unter `sq:result:<task_id>`;
die Detailseite schlägt es bei jedem Aufruf dort nach und speichert es nicht
(R7, KD1). Eine Kopie im Verzeichnis wäre ein zweiter Ort mit einer zweiten
Löschpflicht — und das Verzeichnis könnte die Verfallsfrist des Ergebnisses
gar nicht einhalten, weil es sie nicht kennt.

**Die Frist ist eine Kaskade, kein Feld.** Wer in `TaskInfo.result_ttl`
nachsieht, findet dort meist `None`. Der Worker löst sie in dieser Reihenfolge
auf:

1. `TaskInfo.result_ttl` des Tasks, wenn gesetzt;
2. sonst `SWARMQ_RESULT_TTL` beziehungsweise `config.result_ttl` der
   Anwendung — **Vorgabe 86400 Sekunden, also 24 Stunden**;
3. sonst 24 Stunden.

Ein aufgelöster Wert von `0` heißt: es wird **gar nichts** abgelegt. Das
trifft auf Fire-and-Forget ohne eigene Frist zu und auf ein ausdrücklich
gesetztes `SWARMQ_RESULT_TTL=0`. Der Dead-Letter-Pfad nimmt immer den
globalen Wert, nie den des Tasks.

Darum nennt die Seite bei einem fehlenden Ergebnis **beide** Ursachen —
abgelaufen oder nie gespeichert. Unterscheiden kann sie die zwei nicht, und
eine davon zu behaupten wäre geraten.

**Die beiden Aufbewahrungen sind verschieden lang.** Das Verzeichnis hält
Tasks sieben Tage (`SQD_RETENTION_DAYS`, bewusst so gewählt, damit ein
Vorfall vom Wochenende am Montag noch dasteht). Ergebnisse verfallen per
Vorgabe nach einem Tag. Der Montagsblick auf den Samstagstask zeigt den Task
also, aber keine Antwort mehr darauf, was er zurückgab. Wer das anders will,
setzt `SWARMQ_RESULT_TTL` **in der Anwendung** entsprechend hoch — das
Dashboard hat dafür keinen Schalter, und einen zu bauen hieße, das Ergebnis
doch zu kopieren.

**Was das für die Sichtbarkeit bedeutet.** Jedes angemeldete Konto sieht auf
der Detailseite den vollständigen Rückgabewert jedes beendeten Tasks — auch
dann, wenn darin etwas steht, das in keinem Argument vorkam: ein Token, eine
signierte URL, ein ausgelesener Datensatz. Für die Argumente ist diese
Offenheit oben als bewusste Entscheidung festgehalten; für Rückgabewerte
liegt der einzige Hebel in der Anwendung. Solche Werte nicht zurückgeben,
oder die Frist kurz halten.

## Die Workflow-Ansicht

Der Bereich **Workflows** zeigt Ketten, Gruppen und Chords mit Status,
Fortschritt und Baum. Anders als die Task-Liste lebt er nicht allein vom
Ereignisstrom: Topologie, Status und Fehler eines Workflows kennt nur sein
Zustand im Backend der Bibliothek (`sq:wf:<workflow_id>`, Index 0). Der Dienst
liest ihn selbst und hält eine **Kopie** in der Verzeichnisdatei — so bleibt
ein Workflow die vollen sieben Tage mit Baum und Endstatus sichtbar, auch
wenn der Zustand in Valkey längst verfallen ist. Ergebnisse gehören nicht zur
Kopie; sie werden wie auf der Task-Seite nachgeschlagen.

### Takt: `SQD_WORKFLOW_POLL_S`

| Einstellung | Vorgabe | Bedeutung |
|---|---|---|
| `SQD_WORKFLOW_POLL_S` | `60` Sekunden | Abstand, in dem jeder nicht beendete Workflow einmal gelesen wird |

Gelesen wird zu drei Anlässen:

1. **beim Start** einmal alle offenen Workflows — sonst stünde die Kopie nach
   einem Neustart bis zum ersten Takt auf dem alten Stand;
2. **im Takt** jeder Workflow ohne Endstatus;
3. **nach einem Ereignis**: meldet ein Schritt ein Ergebnis (fertig,
   gescheitert, Dead Letter, storniert) oder reiht ein Worker einen
   Folgeschritt ein, liest der Dienst diesen Workflow sofort und zwei
   Sekunden später noch einmal. Die zweite Lesung ist nötig, weil das
   Ereignis eines Schritts ankommt, bevor die Bibliothek den Zustand
   fortschreibt. Nach „Workflow stornieren" gilt dasselbe.

**Last auf Valkey.** Eine Lesung ist ein einzelnes `GET` auf den
Zustandsschlüssel. Der Takt liest nacheinander, nicht gleichzeitig; bei
tausend offenen Workflows sind das tausend `GET` je Minute. Anstöße aus
Ereignissen werden je Workflow zusammengefasst: die fünfhundert Glieder
einer Gruppe, die kurz nacheinander fertig werden, lösen nicht tausend
Lesungen aus, sondern eine sofortige und eine verzögerte, bei weiteren
Ereignissen während des Wartens eine weitere Runde. Workflows mit Endstatus
liest der Takt nicht mehr, ebenso wenig solche, deren Zustand verfallen ist
oder nie gelesen werden konnte; nur ein neues Ereignis stößt dort noch eine
Lesung an. Ist das Backend nicht erreichbar, setzt der Leser für
30 Sekunden aus, statt jeden Workflow in die volle Wartefrist laufen zu
lassen; die Kopfzeile nennt den Grund, und die Kopie bleibt unverändert.

### Bezug zur `result_ttl` der Worker

Die Frist des Workflow-Zustands **gleitet**. `Client.apply` setzt sie beim
Start aus der `result_ttl` der einreihenden Anwendung, danach setzt jeder
Worker sie bei jedem Schreiben des Zustands auf seine eigene `result_ttl`
neu (Kaskade wie oben, Vorgabe 24 Stunden). Nach dem letzten Schreiben —
typischerweise dem Endstatus — bleibt der Zustand also noch eine
`result_ttl` lang lesbar.

Daraus folgt die Bedingung für den Takt: **`SQD_WORKFLOW_POLL_S` muss weit
unter der kleinsten `result_ttl` der beteiligten Worker liegen.** Verfällt
ein Zustand zwischen zwei Lesungen, hat die Kopie seinen Endstatus nie
gesehen und zeigt „Zustand verfallen, zuletzt: läuft" statt „fertig". Mit den
Vorgaben (60 Sekunden gegen 24 Stunden) ist das kein Thema; wer Worker mit
einer `result_ttl` von wenigen Minuten fährt, senkt den Takt entsprechend.
Die Lesung nach dem Ergebnis-Ereignis fängt den Normalfall zwar schon ab —
der Takt ist das Netz, wenn ein Ereignis verloren ging oder der Dienst zu
dem Zeitpunkt nicht lief.

### Die Anzeigezustände

Ein **Endstatus** — fertig, gescheitert, storniert — steht nur da, wenn eine
Lesung des Zustands ihn geliefert hat. Das Dashboard leitet ihn nie aus den
Schritten ab. Alle anderen Zustände sagen ausdrücklich, dass es keinen
Endstatus kennt.

| Anzeige | Endstatus? | Bedeutung |
|---|---|---|
| läuft | nein | Der Zustand ist lesbar und trägt noch keinen Endstatus. |
| fertig | ja | Der Zustand trägt „completed". |
| gescheitert | ja | Der Zustand trägt „failed"; oben steht der Fehlertext aus dem Zustand, darunter die gescheiterten Schritte laut Verzeichnis. |
| storniert | ja | Der Zustand trägt „cancelled". Die Bibliothek schreibt das nur, wenn nach der Anforderung noch ein Schritt erfolgreich endet. |
| Stornierung angefordert | nein | Dieses Dashboard hat die Stornierung ausgelöst, und laut Verzeichnis wartet oder läuft noch mindestens ein Schritt. Noch nicht aufgegriffene Schritte bricht der Worker beim Aufgreifen ab, laufende laufen zu Ende. Ein zweites Stornieren ist kein Fehler. |
| Stornierung angefordert, nichts mehr aktiv | nein | Wie oben, aber kein Schritt läuft oder wartet mehr. Das ist der gewöhnliche Ausgang, wenn alle Glieder noch in der Queue standen: der Worker bricht jedes beim Aufgreifen ab, kein Schritt endet erfolgreich, und die Bibliothek schreibt deshalb **nie** „storniert". Der Zustand bleibt „läuft", bis er verfällt. Erneutes Stornieren wird nicht angeboten. |
| Zustand verfallen, zuletzt: … | nein | Der Zustand ist aus Valkey verschwunden, ohne dass eine Lesung einen Endstatus gesehen hat. Hinter „zuletzt" steht, was vorher angezeigt wurde — „läuft", „Stornierung angefordert" oder „…, nichts mehr aktiv" —, dazu der Zeitpunkt der letzten Lesung. Wie der Workflow ausging, weiß das Dashboard nicht. |
| Zustand nie gesehen | nein | Das Verzeichnis kennt Schritte des Workflows, aber der Dienst konnte seinen Zustand nie lesen, bevor er verfiel — etwa weil der Dienst länger nicht lief. Baum und Fortschritt zeigen nur die bekannten Schritte. |

Stornierungen, die jemand an der Bibliothek vorbei auslöst (`handle.cancel()`
in einer Anwendung), kennt das Dashboard nicht als „angefordert"; es sieht
sie erst, wenn der Zustand „storniert" trägt.

Status und Fortschritt kommen aus zwei Quellen: der Status aus der Lesung,
der Fortschritt aus den Task-Zeilen, die der Worker-Hook über den
Ereignisstrom meldet. Die Lesung kann schneller sein als der Hook-Puffer
(ein Takt von einer Sekunde); eine Zeile kann deshalb kurz „fertig" mit
weniger fertigen Schritten zeigen, als sie hat, bis die Ereignisse
eingelesen sind.

### Wann ein Workflow erscheint

Mit **Anwendungs-Hook** erscheint ein Workflow mit der Einreihung seiner
ersten Schritte — noch bevor ein Worker etwas aufgreift. **Ohne**
Anwendungs-Hook erscheint er erst, wenn ein Worker einen seiner Schritte
aufgreift: der Worker-Hook sieht die Einreihung nicht, und die erste Front
(alle Glieder einer Gruppe, der ganze Kopf eines Chords) ist dann nur über
das Muster der Kennung `<workflow_id>#<pfad>` erkennbar. Solche Kandidaten
bleiben unsichtbar, bis eine Lesung des Zustands sie bestätigt. Die Liste
trägt dazu einen stehenden Hinweis. Scheduler-Auslösungen und verzögerte
Einreihungen passieren den Anwendungs-Hook ohnehin nie.

### Neuaufbau des Verzeichnisses verliert die Kopie

Die Kopie liegt in `directory.sqlite3`, stammt aber aus Lesungen, nicht aus
dem Strom. Wird die Datei gelöscht und aus dem Strom neu aufgebaut (siehe
oben), kommen die Task-Zeilen wieder, **die Kopie nicht**. Was dann zu
sehen ist:

- Workflows, deren Zustand noch in Valkey liegt, liest der Abgleich beim
  Start neu; sie stehen nach der ersten Runde wieder vollständig da.
- Workflows, deren Zustand schon verfallen ist, fallen auf „Zustand nie
  gesehen" zurück — auch solche, die vorher „fertig" zeigten. Der Endstatus
  ist dann nicht mehr zu erfahren.
- Der eigene Storno-Vermerk ist ebenfalls fort; eine laufende Stornierung
  erscheint wieder als „läuft", bis der Zustand „storniert" trägt.

Wer die Endstatus älterer Workflows behalten will, löscht die Datei also nur,
wenn es sein muss.

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
