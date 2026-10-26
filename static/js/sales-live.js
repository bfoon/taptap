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
  // ───────── money sound: "ka-ching" + coins (synthesised: no audio file, works offline) ─────────
  // MONEY-SOUND-START
  function moneySound(ctx, t0, sales) {
    var out = ctx.createDynamicsCompressor();                 // keeps several layers from clipping
    out.threshold.value = -14; out.ratio.value = 4; out.attack.value = 0.003; out.release.value = 0.25;
    var master = ctx.createGain(); master.gain.value = 0.55;
    out.connect(master); master.connect(ctx.destination);

    function noise(seconds) {
      var len = Math.max(1, Math.floor(ctx.sampleRate * seconds)), buf = ctx.createBuffer(1, len, ctx.sampleRate), d = buf.getChannelData(0);
      for (var i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
      var src = ctx.createBufferSource(); src.buffer = buf; return src;
    }
    function env(g, t, peak, attack, decay) {
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(peak, t + attack);
      g.gain.exponentialRampToValueAtTime(0.0001, t + attack + decay);
    }
    function tone(freq, t, peak, decay, type) {
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = type || 'sine'; o.frequency.setValueAtTime(freq, t);
      o.connect(g); g.connect(out); env(g, t, peak, 0.002, decay);
      o.start(t); o.stop(t + decay + 0.05);
    }

    // 1. "ka" — the register lever / drawer latch: a short, dry, mid-band click
    var k = noise(0.06), kf = ctx.createBiquadFilter(), kg = ctx.createGain();
    kf.type = 'bandpass'; kf.frequency.value = 1700; kf.Q.value = 1.2;
    k.connect(kf); kf.connect(kg); kg.connect(out); env(kg, t0, 1.3, 0.002, 0.05); k.start(t0); k.stop(t0 + 0.07);
    tone(180, t0, 0.35, 0.06, 'triangle');                   // a little body to the click

    // 2. "ching" — the bell: inharmonic partials with a slight detune shimmer
    var tb = t0 + 0.07;
    [[2093, 0.42, 0.8], [2637, 0.22, 0.65], [3520, 0.16, 0.5], [4699, 0.10, 0.4], [6272, 0.06, 0.3]].forEach(function (p) {
      tone(p[0], tb, p[1], p[2]);
      tone(p[0] * 1.004, tb, p[1] * 0.5, p[2] * 0.85);       // beating with its twin = metallic shimmer
    });
    var sh = noise(0.25), sf = ctx.createBiquadFilter(), sg = ctx.createGain();   // the "sss" of the strike
    sf.type = 'highpass'; sf.frequency.value = 6000; sh.connect(sf); sf.connect(sg); sg.connect(out);
    env(sg, tb, 0.12, 0.002, 0.2); sh.start(tb); sh.stop(tb + 0.26);

    // 3. coins dropping into the drawer — more coins when several sales arrive together
    var coins = Math.min(4 + 2 * Math.max(0, (sales || 1) - 1), 10), tc = t0 + 0.32;
    for (var c = 0; c < coins; c++) {
      var t = tc + c * 0.055 + Math.random() * 0.05, base = 3200 + Math.random() * 2600, loud = 0.34 * (1 - c / (coins + 6));
      tone(base, t, loud, 0.07 + Math.random() * 0.06);
      tone(base * 1.48, t, loud * 0.6, 0.05 + Math.random() * 0.04);
      tone(base * 2.31, t, loud * 0.35, 0.04);
      var ck = noise(0.015), cf = ctx.createBiquadFilter(), cg = ctx.createGain();
      cf.type = 'highpass'; cf.frequency.value = 5000; ck.connect(cf); cf.connect(cg); cg.connect(out);
      env(cg, t, loud * 0.8, 0.001, 0.012); ck.start(t); ck.stop(t + 0.02);
    }
    return tc + coins * 0.055 + 0.3;                         // when it has finished
  }
  // MONEY-SOUND-END
  var audioCtx = null;
  function ding(sales) {
    if (!sound) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended' && audioCtx.resume) audioCtx.resume();
      moneySound(audioCtx, audioCtx.currentTime + 0.02, sales || 1);
    } catch (e) {}
  }
  function announceNew() {
    var now = saleIds(), fresh = [];
    Object.keys(now).forEach(function (id) { if (!(id in seen)) fresh.push(id); });
    fresh.forEach(function (id) { var tr = root.querySelector('[data-sale="' + id + '"]'); if (tr) tr.classList.add('sd-new'); });
    if (fresh.length) {                       // `seen` was captured at page load, so these really are new
      toast(now[fresh[0]] || 'A voucher was sold', fresh.length - 1);
      ding(fresh.length);
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
