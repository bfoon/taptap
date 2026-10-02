/* TapTap Traffic — keeps the whole report live without reloading.
 *  - report data: every 30 s (and soon after live sync reports a change), paused while the tab is hidden
 *  - "right now": every 5 s, with a rolling 5-minute speed line
 *  - numbers count to their new value, bars slide, charts animate, table rows keep their place
 */
(function () {
  'use strict';
  var me = document.currentScript, DATA_URL = me.dataset.dataUrl, NOW_URL = me.dataset.nowUrl;
  var REPORT_EVERY = 30000, NOW_EVERY = 5000;
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var data = JSON.parse(document.getElementById('trData').textContent);
  var paused = false, lastFetch = Date.now(), lastOk = Date.now(), failures = 0, timer = null, nowTimer = null, charts = {};
  var PAL = ['#1769e0', '#14a3e2', '#7c3aed', '#f59e0b', '#18a66a', '#e05d9b', '#5b6b7c', '#d64545', '#0ea5a5'];
  var BAND = { peak: '#d64545', shoulder: '#f5b544', off: '#18a66a', none: '#c8d1db' };

  // ─────────── formatting ───────────
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function bytes(n) { n = +n || 0; var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i < 2 ? Math.round(n) : n < 100 ? n.toFixed(1) : Math.round(n)) + ' ' + u[i]; }
  function bps(v) { v = +v || 0; return v >= 1e9 ? (v / 1e9).toFixed(2) + ' Gb/s' : v >= 1e6 ? (v / 1e6).toFixed(1) + ' Mb/s' : v >= 1e3 ? Math.round(v / 1e3) + ' kb/s' : Math.round(v) + ' b/s'; }
  function gb(v) { v = +v || 0; return v >= 1 ? v.toFixed(v >= 100 ? 0 : 1) + ' GB' : Math.round(v * 1024) + ' MB'; }
  function int(v) { return Math.round(+v || 0).toLocaleString(); }
  function hour(h) { return h == null ? '—' : String(h).padStart(2, '0') + ':00'; }
  function $(id) { return document.getElementById(id); }

  // ─────────── animation helpers ───────────
  function flash(el) { if (!el || reduce) return; el.classList.remove('chg'); void el.offsetWidth; el.classList.add('chg'); }
  function tween(el, to, fmt) {
    if (!el) return;
    var from = +el.dataset.v; to = +to || 0;
    if (isNaN(from) || reduce || from === to) { el.textContent = fmt(to); el.dataset.v = to; return; }
    var t0 = performance.now(), d = 900;
    el.dataset.v = to;
    if (Math.abs(to - from) / Math.max(1, Math.abs(from)) > 0.004) flash(el);
    (function step(t) {
      var k = Math.min(1, (t - t0) / d), e = 1 - Math.pow(1 - k, 3);
      el.textContent = fmt(from + (to - from) * e);
      if (k < 1 && +el.dataset.v === to) requestAnimationFrame(step);
    })(t0);
  }
  function setText(el, text) { if (!el) return; if (el.textContent !== text) { if (el.dataset.set) flash(el); el.textContent = text; } el.dataset.set = 1; }
  function bar(el, pct) { // slides from its current width (0 on first paint)
    if (!el) return;
    var w = Math.max(0, Math.min(100, +pct || 0)) + '%';
    if (!el.dataset.init) { el.style.width = '0%'; el.dataset.init = 1; requestAnimationFrame(function () { requestAnimationFrame(function () { el.style.width = w; }); }); }
    else el.style.width = w;
  }

  /* Keyed table: rows keep their DOM element (so bars animate), move into the new order,
     new rows fade in, vanished rows fade out. build(row) → cells HTML once; fill(tr,row) updates values. */
  function keyed(tbody, rows, key, build, fill, emptyText, cols) {
    if (!tbody) return;
    var existing = {};
    [].forEach.call(tbody.querySelectorAll('tr[data-key]'), function (tr) { existing[tr.dataset.key] = tr; });
    var emptyRow = tbody.querySelector('tr.tr-emptyrow');
    if (!rows.length) {
      [].forEach.call(Object.keys(existing), function (k) { existing[k].remove(); });
      if (!emptyRow) tbody.innerHTML = '<tr class="tr-emptyrow"><td colspan="' + cols + '" class="empty-table">' + emptyText + '</td></tr>';
      return;
    }
    if (emptyRow) emptyRow.remove();
    var seen = {};
    rows.forEach(function (r) {
      var k = String(key(r)); seen[k] = 1;
      var tr = existing[k];
      if (!tr) { tr = document.createElement('tr'); tr.dataset.key = k; tr.innerHTML = build(r); if (Object.keys(existing).length || tbody.dataset.painted) tr.className = 'tr-enter'; }
      fill(tr, r);
      tbody.appendChild(tr); // moves existing rows into the new order without re-creating them
    });
    Object.keys(existing).forEach(function (k) { if (!seen[k]) { var tr = existing[k]; tr.classList.add('tr-leave'); setTimeout(function () { tr.remove(); }, 380); } });
    tbody.dataset.painted = 1;
  }
  function q(tr, f) { return tr.querySelector('[data-f="' + f + '"]'); }

  // ─────────── sections ───────────
  function kpis(d) {
    var k = d.kpis, box = document.querySelector('.tr-kpis');
    tween(box.querySelector('[data-k=total]'), k.total, bytes);
    tween(box.querySelector('[data-k=down]'), k.down, bytes);
    tween(box.querySelector('[data-k=up]'), k.up, bytes);
    tween(box.querySelector('[data-k=per_day]'), k.per_day, bytes);
    tween(box.querySelector('[data-k=peak_bps]'), k.peak_bps, bps);
    tween(box.querySelector('[data-k=users]'), k.users, int);
    tween(box.querySelector('[data-k=per_user]'), k.per_user, bytes);
    setText(box.querySelector('[data-k=peak_at]'), k.peak_at || '—');
    setText(box.querySelector('[data-k=busiest]'), hour(k.busiest_h));
    setText(box.querySelector('[data-k=quietest]'), hour(k.quietest_h));
    box.querySelector('[data-k=change]').innerHTML = k.change == null ? '' : '· <b class="' + (k.change >= 0 ? 'text-success' : 'text-danger') + '">' + (k.change >= 0 ? '▲' : '▼') + ' ' + Math.abs(Math.round(k.change)) + '%</b>';
    $('trEmpty').hidden = !!d.has_data;
    $('trEmptyWan').hidden = d.source === 'wan';
  }
  var INS_ICON = { peak: 'bi-sunset', off: 'bi-moon-stars', users: 'bi-people', app: 'bi-play-btn', video: 'bi-play-btn', updates: 'bi-cloud-arrow-down' };
  function insights(d) {
    var box = $('trIns'), have = {};
    [].forEach.call(box.children, function (el) { have[el.dataset.kind] = el; });
    var seen = {};
    d.insights.forEach(function (x) {
      var kind = x[0], text = x[1], el = have[kind]; seen[kind] = 1;
      if (!el) { el = document.createElement('div'); el.className = 'tr-in ' + kind; el.dataset.kind = kind; el.innerHTML = '<i class="bi ' + (INS_ICON[kind] || 'bi-speedometer2') + '"></i><span></span>'; }
      var sp = el.querySelector('span'); if (sp.textContent !== text) { if (sp.textContent) flash(el); sp.textContent = text; }
      box.appendChild(el);
    });
    Object.keys(have).forEach(function (k) { if (!seen[k]) have[k].remove(); });
  }
  function bands(d) {
    var h = '';
    d.peak_windows.forEach(function (w) { h += '<span class="p"><i class="bi bi-sunset"></i> Peak ' + esc(w) + '</span>'; });
    d.off_windows.forEach(function (w) { h += '<span class="o"><i class="bi bi-moon-stars"></i> Off-peak ' + esc(w) + '</span>'; });
    if (!d.peak_windows.length) h = '<span class="s">Peak times appear after a day of data</span>';
    if ($('trBands').innerHTML !== h) { $('trBands').innerHTML = h; }
  }
  function heat(d) {
    var box = $('trHeat'), H = d.heat;
    if (!box.firstChild) {
      var h = '<div class="tr-heat"><span></span>';
      for (var i = 0; i < 24; i++) h += '<span class="h">' + (i % 3 === 0 ? String(i).padStart(2, '0') : '') + '</span>';
      H.rows.forEach(function (r, di) { h += '<span class="d">' + H.days[di] + '</span>'; r.forEach(function (_, hr) { h += '<span class="c" data-d="' + di + '" data-h="' + hr + '"></span>'; }); });
      box.innerHTML = h + '</div>';
    }
    [].forEach.call(box.querySelectorAll('.c'), function (c) {
      var v = H.rows[c.dataset.d][c.dataset.h], a = v ? (0.12 + 0.88 * v / H.max) : 0;
      c.style.backgroundColor = 'rgba(23,105,224,' + a.toFixed(2) + ')';
      c.title = H.days[c.dataset.d] + ' ' + String(c.dataset.h).padStart(2, '0') + ':00 — ' + bytes(v);
    });
  }
  function tables(d) {
    keyed($('tbApps'), d.apps, function (r) { return r.app; },
      function () { return '<td><b data-f="app"></b></td><td><span class="tr-cat" data-f="cat"></span></td><td class="tr-num" data-f="total"></td><td><div class="tr-share"><i data-f="bar"></i></div><small class="text-secondary" data-f="share"></small></td>'; },
      function (tr, r) { q(tr, 'app').textContent = r.app; q(tr, 'cat').textContent = r.category; tween(q(tr, 'total'), r.total, bytes); bar(q(tr, 'bar'), r.share); setText(q(tr, 'share'), r.share + '%'); },
      'App information appears within a few minutes of live sync running.', 4);
    keyed($('tbSites'), d.domains, function (r) { return r.app + '|' + r.domain; },
      function () { return '<td data-f="domain"></td><td><small class="text-secondary" data-f="app"></small></td><td class="tr-num" data-f="total"></td>'; },
      function (tr, r) { q(tr, 'domain').textContent = r.domain; q(tr, 'app').textContent = r.app; tween(q(tr, 'total'), (r.dn || 0) + (r.up || 0), bytes); },
      'No sites recorded yet.', 3);
    keyed($('tbUsers'), d.users, function (r) { return r.username + '|' + r.mac_address; },
      function () { return '<td><b class="font-monospace" data-f="user"></b><br><small class="text-secondary" data-f="plan"></small></td><td><span data-f="device"></span><br><small class="text-secondary font-monospace" data-f="mac"></small></td>' +
        '<td class="tr-num" data-f="dn"></td><td class="tr-num" data-f="up"></td><td class="tr-num"><b data-f="total"></b></td><td><div class="tr-share"><i data-f="bar"></i></div><small class="text-secondary" data-f="share"></small></td>' +
        '<td class="tr-num" data-f="peak"></td><td class="tr-num" data-f="hours"></td>'; },
      function (tr, r) {
        q(tr, 'user').innerHTML = vlink(r.username); q(tr, 'plan').textContent = r.plan || ''; q(tr, 'device').innerHTML = r.device ? dlink(r.mac_address, r.device) : ''; q(tr, 'mac').innerHTML = dlink(r.mac_address);
        tween(q(tr, 'dn'), r.dn, bytes); tween(q(tr, 'up'), r.up, bytes); tween(q(tr, 'total'), r.total, bytes); tween(q(tr, 'peak'), r.peak, bps); tween(q(tr, 'hours'), r.hours, int);
        bar(q(tr, 'bar'), r.share); setText(q(tr, 'share'), r.share + '%');
      }, 'Per-user consumption appears as customers use the hotspot.', 8);
    $('usersNote').textContent = 'Per voucher and device in this period.' + (d.kpis.top5_share ? ' The top 5 used ' + d.kpis.top5_share + '% of all customer data.' : '');
    $('wanPanel').hidden = !d.wans.length;
    keyed($('tbWans'), d.wans, function (r) { return r.router + '|' + r.interface; },
      function () { return '<td data-f="router"></td><td><b data-f="iface"></b></td><td class="tr-num" data-f="dn"></td><td class="tr-num" data-f="up"></td><td class="tr-num" data-f="peak"></td><td><div class="tr-share"><i data-f="bar"></i></div><small class="text-secondary" data-f="share"></small></td>'; },
      function (tr, r) { q(tr, 'router').textContent = r.router; q(tr, 'iface').textContent = r.interface; tween(q(tr, 'dn'), r.down, bytes); tween(q(tr, 'up'), r.up, bytes); setText(q(tr, 'peak'), r.peak_h); bar(q(tr, 'bar'), r.share); setText(q(tr, 'share'), r.share + '%'); },
      '', 6);
  }

  // ─────────── charts (created once, then updated so Chart.js animates the change) ───────────
  function setupCharts() {
    if (!window.Chart) return false;
    Chart.defaults.font.family = 'Inter, system-ui, sans-serif'; Chart.defaults.color = '#6c7c90';
    Chart.defaults.plugins.legend.labels.usePointStyle = true; Chart.defaults.plugins.legend.labels.boxWidth = 8;
    // Adjust, never replace, the animation defaults (replacing drops Chart.js internals).
    if (reduce) Chart.defaults.animation = false; else { Chart.defaults.animation.duration = 800; Chart.defaults.animation.easing = 'easeOutCubic'; }
    charts.hours = new Chart($('trHours'), { type: 'bar', data: { labels: [], datasets: [{ label: 'Average data', data: [], backgroundColor: [], borderRadius: 5 }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return gb(c.parsed.y) + ' on average · ' + ({ peak: 'peak', shoulder: 'normal', off: 'off-peak', none: '' }[data.hours.bands[c.dataIndex]]); } } } },
        scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } }, y: { grid: { color: '#edf1f6' }, ticks: { callback: gb } } } } });
    charts.cats = new Chart($('trCats'), { type: 'doughnut', data: { labels: [], datasets: [{ data: [], backgroundColor: PAL, borderColor: '#fff', borderWidth: 2 }] },
      options: { maintainAspectRatio: false, cutout: '62%', plugins: { legend: { position: 'bottom' }, tooltip: { callbacks: { label: function (c) { var t = c.dataset.data.reduce(function (a, b) { return a + b; }, 0); return c.label + ': ' + bytes(c.parsed) + ' (' + Math.round(c.parsed * 100 / t) + '%)'; } } } } } });
    charts.speed = new Chart($('trSpeed'), { data: { labels: [], datasets: [
      { type: 'line', label: 'Average download (Mb/s)', data: [], borderColor: '#1769e0', backgroundColor: 'rgba(23,105,224,.12)', fill: 'origin', tension: .3, pointRadius: 0, borderWidth: 2 },
      { type: 'line', label: 'Highest download (Mb/s)', data: [], borderColor: '#d64545', borderDash: [5, 4], tension: .3, pointRadius: 0, borderWidth: 1.6 },
      { type: 'line', label: 'Average upload (Mb/s)', data: [], borderColor: '#7c3aed', tension: .3, pointRadius: 0, borderWidth: 1.6 }] },
      options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 14 } }, y: { beginAtZero: true, grid: { color: '#edf1f6' }, title: { display: true, text: 'Mb/s' } } } } });
    charts.apps = new Chart($('trAppHours'), { type: 'bar', data: { labels: [], datasets: [] },
      options: { maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, grid: { color: '#edf1f6' }, title: { display: true, text: 'MB' } } } } });
    charts.spark = new Chart($('nowSpark'), { type: 'line', data: { labels: [], datasets: [
      { label: 'Download', data: [], borderColor: '#44c2ff', backgroundColor: 'rgba(68,194,255,.15)', fill: 'origin', tension: .35, pointRadius: 0, borderWidth: 2 },
      { label: 'Upload', data: [], borderColor: '#b69cff', tension: .35, pointRadius: 0, borderWidth: 1.5 }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return c.dataset.label + ' ' + bps(c.parsed.y); } } } },
        scales: { x: { display: false }, y: { display: false, beginAtZero: true } } } });
    return true;
  }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
  function updateCharts(d) {
    if (!charts.hours) return;
    var hLabels = d.hours.avg_gb.map(function (_, h) { return String(h).padStart(2, '0') + ':00'; });
    var c = charts.hours; c.data.labels = hLabels; c.data.datasets[0].data = d.hours.avg_gb; c.data.datasets[0].backgroundColor = d.hours.bands.map(function (b) { return BAND[b]; }); c.update();
    var cats = d.categories.filter(function (x) { return x.total > 0; });
    $('trCatsEmpty').hidden = !!cats.length; $('trCats').style.visibility = cats.length ? '' : 'hidden';
    c = charts.cats; if (!same(c.data.labels, cats.map(function (x) { return x.name; })) || !same(c.data.datasets[0].data, cats.map(function (x) { return x.total; }))) {
      c.data.labels = cats.map(function (x) { return x.name; }); c.data.datasets[0].data = cats.map(function (x) { return x.total; }); c.update(); }
    c = charts.speed; c.data.labels = d.chart.labels; c.data.datasets[0].data = d.chart.mbps_dn; c.data.datasets[1].data = d.chart.peak_dn; c.data.datasets[2].data = d.chart.mbps_up; c.update();
    var series = d.app_hours.series;
    $('trAppHoursEmpty').hidden = !!series.length; $('trAppHours').style.visibility = series.length ? '' : 'hidden';
    c = charts.apps; c.data.labels = d.app_hours.labels;
    var byLabel = {}; c.data.datasets.forEach(function (ds) { byLabel[ds.label] = ds; });
    c.data.datasets = series.map(function (s, i) { var ds = byLabel[s.label] || { label: s.label, stack: 'a', borderRadius: 2 }; ds.data = s.data; ds.backgroundColor = PAL[i % PAL.length]; return ds; });
    c.update();
  }

  function render(d) { kpis(d); insights(d); bands(d); heat(d); tables(d); updateCharts(d); }

  // ─────────── polling & freshness ───────────
  function fresh(state, text) {
    var el = $('trFresh'); el.className = 'tr-fresh ' + (state || '');
    $('trFreshText').textContent = text;
  }
  function tickFresh() {
    if (paused) return fresh('paused', 'Paused');
    if (failures) return fresh('err', 'Offline — retrying');
    var s = Math.round((Date.now() - lastOk) / 1000);
    fresh(s > 90 ? 'stale' : '', s < 5 ? 'Updated just now' : 'Updated ' + (s < 60 ? s + 's' : Math.round(s / 60) + 'm') + ' ago');
  }
  function schedule(ms) { clearTimeout(timer); if (!paused) timer = setTimeout(load, ms); }
  function load() {
    if (paused) return;
    if (document.hidden) return schedule(REPORT_EVERY);
    lastFetch = Date.now(); $('trFresh').classList.add('busy');
    fetch(DATA_URL, { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function (d) {
      data = d; failures = 0; lastOk = Date.now(); render(d); tickFresh(); schedule(REPORT_EVERY);
    }).catch(function () { failures++; tickFresh(); schedule(Math.min(300000, REPORT_EVERY * Math.pow(2, failures))); })
      .then(function () { $('trFresh').classList.remove('busy'); });
  }

  // ─────────── right now (every 5 s) ───────────
  var sparkPts = [];
  function paintNow(n) {
    tween($('nowDn'), n.down_bps, bps); tween($('nowUp'), n.up_bps, bps); tween($('nowCount'), n.count, int);
    if (charts.spark) {
      sparkPts.push([Date.now(), n.down_bps || 0, n.up_bps || 0]); sparkPts = sparkPts.slice(-60);
      var s = charts.spark; s.data.labels = sparkPts.map(function (p) { return new Date(p[0]).toLocaleTimeString(); });
      s.data.datasets[0].data = sparkPts.map(function (p) { return p[1]; }); s.data.datasets[1].data = sparkPts.map(function (p) { return p[2]; }); s.update();
    }
    var list = $('nowList'), have = {};
    [].forEach.call(list.querySelectorAll('.tr-s'), function (el) { have[el.dataset.key] = el; });
    var seen = {};
    if (!n.sessions.length) { list.innerHTML = '<small style="color:#9fb3c8">Nobody is using much right now. This updates every few seconds while live sync runs.</small>'; return; }
    var hint = list.querySelector('small:not([data-f])'); if (hint && !list.querySelector('.tr-s')) list.innerHTML = '';
    n.sessions.forEach(function (s) {
      var k = s.sid || (s.user + s.mac); seen[k] = 1;
      var el = have[k];
      if (!el) { el = document.createElement('div'); el.className = 'tr-s'; el.dataset.key = k;
        el.innerHTML = '<span class="fill"></span><span class="pct" data-f="pct"></span><b data-f="who"></b><small data-f="meta"></small><div class="rates"><span class="d" data-f="dn"></span><span class="u" data-f="up"></span></div>'; }
      el.querySelector('[data-f=who]').innerHTML = vlink(s.user) + (s.device ? ' · <span style="font-weight:500">' + dlink(s.mac, s.device) + '</span>' : '');
      el.querySelector('[data-f=meta]').innerHTML = [esc(s.ip || ''), dlink(s.mac), esc(s.router || '')].filter(Boolean).join(' · ') + ' · ' + esc(s.session_h) + ' this session';
      var dn = el.querySelector('[data-f=dn]'), up = el.querySelector('[data-f=up]');
      tween(dn, s.down_bps, function (v) { return '↓ ' + bps(v); }); tween(up, s.up_bps, function (v) { return '↑ ' + bps(v); });
      el.querySelector('[data-f=pct]').textContent = s.share ? s.share + '%' : '';
      bar(el.querySelector('.fill'), s.share);
      list.appendChild(el);
    });
    Object.keys(have).forEach(function (k) { if (!seen[k]) have[k].remove(); });
  }
  // links to a voucher's / device's page (bypass devices show as BYPASS:<MAC>)
  function vlink(u) { if (!u) return '—'; var m = /^BYPASS:(.+)$/i.exec(u); if (m) return dlink(m[1], 'Bypass ' + m[1]);
    return '<a class="tt-vlink" href="/go/voucher/' + encodeURIComponent(u) + '/">' + esc(u) + '</a>'; }
  function dlink(mac, label) { return mac ? '<a class="tt-dlink" href="/go/device/' + encodeURIComponent(mac) + '/">' + esc(label || mac) + '</a>' : ''; }
  function loadNow() {
    clearTimeout(nowTimer);
    if (paused || document.hidden) { nowTimer = setTimeout(loadNow, NOW_EVERY); return; }
    fetch(NOW_URL, { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.json(); }).then(paintNow).catch(function () {})
      .then(function () { nowTimer = setTimeout(loadNow, NOW_EVERY); });
  }

  // ─────────── wiring ───────────
  $('trPause').addEventListener('click', function () {
    paused = !paused; this.setAttribute('aria-pressed', paused); this.title = paused ? 'Resume automatic updates' : 'Pause automatic updates';
    this.innerHTML = '<i class="bi ' + (paused ? 'bi-play-fill' : 'bi-pause-fill') + '"></i>';
    if (!paused) { load(); loadNow(); } else clearTimeout(timer);
    tickFresh();
  });
  // Live sync saw something change on a router: refresh soon (at most every 15 s).
  window.addEventListener('taptap:live', function () { if (!paused && Date.now() - lastFetch > 15000) schedule(1500); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && !paused && Date.now() - lastFetch > REPORT_EVERY) load(); });
  setInterval(tickFresh, 1000);

  setupCharts();
  render(data);
  paintNow(JSON.parse(document.getElementById('trNow').textContent));
  tickFresh();
  schedule(REPORT_EVERY);
  nowTimer = setTimeout(loadNow, NOW_EVERY);
})();
