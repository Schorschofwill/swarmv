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

  /* Global, weil die Morph-Erweiterung ihren Einstellungsteil als Ausdruck
     auswertet und nur an ``window`` herankommt. */
  window.__sqdBewahreEingabe = function (alt) {
    if (!alt || !alt.tagName) return true;
    var art = alt.tagName;
    if (art !== "INPUT" && art !== "SELECT" && art !== "TEXTAREA") return true;
    if (alt.type === "hidden") return true;          /* Merkmal muss mit */
    if (alt === document.activeElement) return false;
    if (art === "SELECT") return alt.selectedIndex !== alt.__sqdStart;
    return alt.value !== alt.defaultValue;
  };

  var quelle = new EventSource("/live");
  var offen = null;
  var laeuft = false;
  var verpasst = false;
  /* Ein Vorfall erzeugt Ereignisse im Dutzend. Ohne Sammelfrist holte die
     Seite fuer jedes einzelne den vollen Stand -- der Bildschirm eines
     Teammitglieds waere dann die Last, die den Dienst umwirft. */
  var SAMMELFRIST_MS = 400;
  /* Eigene, laengere Frist fuer das vollstaendige Neuladen. */
  var NEULADEN_FRIST_MS = 2000;

  function nachholen() {
    offen = null;
    if (document.hidden) { verpasst = true; return; }
    if (!window.htmx) { location.reload(); return; }
    /* **innerHTML**, nicht outerHTML. "morph" allein bedeutet bei Idiomorph
       morphStyle:"outerHTML" -- das Ziel wird *ersetzt*. Ziel ist <body>,
       und HTMX streift beim Zerlegen der Antwort die <head>/<body>-Huelle
       ab: nach dem ersten Ereignis gab es kein <body> mehr. Damit fiel die
       ganze Schale weg (sie haengt an body), document.body war null, und
       jedes weitere Ereignis warf -- die Seite zeigte fuer immer den alten
       Stand und sah dabei aktuell aus. In einem echten Browser gemessen. */
    laeuft = true;
    var fertig = function () { laeuft = false; };
    try {
      var zusage = window.htmx.ajax("GET", location.href, {
        /* Der Rueckruf haelt halbfertige Eingaben fest. Ohne ihn loeschte
           jedes Nachladen, was gerade getippt war: Idiomorph gleicht
           ``value`` an die Serverfassung an, und die ist leer. Wer im
           Vorfall nach einer Kennung sucht, konnte nicht tippen -- alle
           400 ms war das Feld wieder leer. Die Erweiterung wertet den Teil
           nach "morph:" als Ausdruck aus, darum der Weg ueber ``window``. */
        target: "body", swap: "morph:{morphStyle:'innerHTML',callbacks:{beforeNodeMorphed:window.__sqdBewahreEingabe}}",
      });
      if (zusage && zusage.then) zusage.then(fertig, fertig); else fertig();
    } catch (e) {
      fertig();
    }
  }

  function anstossen() {
    /* ``laeuft`` verhindert **Gleichzeitigkeit**, die Frist nur die Rate.
       Ohne ihn liefen bei anhaltendem Strom beliebig viele volle
       Seitenabrufe nebeneinander -- gemessen bis zu acht --, und auf der
       Startseite haengt an jedem die Broker-Abfrage im Anfrageweg. */
    if (offen !== null || laeuft) { verpasst = true; return; }
    offen = window.setTimeout(nachholen, SAMMELFRIST_MS);
  }

  /* Verlust im Rueckstau: der Teilstand waere eine Luege, also ganz neu.
     Gedrosselt wie alles andere -- ungedrosselt gemessen: 20 Neuladevorgaenge
     in drei Sekunden fuer eine einzige Ansicht. Und im verdeckten Reiter gar
     nicht: acht Hintergrundreiter luden sonst bei jeder Luecke achtmal die
     ganze Seite. */
  var neuladen_offen = null;
  quelle.addEventListener("reload", function () {
    if (neuladen_offen !== null) return;
    neuladen_offen = window.setTimeout(function () {
      neuladen_offen = null;
      if (document.hidden) { verpasst = true; return; }
      location.reload();
    }, NEULADEN_FRIST_MS);
  });

  /* Sitzung zu Ende (R32). Die Ansicht darf dann nicht stehenbleiben und
     weiter aussehen wie ein angemeldeter Stand. */
  quelle.addEventListener("session_ended", function () {
    quelle.close();
    location.assign("/login");
  });

  /* **Alles Uebrige** stoesst an -- ausser dem Herzschlag, der keine
     Aenderung traegt, und den beiden Arten oben.
     Frueher stand hier eine Aufzaehlung der bekannten Arten. Sie war gegen
     eine offene Menge gebaut und verlor: ``worker_online``,
     ``worker_shutdown``, ``supervisor_report`` und ``scheduler_heartbeat``
     fehlten darin, und ``onmessage`` als Auffanglinie feuerte nie, weil der
     Server **jeder** Nachricht einen Ereignisnamen gibt. Ein Worker, der
     hochkam oder herunterfuhr, aktualisierte die Uebersicht nicht. */
  var STILL = { heartbeat: 1, reload: 1, session_ended: 1 };
  var echt_dispatch = quelle.dispatchEvent.bind(quelle);
  quelle.dispatchEvent = function (ereignis) {
    if (ereignis && ereignis.type && !STILL[ereignis.type]) anstossen();
    return echt_dispatch(ereignis);
  };
  quelle.onmessage = anstossen;

  /* Ein abgerissener Kanal war im Browser unsichtbar: ``EventSource`` stellt
     bei einem HTTP-Fehler **endgueltig** ein, die Ansicht blieb offen und
     sah aktuell aus. Die Marken der Kopfzeile helfen nicht -- sie
     beschreiben den Server zum Renderzeitpunkt, nicht diese Verbindung. */
  quelle.addEventListener("error", function () {
    if (quelle.readyState === 2) kennzeichne("Live-Verbindung abgerissen — die Seite zeigt einen alten Stand.");
    else kennzeichne("Live-Verbindung unterbrochen — wird neu aufgebaut.");
  });
  quelle.addEventListener("open", function () { kennzeichne(null); });

  function kennzeichne(text) {
    var stelle = document.querySelector("[data-live-zustand]");
    if (!stelle) return;
    stelle.textContent = text || "";
    stelle.hidden = !text;
  }

  document.addEventListener("visibilitychange", function () {
    /* Nur nachholen, wenn waehrend der Verdeckung wirklich etwas kam.
       Vorher loeste jeder Reiterwechsel einen vollen Seitenabruf aus. */
    if (!document.hidden && verpasst && offen === null && !laeuft) {
      verpasst = false;
      anstossen();
    }
  });
})();
