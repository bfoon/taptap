/* Shared chart helpers for Finance and Reports (Chart.js 4). */
(function (g) {
  var C = { blue: '#1769e0', sky: '#14a3e2', green: '#18a66a', red: '#e5484d', orange: '#f59e0b', purple: '#7c3aed', teal: '#0ea5a4', pink: '#db2777', slate: '#94a3b8', ink: '#102033' };
  var SERIES = [C.blue, C.green, C.orange, C.purple, C.sky, C.pink, C.teal, C.red, '#65a30d', '#0891b2', '#a16207', C.slate];
  var cur = (g.TT_CURRENCY || 'D');
  function money(v, dp) { v = +v || 0; var a = Math.abs(v), s = v < 0 ? '−' : ''; if (dp == null) dp = a % 1 && a < 1000 ? 2 : 0; if (a >= 1e6) return s + cur + (a / 1e6).toFixed(1) + 'M'; if (a >= 1e4 && !dp) return s + cur + (a / 1e3).toFixed(a >= 1e5 ? 0 : 1) + 'k'; return s + cur + a.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp }); }
  function num(v) { return (+v || 0).toLocaleString(); }
  function alpha(hex, a) { var n = hex.replace('#', ''); return 'rgba(' + parseInt(n.slice(0, 2), 16) + ',' + parseInt(n.slice(2, 4), 16) + ',' + parseInt(n.slice(4, 6), 16) + ',' + a + ')'; }

  if (g.Chart) {
    Chart.defaults.font.family = 'Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif';
    Chart.defaults.font.size = 11.5; Chart.defaults.color = '#6c7c90';
    Chart.defaults.plugins.legend.labels.boxWidth = 10; Chart.defaults.plugins.legend.labels.boxHeight = 10; Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.tooltip.backgroundColor = '#0b2237'; Chart.defaults.plugins.tooltip.padding = 10; Chart.defaults.plugins.tooltip.cornerRadius = 8;
    Chart.defaults.plugins.tooltip.titleFont = { weight: '700' }; Chart.defaults.maintainAspectRatio = false;
    Chart.defaults.elements.bar.borderRadius = 5; Chart.defaults.elements.line.tension = .32; Chart.defaults.elements.point.radius = 0; Chart.defaults.elements.point.hoverRadius = 5;
  }
  var registry = {};
  function make(id, cfg) { var el = document.getElementById(id); if (!el || !g.Chart) return null; if (registry[id]) registry[id].destroy(); registry[id] = new Chart(el, cfg); return registry[id]; }
  function moneyAxis() { return { grid: { color: '#eef2f7' }, border: { display: false }, ticks: { callback: function (v) { return money(v); } } }; }
  function plainAxis() { return { grid: { display: false }, border: { display: false }, ticks: { maxRotation: 0, autoSkip: true, autoSkipPadding: 12 } }; }
  function moneyTip() { return { callbacks: { label: function (c) { return ' ' + c.dataset.label + ': ' + money(c.parsed.y != null ? c.parsed.y : c.parsed); } } }; }
  function empty(values) { return !(values || []).some(function (v) { return +v; }); }
  function showEmpty(id, msg, show) { var el = document.getElementById(id); if (!el) return; var box = el.parentElement, n = box.querySelector('.chart-empty'); if (show) { if (!n) { n = document.createElement('div'); n.className = 'chart-empty'; box.appendChild(n); } n.textContent = msg; } else if (n) n.remove(); }

  function doughnut(id, labels, values, opts) {
    opts = opts || {};
    showEmpty(id, opts.empty || 'No data for this period yet.', empty(values));
    return make(id, { type: 'doughnut', data: { labels: labels, datasets: [{ data: values, backgroundColor: SERIES.slice(0, values.length), borderWidth: 2, borderColor: '#fff', hoverOffset: 6 }] },
      options: { cutout: '68%', plugins: { legend: { position: opts.legend || 'right' }, tooltip: { callbacks: { label: function (c) { var t = c.dataset.data.reduce(function (a, b) { return a + b; }, 0); return ' ' + c.label + ': ' + (opts.count ? num(c.parsed) : money(c.parsed)) + ' (' + (t ? Math.round(c.parsed / t * 100) : 0) + '%)'; } } } } } });
  }

  g.TT = { C: C, SERIES: SERIES, money: money, num: num, alpha: alpha, make: make, moneyAxis: moneyAxis, plainAxis: plainAxis, moneyTip: moneyTip, doughnut: doughnut, empty: empty, showEmpty: showEmpty };
})(window);
