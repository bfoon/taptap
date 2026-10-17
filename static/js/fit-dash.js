/* Live dashboard page (templates/core/fit_dashboards.html): polls TapTap and redraws every widget. */
(function () {
  'use strict';
  var root = document.getElementById('fdash');
  if (!root) return;
  var every = Math.max(10, +root.dataset.refresh || 30) * 1000, boxes = [], timer = null, fails = 0;
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function layout(ws) {
    root.innerHTML = '';
    boxes = ws.map(function (w) {
      var s = w.spec || {}, card = document.createElement('section');
      card.className = 'fdash-card t-' + (s.type || 'bar') + (w.live ? ' is-live' : '');
      card.innerHTML = '<header><h3>' + esc(s.title) + '</h3>' + (w.live ? '<span class="fdash-live"><i></i>live</span>' : '') + '</header><div class="fch-box"></div>';
      root.appendChild(card);
      return card.querySelector('.fch-box');
    });
  }
  function load() {
    fetch(root.dataset.url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (j) {
      fails = 0;
      if (j.currency) window.TT_CURRENCY = j.currency;
      if (boxes.length !== (j.widgets || []).length) layout(j.widgets || []);
      (j.widgets || []).forEach(function (w, i) { FitCharts.render(boxes[i], w); });
      var at = document.getElementById('fdashAt'); if (at) at.textContent = 'updated ' + new Date().toLocaleTimeString();
    }).catch(function () {
      fails++; var at = document.getElementById('fdashAt'); if (at) at.textContent = 'reconnecting…';
    }).then(function () { timer = setTimeout(load, document.hidden ? every * 4 : every * Math.min(4, 1 + fails)); });
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { clearTimeout(timer); load(); } });
  var full = document.getElementById('fdashFull');
  if (full) full.addEventListener('click', function () { document.body.classList.toggle('fdash-wall'); try { if (!document.fullscreenElement) document.documentElement.requestFullscreen(); else document.exitFullscreen(); } catch (e) {} });
  load();
})();
