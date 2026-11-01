/* TapTap call Studio — camera effects that run on YOUR device.

   The camera picture is changed in this browser before it is sent: the other person only ever receives the
   finished video. Nothing extra leaves the device — no frame goes to TapTap or anyone else for these effects.

   Background   blur (soft / strong), spotlight, scenes painted on the fly (no image files), a colour, or your own
                photo (kept only in this browser). While the background model loads, the WHOLE picture is softened,
                so a room you chose to hide is never shown by surprise.
   Face         masks that follow your head — shades, glasses, masquerade, hero, cat, crown, support headset,
                mustache, party hat, halo, an emoji head that opens its mouth when you do, and a privacy face that
                pixelates you while you keep talking. Touch-up and low-light boost.
   Draw         pen, highlighter, arrows, stickers and text on your own video. "Stick to my face" pins a drawing to
                your face (draw a mustache — it moves with you). "Disappearing ink" fades after a few seconds.
   Name tag     a lower-third with your name and role ("Lamin · TapTap Support").

   Models: MediaPipe Selfie Segmentation and Face Mesh, served by TapTap (static/vendor/mediapipe). They load
   only when an effect needs them. Frame pipeline: MediaStreamTrackProcessor/Generator where available (keeps
   running when the call window is in the background), else a canvas + captureStream. */
(function () {
  'use strict';
  var VER = { ss: '0.1.1675465747', fm: '0.4.1633559619' };
  var CDN = { ss: 'https://cdn.jsdelivr.net/npm/@mediapipe/selfie_segmentation@' + VER.ss + '/', fm: 'https://cdn.jsdelivr.net/npm/@mediapipe/face_mesh@' + VER.fm + '/' };
  var KEY = 'tt-fx-v1', IMG_KEY = 'tt-fx-bg';
  var FILTER_OK = (function () { try { return 'filter' in document.createElement('canvas').getContext('2d'); } catch (e) { return false; } })();

  function simd() {        // WebAssembly SIMD (the vendored builds need it)
    try { return WebAssembly.validate(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0, 253, 15, 253, 98, 11])); } catch (e) { return false; }
  }
  function canvas(w, h) { var c = document.createElement('canvas'); c.width = w || 2; c.height = h || 2; return c; }
  // Hand control back to the browser between frames. A frame pipeline that is slower than the camera otherwise
  // runs back-to-back in microtasks: timers stop, so the call's heartbeat stops and the call drops as "lost".
  var yieldTask = (function () {
    if (!window.MessageChannel) return function () { return new Promise(function (r) { setTimeout(r, 0); }); };
    var ch = new MessageChannel(), q = []; ch.port1.onmessage = function () { var r = q.shift(); if (r) r(); };
    return function () { return new Promise(function (r) { q.push(r); ch.port2.postMessage(0); }); };
  })();
  function size(c, w, h) { if (c.width !== w) c.width = w; if (c.height !== h) c.height = h; return c; }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  var scripts = {};
  function script(src, global) {
    if (window[global]) return Promise.resolve(window[global]);
    if (!scripts[src]) scripts[src] = new Promise(function (res, rej) {
      var s = document.createElement('script'); s.src = src; s.async = true; s.crossOrigin = 'anonymous';
      s.onload = function () { window[global] ? res(window[global]) : rej(new Error('not loaded')); }; s.onerror = function () { rej(new Error('blocked')); };
      document.head.appendChild(s);
    });
    return scripts[src];
  }

  // ─────────────────────────────── catalogue ───────────────────────────────
  var BACKGROUNDS = [
    { id: 'none', icon: '🚫', name: 'Off' }, { id: 'blur', icon: '🌫️', name: 'Soft blur' }, { id: 'blur2', icon: '🌁', name: 'Strong blur' },
    { id: 'spot', icon: '🔦', name: 'Spotlight' }, { id: 'scene:studio', icon: '🎙️', name: 'Studio' }, { id: 'scene:office', icon: '🪴', name: 'Office' },
    { id: 'scene:sunset', icon: '🌅', name: 'Sunset' }, { id: 'scene:ocean', icon: '🌊', name: 'Ocean' }, { id: 'scene:night', icon: '🌌', name: 'Night sky' },
    { id: 'scene:bokeh', icon: '✨', name: 'Café lights' }, { id: 'scene:paper', icon: '📄', name: 'Clean wall' }, { id: 'image', icon: '🖼️', name: 'My photo' }
  ];
  var COLORS = ['#0b1220', '#1769e0', '#18a66a', '#7c3aed', '#e5484d', '#f59e0b', '#f1f5f9'];
  var FACES = [
    { id: 'none', icon: '🚫', name: 'Off' }, { id: 'shades', icon: '😎', name: 'Shades' }, { id: 'glasses', icon: '🤓', name: 'Glasses' },
    { id: 'masq', icon: '🎭', name: 'Masquerade' }, { id: 'hero', icon: '🦸', name: 'Hero mask' }, { id: 'cat', icon: '🐱', name: 'Cat' },
    { id: 'crown', icon: '👑', name: 'Crown' }, { id: 'headset', icon: '🎧', name: 'Support headset' }, { id: 'stache', icon: '🥸', name: 'Mustache' },
    { id: 'party', icon: '🥳', name: 'Party hat' }, { id: 'halo', icon: '😇', name: 'Halo' }, { id: 'emoji', icon: '😀', name: 'Emoji head' },
    { id: 'pixel', icon: '🫥', name: 'Privacy face' }
  ];
  var INK_COLORS = ['#facc15', '#ef4444', '#22c55e', '#38bdf8', '#ffffff', '#111827', '#ec4899'];
  var STICKERS = ['⭐', '❤️', '👍', '🔥', '✅', '❓', '👉', '📶', '🎉', '💡'];

  // ─────────────────────────────── scenes (painted, no files) ───────────────────────────────
  function rnd(seed) { var s = seed >>> 0; return function () { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; }; }
  function paintScene(id, W, H, brand) {
    var c = canvas(W, H), g = c.getContext('2d'), r = rnd(id.length * 977 + W), gr;
    function lin(y0, y1, stops) { var l = g.createLinearGradient(0, y0, 0, y1); stops.forEach(function (s) { l.addColorStop(s[0], s[1]); }); return l; }
    if (id === 'studio') {
      g.fillStyle = lin(0, H, [[0, '#0b1220'], [1, '#172554']]); g.fillRect(0, 0, W, H);
      [[0.18, 0.25, brand || '#1769e0'], [0.85, 0.7, '#7c3aed'], [0.6, 0.1, '#0ea5a4']].forEach(function (o) {
        gr = g.createRadialGradient(o[0] * W, o[1] * H, 0, o[0] * W, o[1] * H, W * 0.45); gr.addColorStop(0, o[2] + '88'); gr.addColorStop(1, o[2] + '00');
        g.fillStyle = gr; g.fillRect(0, 0, W, H);
      });
      g.strokeStyle = 'rgba(255,255,255,.05)'; g.lineWidth = 1;
      for (var x = 0; x < W; x += W / 16) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke(); }
      for (var y = 0; y < H; y += W / 16) { g.beginPath(); g.moveTo(0, y); g.lineTo(W, y); g.stroke(); }
    } else if (id === 'sunset') {
      g.fillStyle = lin(0, H, [[0, '#ff7e5f'], [0.55, '#feb47b'], [1, '#ffd194']]); g.fillRect(0, 0, W, H);
      gr = g.createRadialGradient(W * 0.7, H * 0.55, 0, W * 0.7, H * 0.55, H * 0.35); gr.addColorStop(0, '#fff7d6'); gr.addColorStop(0.35, '#ffe08a'); gr.addColorStop(1, 'rgba(255,224,138,0)');
      g.fillStyle = gr; g.fillRect(0, 0, W, H);
      [['#7b4b6a', 0.72, 0.09], ['#4c2c4a', 0.82, 0.07], ['#2d1b33', 0.92, 0.05]].forEach(function (h) {
        g.fillStyle = h[0]; g.beginPath(); g.moveTo(0, H);
        for (var i = 0; i <= 12; i++) g.lineTo(i / 12 * W, H * h[1] - Math.sin(i * 1.3 + h[1] * 9) * H * h[2]);
        g.lineTo(W, H); g.fill();
      });
    } else if (id === 'night') {
      g.fillStyle = lin(0, H, [[0, '#020617'], [1, '#1e1b4b']]); g.fillRect(0, 0, W, H);
      for (var s = 0; s < 220; s++) { g.fillStyle = 'rgba(255,255,255,' + (0.3 + r() * 0.7).toFixed(2) + ')'; var sz = r() < 0.92 ? 1 : 2; g.fillRect(r() * W, r() * H * 0.85, sz, sz); }
      g.fillStyle = '#fef9c3'; g.beginPath(); g.arc(W * 0.78, H * 0.22, H * 0.09, 0, 7); g.fill();
      g.fillStyle = '#0f0d33'; g.beginPath(); g.arc(W * 0.8 + H * 0.035, H * 0.2, H * 0.085, 0, 7); g.fill();
    } else if (id === 'office') {
      g.fillStyle = lin(0, H, [[0, '#ece7df'], [1, '#d9d2c5']]); g.fillRect(0, 0, W, H);
      var wx = W * 0.58, wy = H * 0.12, ww = W * 0.34, wh = H * 0.52;
      g.fillStyle = lin(wy, wy + wh, [[0, '#9fd3ff'], [1, '#e0f2fe']]); g.fillRect(wx, wy, ww, wh);
      g.strokeStyle = '#f8f5ef'; g.lineWidth = H * 0.025; g.strokeRect(wx, wy, ww, wh);
      g.beginPath(); g.moveTo(wx + ww / 2, wy); g.lineTo(wx + ww / 2, wy + wh); g.moveTo(wx, wy + wh / 2); g.lineTo(wx + ww, wy + wh / 2); g.stroke();
      g.fillStyle = '#b08968'; g.fillRect(W * 0.04, H * 0.62, W * 0.3, H * 0.025);
      g.fillStyle = '#7c5c45'; g.fillRect(W * 0.08, H * 0.82, W * 0.09, H * 0.18);
      g.fillStyle = '#4d7c4a'; for (var l = 0; l < 9; l++) { g.beginPath(); g.ellipse(W * 0.125 + (r() - 0.5) * W * 0.09, H * 0.74 - r() * H * 0.16, W * 0.03, H * 0.07, (r() - 0.5) * 1.6, 0, 7); g.fill(); }
      [['#1769e0', 0.07], ['#e5484d', 0.12], ['#f59e0b', 0.17], ['#18a66a', 0.22]].forEach(function (b) { g.fillStyle = b[0]; g.fillRect(W * b[1], H * 0.5, W * 0.035, H * 0.12); });
      if (FILTER_OK) { var t = canvas(W, H), tg = t.getContext('2d'); tg.filter = 'blur(' + Math.round(W / 180) + 'px)'; tg.drawImage(c, 0, 0); return t; }
    } else if (id === 'ocean') {
      g.fillStyle = lin(0, H * 0.55, [[0, '#7dd3fc'], [1, '#e0f2fe']]); g.fillRect(0, 0, W, H * 0.55);
      g.fillStyle = lin(H * 0.55, H, [[0, '#0284c7'], [1, '#0c4a6e']]); g.fillRect(0, H * 0.55, W, H * 0.45);
      g.strokeStyle = 'rgba(255,255,255,.35)'; g.lineWidth = Math.max(1, H / 300);
      for (var k = 0; k < 40; k++) { var yy = H * 0.57 + r() * H * 0.4, xx = r() * W, len = W * (0.02 + r() * 0.06); g.beginPath(); g.moveTo(xx, yy); g.lineTo(xx + len, yy); g.stroke(); }
    } else if (id === 'bokeh') {
      g.fillStyle = lin(0, H, [[0, '#2b1d14'], [1, '#120c08']]); g.fillRect(0, 0, W, H);
      var hues = ['255,200,120', '255,160,90', '255,230,170', '200,140,255', '120,200,255'];
      for (var b2 = 0; b2 < 46; b2++) {
        var bx = r() * W, by = r() * H, br = H * (0.03 + r() * 0.09), hc = hues[Math.floor(r() * hues.length)];
        gr = g.createRadialGradient(bx, by, 0, bx, by, br); gr.addColorStop(0, 'rgba(' + hc + ',.55)'); gr.addColorStop(0.7, 'rgba(' + hc + ',.25)'); gr.addColorStop(1, 'rgba(' + hc + ',0)');
        g.fillStyle = gr; g.beginPath(); g.arc(bx, by, br, 0, 7); g.fill();
      }
    } else {     // paper
      g.fillStyle = lin(0, H, [[0, '#f8fafc'], [1, '#e2e8f0']]); g.fillRect(0, 0, W, H);
    }
    return c;
  }

  // ─────────────────────────────── face geometry ───────────────────────────────
  // Landmark indices (MediaPipe Face Mesh). "a" = the eye on the left of the picture.
  var IX = { aOut: 33, aIn: 133, bOut: 263, bIn: 362, top: 10, chin: 152, nose: 1, noseBase: 2, lipTop: 0, lipIn: 13, lipInB: 14,
             mA: 61, mB: 291, cheekA: 234, cheekB: 454, browA: 105, browB: 334, lidA: 159, lidAb: 145, lidB: 386, lidBb: 374 };
  var OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109];
  // A face frame: origin between the outer eye corners, x along the eye line, 1 unit = eye-corner distance.
  function faceFrame(lm, W, H) {
    function P(i) { return [lm[i][0] * W, lm[i][1] * H]; }
    var a = P(IX.aOut), b = P(IX.bOut), cx = (a[0] + b[0]) / 2, cy = (a[1] + b[1]) / 2;
    var ang = Math.atan2(b[1] - a[1], b[0] - a[0]), u = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1, co = Math.cos(-ang), si = Math.sin(-ang);
    var F = { cx: cx, cy: cy, ang: ang, u: u, P: P };
    F.local = function (p) { var dx = p[0] - cx, dy = p[1] - cy; return [(dx * co - dy * si) / u, (dx * si + dy * co) / u]; };
    F.world = function (q) { var x = q[0] * u, y = q[1] * u, c2 = Math.cos(ang), s2 = Math.sin(ang); return [cx + x * c2 - y * s2, cy + x * s2 + y * c2]; };
    F.L = function (i) { return F.local(P(i)); };
    F.mid = function (i, j) { var p = F.L(i), q = F.L(j); return [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2]; };
    return F;
  }

  // ─────────────────────────────── masks (drawn in face units) ───────────────────────────────
  function eyeHoles(g, F, rx, ry) { [F.mid(IX.aOut, IX.aIn), F.mid(IX.bOut, IX.bIn)].forEach(function (e) { g.moveTo(e[0] + rx, e[1]); g.ellipse(e[0], e[1], rx, ry, 0, 0, Math.PI * 2, true); }); }
  var DRAW = {
    shades: function (g, F) {
      var ea = F.mid(IX.aOut, IX.aIn), eb = F.mid(IX.bOut, IX.bIn);
      [ea, eb].forEach(function (e) {
        var gr = g.createLinearGradient(0, e[1] - 0.2, 0, e[1] + 0.2); gr.addColorStop(0, 'rgba(20,20,28,.96)'); gr.addColorStop(1, 'rgba(55,55,70,.93)');
        g.fillStyle = gr; g.beginPath(); g.ellipse(e[0], e[1] + 0.02, 0.3, 0.2, 0, 0, 7); g.fill();
        g.strokeStyle = '#0a0a0a'; g.lineWidth = 0.035; g.stroke();
        g.strokeStyle = 'rgba(255,255,255,.35)'; g.lineWidth = 0.025; g.beginPath(); g.moveTo(e[0] - 0.17, e[1] - 0.07); g.lineTo(e[0] - 0.05, e[1] - 0.13); g.stroke();
      });
      g.strokeStyle = '#0a0a0a'; g.lineWidth = 0.05; g.beginPath(); g.moveTo(ea[0] + 0.26, ea[1] - 0.04); g.quadraticCurveTo(0, ea[1] - 0.12, eb[0] - 0.26, eb[1] - 0.04); g.stroke();
      g.beginPath(); g.moveTo(ea[0] - 0.3, ea[1] - 0.02); g.lineTo(F.L(IX.cheekA)[0], ea[1] - 0.04); g.moveTo(eb[0] + 0.3, eb[1] - 0.02); g.lineTo(F.L(IX.cheekB)[0], eb[1] - 0.04); g.stroke();
    },
    glasses: function (g, F) {
      var ea = F.mid(IX.aOut, IX.aIn), eb = F.mid(IX.bOut, IX.bIn);
      g.strokeStyle = '#5b3a1e'; g.lineWidth = 0.045; g.fillStyle = 'rgba(200,230,255,.12)';
      [ea, eb].forEach(function (e) { g.beginPath(); g.arc(e[0], e[1] + 0.02, 0.25, 0, 7); g.fill(); g.stroke(); });
      g.beginPath(); g.moveTo(ea[0] + 0.25, ea[1]); g.quadraticCurveTo(0, ea[1] - 0.1, eb[0] - 0.25, eb[1]); g.stroke();
      g.beginPath(); g.moveTo(ea[0] - 0.25, ea[1]); g.lineTo(F.L(IX.cheekA)[0], ea[1] - 0.03); g.moveTo(eb[0] + 0.25, eb[1]); g.lineTo(F.L(IX.cheekB)[0], eb[1] - 0.03); g.stroke();
    },
    masq: function (g, F) {
      var y = F.mid(IX.aOut, IX.bOut)[1];
      g.beginPath(); g.moveTo(-0.95, y - 0.28); g.quadraticCurveTo(-0.55, y - 0.48, 0, y - 0.22); g.quadraticCurveTo(0.55, y - 0.48, 0.95, y - 0.28);
      g.quadraticCurveTo(0.8, y + 0.3, 0.35, y + 0.3); g.quadraticCurveTo(0.1, y + 0.3, 0, y + 0.14); g.quadraticCurveTo(-0.1, y + 0.3, -0.35, y + 0.3);
      g.quadraticCurveTo(-0.8, y + 0.3, -0.95, y - 0.28); g.closePath(); eyeHoles(g, F, 0.17, 0.11);
      var gr = g.createLinearGradient(-1, 0, 1, 0); gr.addColorStop(0, '#6d28d9'); gr.addColorStop(0.5, '#a855f7'); gr.addColorStop(1, '#6d28d9');
      g.fillStyle = gr; g.fill('evenodd'); g.strokeStyle = '#facc15'; g.lineWidth = 0.035; g.stroke();
      g.fillStyle = '#fde68a'; [-0.7, -0.45, 0.45, 0.7].forEach(function (x) { g.beginPath(); g.arc(x, y - 0.25, 0.03, 0, 7); g.fill(); });
      g.strokeStyle = '#ec4899'; g.lineWidth = 0.05; for (var i = 0; i < 4; i++) { g.beginPath(); g.moveTo(0.9, y - 0.3); g.quadraticCurveTo(1.15 + i * 0.05, y - 0.8 - i * 0.12, 1.0 + i * 0.12, y - 1.1 - i * 0.1); g.stroke(); }
    },
    hero: function (g, F) {
      var y = F.mid(IX.aOut, IX.bOut)[1];
      g.beginPath(); g.moveTo(-0.85, y - 0.12); g.quadraticCurveTo(-0.5, y - 0.36, 0, y - 0.16); g.quadraticCurveTo(0.5, y - 0.36, 0.85, y - 0.12);
      g.quadraticCurveTo(0.65, y + 0.26, 0.3, y + 0.22); g.quadraticCurveTo(0, y + 0.1, -0.3, y + 0.22); g.quadraticCurveTo(-0.65, y + 0.26, -0.85, y - 0.12); g.closePath();
      eyeHoles(g, F, 0.15, 0.09); g.fillStyle = '#dc2626'; g.fill('evenodd'); g.strokeStyle = '#450a0a'; g.lineWidth = 0.04; g.stroke();
    },
    cat: function (g, F) {
      var t = F.L(IX.top), n = F.L(IX.nose);
      [-1, 1].forEach(function (s) {
        g.fillStyle = '#3f3f46'; g.beginPath(); g.moveTo(s * 0.18, t[1] + 0.05); g.lineTo(s * 0.62, t[1] - 0.7); g.lineTo(s * 0.78, t[1] + 0.15); g.closePath(); g.fill();
        g.fillStyle = '#f9a8d4'; g.beginPath(); g.moveTo(s * 0.32, t[1] + 0.02); g.lineTo(s * 0.6, t[1] - 0.48); g.lineTo(s * 0.68, t[1] + 0.07); g.closePath(); g.fill();
        g.strokeStyle = 'rgba(255,255,255,.9)'; g.lineWidth = 0.02;
        for (var i = -1; i <= 1; i++) { g.beginPath(); g.moveTo(s * 0.16, n[1] + 0.12 + i * 0.05); g.lineTo(s * 0.85, n[1] + 0.05 + i * 0.14); g.stroke(); }
      });
      g.fillStyle = '#ec4899'; g.beginPath(); g.moveTo(n[0] - 0.09, n[1] - 0.03); g.lineTo(n[0] + 0.09, n[1] - 0.03); g.lineTo(n[0], n[1] + 0.07); g.closePath(); g.fill();
    },
    crown: function (g, F) {
      var t = F.L(IX.top), y = t[1] - 0.05, h = 0.6;
      g.beginPath(); g.moveTo(-0.55, y); g.lineTo(-0.62, y - h * 0.75); g.lineTo(-0.33, y - h * 0.45); g.lineTo(-0.18, y - h); g.lineTo(0, y - h * 0.5);
      g.lineTo(0.18, y - h); g.lineTo(0.33, y - h * 0.45); g.lineTo(0.62, y - h * 0.75); g.lineTo(0.55, y); g.closePath();
      var gr = g.createLinearGradient(0, y - h, 0, y); gr.addColorStop(0, '#fde047'); gr.addColorStop(1, '#ca8a04'); g.fillStyle = gr; g.fill();
      g.strokeStyle = '#854d0e'; g.lineWidth = 0.025; g.stroke();
      [['#ef4444', 0], ['#3b82f6', -0.32], ['#22c55e', 0.32]].forEach(function (j) { g.fillStyle = j[0]; g.beginPath(); g.arc(j[1], y - 0.13, 0.055, 0, 7); g.fill(); });
    },
    headset: function (g, F, k) {
      var t = F.L(IX.top), ca = F.L(IX.cheekA), cb = F.L(IX.cheekB), mA = F.L(IX.mA);
      g.strokeStyle = '#1f2937'; g.lineWidth = 0.1; g.lineCap = 'round';
      g.beginPath(); g.moveTo(ca[0] - 0.02, ca[1] - 0.1); g.bezierCurveTo(ca[0] - 0.1, t[1] - 0.55, cb[0] + 0.1, t[1] - 0.55, cb[0] + 0.02, cb[1] - 0.1); g.stroke();
      [ca, cb].forEach(function (c, i) {
        g.fillStyle = '#111827'; g.beginPath(); g.ellipse(c[0] + (i ? 0.04 : -0.04), c[1] + 0.05, 0.13, 0.22, 0, 0, 7); g.fill();
        g.fillStyle = '#374151'; g.beginPath(); g.ellipse(c[0] + (i ? 0.04 : -0.04), c[1] + 0.05, 0.08, 0.15, 0, 0, 7); g.fill();
      });
      g.strokeStyle = '#111827'; g.lineWidth = 0.035; g.beginPath(); g.moveTo(ca[0], ca[1] + 0.2); g.quadraticCurveTo(ca[0] + 0.05, mA[1] + 0.15, mA[0] - 0.12, mA[1] + 0.02); g.stroke();
      g.fillStyle = '#111827'; g.beginPath(); g.ellipse(mA[0] - 0.1, mA[1] + 0.02, 0.07, 0.045, 0.3, 0, 7); g.fill();
      g.fillStyle = (Math.floor(k / 600) % 2) ? '#22c55e' : '#1769e0'; g.beginPath(); g.arc(cb[0] + 0.04, cb[1] - 0.06, 0.025, 0, 7); g.fill();     // "on air" light
    },
    stache: function (g, F) {
      var nb = F.L(IX.noseBase), lt = F.L(IX.lipTop), y = (nb[1] + lt[1]) / 2 + 0.02;
      g.fillStyle = '#3b2314'; g.beginPath(); g.moveTo(0, y - 0.05);
      g.bezierCurveTo(0.12, y - 0.13, 0.32, y - 0.1, 0.38, y); g.bezierCurveTo(0.45, y + 0.04, 0.52, y - 0.06, 0.5, y - 0.12);
      g.bezierCurveTo(0.58, y + 0.02, 0.45, y + 0.12, 0.3, y + 0.08); g.bezierCurveTo(0.18, y + 0.06, 0.08, y + 0.04, 0, y + 0.06);
      g.bezierCurveTo(-0.08, y + 0.04, -0.18, y + 0.06, -0.3, y + 0.08); g.bezierCurveTo(-0.45, y + 0.12, -0.58, y + 0.02, -0.5, y - 0.12);
      g.bezierCurveTo(-0.52, y - 0.06, -0.45, y + 0.04, -0.38, y); g.bezierCurveTo(-0.32, y - 0.1, -0.12, y - 0.13, 0, y - 0.05); g.fill();
    },
    party: function (g, F, k) {
      var t = F.L(IX.top);
      g.save(); g.translate(0.15, t[1] + 0.02); g.rotate(0.22);
      g.beginPath(); g.moveTo(-0.38, 0); g.lineTo(0, -1.0); g.lineTo(0.38, 0); g.closePath(); g.fillStyle = '#ec4899'; g.fill();
      g.save(); g.clip(); g.strokeStyle = '#fde047'; g.lineWidth = 0.08; for (var i = -1; i < 6; i++) { g.beginPath(); g.moveTo(-0.5, -i * 0.2); g.lineTo(0.5, -i * 0.2 - 0.25); g.stroke(); } g.restore();
      g.fillStyle = '#facc15'; g.beginPath(); g.arc(0, -1.0, 0.09, 0, 7); g.fill(); g.restore();
      var r = rnd(7), cols = ['#ef4444', '#22c55e', '#3b82f6', '#facc15', '#a855f7'];
      for (var c = 0; c < 18; c++) {
        var x0 = (r() - 0.5) * 2.6, sp = 0.3 + r() * 0.5, y0 = ((r() * 3 + k / 1000 * sp) % 3) - 1.8;
        g.fillStyle = cols[c % 5]; g.save(); g.translate(x0, t[1] + y0); g.rotate(k / 300 + c); g.fillRect(-0.03, -0.015, 0.06, 0.03); g.restore();
      }
    },
    halo: function (g, F, k) {
      var t = F.L(IX.top), y = t[1] - 0.35 + Math.sin(k / 500) * 0.03;
      g.save(); g.shadowColor = '#fde047'; g.shadowBlur = 0.3; g.strokeStyle = '#fde047'; g.lineWidth = 0.07; g.beginPath(); g.ellipse(0, y, 0.48, 0.12, 0, 0, 7); g.stroke(); g.restore();
    },
    emoji: function (g, F) {
      var t = F.L(IX.top), c = F.L(IX.chin), open = F.L(IX.lipInB)[1] - F.L(IX.lipIn)[1], tall = c[1] - t[1];
      var e = open / tall > 0.1 ? '\uD83D\uDE2E' : '\uD83D\uDE00', s = tall * 1.25, cy = (t[1] + c[1]) / 2;     // 😮 when your mouth opens, else 😀
      g.font = s + 'px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText(e, 0, cy);
    }
  };
  function pixelFace(g, F, src, W, H) {
    var xs = [], ys = [];
    g.save(); g.beginPath(); OVAL.forEach(function (i, n) { var p = F.P(i); xs.push(p[0]); ys.push(p[1]); n ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1]); }); g.closePath();
    var x0 = Math.max(0, Math.min.apply(null, xs) - F.u * 0.1), y0 = Math.max(0, Math.min.apply(null, ys) - F.u * 0.4), x1 = Math.min(W, Math.max.apply(null, xs) + F.u * 0.1), y1 = Math.min(H, Math.max.apply(null, ys) + F.u * 0.1);
    g.moveTo(x0, y0); g.rect(x0, y0, x1 - x0, Math.max(0, Math.min.apply(null, ys) - y0) + F.u * 0.15);   // hair line too
    g.clip();
    var bs = Math.max(8, F.u / 5), tw = Math.max(1, Math.ceil((x1 - x0) / bs)), th = Math.max(1, Math.ceil((y1 - y0) / bs)), t = canvas(tw, th);
    t.getContext('2d').drawImage(src, x0, y0, x1 - x0, y1 - y0, 0, 0, tw, th);
    g.imageSmoothingEnabled = false; g.drawImage(t, 0, 0, tw, th, x0, y0, x1 - x0, y1 - y0); g.restore();
  }

  // ─────────────────────────────── the engine ───────────────────────────────
  function Engine(opt) {
    this.opt = opt || {}; this.base = (this.opt.base || '/static/vendor/mediapipe/').replace(/\/?$/, '/');
    var saved = {}; try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) {}
    this.st = { bg: saved.bg || 'none', face: saved.face || 'none', glow: !!saved.glow, light: saved.light !== false, tag: !!saved.tag,
                tagName: saved.tagName || this.opt.name || '', tagTitle: saved.tagTitle != null ? saved.tagTitle : (this.opt.title || ''), color: saved.color || this.opt.color || '#1769e0' };
    if (this.st.bg === 'image') this.loadImage();
    this.ink = { strokes: [], tool: 'pen', color: '#facc15', width: 4, stick: true, fade: false, sticker: '⭐' };
    this.frameCv = canvas(); this.outCv = canvas(); this.segCv = canvas(); this.faceCv = canvas(); this.maskCv = canvas(); this.cutCv = canvas(); this.blurA = canvas(); this.blurB = canvas(); this.lumCv = canvas(32, 18);
    this.mask = { ready: false, busy: false, model: null, failed: '', at: 0, sel: -1 };
    this.face = { busy: false, model: null, failed: '', lm: null, at: 0 };
    this.scenes = {}; this.stats = { n: 0, ms: 0, fps: 0, t0: 0, frames: 0 }; this.luma = 128; this.track = null; this.src = null;
    this.listeners = [];
  }
  var E = Engine.prototype;
  E.on = function (fn) { this.listeners.push(fn); };
  E.emit = function (what) { var self = this; this.listeners.forEach(function (f) { try { f(what, self); } catch (e) {} }); };
  E.save = function () { try { var s = Object.assign({}, this.st); localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {} };
  E.active = function () { var s = this.st; return s.bg !== 'none' || s.face !== 'none' || s.glow || s.tag || this.ink.strokes.length > 0; };
  E.set = function (patch) {
    var was = this.active(); Object.assign(this.st, patch || {}); this.save();
    if (patch && patch.bg === 'image') this.loadImage();
    if (this.st.bg !== 'none') this.loadSeg();
    if (this.st.face !== 'none') this.loadFace();
    this.emit(was !== this.active() ? 'active' : 'change');
  };
  E.status = function () {
    var s = this.st, out = [];
    if (s.bg !== 'none') out.push(this.mask.failed ? 'Background: ' + this.mask.failed : this.mask.ready ? '' : 'Loading background effect… your room stays hidden meanwhile');
    if (s.face !== 'none' || this.inkNeedsFace()) out.push(this.face.failed ? 'Face: ' + this.face.failed : (this.face.model && !this.face.lm && this.face.at) ? 'Looking for your face…' : this.face.model ? '' : 'Loading face tracking…');
    return out.filter(Boolean).join(' · ');
  };
  E.locate = function (dir, cdn) {
    var base = this.base + dir + '/';
    return function (f) { return /simd|\.tflite$|\.binarypb$|packed_assets|^[a-z_]+\.js$/.test(f) && (f.indexOf('wasm_bin') < 0 || f.indexOf('simd') >= 0) ? base + f : cdn + f; };
  };
  E.loadSeg = function () {
    var self = this, m = this.mask; if (m.model || m.loading || m.failed) return;
    m.loading = true; this.emit('status');
    var src = simd() ? this.base + 'selfie_segmentation/selfie_segmentation.js' : CDN.ss + 'selfie_segmentation.js';
    script(src, 'SelfieSegmentation').then(function (SS) {
      var model = new SS({ locateFile: self.locate('selfie_segmentation', CDN.ss) });
      model.setOptions({ modelSelection: 1, selfieMode: false });
      model.onResults(function (r) { self.onMask(r); });
      m.model = model; m.loading = false; self.emit('status');
    }).catch(function () { m.loading = false; m.failed = 'needs a newer browser or the internet — your picture is softened instead'; self.emit('status'); });
  };
  E.loadFace = function () {
    var self = this, f = this.face; if (f.model || f.loading || f.failed) return;
    f.loading = true; this.emit('status');
    var src = simd() ? this.base + 'face_mesh/face_mesh.js' : CDN.fm + 'face_mesh.js';
    script(src, 'FaceMesh').then(function (FM) {
      var model = new FM({ locateFile: self.locate('face_mesh', CDN.fm) });
      model.setOptions({ maxNumFaces: 1, refineLandmarks: false, minDetectionConfidence: 0.5, minTrackingConfidence: 0.5, selfieMode: false });
      model.onResults(function (r) { self.onFace(r); });
      f.model = model; f.loading = false; self.emit('status');
    }).catch(function () { f.loading = false; f.failed = 'face tracking could not load on this browser'; self.emit('status'); });
  };
  // Opening the Studio shows intent: load and start the background model now (one tiny frame), so picking an
  // effect is instant instead of a pause while the model starts up.
  E.warm = function () {
    var self = this; this.loadSeg();
    var tries = 0;
    (function prime() {
      var m = self.mask; if (m.failed || m.primed || ++tries > 60) return;
      if (!m.model) return setTimeout(prime, 250);
      m.primed = true; var c = canvas(64, 36); c.getContext('2d').fillRect(0, 0, 64, 36);
      if (!m.busy) { m.busy = true; m.model.send({ image: c }).then(function () { m.busy = false; self.emit('status'); }, function () { m.busy = false; }); }
    })();
  };
  E.onMask = function (r) {
    var m = r && r.segmentationMask; if (!m) return;
    var w = this.segCv.width, h = this.segCv.height; if (w < 16 || h < 16) return;          // the warm-up frame
    var g = size(this.maskCv, w, h).getContext('2d');
    g.globalCompositeOperation = 'copy'; g.drawImage(m, 0, 0, w, h); g.globalCompositeOperation = 'source-over';
    if (this.maskAlpha === undefined) {          // the mask's confidence: in alpha, or (some builds) only in red
      var d = g.getImageData(0, 0, w, h).data, opaque = 0; for (var i = 3; i < d.length; i += 16) if (d[i] > 250) opaque++;
      this.maskAlpha = opaque < d.length / 16 * 0.97;
    }
    if (!this.maskAlpha) { var id = g.getImageData(0, 0, w, h), p = id.data; for (var j = 0; j < p.length; j += 4) { p[j + 3] = p[j]; } g.putImageData(id, 0, 0); }
    this.mask.ready = true; this.mask.at = performance.now(); if (!this.mask.seen) { this.mask.seen = true; this.emit('status'); }
  };
  E.onFace = function (r) {
    var L = r && r.multiFaceLandmarks && r.multiFaceLandmarks[0], f = this.face, was = !!f.lm; f.at = performance.now();
    if (!L) { if (f.lm && performance.now() - (f.seenAt || 0) > 600) f.lm = null; if (was !== !!f.lm) this.emit('status'); return; }
    f.seenAt = performance.now();
    // smooth small jitter, but snap to big or late moves (a slow device updates rarely: smoothing would only add lag)
    var gap = performance.now() - (f.prevAt || 0), mv = f.lm ? Math.abs(L[1].x - f.lm[1][0]) + Math.abs(L[1].y - f.lm[1][1]) : 1;
    var a = !f.lm ? 1 : (mv > 0.02 || gap > 120) ? 0.9 : 0.55, out = f.lm || new Array(L.length); f.prevAt = performance.now();
    for (var i = 0; i < L.length; i++) { var p = L[i], q = out[i]; out[i] = q ? [q[0] + (p.x - q[0]) * a, q[1] + (p.y - q[1]) * a] : [p.x, p.y]; }
    f.lm = out; if (!was) this.emit('status');
  };
  E.inkNeedsFace = function () { return this.ink.strokes.some(function (s) { return s.anchor === 'face'; }) || (this.ink.stick && this.drawing); };

  E.loadImage = function () {
    var self = this, url = ''; try { url = localStorage.getItem(IMG_KEY) || ''; } catch (e) {}
    if (!url) { this.img = null; return; }
    var im = new Image(); im.onload = function () { self.img = im; self.scenes = {}; }; im.src = url;
  };
  E.setImage = function (file) {
    var self = this;
    return new Promise(function (res, rej) {
      if (!file || !/^image\//.test(file.type)) return rej(new Error('Choose a photo.'));
      var url = URL.createObjectURL(file), im = new Image();
      im.onload = function () {
        var sc = Math.min(1, 1280 / Math.max(im.width, im.height)), c = canvas(Math.round(im.width * sc), Math.round(im.height * sc));
        c.getContext('2d').drawImage(im, 0, 0, c.width, c.height); URL.revokeObjectURL(url);
        var data = c.toDataURL('image/jpeg', 0.82);
        try { localStorage.setItem(IMG_KEY, data); } catch (e) { /* too big for this browser: keep it for this call only */ }
        var im2 = new Image(); im2.onload = function () { self.img = im2; self.scenes = {}; self.set({ bg: 'image' }); res(); }; im2.src = data;
      };
      im.onerror = function () { URL.revokeObjectURL(url); rej(new Error('That photo could not be opened.')); };
      im.src = url;
    });
  };

  // One frame in, one frame out. `src` is a canvas holding the camera frame.
  E.compose = function (W, H) {
    var s = this.st, t0 = performance.now(), F = this.frameCv, out = size(this.outCv, W, H), g = out.getContext('2d'), m = this.mask;
    var wantBg = s.bg !== 'none', wantFace = s.face !== 'none' || this.inkNeedsFace();
    if (wantBg && m.model && !m.busy) {
      var sw = 256, sh = Math.max(64, Math.round(256 * H / W)); size(this.segCv, sw, sh).getContext('2d').drawImage(F, 0, 0, sw, sh);
      var sel = H > W ? 0 : 1; if (sel !== m.sel) { m.sel = sel; try { m.model.setOptions({ modelSelection: sel }); } catch (e) {} }
      m.busy = true; var self = this;
      m.model.send({ image: this.segCv }).then(function () { m.busy = false; }, function () { m.busy = false; });
    }
    if (wantFace && this.face.model && !this.face.busy) {
      var fw = 480, fh = Math.round(480 * H / W); size(this.faceCv, fw, fh).getContext('2d').drawImage(F, 0, 0, fw, fh);
      var fc = this.face; fc.busy = true; fc.model.send({ image: this.faceCv }).then(function () { fc.busy = false; }, function () { fc.busy = false; });
    }
    if (++this.stats.frames % 30 === 1) this.measureLight(F);
    var filt = this.lookFilter();
    // background
    if (wantBg && m.ready && performance.now() - m.at < 3000) {
      this.paintBackground(g, W, H);
      var c = size(this.cutCv, W, H), cg = c.getContext('2d');
      cg.globalCompositeOperation = 'copy'; cg.imageSmoothingEnabled = true; cg.imageSmoothingQuality = 'high';
      if (FILTER_OK) cg.filter = 'blur(' + Math.max(1, Math.round(W / 280)) + 'px)';      // feather the low-res mask edge
      cg.drawImage(this.maskCv, 0, 0, W, H); if (FILTER_OK) cg.filter = 'none';
      cg.globalCompositeOperation = 'source-in'; if (FILTER_OK) cg.filter = filt; cg.drawImage(F, 0, 0, W, H); if (FILTER_OK) cg.filter = 'none';
      cg.globalCompositeOperation = 'source-over';
      if (s.bg === 'spot') { g.save(); g.shadowColor = 'rgba(255,240,200,.55)'; g.shadowBlur = H / 14; g.drawImage(c, 0, 0); g.restore(); } else g.drawImage(c, 0, 0);
    } else if (wantBg) {
      this.blur(g, F, W, H, 18);                                    // the model is not ready: never show the room by surprise
    } else {
      if (FILTER_OK && filt !== 'none') g.filter = filt;
      g.drawImage(F, 0, 0, W, H); if (FILTER_OK) g.filter = 'none';
    }
    // face
    var lm = this.face.lm, k = performance.now();
    if (lm && s.face !== 'none') {
      var FF = faceFrame(lm, W, H);
      if (s.face === 'pixel') pixelFace(g, FF, F, W, H);
      else if (DRAW[s.face]) { g.save(); g.translate(FF.cx, FF.cy); g.rotate(FF.ang); g.scale(FF.u, FF.u); g.lineJoin = 'round'; try { DRAW[s.face](g, FF, k); } catch (e) {} g.restore(); }
    }
    this.paintInk(g, W, H, lm ? faceFrame(lm, W, H) : null, k);
    if (s.tag && (s.tagName || s.tagTitle)) this.paintTag(g, W, H);
    var dt = performance.now() - t0, st = this.stats; st.ms = st.ms * 0.9 + dt * 0.1;
    if (!st.t0) st.t0 = k; st.n++; if (k - st.t0 > 1000) { st.fps = Math.round(st.n * 1000 / (k - st.t0)); st.n = 0; st.t0 = k; }
    return out;
  };
  E.lookFilter = function () {
    var f = [];
    if (this.st.light && this.luma < 95) f.push('brightness(' + (1 + clamp((95 - this.luma) / 95, 0, 0.6)).toFixed(2) + ')');
    if (this.st.glow) f.push('brightness(1.06) contrast(1.05) saturate(1.12)');
    return f.join(' ') || 'none';
  };
  E.measureLight = function (F) {
    try { var g = this.lumCv.getContext('2d'); g.drawImage(F, 0, 0, 32, 18); var d = g.getImageData(0, 0, 32, 18).data, s = 0;
      for (var i = 0; i < d.length; i += 4) s += d[i] * 0.299 + d[i + 1] * 0.587 + d[i + 2] * 0.114; this.luma = s / (d.length / 4); } catch (e) {}
  };
  E.blur = function (g, src, W, H, strength) {      // fast blur that works everywhere: shrink, then stretch back
    var a = size(this.blurA, Math.max(2, Math.round(W / (strength / 2))), Math.max(2, Math.round(H / (strength / 2)))), b = size(this.blurB, Math.max(2, Math.round(W / strength)), Math.max(2, Math.round(H / strength)));
    var ag = a.getContext('2d'), bg = b.getContext('2d'); ag.imageSmoothingEnabled = bg.imageSmoothingEnabled = true; ag.imageSmoothingQuality = bg.imageSmoothingQuality = 'high';
    ag.drawImage(src, 0, 0, a.width, a.height); bg.drawImage(a, 0, 0, b.width, b.height);
    g.imageSmoothingEnabled = true; g.imageSmoothingQuality = 'high'; g.drawImage(b, 0, 0, W, H);
  };
  E.paintBackground = function (g, W, H) {
    var bg = this.st.bg, F = this.frameCv;
    if (bg === 'blur') return this.blur(g, F, W, H, 10);
    if (bg === 'blur2') return this.blur(g, F, W, H, 22);
    if (bg === 'spot') { this.blur(g, F, W, H, 6); g.fillStyle = 'rgba(2,6,23,.62)'; g.fillRect(0, 0, W, H); return; }
    if (bg.indexOf('color:') === 0) { g.fillStyle = bg.slice(6); g.fillRect(0, 0, W, H); return; }
    var key = bg + W + 'x' + H, sc = this.scenes[key];
    if (!sc) {
      if (bg === 'image' && this.img) { sc = canvas(W, H); var im = this.img, r = Math.max(W / im.width, H / im.height); sc.getContext('2d').drawImage(im, (W - im.width * r) / 2, (H - im.height * r) / 2, im.width * r, im.height * r); }
      else if (bg.indexOf('scene:') === 0) sc = paintScene(bg.slice(6), W, H, this.st.color);
      if (sc) { this.scenes = {}; this.scenes[key] = sc; }
    }
    if (sc) g.drawImage(sc, 0, 0); else this.blur(g, F, W, H, 18);
  };
  E.paintTag = function (g, W, H) {
    var s = this.st, u = H / 360, pad = 10 * u, x = 14 * u, name = String(s.tagName || '').slice(0, 40), title = String(s.tagTitle || '').slice(0, 50);
    g.save(); g.font = '700 ' + Math.round(17 * u) + 'px system-ui,-apple-system,"Segoe UI",sans-serif'; var w1 = g.measureText(name).width;
    g.font = '500 ' + Math.round(12 * u) + 'px system-ui,-apple-system,"Segoe UI",sans-serif'; var w2 = g.measureText(title).width;
    var w = Math.max(w1, w2) + pad * 2 + 6 * u, h = (title ? 46 : 30) * u, y = H - h - 14 * u;
    g.fillStyle = 'rgba(11,18,32,.78)'; g.beginPath(); if (g.roundRect) g.roundRect(x, y, w, h, 8 * u); else g.rect(x, y, w, h); g.fill();
    g.fillStyle = s.color || '#1769e0'; g.fillRect(x, y + 6 * u, 4 * u, h - 12 * u);
    g.fillStyle = '#fff'; g.textBaseline = 'top'; g.font = '700 ' + Math.round(17 * u) + 'px system-ui,-apple-system,"Segoe UI",sans-serif'; g.fillText(name, x + pad + 4 * u, y + 7 * u);
    if (title) { g.fillStyle = 'rgba(255,255,255,.75)'; g.font = '500 ' + Math.round(12 * u) + 'px system-ui,-apple-system,"Segoe UI",sans-serif'; g.fillText(title, x + pad + 4 * u, y + 27 * u); }
    g.restore();
  };

  // ───────── drawing on your own video ─────────
  // Points are fractions of the frame; a stroke pinned to the face is kept in face units and follows the head.
  E.inkStart = function (x, y, W, H) {
    var k = this.ink, lm = this.face.lm, F = k.stick && lm ? faceFrame(lm, W, H) : null;
    if (k.stick) this.loadFace();
    var s = { tool: k.tool, color: k.color, w: k.width, anchor: F ? 'face' : 'frame', t: performance.now(), pts: [], text: k.tool === 'sticker' ? k.sticker : '' };
    s.scale = F ? (H / 360) / F.u : H / 360;        // px per "360p pixel": a face stroke grows and shrinks with the face
    this.ink.strokes.push(s); this.drawing = s; this.inkAdd(x, y, W, H);
    if (this.ink.strokes.length > 60) this.ink.strokes.shift();
    this.emit(this.ink.strokes.length === 1 ? 'active' : 'change');
    return s;
  };
  E.inkAdd = function (x, y, W, H) {
    var s = this.drawing; if (!s) return;
    var p = [x * W, y * H];
    if (s.anchor === 'face') { var lm = this.face.lm; if (!lm) return; p = faceFrame(lm, W, H).local(p); } else p = [x, y];
    var last = s.pts[s.pts.length - 1]; if (last && Math.abs(last[0] - p[0]) + Math.abs(last[1] - p[1]) < (s.anchor === 'face' ? 0.004 : 0.001)) return;
    s.pts.push(p); if (s.pts.length > 800) s.pts.shift();
  };
  E.inkEnd = function () { if (this.drawing) { this.drawing.t = performance.now(); this.drawing = null; } };
  E.inkText = function (text, x, y, W, H) { var t = this.ink.tool; this.ink.tool = 'text'; var s = this.inkStart(x, y, W, H); s.text = String(text || '').slice(0, 60); this.inkEnd(); this.ink.tool = t; };
  E.undo = function () { var was = this.active(); this.ink.strokes.pop(); this.emit(was !== this.active() ? 'active' : 'change'); };
  E.clearInk = function () { var was = this.active(); this.ink.strokes = []; this.drawing = null; this.emit(was !== this.active() ? 'active' : 'change'); };
  E.paintInk = function (g, W, H, F, now) {
    var k = this.ink, self = this;
    if (k.fade) k.strokes = k.strokes.filter(function (s) { return s === self.drawing || now - s.t < 6000; });
    k.strokes.forEach(function (s) {
      if (!s.pts.length || (s.anchor === 'face' && !F)) return;
      var pts = s.pts.map(function (p) { return s.anchor === 'face' ? F.world(p) : [p[0] * W, p[1] * H]; });
      var unit = s.anchor === 'face' ? F.u * s.scale : H / 360, px = s.w * unit, a = 1;
      if (k.fade && s !== self.drawing) a = clamp(1 - (now - s.t - 4000) / 2000, 0, 1);
      g.save(); g.globalAlpha = a; g.lineCap = g.lineJoin = 'round'; g.strokeStyle = g.fillStyle = s.color;
      if (s.tool === 'sticker' || s.tool === 'text') {
        var fs = (s.tool === 'sticker' ? 34 : 22) * unit, q = pts[0];
        if (s.anchor === 'face') { g.translate(q[0], q[1]); g.rotate(F.ang); q = [0, 0]; }
        g.font = (s.tool === 'text' ? '800 ' : '') + Math.round(fs) + 'px system-ui,"Apple Color Emoji","Segoe UI Emoji",sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle';
        if (s.tool === 'text') { g.lineWidth = fs / 6; g.strokeStyle = 'rgba(0,0,0,.65)'; g.strokeText(s.text, q[0], q[1]); g.fillStyle = s.color; }
        g.fillText(s.text, q[0], q[1]);
      } else {
        if (s.tool === 'hl') { g.globalAlpha = a * 0.38; px *= 4; }
        g.lineWidth = Math.max(1, px);
        if (s.tool === 'arrow' && pts.length > 1) {
          var p0 = pts[0], p1 = pts[pts.length - 1], an = Math.atan2(p1[1] - p0[1], p1[0] - p0[0]), hl = Math.max(10, px * 4);
          g.beginPath(); g.moveTo(p0[0], p0[1]); g.lineTo(p1[0], p1[1]); g.stroke();
          g.beginPath(); g.moveTo(p1[0], p1[1]); g.lineTo(p1[0] - hl * Math.cos(an - 0.45), p1[1] - hl * Math.sin(an - 0.45)); g.lineTo(p1[0] - hl * Math.cos(an + 0.45), p1[1] - hl * Math.sin(an + 0.45)); g.closePath(); g.fill();
        } else {
          g.beginPath(); g.moveTo(pts[0][0], pts[0][1]);
          for (var i = 1; i < pts.length; i++) { var m = [(pts[i - 1][0] + pts[i][0]) / 2, (pts[i - 1][1] + pts[i][1]) / 2]; g.quadraticCurveTo(pts[i - 1][0], pts[i - 1][1], m[0], m[1]); }
          if (pts.length === 1) { g.arc(pts[0][0], pts[0][1], px / 2, 0, 7); g.fill(); } else { g.lineTo(pts[pts.length - 1][0], pts[pts.length - 1][1]); g.stroke(); }
        }
      }
      g.restore();
    });
  };

  // ───────── the pipeline: camera track in, finished track out ─────────
  E.attach = function (track) {
    var self = this; this.detach(true); this.src = track;
    var st = track.getSettings ? track.getSettings() : {}, cap = function (w, h) { var sc = Math.min(1, 1280 / Math.max(w, h)); return [Math.round(w * sc) || 640, Math.round(h * sc) || 360]; };
    if (window.MediaStreamTrackProcessor && window.MediaStreamTrackGenerator && window.VideoFrame) {
      try {
        var proc = new MediaStreamTrackProcessor({ track: track }), gen = new MediaStreamTrackGenerator({ kind: 'video' }), alive = { on: true }, last = 0, gap = 1000 / 30;
        proc.readable.pipeThrough(new TransformStream({
          transform: function (frame, ctl) {
            if (!alive.on) { frame.close(); return; }
            if (!self.active()) return ctl.enqueue(frame);                 // nothing on: the frame passes untouched
            var t0 = performance.now();
            // Behind (a slow device)? Drop this frame — never send it raw while effects are on — and catch up.
            if (t0 - last < gap) { frame.close(); self.stats.dropped = (self.stats.dropped || 0) + 1; return; }
            last = t0;
            var d = cap(frame.displayWidth, frame.displayHeight), ts = frame.timestamp;
            size(self.frameCv, d[0], d[1]).getContext('2d').drawImage(frame, 0, 0, d[0], d[1]); frame.close();
            var out; try { out = self.compose(d[0], d[1]); } catch (e) { out = self.frameCv; }
            ctl.enqueue(new VideoFrame(out, { timestamp: ts }));
            gap = Math.min(250, Math.max(1000 / 30, (performance.now() - t0) * 1.25));
            return yieldTask();
          }
        })).pipeTo(gen.writable).catch(function () {});
        try { gen.contentHint = 'motion'; } catch (e) {}
        this.track = gen; this.stopper = function () { alive.on = false; try { gen.stop(); } catch (e) {} };
        return gen;
      } catch (e) {}
    }
    var v = document.createElement('video'); v.muted = true; v.playsInline = true; v.autoplay = true; v.srcObject = new MediaStream([track]);
    var p = v.play(); if (p && p.catch) p.catch(function () {});
    var fps = Math.min(24, st.frameRate || 24), out = this.outCv, outTrack = out.captureStream ? out.captureStream(fps).getVideoTracks()[0] : null, on = true;
    if (!outTrack) { this.track = track; return track; }
    (function loop() {
      if (!on) return;
      var w = v.videoWidth, h = v.videoHeight;
      if (w && h) { var d = cap(w, h); size(self.frameCv, d[0], d[1]).getContext('2d').drawImage(v, 0, 0, d[0], d[1]);
        if (self.active()) { try { self.compose(d[0], d[1]); } catch (e) {} } else { size(out, d[0], d[1]).getContext('2d').drawImage(self.frameCv, 0, 0); } }
      setTimeout(loop, 1000 / fps);
    })();
    this.track = outTrack; this.stopper = function () { on = false; try { outTrack.stop(); } catch (e) {} v.srcObject = null; };
    return outTrack;
  };
  E.detach = function (quiet) { if (this.stopper) this.stopper(); this.stopper = null; this.track = null; this.src = null; this.face.lm = null; };
  E.off = function () { this.clearInk(); this.set({ bg: 'none', face: 'none', glow: false, tag: false }); };

  // ─────────────────────────────── Studio panel ───────────────────────────────
  function studio(fx, opt) {
    opt = opt || {};
    var host = document.createElement('div'); host.className = 'fx-sheet'; host.setAttribute('role', 'dialog'); host.setAttribute('aria-label', 'Studio effects');
    function tiles(list, cur, attr) { return list.map(function (t) { return '<button class="fx-tile' + (t.id === cur ? ' on' : '') + '" data-' + attr + '="' + t.id + '" title="' + esc(t.name) + '"><b>' + t.icon + '</b><small>' + esc(t.name) + '</small></button>'; }).join(''); }
    host.innerHTML =
      '<header class="fx-head"><b>✨ Studio</b><small>Effects run on this device. Only the finished video is sent.</small><button class="fx-x" data-fx="close" title="Close">✕</button></header>' +
      '<div class="fx-stage"><video class="fx-prev" muted playsinline autoplay></video><div class="fx-hint"></div><div class="fx-status"></div><div class="fx-nocam cw-h"><p>Turn on your camera to try effects.</p><button class="fx-pill" data-fx="cam">Turn on camera</button></div></div>' +
      '<nav class="fx-tabs"><button data-tab="bg" class="on">Background</button><button data-tab="face">Face</button><button data-tab="draw">Draw</button><button data-tab="tag">Name tag</button></nav>' +
      '<section class="fx-pane" data-pane="bg"><div class="fx-grid fx-bgs"></div><div class="fx-row fx-colors"></div><label class="fx-upload">🖼️ Use my photo<input type="file" accept="image/*" hidden></label></section>' +
      '<section class="fx-pane cw-h" data-pane="face"><div class="fx-grid fx-faces"></div><div class="fx-row"><label class="fx-chk"><input type="checkbox" data-st="glow"> Touch-up</label><label class="fx-chk"><input type="checkbox" data-st="light"> Low-light boost</label></div></section>' +
      '<section class="fx-pane cw-h" data-pane="draw"><div class="fx-row fx-tools">' +
        '<button data-tool="pen" title="Pen">✏️</button><button data-tool="hl" title="Highlighter">🖍️</button><button data-tool="arrow" title="Arrow">➡️</button><button data-tool="sticker" title="Sticker">⭐</button><button data-tool="text" title="Text">🔤</button>' +
        '<span class="fx-sep"></span><button data-fx="undo" title="Undo">↶</button><button data-fx="clear" title="Clear all">🧽</button></div>' +
        '<div class="fx-row fx-ink"></div><div class="fx-row fx-stickers cw-h"></div>' +
        '<div class="fx-row"><label class="fx-chk"><input type="checkbox" data-ink="stick"> Stick to my face</label><label class="fx-chk"><input type="checkbox" data-ink="fade"> Disappearing ink</label></div>' +
        '<p class="fx-tip">Draw on the preview above. With “Stick to my face” on, a drawing follows your head — try a mustache or a pair of glasses.</p></section>' +
      '<section class="fx-pane cw-h" data-pane="tag"><label class="fx-chk"><input type="checkbox" data-st="tag"> Show my name tag</label>' +
        '<input class="fx-in" data-tag="tagName" maxlength="40" placeholder="Your name"><input class="fx-in" data-tag="tagTitle" maxlength="50" placeholder="Role — e.g. TapTap Support">' +
        '<div class="fx-row fx-tagcolors"></div></section>' +
      '<footer class="fx-foot"><button class="fx-pill ghost" data-fx="off">Turn everything off</button><span class="fx-perf"></span><button class="fx-pill" data-fx="close">Done</button></footer>';
    var $ = function (s) { return host.querySelector(s); }, prev = $('.fx-prev'), tab = 'bg';
    function paint() {
      var s = fx.st, k = fx.ink;
      $('.fx-bgs').innerHTML = tiles(BACKGROUNDS, s.bg, 'bg');
      $('.fx-colors').innerHTML = COLORS.map(function (c) { return '<button class="fx-sw' + (s.bg === 'color:' + c ? ' on' : '') + '" data-bg="color:' + c + '" style="background:' + c + '" title="Plain colour"></button>'; }).join('') +
        '<label class="fx-sw fx-pick" title="Any colour">🎨<input type="color" data-color="bg" hidden></label>';
      $('.fx-faces').innerHTML = tiles(FACES, s.face, 'face');
      $('.fx-ink').innerHTML = INK_COLORS.map(function (c) { return '<button class="fx-sw' + (k.color === c ? ' on' : '') + '" data-ink-color="' + c + '" style="background:' + c + '"></button>'; }).join('');
      $('.fx-stickers').innerHTML = STICKERS.map(function (e) { return '<button class="fx-stk' + (k.sticker === e ? ' on' : '') + '" data-sticker="' + e + '">' + e + '</button>'; }).join('');
      $('.fx-stickers').classList.toggle('cw-h', k.tool !== 'sticker');
      $('.fx-tagcolors').innerHTML = ['#1769e0', '#18a66a', '#7c3aed', '#e5484d', '#f59e0b', '#0ea5a4'].map(function (c) { return '<button class="fx-sw' + (s.color === c ? ' on' : '') + '" data-tagcolor="' + c + '" style="background:' + c + '"></button>'; }).join('');
      [].forEach.call(host.querySelectorAll('[data-tool]'), function (b) { b.classList.toggle('on', b.dataset.tool === k.tool); });
      [].forEach.call(host.querySelectorAll('[data-st]'), function (i) { i.checked = !!s[i.dataset.st]; });
      [].forEach.call(host.querySelectorAll('[data-ink]'), function (i) { i.checked = !!k[i.dataset.ink]; });
      [].forEach.call(host.querySelectorAll('[data-tag]'), function (i) { if (document.activeElement !== i) i.value = s[i.dataset.tag] || ''; });
      $('.fx-hint').textContent = tab === 'draw' ? (k.tool === 'sticker' ? 'Tap to place a sticker' : k.tool === 'text' ? 'Tap where the text goes' : 'Draw here') : '';
      host.classList.toggle('drawing', tab === 'draw');
      status();
    }
    function status() { $('.fx-status').textContent = fx.status(); $('.fx-perf').textContent = fx.active() && fx.stats.fps ? fx.stats.fps + ' fps' : ''; }
    function refreshPreview() {
      var t = opt.preview && opt.preview();
      $('.fx-nocam').classList.toggle('cw-h', !!t);
      if (t) { if (!prev.srcObject || prev.srcObject.getVideoTracks()[0] !== t) prev.srcObject = new MediaStream([t]); } else prev.srcObject = null;
    }
    var timer = setInterval(function () { status(); }, 1000);
    host.addEventListener('click', function (e) {
      var b = e.target.closest('button,[data-fx]'); if (!b) return;
      var d = b.dataset;
      if (d.tab) { tab = d.tab; [].forEach.call(host.querySelectorAll('.fx-tabs button'), function (x) { x.classList.toggle('on', x === b); }); [].forEach.call(host.querySelectorAll('.fx-pane'), function (p) { p.classList.toggle('cw-h', p.dataset.pane !== tab); }); paint(); return; }
      if (d.bg) { if (d.bg === 'image' && !fx.img) { $('.fx-upload input').click(); return; } fx.set({ bg: d.bg }); }
      else if (d.face) fx.set({ face: d.face });
      else if (d.tool) fx.ink.tool = d.tool;
      else if (d.inkColor) fx.ink.color = d.inkColor;
      else if (d.sticker) fx.ink.sticker = d.sticker;
      else if (d.tagcolor) { fx.set({ color: d.tagcolor }); fx.scenes = {}; }
      else if (d.fx === 'undo') fx.undo();
      else if (d.fx === 'clear') fx.clearInk();
      else if (d.fx === 'off') fx.off();
      else if (d.fx === 'cam') { if (opt.onCamera) opt.onCamera(); }
      else if (d.fx === 'close') { close(); return; }
      paint();
    });
    host.addEventListener('change', function (e) {
      var i = e.target;
      if (i.dataset.st) { var p = {}; p[i.dataset.st] = i.checked; fx.set(p); }
      else if (i.dataset.ink) { fx.ink[i.dataset.ink] = i.checked; if (i.dataset.ink === 'stick' && i.checked) fx.loadFace(); }
      else if (i.dataset.color === 'bg') fx.set({ bg: 'color:' + i.value });
      else if (i.type === 'file' && i.files[0]) fx.setImage(i.files[0]).catch(function (err) { $('.fx-status').textContent = err.message; });
      paint();
    });
    host.addEventListener('input', function (e) { var i = e.target; if (i.dataset.tag) { var p = {}; p[i.dataset.tag] = i.value; if (!fx.st.tag && i.value) p.tag = true; fx.set(p); } });
    // draw on the preview
    function pt(ev) {
      var r = prev.getBoundingClientRect(), vw = prev.videoWidth, vh = prev.videoHeight; if (!vw || !vh) return null;
      var sc = Math.min(r.width / vw, r.height / vh), w = vw * sc, h = vh * sc, x = (ev.clientX - r.left - (r.width - w) / 2) / w, y = (ev.clientY - r.top - (r.height - h) / 2) / h;
      return x < 0 || y < 0 || x > 1 || y > 1 ? null : { x: x, y: y, W: fx.outCv.width || vw, H: fx.outCv.height || vh, cx: ev.clientX - r.left, cy: ev.clientY - r.top };
    }
    var stage = $('.fx-stage'), down = false;
    stage.addEventListener('pointerdown', function (ev) {
      if (tab !== 'draw' || ev.button !== 0) return; var p = pt(ev); if (!p) return; ev.preventDefault();
      if (fx.ink.tool === 'text') return askText(p);
      fx.inkStart(p.x, p.y, p.W, p.H); if (fx.ink.tool === 'sticker') { fx.inkEnd(); return; }
      down = true; try { stage.setPointerCapture(ev.pointerId); } catch (e) {}
    });
    stage.addEventListener('pointermove', function (ev) { if (!down) return; var p = pt(ev); if (p) fx.inkAdd(p.x, p.y, p.W, p.H); });
    function up() { if (down) { down = false; fx.inkEnd(); } }
    stage.addEventListener('pointerup', up); stage.addEventListener('pointercancel', up);
    function askText(p) {
      var old = stage.querySelector('.fx-text'); if (old) old.remove();
      var i = document.createElement('input'); i.className = 'fx-text'; i.maxLength = 60; i.placeholder = 'Type, then Enter'; i.style.left = p.cx + 'px'; i.style.top = p.cy + 'px';
      stage.appendChild(i); i.focus();
      i.addEventListener('keydown', function (e) { if (e.key === 'Enter') { if (i.value.trim()) fx.inkText(i.value.trim(), p.x, p.y, p.W, p.H); i.remove(); } else if (e.key === 'Escape') i.remove(); e.stopPropagation(); });
      i.addEventListener('blur', function () { setTimeout(function () { i.remove(); }, 150); });
    }
    function listen(what) { if (host.isConnected) { if (what === 'active' || what === 'track') refreshPreview(); status(); } }
    function close() { clearInterval(timer); host.remove(); fx.listeners = fx.listeners.filter(function (f) { return f !== listen; }); if (opt.onClose) opt.onClose(); }
    fx.on(listen);
    (opt.mount || document.body).appendChild(host);
    paint(); refreshPreview(); fx.warm();
    return { el: host, close: close, refresh: function () { refreshPreview(); paint(); } };
  }

  window.TTCallFX = {
    engine: function (opt) { return new Engine(opt); }, studio: studio,
    supported: function () { return !!(document.createElement('canvas').captureStream || window.MediaStreamTrackGenerator); },
    _test: { faceFrame: faceFrame, paintScene: paintScene, simd: simd, FACES: FACES, BACKGROUNDS: BACKGROUNDS }
  };
})();
