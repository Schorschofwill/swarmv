/* Die Ansicht am Live-Kanal (R13, R13a, R32).
 *
 * Eigene Datei statt der SSE-Erweiterung von HTMX: die liegt nicht im Paket,
 * und nachladen darf diese Seite nichts (KTD7). Der Browser kann SSE von
 * Haus aus -- es braucht nur die paar Zeilen hier.
 *
 * Bewusst **kein** Austausch einzelner Zeilen aus der Nachricht heraus: die
 * Nachricht sagt, DASS sich etwas geaendert hat, nicht wie die Zeile danach
 * aussieht. Die Seite holt sich denselben Stand neu, den ein Neuladen
 * ergaebe, und Idiomorph tauscht nur das Geaenderte -- damit kann die
 * Ansicht nichts zeigen, was der Server nicht gerade so gerendert hat.
 */
(function () {
  "use strict";
  if (!window.EventSource) return;

  var quelle = new EventSource("/live");
  var offen = null;
  /* Ein Vorfall erzeugt Ereignisse im Dutzend. Ohne Sammelfrist holte die
     Seite fuer jedes einzelne den vollen Stand -- der Bildschirm eines
     Teammitglieds waere dann die Last, die den Dienst umwirft. */
  var SAMMELFRIST_MS = 400;

  function nachholen() {
    offen = null;
    if (document.hidden) return;          /* ein verdeckter Reiter holt nichts */
    if (!window.htmx) { location.reload(); return; }
    window.htmx.ajax("GET", location.href, { target: "body", swap: "morph" });
  }

  function anstossen() {
    if (offen === null) offen = window.setTimeout(nachholen, SAMMELFRIST_MS);
  }

  /* Verlust im Rueckstau: der Teilstand waere eine Luege, also ganz neu. */
  quelle.addEventListener("reload", function () { location.reload(); });

  /* Sitzung zu Ende (R32). Die Ansicht darf dann nicht stehenbleiben und
     weiter aussehen wie ein angemeldeter Stand. */
  quelle.addEventListener("session_ended", function () {
    quelle.close();
    location.assign("/login");
  });

  /* Alles Uebrige ist ein Task- oder Worker-Ereignis. Der Herzschlag traegt
     keine Aenderung und stoesst darum nichts an -- er haelt nur die
     Verbindung offen und beweist, dass sie noch steht. */
  quelle.onmessage = anstossen;
  ["task_enqueue", "task_pickup", "task_started", "task_completed",
   "task_failed", "task_cancelled", "task_dlq", "task_expired",
   "task_rejected", "task_retrying", "worker_heartbeat", "sqd_worker_meta",
   "sqd_events_lost"].forEach(function (art) {
    quelle.addEventListener(art, anstossen);
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && offen === null) anstossen();
  });
})();
