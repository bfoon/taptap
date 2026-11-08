/* Fit's "How to" — guided tours (tours: core/fit_tours.py, served by /fit/tours/).

   Fit points at the next thing to press by throwing something at it — a spear, an arrow, a light-gun beam, a water
   jet, a lightning bolt, a paper plane — or by flying there in a little single-seat propeller plane. The item
   ripples, a hand presses it, and a tip says what it does. The tour follows you from page to page (it is kept in
   sessionStorage) until the job is done.

   Hidden things are revealed in order: a menu item inside the phone menu → the ☰ button first; a button inside a
   dropdown → the dropdown toggle; a field inside a closed dialog → the button that opens it.

   API: FitTour.start(tour) · FitTour.stop() · FitTour.weapons · FitTour.weapon(id?) */
(function () {
  'use strict';
  var KEY = 'fit:tour', WKEY = 'fit:weapon', MAX_AGE = 45 * 60 * 1000;
  var REDUCED = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var WEAPONS = [
    { id: 'mix', label: 'Mix it up', emoji: '🎲' },
    { id: 'spear', label: 'Spear', emoji: '🔱' },
    { id: 'arrow', label: 'Bow & arrow', emoji: '🏹' },
    { id: 'light', label: 'Light gun', emoji: '🔦' },
    { id: 'water', label: 'Water gun', emoji: '💦' },
    { id: 'lightning', label: 'Lightning', emoji: '⚡' },
    { id: 'paper', label: 'Paper plane', emoji: '📄' },
    { id: 'plane', label: 'Fly the plane', emoji: '🛩️' }
  ];
  var ORDER = ['spear', 'arrow', 'light', 'water', 'lightning', 'paper', 'plane'];
  var T = null;          // {tour, i, at}
  var UI = { layer: null, tip: null, ring: null, hand: null, el: null, raf: 0, timer: 0, clickOff: null, busy: false };

  function ss(v) {
    try {
      if (v === undefined) return JSON.parse(sessionStorage.getItem(KEY) || 'null');
      if (v === null) sessionStorage.removeItem(KEY); else sessionStorage.setItem(KEY, JSON.stringify(v));
    } catch (e) { return null; }
  }
  function weapon(v) {
    try { if (v === undefined) return localStorage.getItem(WKEY) || 'mix'; localStorage.setItem(WKEY, v); } catch (e) { return 'mix'; }
    return v;
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function md(s) { return esc(s).replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>'); }
  function norm(s) { return String(s || '').replace(/[’‘]/g, "'").replace(/\s+/g, ' ').trim().toLowerCase(); }
  function fit() { return document.getElementById('ttFit'); }

  // ───────── finding the thing to point at ─────────
  function visible(el) {
    if (!el || !el.isConnected) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    var cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity > 0.05;
  }
  function ours(el) { return el.closest && el.closest('.fitp, .fitt-tip, #ttFit, .fitt-layer'); }
  function byText(text) {
    var want = norm(text), best = null, bestScore = 99, hidden = null;
    var all = document.querySelectorAll('a, button, [role="button"], [role="tab"], summary, label, .btn, .dropdown-item, .nav-link, input[type="submit"], input[type="button"]');
    for (var i = 0; i < all.length; i++) {
      var el = all[i]; if (ours(el)) continue;
      var vis = visible(el);
      var t = norm(vis ? el.innerText : el.textContent) || norm(el.value) || norm(el.getAttribute('aria-label')) || norm(el.title);
      if (!t || t.indexOf(want) < 0) continue;
      var score = t === want ? 0 : (t.indexOf(want) === 0 ? 1 : 2);
      score += Math.min(0.9, t.length / 400);
      if (vis) { if (score < bestScore) { best = el; bestScore = score; } }
      else if (!hidden) hidden = el;
    }
    return best || hidden;
  }
  function navLink(url) {
    var links = document.querySelectorAll('a[href="' + url + '"]'), hidden = null;
    for (var i = 0; i < links.length; i++) {
      if (ours(links[i])) continue;
      if (visible(links[i]) && links[i].closest('aside, nav, .sidebar, .mobile-nav, .offcanvas')) return links[i];
      if (!hidden) hidden = links[i];
    }
    for (i = 0; i < links.length; i++) if (visible(links[i]) && !ours(links[i])) return links[i];
    // nothing visible (a phone): the copy in the ☰ menu can be revealed, the desktop sidebar's cannot
    for (i = 0; i < links.length; i++) if (!ours(links[i]) && reveal(links[i])) return links[i];
    return hidden;
  }
  function resolve(spec, step) {
    if (!spec) return null;
    if (spec === 'nav') return step.url ? navLink(step.url) : null;
    var k = spec.slice(0, spec.indexOf(':')), v = spec.slice(spec.indexOf(':') + 1);
    try {
      if (k === 'text') return byText(v);
      if (k === 'sel') { var els = document.querySelectorAll(v); for (var i = 0; i < els.length; i++) if (visible(els[i]) && !ours(els[i])) return els[i]; return els[0] || null; }
      if (k === 'field') {
        var f = document.querySelectorAll('[name="' + v.replace(/"/g, '') + '"]'), pick = null;
        for (var j = 0; j < f.length; j++) { if (f[j].type === 'hidden') continue; if (visible(f[j])) { pick = f[j]; break; } if (!pick) pick = f[j]; }
        if (pick && (pick.type === 'checkbox' || pick.type === 'radio')) return pick.closest('.form-check, .form-switch, label') || pick;
        return pick;
      }
    } catch (e) { return null; }
    return null;
  }
  // Something hidden: point at what opens it (phone menu, dropdown, dialog, tab, collapsed section).
  function reveal(el) {
    if (!el) return null;
    if (visible(el)) return { el: el };
    var box, id, tg;
    if ((box = el.closest('.dropdown-menu'))) {
      var wrap = box.parentElement; tg = wrap && wrap.querySelector('[data-bs-toggle="dropdown"]');
      if (tg && visible(tg)) return { el: tg, via: 'Open this menu first' };
    }
    // a hidden dialog, phone menu, tab or pane: the visible button that opens it (Bootstrap or TapTap's own tabs)
    for (box = el.parentElement; box && box !== document.body; box = box.parentElement) {
      if (!box.id || visible(box)) continue;
      id = box.id.replace(/"/g, '');
      tg = Array.prototype.filter.call(document.querySelectorAll('[data-bs-target="#' + id + '"], [href="#' + id + '"], [aria-controls="' + id + '"], ' +
        '[data-target="#' + id + '"], [data-lk-tab="' + id + '"], [data-tab="' + id + '"], [data-pane="' + id + '"]'), function (x) { return visible(x) && !ours(x); })[0];
      if (tg) return { el: tg, via: box.classList.contains('offcanvas') ? 'Open the menu first' : 'Open this first' };
      if (box.classList.contains('offcanvas')) {
        tg = document.querySelector('[data-bs-toggle="offcanvas"]');
        if (tg && visible(tg)) return { el: tg, via: 'Open the menu first' };
      }
    }
    if ((box = el.closest('details')) && !box.open) {
      tg = box.querySelector('summary'); if (tg && visible(tg)) return { el: tg, via: 'Open this first' };
    }
    return null;
  }
  function onPage(step) {
    var p = location.pathname;
    if (step.match) { try { return new RegExp(step.match).test(p); } catch (e) { return false; } }
    if (!step.url) return true;
    return p === step.url;
  }

  // ───────── the show ─────────
  function layer() {
    if (UI.layer && UI.layer.isConnected) return UI.layer;
    UI.layer = document.createElement('div');
    UI.layer.className = 'fitt-layer';
    UI.layer.setAttribute('aria-hidden', 'true');
    UI.layer.innerHTML = '<svg class="fitt-svg" width="100%" height="100%"></svg>';
    document.body.appendChild(UI.layer);
    return UI.layer;
  }
  function svgEl(tag, attrs, parent) {
    var e = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    (parent || layer().firstChild).appendChild(e);
    return e;
  }
  function from() {
    var r = fit() ? fit().getBoundingClientRect() : { left: innerWidth - 80, top: innerHeight - 160, width: 66, height: 80 };
    return { x: r.left + r.width * 0.86, y: r.top + r.height * 0.7, top: { x: r.left + r.width * 0.5, y: r.top + 4 } };
  }
  function centre(el) { var r = el.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2, r: r }; }
  function robot(cls, ms) {
    var f = fit(); if (!f || REDUCED) return;
    f.classList.remove(cls); void f.offsetWidth; f.classList.add(cls);
    if (ms) setTimeout(function () { f.classList.remove(cls); }, ms);
  }
  function quad(a, c, b, t) { var u = 1 - t; return { x: u * u * a.x + 2 * u * t * c.x + t * t * b.x, y: u * u * a.y + 2 * u * t * c.y + t * t * b.y }; }
  function animate(ms, fn, done) {
    var t0 = performance.now();
    (function f(now) {
      var t = Math.min(1, (now - t0) / ms); fn(t);
      if (t < 1) requestAnimationFrame(f); else if (done) done();
    })(t0);
  }
  var ease = function (t) { return t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; };

  // Something flies along a curve, turned along its path.
  var SCALE = 1.6;      // projectiles big enough to follow with the eye
  function fly(shape, a, b, lift, ms, done, wobble) {
    var c = { x: (a.x + b.x) / 2, y: Math.min(a.y, b.y) - lift };
    var g = svgEl('g', { 'class': 'fitt-shot' }); g.innerHTML = shape;
    var trail = [];
    animate(ms, function (t) {
      var e = wobble ? t : ease(t), p = quad(a, c, b, e), q = quad(a, c, b, Math.min(1, e + 0.02));
      var ang = Math.atan2(q.y - p.y, q.x - p.x) * 180 / Math.PI;
      var wy = wobble ? Math.sin(t * Math.PI * 4) * 14 * (1 - t) : 0;
      g.setAttribute('transform', 'translate(' + p.x.toFixed(1) + ' ' + (p.y + wy).toFixed(1) + ') rotate(' + (ang + (wobble ? Math.sin(t * 20) * 8 : 0)).toFixed(1) + ') scale(' + SCALE + ')');
      if (wobble === 'smoke' && Math.random() < .5) trail.push(svgEl('circle', { cx: p.x, cy: p.y + wy, r: 3 + Math.random() * 3, 'class': 'fitt-smoke' }));
    }, function () {
      g.remove();
      setTimeout(function () { trail.forEach(function (s) { s.remove(); }); }, 900);
      done();
    });
  }
  var SHAPES = {
    spear: '<line x1="-46" y1="0" x2="6" y2="0" stroke="#8b5a2b" stroke-width="3.2" stroke-linecap="round"/><path d="M4 -5 L20 0 L4 5 Z" fill="#cbd5e1" stroke="#64748b" stroke-width="1"/><path d="M-46 0 l-6 -4 M-46 0 l-6 4" stroke="#d62828" stroke-width="2"/>',
    arrow: '<line x1="-34" y1="0" x2="8" y2="0" stroke="#7c4a1e" stroke-width="2.2"/><path d="M6 -4 L16 0 L6 4 Z" fill="#475569"/><path d="M-34 0 L-42 -6 L-30 -1 Z M-34 0 L-42 6 L-30 1 Z" fill="#e03131"/>',
    paper: '<path d="M18 0 L-14 -12 L-6 0 L-14 12 Z" fill="#fff" stroke="#94a3b8" stroke-width="1"/><path d="M18 0 L-6 0 L-10 7 Z" fill="#e2e8f0" stroke="#94a3b8" stroke-width=".8"/>',
    plane: '<g class="fitt-plane"><ellipse cx="0" cy="0" rx="22" ry="6.5" fill="#e03131"/><path d="M-6 -2 L-12 -22 L2 -22 L6 -2 Z" fill="#c92a2a"/><path d="M-6 2 L-12 20 L2 20 L6 2 Z" fill="#a51111"/>' +
      '<path d="M-22 0 L-30 -8 L-26 0 L-30 8 Z" fill="#c92a2a"/><circle cx="-2" cy="-1" r="5" fill="#a5d8ff" stroke="#1864ab" stroke-width="1"/>' +
      '<circle cx="-2" cy="-1.6" r="3" fill="#e3e9f1"/><rect x="-4.4" y="-2.6" width="4.8" height="2" rx="1" fill="#47e4ff"/>' +
      '<rect x="21" y="-2.2" width="4" height="4.4" rx="1" fill="#495057"/><g class="fitt-prop"><rect x="25" y="-13" width="2.4" height="26" rx="1.2" fill="#343a40"/></g></g>'
  };

  function shoot(kind, el, done) {
    var a = from(), b = centre(el), dist = Math.hypot(b.x - a.x, b.y - a.y);
    if (REDUCED) return done();
    if (kind === 'spear' || kind === 'arrow') {
      robot('is-throw', 700);
      setTimeout(function () { fly(SHAPES[kind], a, b, Math.min(260, dist * .35), 520 + dist * .45, done); }, 220);
    } else if (kind === 'paper') {
      robot('is-throw', 700);
      setTimeout(function () { fly(SHAPES.paper, a, b, Math.min(200, dist * .25), 900 + dist * .7, done, true); }, 220);
    } else if (kind === 'plane') {
      var f = fit(); if (f) f.classList.add('is-away');
      fly(SHAPES.plane, a, b, Math.min(320, dist * .45), 1100 + dist * .6, function () {
        // a quick climb back home after the drop
        fly(SHAPES.plane, b, from(), 160, 900, function () { if (f) { f.classList.remove('is-away'); robot('is-hop', 650); } }, 'smoke');
        done();
      }, 'smoke');
    } else if (kind === 'light') {
      robot('is-aim', 900);
      var beam = svgEl('line', { x1: a.x, y1: a.y, x2: a.x, y2: a.y, 'class': 'fitt-beam' });
      var glow = svgEl('line', { x1: a.x, y1: a.y, x2: a.x, y2: a.y, 'class': 'fitt-beam-glow' });
      animate(260, function (t) {
        var x = a.x + (b.x - a.x) * t, y = a.y + (b.y - a.y) * t;
        beam.setAttribute('x2', x); beam.setAttribute('y2', y); glow.setAttribute('x2', x); glow.setAttribute('y2', y);
      }, function () { setTimeout(function () { beam.remove(); glow.remove(); done(); }, 260); });
    } else if (kind === 'water') {
      robot('is-aim', 1100);
      var c = { x: (a.x + b.x) / 2, y: Math.min(a.y, b.y) - Math.min(140, dist * .2) }, n = 0;
      var iv = setInterval(function () {
        if (n++ > 16) { clearInterval(iv); return; }
        var d = svgEl('circle', { r: 3 + Math.random() * 2.5, 'class': 'fitt-drop' });
        animate(520, function (t) { var p = quad(a, c, b, t); d.setAttribute('cx', p.x); d.setAttribute('cy', p.y); }, function () { d.remove(); });
      }, 38);
      setTimeout(function () {
        for (var i = 0; i < 9; i++) (function (i) {
          var s = svgEl('circle', { cx: b.x, cy: b.y, r: 2.5, 'class': 'fitt-drop' }), ang = i / 9 * Math.PI * 2;
          animate(420, function (t) { s.setAttribute('cx', b.x + Math.cos(ang) * 26 * t); s.setAttribute('cy', b.y + Math.sin(ang) * 18 * t + 20 * t * t); s.setAttribute('opacity', 1 - t); }, function () { s.remove(); });
        })(i);
        done();
      }, 620);
    } else {      // lightning from the antenna
      robot('is-zap', 900);
      var pts = [], steps = 9, s0 = a.top;
      for (var i = 0; i <= steps; i++) {
        var t = i / steps, jx = i === 0 || i === steps ? 0 : (Math.random() - .5) * 46, jy = i === 0 || i === steps ? 0 : (Math.random() - .5) * 20;
        pts.push((s0.x + (b.x - s0.x) * t + jx).toFixed(1) + ',' + (s0.y + (b.y - s0.y) * t + jy).toFixed(1));
      }
      var bolt = svgEl('polyline', { points: pts.join(' '), 'class': 'fitt-bolt' });
      var halo = svgEl('polyline', { points: pts.join(' '), 'class': 'fitt-bolt-glow' });
      document.documentElement.classList.add('fitt-flash');
      setTimeout(function () { bolt.style.opacity = .2; halo.style.opacity = .1; }, 120);
      setTimeout(function () { bolt.style.opacity = 1; halo.style.opacity = .7; }, 200);
      setTimeout(function () { bolt.remove(); halo.remove(); document.documentElement.classList.remove('fitt-flash'); done(); }, 420);
    }
  }

  // ───────── ripple, hand and tip ─────────
  function clearMarks() {
    cancelAnimationFrame(UI.raf); clearTimeout(UI.timer);
    if (UI.clickOff) { UI.clickOff(); UI.clickOff = null; }
    if (UI.el) UI.el.classList.remove('fitt-target');
    ['ring', 'hand', 'tip'].forEach(function (k) { if (UI[k]) { UI[k].remove(); UI[k] = null; } });
    UI.el = null;
  }
  function track() {
    if (!UI.el) return;
    var r = UI.el.getBoundingClientRect();
    if (UI.ring) { UI.ring.style.left = (r.left + r.width / 2) + 'px'; UI.ring.style.top = (r.top + r.height / 2) + 'px'; UI.ring.style.setProperty('--w', Math.max(36, r.width) + 'px'); UI.ring.style.setProperty('--h', Math.max(30, r.height) + 'px'); }
    if (UI.hand) { UI.hand.style.left = (r.left + r.width / 2) + 'px'; UI.hand.style.top = (r.top + r.height / 2) + 'px'; }
    if (UI.tip) place(UI.tip, r);
    UI.raf = requestAnimationFrame(track);
  }
  function place(tip, r) {
    var w = tip.offsetWidth, h = tip.offsetHeight, m = 12, x, y;
    if (!r) { var f = fit() && fit().getBoundingClientRect(); x = (f ? f.left : innerWidth) - w - 14; y = (f ? f.top : innerHeight) - h + 40; }
    else {
      x = r.right + m; y = r.top + r.height / 2 - h / 2;                       // right of it
      if (x + w > innerWidth - 8) { x = r.left + r.width / 2 - w / 2; y = r.bottom + m + 14; }   // under it
      if (y + h > innerHeight - 8) y = r.top - h - m - 6;                       // above it
    }
    tip.style.left = Math.max(8, Math.min(innerWidth - w - 8, x)) + 'px';
    tip.style.top = Math.max(8, Math.min(innerHeight - h - 8, y)) + 'px';
  }
  function tipBox(step, opts) {
    var tour = T.tour, n = tour.steps.length, i = T.i;
    var tip = document.createElement('div');
    tip.className = 'fitt-tip'; tip.setAttribute('role', 'dialog'); tip.setAttribute('aria-live', 'polite');
    var dots = ''; for (var k = 0; k < n; k++) dots += '<i class="' + (k < i ? 'd' : k === i ? 'on' : '') + '"></i>';
    tip.innerHTML =
      '<div class="fitt-top"><span>' + esc(tour.icon || '🧭') + ' ' + esc(tour.title) + '</span><button type="button" data-t="stop" aria-label="End the tour">×</button></div>' +
      '<div class="fitt-dots">' + dots + '<small>Step ' + (i + 1) + ' of ' + n + '</small></div>' +
      '<b class="fitt-h">' + esc(opts.title || step.title) + '</b>' +
      (opts.text || step.text ? '<p>' + md(opts.text || step.text) + '</p>' : '') +
      (opts.tip || step.tip ? '<div class="fitt-hint">💡 ' + md(opts.tip || step.tip) + '</div>' : '') +
      '<div class="fitt-row">' +
        (i > 0 ? '<button type="button" class="fitt-b" data-t="back">Back</button>' : '') +
        (opts.go ? '<button type="button" class="fitt-b go" data-t="go">' + esc(opts.go) + '</button>' : '') +
        '<button type="button" class="fitt-b' + (opts.go ? '' : ' go') + '" data-t="next">' + (i === n - 1 ? 'Done 🎉' : opts.go ? 'Skip' : 'Next') + '</button>' +
        (UI.el ? '<button type="button" class="fitt-b ghost" data-t="again" title="Show me again">↺</button>' : '') +
      '</div>';
    tip.addEventListener('click', function (e) {
      var b = e.target.closest('[data-t]'); if (!b) return;
      var a = b.dataset.t;
      if (a === 'stop') stop(true);
      else if (a === 'back') go(T.i - 1);
      else if (a === 'next') go(T.i + 1);
      else if (a === 'again') run(true);
      else if (a === 'go' && opts.onGo) opts.onGo();
    });
    document.body.appendChild(tip);
    return tip;
  }
  function mark(el, step, opts) {
    UI.el = el;
    if (el) {
      el.classList.add('fitt-target');
      UI.ring = document.createElement('div'); UI.ring.className = 'fitt-ring'; UI.ring.innerHTML = '<i></i><i></i><i></i>';
      UI.hand = document.createElement('div'); UI.hand.className = 'fitt-hand';
      UI.hand.innerHTML = '<svg viewBox="0 0 32 40" width="34" height="42" aria-hidden="true"><path d="M11 3.5c1.7 0 3 1.3 3 3V17l1-.1V14c0-1.6 1.3-2.9 2.9-2.9s2.9 1.3 2.9 2.9v3.2l.6-.1c0-1.5 1.2-2.6 2.7-2.6s2.8 1.2 2.8 2.7v3.6c1.2-.6 2.8.1 3.2 1.5.2.6.2 1.3.2 2v6.4C29.3 34.6 25 38.5 19.8 38.5h-2.6c-3.7 0-6.9-2-8.6-5.2L3.4 23.5c-.8-1.5-.3-3.3 1.2-4.1 1.4-.8 3.2-.4 4.1 1l2.3 3.5V6.5c0-1.7-1.4-3 0-3Z" fill="#fff" stroke="#1f2937" stroke-width="1.6" stroke-linejoin="round"/></svg>';
      document.body.appendChild(UI.ring); document.body.appendChild(UI.hand);
      // Pressing the real thing moves the tour on (a link or a form may open the next page: saved first).
      var advance = function () {
        if (opts.onPress) return opts.onPress();
        if (!opts.waitOnly) { T.i += 1; save(); setTimeout(function () { if (T) run(); }, 450); }
      };
      el.addEventListener('click', advance, { once: true, capture: true });
      UI.clickOff = function () { el.removeEventListener('click', advance, { capture: true }); };
    }
    UI.tip = tipBox(step, opts);
    place(UI.tip, el && el.getBoundingClientRect());
    track();
  }

  // ───────── running a tour ─────────
  function save() { if (T) { T.at = Date.now(); ss(T); } }
  function pickWeapon() {
    var w = weapon();
    if (w !== 'mix') return w;
    return ORDER[(T.i + (T.tour.slug || '').length) % ORDER.length];
  }
  function go(i) {
    if (!T) return;
    if (i >= T.tour.steps.length) { finish(); return; }
    T.i = Math.max(0, i); save(); run();
  }
  function finish() {
    var t = T && T.tour;
    stop(false);
    if (window.TTFit && window.TTFit.cheer) window.TTFit.cheer(t ? 'Done! That’s how you ' + t.title.charAt(0).toLowerCase() + t.title.slice(1) + '.' : 'Done!');
  }
  function stop(byPerson) {
    clearMarks(); T = null; ss(null);
    var f = fit(); if (f) f.classList.remove('is-away', 'is-touring');
    if (byPerson && window.TTFit && window.TTFit.mood) window.TTFit.mood('', 0);
  }
  function lastPage() {          // the most recent step (this one or before) that names a page — to send people back
    for (var k = T.i; k >= 0; k--) { var s = T.tour.steps[k]; if (s.url) return s; }
    return null;
  }

  // Find the target, waiting a little for dialogs / menus that are still opening.
  function seek(fn, cb, tries) {
    var el = fn();
    if (el && (visible(el) || reveal(el)) || (tries || 0) >= 10) return cb(el);
    setTimeout(function () { seek(fn, cb, (tries || 0) + 1); }, 250);
  }

  function run(again) {
    if (!T || UI.busy) return;
    clearMarks();
    var step = T.tour.steps[T.i];
    if (!step) { finish(); return; }
    var f = fit(); if (f) f.classList.add('is-touring');

    // 1. Not on the right page yet: point at the way there.
    if (!onPage(step)) {
      if (step.match) {
        var goSpec = step.go;
        return seek(function () { return goSpec ? resolve(goSpec, step) : null; }, function (el) {
          if (el) return aim(el, step, { title: 'Get there first', text: step.text ? 'Open it here — then: **' + step.title + '**.' : '' , tip: step.tip, onPress: function () { save(); } });
          var back = lastPage();
          if (back && location.pathname !== back.url) return toPage(back, step);
          aim(null, step, { title: step.title, text: 'Open the page for this step first, then press Next.', tip: step.tip });
        });
      }
      return toPage(step, step);
    }
    // 2. On the page: the thing to press.
    if (!step.target || step.target === 'nav' && !step.url) return aim(null, step, {});
    seek(function () { return resolve(step.target, step); }, function (el) {
      if (!el) return aim(null, step, { tip: (step.tip ? step.tip + ' ' : '') + 'I can’t see it on this page right now — it may be switched off or not for your role.' });
      if (step.target === 'nav') return aim(el, step, { waitOnly: true });       // you are here: just show where it lives
      aim(el, step, {});
    });
  }
  function toPage(target, step) {
    seek(function () { return navLink(target.url); }, function (el) {
      var label = target.label || target.title;
      var opts = {
        title: label ? 'Open ' + label : target.title,
        text: target === step ? step.text : 'Then: **' + step.title + '**.',
        tip: target === step ? step.tip : '',
        go: 'Take me there', onGo: function () { save(); location.href = target.url; },
        onPress: function () { save(); }
      };
      aim(el, step, opts);
    });
  }
  function aim(el, step, opts) {
    var r = el && reveal(el);
    if (r && r.via) { opts = Object.assign({}, opts, { title: r.via, text: 'Then: **' + step.title + '**.', onPress: function () { setTimeout(function () { if (T) run(); }, 450); } }); el = r.el; }
    else if (el && !r) el = null;
    if (el) el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: REDUCED ? 'auto' : 'smooth' });
    UI.busy = true;
    var after = function () { UI.busy = false; if (!T) return; mark(el, step, opts); if (window.TTFit && window.TTFit.mood) window.TTFit.mood('talking', 1400); };
    if (!el) return after();
    setTimeout(function () { if (!T) { UI.busy = false; return; } shoot(pickWeapon(), el, after); }, REDUCED ? 0 : 380);
  }

  // Dialogs / menus opening or closing: aim again at the same step.
  ['shown.bs.modal', 'shown.bs.dropdown', 'shown.bs.offcanvas', 'shown.bs.tab', 'shown.bs.collapse'].forEach(function (ev) {
    document.addEventListener(ev, function () { if (T && !UI.busy) setTimeout(function () { if (T && !UI.busy) run(); }, 200); });
  });
  addEventListener('keydown', function (e) { if (T && e.key === 'Escape' && UI.tip) stop(true); });

  function start(tour) {
    if (!tour || !tour.steps || !tour.steps.length) return;
    clearMarks();
    T = { tour: tour, i: 0, at: Date.now() }; save();
    run();
  }
  // Coming back to a page in the middle of a tour.
  function resume() {
    var s = ss();
    if (!s || !s.tour || Date.now() - (s.at || 0) > MAX_AGE) { ss(null); return; }
    T = s;
    setTimeout(run, 900);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', resume); else resume();

  window.FitTour = { start: start, stop: function () { stop(true); }, active: function () { return !!T; }, weapons: WEAPONS, weapon: weapon };
})();
