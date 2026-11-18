/* TapTap — watches for incoming calls from a Web Worker (started by static/js/chat.js).

   Browsers slow down the timers of a tab that sits in the background — Chrome to about once a minute after a few
   minutes — so a page that checks for calls by itself can miss a whole ring (a customer call rings 60 s). A
   worker's timers keep their pace, so this checks every few seconds wherever the TapTap tab is, and hands each
   answer to the page, which rings, blinks the tab title and shows the desktop alert.

   The page sends {every: ms} (how often), {now: 1} (check at once) and {stop: 1}. */
'use strict';
var every = 3000, timer = 0, stopped = false, quiet = 0;

function tick() {
  clearTimeout(timer);
  if (stopped) return;
  fetch('/chat/call/poll/', { credentials: 'same-origin', cache: 'no-store',
                              headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' } })
    .then(function (r) {
      // signed out (a login page instead of JSON) or the server is restarting: check less often, never stop
      if (!r.ok || (r.headers.get('content-type') || '').indexOf('json') < 0) { quiet = Math.min(quiet + 1, 10); return null; }
      return r.json();
    })
    .then(function (d) {
      if (d && d.ok) { quiet = 0; postMessage({ rings: d.rings || [] }); }
    })
    .catch(function () { quiet = Math.min(quiet + 1, 10); })
    .then(function () { timer = setTimeout(tick, quiet ? Math.min(30000, every * (1 + quiet)) : every); });
}

onmessage = function (e) {
  var m = e.data || {};
  if (m.every) every = Math.max(1500, Math.min(15000, +m.every || 3000));
  if (m.stop) { stopped = true; clearTimeout(timer); return; }
  if (m.now) tick();
};

tick();
