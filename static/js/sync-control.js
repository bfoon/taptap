/* Sync control (core/sync_control.py): the 24-hour dial, the quiet-hours strip, the form's live hints and a light
   status poll (CPU, the running sync, what TapTap decided). No framework; everything degrades to the plain form. */
(function () {
  'use strict';
  var form = document.getElementById('scForm');
  if (!form) return;
  var data = {}, load = [];
  try { data = JSON.parse(document.getElementById('scData').textContent); } catch (e) { }
  try { load = JSON.parse(document.getElementById('scLoad').textContent) || []; } catch (e) { }
  var NS = 'http://www.w3.org/2000/svg';

  function hm(v) { var p = String(v || '0:0').split(':'); return (+p[0] || 0) + (+p[1] || 0) / 60; }
  function inQuiet(h, a, b) { if (a === b) return true; return a < b ? (h >= a && h < b) : (h >= a || h < b); }
  function el(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function pt(hour, r) { var a = (hour / 24) * Math.PI * 2 - Math.PI / 2; return [Math.cos(a) * r, Math.sin(a) * r]; }
  function arc(h1, h2, r) {
    var span = ((h2 - h1) + 24) % 24 || 24, p1 = pt(h1, r), p2 = pt(h1 + span - 0.001, r);
    return 'M' + p1[0].toFixed(2) + ' ' + p1[1].toFixed(2) + ' A' + r + ' ' + r + ' 0 ' + (span > 12 ? 1 : 0) + ' 1 ' + p2[0].toFixed(2) + ' ' + p2[1].toFixed(2);
  }

  /* ───────── the dial ───────── */
  var svg = document.getElementById('scDialSvg');
  function drawDial() {
    if (!svg) return;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var qa = hm(form.quiet_from.value), qb = hm(form.quiet_to.value), max = Math.max.apply(null, load.concat([1]));
    el('circle', { r: 128, fill: 'none', stroke: '#ffffff12', 'stroke-width': 26 }, svg);
    el('path', { d: arc(qa, qb, 128), fill: 'none', stroke: '#18a66a', 'stroke-width': 26, 'stroke-opacity': '.55', 'stroke-linecap': 'butt' }, svg);
    for (var h = 0; h < 24; h++) {                       // traffic: one bar per hour, inside the quiet ring
      var v = (load[h] || 0) / max, r1 = 70, r2 = 70 + Math.max(2, v * 38), a = pt(h + 0.5, r1), b = pt(h + 0.5, r2);
      el('line', { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: '#5aa2ff', 'stroke-width': 7, 'stroke-linecap': 'round', 'stroke-opacity': (0.35 + v * 0.65).toFixed(2) }, svg);
      if (h % 3 === 0) {
        var t = pt(h, 146), tx = el('text', { x: t[0], y: t[1] + 3, 'text-anchor': 'middle', 'font-size': 9, fill: '#9fb3c8' }, svg);
        tx.textContent = h;
      }
    }
    (data.syncs || []).forEach(function (s) {           // recent syncs as dots on the ring
      var p = pt(s.h, 128);
      el('circle', { cx: p[0], cy: p[1], r: s.b === 'you' ? 5 : 4, fill: s.s === 'failed' ? '#d64545' : (s.s === 'success' ? '#fff' : '#ffd38a'),
        stroke: '#0b1726', 'stroke-width': 1.5 }, svg);
    });
    var now = data.now || 0, n1 = pt(now, 58), n2 = pt(now, 142);     // the needle
    el('line', { x1: n1[0], y1: n1[1], x2: n2[0], y2: n2[1], stroke: '#ffd38a', 'stroke-width': 2.5, 'stroke-linecap': 'round' }, svg);
    el('circle', { cx: n2[0], cy: n2[1], r: 4.5, fill: '#ffd38a' }, svg);
  }

  /* ───────── the quiet-hours strip ───────── */
  var strip = document.getElementById('scStrip');
  function drawStrip(suggest) {
    if (!strip) return;
    var qa = hm(form.quiet_from.value), qb = hm(form.quiet_to.value), max = Math.max.apply(null, load.concat([1]));
    Array.prototype.forEach.call(strip.children, function (d, h) {
      d.style.height = Math.max(6, ((load[h] || 0) / max) * 100) + '%';
      d.className = (inQuiet(h + 0.5, qa, qb) ? 'q' : '') + (suggest && inQuiet(h + 0.5, hm(suggest[0]), hm(suggest[1])) ? ' s' : '');
      d.title = h + ':00 — ' + (load[h] ? Math.round(load[h] / 1048576) + ' MB' : 'no traffic seen');
    });
  }

  /* ───────── the form ───────── */
  var bar = document.getElementById('scSaveBar'), barTxt = document.getElementById('scSaveTxt'), start = new FormData(form);
  function snapshot(fd) { var o = []; fd.forEach(function (v, k) { if (k !== 'csrfmiddlewaretoken') o.push(k + '=' + v); }); return o.sort().join('&'); }
  var initial = snapshot(start);
  function refresh() {
    var mode = (form.querySelector('input[name=full_mode]:checked') || {}).value;
    document.getElementById('scEvery').classList.toggle('sc-off', mode === 'manual');
    var guard = document.getElementById('scGuard');
    document.getElementById('scCpuRow').classList.toggle('sc-off', !guard.checked);
    document.getElementById('scCpuVal').textContent = form.busy_cpu.value;
    var dirty = snapshot(new FormData(form)) !== initial;
    bar.classList.toggle('sc-dirty', dirty);
    if (dirty) barTxt.textContent = 'Unsaved changes';
    drawDial(); drawStrip();
  }
  form.addEventListener('input', refresh);
  form.addEventListener('change', refresh);
  var sug = document.getElementById('scSuggest');
  if (sug) sug.addEventListener('click', function () {
    form.quiet_from.value = sug.getAttribute('data-from'); form.quiet_to.value = sug.getAttribute('data-to');
    refresh(); drawStrip([sug.getAttribute('data-from'), sug.getAttribute('data-to')]);
  });

  /* ───────── the clock and a light status poll ───────── */
  var clock = document.getElementById('scClock');
  function tick() {
    var d = new Date();
    if (clock) clock.textContent = ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  }
  tick(); setInterval(tick, 15000);
  function poll() {
    if (document.hidden || !data.status) return;
    fetch(data.status, { credentials: 'same-origin' }).then(function (r) { return r.ok ? r.json() : null; }).then(function (s) {
      if (!s) return;
      var cpu = document.getElementById('scCpu'), now = document.getElementById('scCpuNow'), txt = document.getElementById('scCpuNowTxt');
      if (cpu) { cpu.textContent = s.cpu == null ? 'CPU —' : 'CPU ' + s.cpu + ' %'; cpu.classList.toggle('hot', !!s.busy); }
      if (now && s.cpu != null) { now.style.left = s.cpu + '%'; txt.style.left = s.cpu + '%'; txt.textContent = 'now ' + s.cpu + ' %'; }
      var why = document.getElementById('scWhy');
      if (why && s.decision && s.decision.why) why.textContent = s.decision.why;
      var run = document.getElementById('scRunBar'), phase = document.getElementById('scPhase');
      if (s.job && run) { run.style.width = s.job.progress + '%'; if (phase) phase.textContent = s.job.phase; }
      if (!s.job && run) { location.reload(); }
    }).catch(function () { });
  }
  setInterval(poll, 10000);
  refresh();
})();
