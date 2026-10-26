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
  var paused = false, soundStyle = '';      // '' = off, else 'register' | 'coins' | 'bell'
  try {
    paused = localStorage.getItem('tt-sales-live-paused') === '1';
    var sv = localStorage.getItem('tt-sales-live-sound') || '';
    soundStyle = sv === '1' ? 'register' : (/^(register|coins|bell)$/.test(sv) ? sv : '');
  } catch (e) {}
  function styleName(k) { for (var i = 0; i < SOUND_STYLES.length; i++) if (SOUND_STYLES[i][0] === k) return SOUND_STYLES[i][1]; return 'Off'; }

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
    if (so) {
      so.innerHTML = soundStyle ? '<i class="bi bi-volume-up"></i>' : '<i class="bi bi-volume-mute"></i>';
      so.title = 'Sound on new sales: ' + styleName(soundStyle) + ' — tap to change';
      so.setAttribute('aria-label', so.title);
    }
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
  // ───────── new-sale sounds (synthesised in the browser: no audio file, works offline) ─────────
  // MONEY-SOUND-START
  var SOUND_STYLES = [
    ['register', 'Ka-ching'],   // cash register: lever click, bright bell "CHING" ringing out, a few coins
    ['coins', 'Coins'],         // a handful of coins poured onto the counter
    ['bell', 'Counter bell'],   // shop counter bell, "ting-ting"
  ];
  function moneySound(ctx, t0, sales, style) {
    sales = Math.max(1, sales || 1);
    var out = ctx.createDynamicsCompressor();
    out.threshold.value = -12; out.ratio.value = 3.5; out.attack.value = 0.002; out.release.value = 0.3;
    var master = ctx.createGain(); master.gain.value = 0.7;
    out.connect(master); master.connect(ctx.destination);

    function noise(seconds) {
      var len = Math.max(1, Math.floor(ctx.sampleRate * seconds)), buf = ctx.createBuffer(1, len, ctx.sampleRate), d = buf.getChannelData(0);
      for (var i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
      var src = ctx.createBufferSource(); src.buffer = buf; return src;
    }
    function env(g, t, peak, attack, decay) {
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(Math.max(peak, 0.0002), t + attack);
      g.gain.exponentialRampToValueAtTime(0.0001, t + attack + decay);
    }
    function tone(freq, t, peak, decay, type, dest) {
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = type || 'sine'; o.frequency.setValueAtTime(freq, t);
      o.connect(g); g.connect(dest || out); env(g, t, peak, 0.0015, decay);
      o.start(t); o.stop(t + decay + 0.05);
    }
    function burst(t, seconds, peak, type, freq, q) {        // filtered noise: clicks, "ch", "sss"
      var n = noise(seconds + 0.02), f = ctx.createBiquadFilter(), g = ctx.createGain();
      f.type = type; f.frequency.value = freq; if (q) f.Q.value = q;
      n.connect(f); f.connect(g); g.connect(out); env(g, t, peak, 0.001, seconds); n.start(t); n.stop(t + seconds + 0.03);
    }
    // A struck metal bell. Its partials are NOT whole multiples of the lowest one (that is what makes it
    // sound like metal, not a beep), and each partial has a slightly detuned twin so the ring shimmers
    // ("ing-ing-ing") instead of sounding flat.
    function bell(t, f0, peak, ring) {
      burst(t, 0.035, peak * 1.6, 'highpass', 4500);          // the "CH": bright strike noise
      burst(t, 0.012, peak * 1.2, 'bandpass', 2500, 1.5);      // the hammer touching the bell
      [[1.00, 1.00, 1.00], [2.76, 0.55, 0.62], [5.40, 0.28, 0.38], [8.93, 0.12, 0.22]].forEach(function (m) {
        var f = f0 * m[0];
        if (f > ctx.sampleRate / 2 - 1000) return;
        tone(f, t, peak * m[1], ring * m[2]);
        tone(f + 3.5 + m[0], t, peak * m[1] * 0.7, ring * m[2] * 0.9);   // twin → beating shimmer
      });
    }
    function coin(t, loud) {
      var base = 4200 + Math.random() * 2400;
      burst(t, 0.006, loud * 1.2, 'highpass', 6000);
      tone(base, t, loud, 0.06 + Math.random() * 0.05);
      tone(base * 1.59, t, loud * 0.55, 0.04 + Math.random() * 0.03);
      tone(base * 2.47, t, loud * 0.3, 0.03);
    }

    if (style === 'coins') {                                  // coins poured onto the counter, then settling
      var n = Math.min(7 + 3 * (sales - 1), 16), t = t0;
      for (var i = 0; i < n; i++) {
        t += 0.025 + Math.random() * 0.06 * (1 + i / n);
        coin(t, 0.32 * (1 - i / (n + 5)));
      }
      tone(5200, t + 0.06, 0.12, 0.5); tone(5200 * 1.59, t + 0.06, 0.06, 0.35);   // the last coin spinning out
      return t + 0.6;
    }
    if (style === 'bell') {                                   // counter bell: "ting", and "ting" again for more sales
      var hits = Math.min(sales, 3) + 1;
      for (var h = 0; h < hits; h++) bell(t0 + h * 0.22, 2550, 0.30 * (h ? 0.85 : 1), 2.6);
      return t0 + (hits - 1) * 0.22 + 1.9;
    }
    // "register": KA (lever, two quick mechanical clicks + a thunk) … CHING (bright bell ringing out) … coins
    burst(t0, 0.018, 0.9, 'bandpass', 2600, 2);
    tone(140, t0, 0.45, 0.07, 'triangle');
    burst(t0 + 0.045, 0.014, 0.7, 'bandpass', 3400, 2);
    var tb = t0 + 0.10;
    bell(tb, 2350, 0.34, 3.0);
    var coins = Math.min(3 + (sales - 1), 6), tc = tb + 0.55;
    for (var c = 0; c < coins; c++) coin(tc + c * 0.07 + Math.random() * 0.04, 0.16 * (1 - c / (coins + 4)));
    return tb + 1.9;
  }
  // MONEY-SOUND-END
  var audioCtx = null;
  function ding(sales) {
    if (!soundStyle) return;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended' && audioCtx.resume) audioCtx.resume();
      moneySound(audioCtx, audioCtx.currentTime + 0.02, sales || 1, soundStyle);
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

  var hintTimer = null;
  function soundHint(text) {
    var c = chip(); if (!c) return;
    var h = document.getElementById('sdSoundHint');
    if (!h) { h = document.createElement('span'); h.id = 'sdSoundHint'; h.className = 'sd-sound-hint'; c.appendChild(h); }
    h.textContent = text; h.classList.add('on');
    clearTimeout(hintTimer); hintTimer = setTimeout(function () { h.classList.remove('on'); }, 1800);
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
      // off → Ka-ching → Coins → Counter bell → off; each choice plays once so you can pick by ear
      var order = [''].concat(SOUND_STYLES.map(function (x) { return x[0]; }));
      soundStyle = order[(order.indexOf(soundStyle) + 1) % order.length];
      try { localStorage.setItem('tt-sales-live-sound', soundStyle || '0'); } catch (x) {}
      soundHint(soundStyle ? 'Sound: ' + styleName(soundStyle) : 'Sound off');
      if (soundStyle) ding();                 // preview — also unlocks audio on browsers that need a tap first
    }
    paint();
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && !paused) tick(Date.now() - lastOk > EVERY); });
  window.addEventListener('online', function () { failures = 0; tick(true); });

  paint();
  schedule(EVERY);
})();
