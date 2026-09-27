/* TapTap live-sync heartbeat: keeps pages fresh and, if no scheduler is running, drives the sync itself. */
(function () {
  'use strict';
  var pill = document.getElementById('livePill'); if (!pill) return;
  var TICK = pill.dataset.tick, NOW = pill.dataset.now, lastIds = '', state = null, fail = 0, timer = null;
  var token = (document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || '';
  function ago(iso) { if (!iso) return 'never'; var s = Math.max(0, (Date.now() - new Date(iso)) / 1000); return s < 60 ? Math.round(s) + 's ago' : s < 3600 ? Math.round(s / 60) + 'm ago' : Math.round(s / 3600) + 'h ago'; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function paint() {
    if (!state) return;
    var offline = state.routers.filter(function (r) { return r.status !== 'Online'; }).length;
    var stale = !state.last_watch_at || (Date.now() - new Date(state.last_watch_at)) / 1000 > state.interval * 4;
    var cls = !state.live ? 'off' : offline ? 'warn' : stale ? 'warn' : 'ok';
    pill.className = 'live-pill ' + cls;
    pill.querySelector('.lp-text').textContent = !state.live ? 'Live sync off' : (offline ? offline + ' router' + (offline > 1 ? 's' : '') + ' offline' : 'Live · ' + ago(state.last_watch_at));
    var badge = pill.querySelector('.lp-badge'); badge.hidden = !state.incidents; badge.textContent = state.incidents;
    var box = document.getElementById('livePanel'); if (!box || !box.classList.contains('show')) return;
    box.querySelector('.lp-mode').innerHTML = state.mode === 'scheduler' ? '<i class="bi bi-check-circle text-success"></i> Checking every ' + state.interval + ' s, around the clock.' :
      '<i class="bi bi-exclamation-circle text-warning"></i> Checking every ' + state.interval + ' s while TapTap is open. Start the <b>live</b> service for 24/7 checks.';
    box.querySelector('.lp-routers').innerHTML = state.routers.map(function (r) { return '<div><span class="lp-dot ' + (r.status === 'Online' ? 'on' : '') + '"></span>' + esc(r.name) + '<small>' + (r.status === 'Online' ? ago(r.last_watch_at) : esc(r.status)) + '</small></div>'; }).join('') || '<small class="text-secondary">No routers yet.</small>';
    box.querySelector('.lp-events').innerHTML = (state.events || []).map(function (e) { return '<div class="lp-ev ' + e.kind + '"><time>' + new Date(e.t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + '</time>' + esc(e.text) + '</div>'; }).join('') || '<small class="text-secondary">No changes yet — they appear here as they happen on your routers.</small>';
    box.querySelector('.lp-inc').innerHTML = state.incidents ? '<a href="/security/#incidents" class="btn btn-sm btn-danger w-100 mt-2"><i class="bi bi-shield-exclamation"></i> ' + state.incidents + ' session' + (state.incidents > 1 ? 's' : '') + ' should not be online</a>' : '';
  }
  function apply(d) {
    state = d; fail = 0;
    var ids = (d.events || []).map(function (e) { return e.t; }).join('|');
    if (lastIds && ids !== lastIds) window.dispatchEvent(new CustomEvent('taptap:live', { detail: d }));
    lastIds = ids; paint();
  }
  function tick() {
    clearTimeout(timer);
    if (document.hidden) { timer = setTimeout(tick, 30000); return; }
    fetch(TICK, { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.json(); }).then(apply)
      .catch(function () { fail++; }).then(function () { timer = setTimeout(tick, Math.min(60000, ((state && state.interval) || 15) * 1000 * Math.pow(2, fail))); });
  }
  pill.addEventListener('click', function (e) { e.preventDefault(); var box = document.getElementById('livePanel'); box.classList.toggle('show'); pill.setAttribute('aria-expanded', box.classList.contains('show')); paint(); });
  document.addEventListener('click', function (e) { var box = document.getElementById('livePanel'); if (box && box.classList.contains('show') && !box.contains(e.target) && !pill.contains(e.target)) { box.classList.remove('show'); pill.setAttribute('aria-expanded', 'false'); } });
  document.getElementById('liveNow').addEventListener('click', function () {
    var b = this; b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Checking routers…';
    fetch(NOW, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': decodeURIComponent(token) } }).then(function (r) { return r.json(); }).then(function (d) {
      apply(d); b.innerHTML = '<i class="bi bi-check2"></i> ' + esc(d.message || 'Done'); window.dispatchEvent(new CustomEvent('taptap:live', { detail: d }));
    }).catch(function (e) { b.textContent = 'Failed: ' + e.message; }).then(function () { setTimeout(function () { b.disabled = false; b.innerHTML = '<i class="bi bi-arrow-repeat"></i> Sync now'; }, 2500); });
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(); });
  setInterval(paint, 1000);
  tick();
})();
