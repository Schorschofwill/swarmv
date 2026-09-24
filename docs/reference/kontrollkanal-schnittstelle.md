# Schnittstelle des Kontrollkanals (Stand swarmq MR !60, 2026-09-17)

Der Kontrollkanal ist die einzige abgestimmte Kernänderung für das Dashboard
(Dashboard-Plan KD3, R15–R17). Er liegt in swarmq auf dem Branch
`feat/kontrollkanal`, MR https://gitlab.com/schorsch-dev/swarmq/-/merge_requests/60,
und erscheint mit dem nächsten Release nach 0.6.2. Quelle der Wahrheit ist der
Plan `docs/plans/2026-09-16-1753-feat-kontrollkanal-plan.md` und die
CLI-Referenz `website/reference/cli.md`, Abschnitt „Control channel“.

## Einschalten auf Worker-Seite

```bash
swarmq worker --module app.tasks --autoscale 2,4 --max-processes 16 --group orders-eu
```

- `--max-processes N` schaltet den Kanal ein und erklärt den Spielraum: höchste
  Prozesszahl, auf die ein Sollwert diesen Supervisor bringen darf.
- `--group LABEL` ist der Gruppenschlüssel; ohne Label die sortierte,
  kommagetrennte Queue-Menge (`billing,orders`). Nur je Backend eindeutig.
- Ohne `--max-processes` liest und schreibt der Supervisor nichts.

## Schlüssel in Valkey (Datenbank des `SWARMQ_BACKEND_URL`)

| Richtung | Schlüssel | Schreiber | Leser |
|---|---|---|---|
| Sollwert | `sq:control:setpoint:<gruppe>` | Dashboard | jeder Supervisor der Gruppe |
| Rückmeldung | `sq:control:status:<gruppe>:<supervisor>` | Supervisor, mit TTL | Dashboard |

Die Supervisor-Kennung ist `<SWARMQ_WORKER_ID oder Hostname>:p<PID des Supervisors>`.
Sie wechselt bei jedem Neustart; das Dashboard listet daher alle Schlüssel
unter `sq:control:status:<gruppe>:*` (SCAN) statt eine Kennung zu kennen.

## Sollwert

```json
{"min": 2, "max": 8}
```

- Ganze Zahlen, `min >= 1`, `min <= max`, `max <= Spielraum`.
- `min == max` ist die feste Form `N..N` und schaltet die Autoskalierung der
  Gruppe ab. Gruppen ohne Autoskalierung (festes `--processes`, `--pool cpu`)
  nehmen **nur** diese Form an.
- Ein neuer Wert ersetzt den vorigen vollständig (`SET`). Aufheben = `DEL`;
  danach gelten wieder die Startargumente.
- Der Sollwert gilt **je Supervisor**: zwei Hosts derselben Gruppe mit 4..4
  führen zusammen acht Worker.

## Verhalten des Supervisors

- Liest einmal vor dem ersten Kindprozess, danach alle **5 s**
  (`DEFAULT_SAMPLE_INTERVAL_S` in `swarmq.process_manager.setpoint_store`).
- Höhere Untergrenze: fehlende Worker starten sofort. Niedrigere Obergrenze:
  überzählige Worker enden sanft (laufender Task wird fertig).
- Ablehnung (Grund landet in der Rückmeldung, alte Grenzen bleiben):
  Obergrenze über Spielraum, Untergrenze < 1, verdrehte Grenzen, unlesbarer
  Wert, Bereich auf einer Gruppe ohne Autoskalierung.
- Ein Sollwert lässt den Start nie scheitern; bei nicht erreichbarem Backend
  fährt die Gruppe mit ihren Startargumenten hoch und zieht später nach.
- Slots und Portbereiche wachsen zur Laufzeit nicht; mehr Spielraum braucht
  einen Deploy.

## Rückmeldung (Beispiel aus dem Testlauf)

```json
{
  "group": "demo",
  "supervisor": "demo:p2700209",
  "headroom": 6,
  "store": "present",
  "setpoint": {"min": 4, "max": 4},
  "effective": {"min": 4, "max": 4},
  "source": "setpoint",
  "autoscaling": false,
  "rejection": null,
  "slots": {"size": 6, "occupied": 4, "locked": 0},
  "workers": 4,
  "reported_at": "2026-09-17T06:12:21+00:00",
  "ttl_s": 15.0
}
```

| Feld | Bedeutung |
|---|---|
| `store` | `present` Sollwert gelesen, `absent` keiner hinterlegt, `unread` Backend beim letzten Versuch nicht erreichbar |
| `setpoint` | zuletzt gelesener Sollwert oder `null` (auch bei unlesbarem Inhalt) |
| `effective` / `source` | wirksame Grenzen und ob sie aus `setpoint` oder `startup` stammen |
| `rejection` | Grund der letzten Ablehnung oder `null`; `unreadable: …` bei kaputtem Inhalt |
| `autoscaling` | Autoskalierung konfiguriert **und** wirksame Grenzen sind ein Bereich |
| `slots` | `size` = Spielraum, `occupied` belegte, `locked` vom Neustart-Budget gesperrte Slots |
| `workers` | Zahl der Kindprozesse inkl. gerade sanft endender |
| `ttl_s` | Verfallsfrist = Gnadenfrist (`SWARMQ_SHUTDOWN_TIMEOUT`, Vorgabe 30) + 5 s Terminierungsfrist + 5 s Intervall |

Lesart für R17 des Dashboard-Plans:

- **bestätigt**: `setpoint` gleich dem gesendeten Wert, `source == "setpoint"`, `rejection == null`.
- **abgelehnt**: `setpoint` gleich dem gesendeten Wert (oder `null` bei unlesbar), `rejection` gesetzt.
- **noch nicht angekommen**: `setpoint` noch der alte Wert, `reported_at` älter als das Senden; nach mehr als etwa zwei Intervallen (10 s) plus Nachsicht „unbestätigt“ melden.
- **Backend nicht erreichbar**: `store == "unread"` oder gar kein Statusschlüssel mehr (TTL abgelaufen).
- **Supervisor weg**: Statusschlüssel verschwindet spätestens `ttl_s` nach der letzten Meldung.

## Python-Zugriff (öffentliche Klasse in swarmq)

```python
from swarmq.backend.valkey.control import ValkeySetpointStore
from swarmq.contracts.control import Setpoint

store = ValkeySetpointStore("valkey://localhost:6399/0")
await store.connect()
await store.write_setpoint("orders-eu", Setpoint(low=2, high=8))
status = await store.read_status("orders-eu", "host:p4711")   # dict | None
await store.clear_setpoint("orders-eu")
await store.close()
```

`read_setpoint(group)` liefert `Setpoint | SetpointUnreadable | None`. Für die
Liste aller Supervisoren einer Gruppe gibt es noch keine Methode; SCAN über
`sq:control:status:<gruppe>:*` mit dem Valkey-Client (`store.client`).

## Sicherheit

Wer auf das Backend schreiben darf, kann Sollwerte setzen. Die Präfixe
`sq:control:setpoint:` und `sq:control:status:` sind getrennt, damit der
Betreiber per Valkey-ACL dem Dashboard Schreibrechte nur auf Sollwerte und den
Supervisoren nur auf Statusschlüssel geben kann. Das Dashboard sollte mit
eigenen Zugangsdaten laufen, sobald der Betreiber sie vergibt.

## Manuell ausprobieren

Testcontainer von swarmq: RabbitMQ `localhost:5699`, Valkey `localhost:6399`
(`docker ps | grep swarmq-test`). Worker starten wie oben mit
`SWARMQ_BACKEND_URL=valkey://localhost:6399/0`, dann

```bash
docker exec swarmq-test-valkey valkey-cli SET sq:control:setpoint:<gruppe> '{"min":4,"max":6}'
docker exec swarmq-test-valkey valkey-cli --scan --pattern 'sq:control:status:<gruppe>:*'
```

Automatisierte Vorlage: `tests/integration/test_cli_worker_setpoint.py` in swarmq.
