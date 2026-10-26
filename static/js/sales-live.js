/* TapTap — Daily sales updates itself.
 *
 * Every few seconds the page asks the server for a fingerprint of the day (sales + cash handed in).
 * Only when it changed are the figures re-rendered on the server and merged into the page IN PLACE
 * (a small DOM morph): bars grow smoothly, changed numbers flash, new sales glow, and the scroll
 * position, an open date picker or a focused field are left alone.
 *
 *  - today: checks every 10 s; another day: every 60 s (late or corrected sales)
 *  - full refresh every 2 min anyway (stock left, "x minutes ago" texts)
 *  - pauses while the tab is hidden, catches up the moment it is visible again
 *  - backs off on network or server errors (20 s → 40 s → … 2 min) and says so
 *  - new-sale toast; optional "ding" (remembered on this device)
 */
(function () {
  'use strict';
  var root = document.getElementById('sdLive');
  if (!root || !window.fetch) return;

  var TODAY = root.dataset.today === '1';
  var EVERY = TODAY ? 10000 : 60000, FULL_EVERY = 120000, MAX_BACKOFF = 120000;
  var fp = root.dataset.fp || '', timer = null, failures = 0, lastFull = Date.now(), lastOk = Date.now(), busy = false;
  var paused = false, sound = false;
  try { paused = localStorage.getItem('tt-sales-live-paused') === '1'; sound = localStorage.getItem('tt-sales-live-sound') === '1'; } catch (e) {}

  // ───────── seen sales (for highlights / toasts) ─────────
  function saleIds(scope) {
    var out = {};
    (scope || root).querySelectorAll('[data-sale]').forEach(function (tr) { out[tr.getAttribute('data-sale')] = tr.getAttribute('data-sale-text') || ''; });
    return out;
  }
  var seen = saleIds();

  // ───────── status chip ─────────
  function chip() { return document.getElementById('sdLiveChip'); }
  function ago(ms) { var s = Math.round(ms / 1000); return s < 5 ? 'just now' : s < 60 ? s + 's ago' : Math.round(s / 60) + ' min ago'; }
  function paint(state) {
    var c = chip(); if (!c) return;
    c.classList.remove('is-paused', 'is-error', 'is-busy');
    var label = c.querySelector('span');
    if (paused) { c.classList.add('is-paused'); label.textContent = 'Paused'; }
    else if (state === 'error' || failures > 0) { c.classList.add('is-error'); label.textContent = 'Reconnecting…'; }
    else { if (state === 'busy') c.classList.add('is-busy'); label.textContent = 'Live · ' + ago(Date.now() - lastOk); }
    var p = document.getElementById('sdLivePause'), so = document.getElementById('sdLiveSound');
    if (p) { p.innerHTML = paused ? '<i class="bi bi-play-fill"></i>' : '<i class="bi bi-pause-fill"></i>'; p.title = paused ? 'Resume live updates' : 'Pause live updates'; p.setAttribute('aria-label', p.title); }
    if (so) { so.innerHTML = sound ? '<i class="bi bi-volume-up"></i>' : '<i class="bi bi-volume-mute"></i>'; so.title = sound ? 'Sound on new sales: on' : 'Sound on new sales: off'; so.setAttribute('aria-label', so.title); }
  }
  setInterval(function () { if (!paused && !document.hidden) paint(); }, 5000);

  // ───────── morph: update the live DOM to match new HTML, touching only what changed ─────────
  function flash(el) {
    if (!el || el.nodeType !== 1) return;
    el.classList.remove('sd-flash'); void el.offsetWidth; el.classList.add('sd-flash');
  }
  function sameNode(a, b) {
    if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName) return false;
    if (a.nodeType !== 1) return true;
    if (a.id || b.id) return a.id === b.id;
    var ka = a.getAttribute('data-sale'), kb = b.getAttribute('data-sale');
    return ka === kb;
  }
  function syncAttrs(cur, nxt) {
    var i, a;
    for (i = cur.attributes.length - 1; i >= 0; i--) { a = cur.attributes[i].name; if (!nxt.hasAttribute(a)) cur.removeAttribute(a); }
    for (i = 0; i < nxt.attributes.length; i++) { a = nxt.attributes[i]; if (cur.getAttribute(a.name) !== a.value) cur.setAttribute(a.name, a.value); }
  }
  function morph(cur, nxt) {
    if (cur.nodeType === 3 || cur.nodeType === 8) {
      if (cur.nodeValue !== nxt.nodeValue) {
        var changed = cur.nodeValue.trim() !== nxt.nodeValue.trim();
        cur.nodeValue = nxt.nodeValue;
        if (changed) flash(cur.parentNode);
      }
      return;
    }
    if (cur.hasAttribute && cur.hasAttribute('data-keep')) return;           // live chip, etc.
    if (cur === document.activeElement && /^(INPUT|SELECT|TEXTAREA)$/.test(cur.nodeName)) return;   // user is typing / picking
    if (cur.nodeName === 'SCRIPT') return;
    syncAttrs(cur, nxt);
    var cc = Array.prototype.slice.call(cur.childNodes), nc = Array.prototype.slice.call(nxt.childNodes), i = 0, j = 0;
    while (j < nc.length) {
      var n = nc[j], c = cc[i];
      if (c && sameNode(c, n)) { morph(c, n); i++; j++; continue; }
      // a keyed row further down? (e.g. a new sale was inserted above it)
      var match = -1;
      if (n.nodeType === 1 && n.getAttribute('data-sale')) {
        for (var k = i + 1; k < cc.length; k++) { if (cc[k].nodeType === 1 && cc[k].getAttribute('data-sale') === n.getAttribute('data-sale')) { match = k; break; } }
      }
      if (match > -1) {                    // drop the rows in between (they were removed)
        while (i < match) { cur.removeChild(cc[i]); i++; }
        continue;
      }
      var fresh = document.importNode(n, true);
      cur.insertBefore(fresh, c || null);
      j++;
    }
    while (i < cc.length) { if (cc[i].parentNode === cur) cur.removeChild(cc[i]); i++; }
  }

  // ───────── new sales: glow, toast, sound ─────────
  var toastBox = null;
  function toast(text, more) {
    if (!toastBox) { toastBox = document.createElement('div'); toastBox.className = 'sd-toasts'; toastBox.setAttribute('aria-live', 'polite'); document.body.appendChild(toastBox); }
    var t = document.createElement('div'); t.className = 'sd-toast';
    t.innerHTML = '<i class="bi bi-cash-coin"></i><div><b>New sale</b><span></span></div>';
    t.querySelector('span').textContent = text + (more ? ' (+' + more + ' more)' : '');
    toastBox.appendChild(t);
    setTimeout(function () { t.classList.add('out'); setTimeout(function () { t.remove(); }, 400); }, 5000);
  }
  var audioCtx = null;
  function ding() {
    if (!sound) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      var now = audioCtx.currentTime;
      [880, 1320].forEach(function (f, i) {
        var o = audioCtx.createOscillator(), g = audioCtx.createGain();
        o.type = 'sine'; o.frequency.value = f; o.connect(g); g.connect(audioCtx.destination);
        g.gain.setValueAtTime(0.0001, now + i * 0.12); g.gain.exponentialRampToValueAtTime(0.18, now + i * 0.12 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, now + i * 0.12 + 0.35);
        o.start(now + i * 0.12); o.stop(now + i * 0.12 + 0.4);
      });
    } catch (e) {}
  }
  function announceNew() {
    var now = saleIds(), fresh = [];
    Object.keys(now).forEach(function (id) { if (!(id in seen)) fresh.push(id); });
    fresh.forEach(function (id) { var tr = root.querySelector('[data-sale="' + id + '"]'); if (tr) tr.classList.add('sd-new'); });
    if (fresh.length) {                       // `seen` was captured at page load, so these really are new
      toast(now[fresh[0]] || 'A voucher was sold', fresh.length - 1);
      ding();
    }
    seen = now;
  }

  // ───────── polling ─────────
  function schedule(ms) { clearTimeout(timer); if (!paused) timer = setTimeout(tick, ms); }
  function tick(force) {
    if (paused || busy) return;
    if (document.hidden) { schedule(EVERY); return; }
    busy = true; if (!failures) paint('busy');
    var full = force === true || Date.now() - lastFull > FULL_EVERY;
    fetch(location.pathname + location.search, {
      headers: { 'X-TT-Live': '1', 'X-TT-FP': fp, 'X-TT-Force': full ? '1' : '0', 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' },
      credentials: 'same-origin', cache: 'no-store'
    }).then(function (r) {
      if (r.redirected || r.status === 401 || r.status === 403) { location.reload(); throw new Error('signed out'); }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (d) {
      failures = 0; lastOk = Date.now();
      if (d.changed && typeof d.html === 'string') {
        var tpl = document.createElement('div'); tpl.innerHTML = d.html;
        morph(root, (function () { var w = root.cloneNode(false); while (tpl.firstChild) w.appendChild(tpl.firstChild); return w; })());
        fp = d.fp; root.dataset.fp = fp; lastFull = Date.now();
        announceNew();
      } else if (d.fp) { fp = d.fp; }
      paint();
      schedule(EVERY);
    }).catch(function () {
      failures += 1; paint('error');
      schedule(Math.min(MAX_BACKOFF, 20000 * Math.pow(2, failures - 1)));
    }).then(function () { busy = false; });
  }

  // ───────── controls ─────────
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('#sdLivePause, #sdLiveSound');
    if (!b) return;
    if (b.id === 'sdLivePause') {
      paused = !paused;
      try { localStorage.setItem('tt-sales-live-paused', paused ? '1' : '0'); } catch (x) {}
      if (paused) clearTimeout(timer); else tick(true);
    } else {
      sound = !sound;
      try { localStorage.setItem('tt-sales-live-sound', sound ? '1' : '0'); } catch (x) {}
      if (sound) ding();                      // also unlocks audio on browsers that need a tap first
    }
    paint();
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && !paused) tick(Date.now() - lastOk > EVERY); });
  window.addEventListener('online', function () { failures = 0; tick(true); });

  paint();
  schedule(EVERY);
})();
