/* Fit charts: draws what core/fit_charts.py computed (chat cards and live dashboards). Chart.js 4. */
(function (g) {
  'use strict';
  var PAL = ['#1769e0', '#18a66a', '#f59e0b', '#7c3aed', '#14a3e2', '#db2777', '#0ea5a4', '#e5484d', '#65a30d', '#0891b2'];
  function cur() { return g.TT_CURRENCY || 'D'; }
  function alpha(hex, a) { var n = hex.replace('#', ''); return 'rgba(' + parseInt(n.slice(0, 2), 16) + ',' + parseInt(n.slice(2, 4), 16) + ',' + parseInt(n.slice(4, 6), 16) + ',' + a + ')'; }
  function fmt(v, unit) {
    v = +v || 0;
    if (unit === 'money') { var a = Math.abs(v); return (v < 0 ? '−' : '') + cur() + (a >= 1e6 ? (a / 1e6).toFixed(1) + 'M' : a >= 1e4 ? (a / 1e3).toFixed(1) + 'k' : a.toLocaleString(undefined, { maximumFractionDigits: a % 1 ? 2 : 0 })); }
    if (unit === 'pct') return Math.round(v) + '%';
    if (unit === 'mb') return v >= 1024 ? (v / 1024).toFixed(1) + ' GB' : v.toFixed(v < 10 ? 1 : 0) + ' MB';
    return v.toLocaleString();
  }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  function kpis(box, d) {
    box.classList.add('fch-kpis');
    box.innerHTML = (d.tiles || []).map(function (t) {
      var delta = '';
      if (t.compare != null && t.unit === 'money') {
        var diff = t.value - t.compare, up = diff >= 0;
        delta = '<em class="' + (up ? 'up' : 'down') + '">' + (up ? '▲ ' : '▼ ') + fmt(Math.abs(diff), 'money') + ' vs ' + esc(t.compare_label || 'before') + '</em>';
      }
      return '<div class="fch-tile' + (t.live ? ' live' : '') + '"><small>' + esc(t.name) + (t.live ? ' <i></i>' : '') + '</small><b>' + fmt(t.value, t.unit) +
        (t.of != null ? '<span>/' + t.of + '</span>' : '') + '</b>' + delta + '</div>';
    }).join('') || '<p class="fch-empty">Nothing to show for your role.</p>';
  }

  function config(d) {
    var spec = d.spec || {}, type = spec.type || 'bar', series = d.series || [], labels = d.labels || [];
    var useHist = (type === 'line' || type === 'area') && d.history && d.history.length > 1 && (spec.source === 'online_users' || spec.source === 'router_load');
    if (useHist) {           // live: one moving line per router, from the history TapTap keeps
      labels = d.history.map(function (h) { return h.t; });
      var keys = {}; d.history.forEach(function (h) { Object.keys(h.v || {}).forEach(function (k) { keys[k] = 1; }); });
      series = Object.keys(keys).map(function (k) { return { name: k, unit: (d.series[0] || {}).unit, data: d.history.map(function (h) { return (h.v || {})[k] || 0; }) }; });
    }
    if (type === 'doughnut' || type === 'pie') {
      var s0 = series[0] || { data: [] };
      return { type: type, data: { labels: labels, datasets: [{ data: s0.data, backgroundColor: PAL.slice(0, s0.data.length), borderWidth: 2, borderColor: 'rgba(255,255,255,.9)', hoverOffset: 6 }] },
        options: { maintainAspectRatio: false, cutout: type === 'doughnut' ? '64%' : 0, animation: { duration: 400 },
          plugins: { legend: { position: 'right', labels: { boxWidth: 10, usePointStyle: true } },
            tooltip: { callbacks: { label: function (c) { var t = c.dataset.data.reduce(function (a, b) { return a + (+b || 0); }, 0); return ' ' + c.label + ': ' + fmt(c.parsed, s0.unit) + ' (' + (t ? Math.round(c.parsed / t * 100) : 0) + '%)'; } } } } } };
    }
    var right = series.some(function (s) { return s.axis === 'right'; });
    var ds = series.map(function (s, i) {
      var col = s.muted ? '#94a3b8' : PAL[i % PAL.length];
      return { label: s.name, data: s.data, yAxisID: s.axis === 'right' ? 'y1' : 'y', borderColor: col, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2,
        backgroundColor: type === 'bar' ? alpha(col, s.axis === 'right' ? .35 : .85) : alpha(col, type === 'area' ? .18 : .06),
        fill: type === 'area', tension: .32, borderDash: s.muted ? [5, 4] : undefined, type: (type === 'bar' && s.axis === 'right') ? 'line' : undefined, borderRadius: 5 };
    });
    var unitL = (series.find(function (s) { return s.axis !== 'right'; }) || {}).unit, unitR = (series.find(function (s) { return s.axis === 'right'; }) || {}).unit;
    var scales = { x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, autoSkipPadding: 14 } },
      y: { beginAtZero: true, grid: { color: 'rgba(127,127,127,.12)' }, border: { display: false }, ticks: { callback: function (v) { return fmt(v, unitL); } } } };
    if (right) scales.y1 = { position: 'right', beginAtZero: true, grid: { display: false }, ticks: { callback: function (v) { return fmt(v, unitR); } } };
    return { type: type === 'area' ? 'line' : type, data: { labels: labels, datasets: ds },
      options: { maintainAspectRatio: false, animation: { duration: 400 }, interaction: { mode: 'index', intersect: false }, scales: scales,
        plugins: { legend: { display: ds.length > 1, labels: { boxWidth: 10, usePointStyle: true } },
          tooltip: { callbacks: { label: function (c) { var s = series[c.datasetIndex] || {}; return ' ' + c.dataset.label + ': ' + fmt(c.parsed.y, s.unit); } } } } } };
  }

  /* render(box, data) — draws or updates the chart inside box. */
  function render(box, d) {
    if (d.error) { box.innerHTML = '<p class="fch-empty">' + esc(d.error) + '</p>'; return; }
    if ((d.spec || {}).type === 'kpi' || d.tiles) { kpis(box, d); return; }
    var cfg = config(d), empty = !cfg.data.datasets.some(function (s) { return (s.data || []).some(function (v) { return +v; }); });
    if (empty) { if (box._chart) { box._chart.destroy(); box._chart = null; } box.innerHTML = '<p class="fch-empty">No data for this yet.</p>'; return; }
    if (!g.Chart) { box.innerHTML = '<p class="fch-empty">Charts are loading…</p>'; return; }
    if (box._chart && box._chart.config.type === cfg.type) {
      box._chart.data.labels = cfg.data.labels;
      cfg.data.datasets.forEach(function (ds, i) { if (box._chart.data.datasets[i]) { box._chart.data.datasets[i].data = ds.data; box._chart.data.datasets[i].label = ds.label; } else box._chart.data.datasets.push(ds); });
      box._chart.data.datasets.length = cfg.data.datasets.length;
      box._chart.update('none');
      return;
    }
    if (box._chart) box._chart.destroy();
    box.innerHTML = '<canvas></canvas>';
    box._chart = new g.Chart(box.firstChild, cfg);
  }

  var loading = null;
  function ensure(base) {      /* lazy-load Chart.js on pages that do not have it (the Fit panel is on every page) */
    if (g.Chart) return Promise.resolve();
    if (loading) return loading;
    loading = new Promise(function (ok, bad) { var s = document.createElement('script'); s.src = base; s.onload = ok; s.onerror = bad; document.head.appendChild(s); });
    return loading;
  }
  g.FitCharts = { render: render, ensure: ensure, fmt: fmt };
})(window);
