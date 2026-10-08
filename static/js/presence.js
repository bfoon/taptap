/* TapTap — who else is on this page (core/page_presence.py). */
(function () {
  var box = document.getElementById('ttPresence'); if (!box || !window.fetch) return;
  var kind = box.dataset.kind, id = box.dataset.id, csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
  if (!csrf) { var t = document.querySelector('[name=csrfmiddlewaretoken]'); csrf = t ? t.value : ''; }
  var tab = Math.random().toString(36).slice(2, 10), every = 15000, timer = null, lastEdit = 0, stopped = false;
  var MAX = 5;

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function ago(ts) { var s = Math.max(0, Math.round(Date.now() / 1000 - ts)); return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + ' min' : Math.round(s / 3600) + ' h'; }
  function state() {
    if (document.hidden) return 'away';
    var a = document.activeElement;
    var typing = a && a.closest && a.closest('form') && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a.type !== 'search' && !a.closest('[role=search]');
    return (typing || Date.now() - lastEdit < 20000) ? 'editing' : 'viewing';
  }
  document.addEventListener('input', function (e) { if (e.target.closest && e.target.closest('form') && e.target.type !== 'search') lastEdit = Date.now(); }, true);

  function draw(list) {
    var others = list.filter(function (v) { return !v.you; });
    if (!others.length) { box.innerHTML = ''; return; }                  // alone: nothing to show
    var shown = list.slice(0, MAX), extra = list.length - shown.length, html = '';
    shown.forEach(function (v, i) {
      var what = v.state === 'editing' ? 'editing' : v.state === 'away' ? 'tab in the background' : 'viewing';
      var tip = (v.you ? 'You' : v.name) + ' · ' + v.role + ' — ' + what + ' · here ' + ago(v.since) + (v.tabs > 1 ? ' · ' + v.tabs + ' tabs' : '');
      html += '<span class="pp-av ' + esc(v.state) + (v.you ? ' you' : '') + '" style="background:' + esc(v.color) + ';z-index:' + (MAX - i + 1) + '" title="' + esc(tip) + '" aria-label="' + esc(tip) + '">'
        + esc(v.initials) + (v.state === 'editing' ? '<span class="pp-edit"><i class="bi bi-pencil-fill"></i></span>' : '<span class="pp-dot"></span>') + '</span>';
    });
    if (extra > 0) html += '<span class="pp-av pp-more" title="' + esc(list.slice(MAX).map(function (v) { return v.name; }).join(', ')) + '">+' + extra + '</span>';
    var editors = others.filter(function (v) { return v.state === 'editing'; });
    if (editors.length) html += '<span class="pp-note"><i class="bi bi-pencil"></i> ' + esc(editors.length === 1 ? editors[0].name.split(' ')[0] + ' is editing' : editors.length + ' people are editing') + '</span>';
    box.innerHTML = html;
  }

  function ping() {
    if (stopped) return;
    var body = new URLSearchParams({ kind: kind, id: id, state: state(), tab: tab });
    fetch(box.dataset.ping, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': csrf }, body: body })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (j && j.ok) draw(j.viewers); })
      .catch(function () {})
      .then(schedule);
  }
  function schedule() { clearTimeout(timer); if (!stopped) timer = setTimeout(ping, document.hidden ? every * 2 : every); }
  function leave() {
    if (stopped) return; stopped = true; clearTimeout(timer);
    var fd = new FormData(); fd.append('kind', kind); fd.append('id', id); fd.append('tab', tab); fd.append('csrfmiddlewaretoken', csrf);
    if (navigator.sendBeacon) navigator.sendBeacon(box.dataset.leave, fd);
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden && stopped) { stopped = false; } ping(); });
  var lastState = '';
  ['focusin', 'focusout'].forEach(function (ev) { document.addEventListener(ev, function () { setTimeout(function () { var s = state(); if (s !== lastState) { lastState = s; ping(); } }, 50); }); });
  window.addEventListener('pagehide', leave);
  window.addEventListener('pageshow', function (e) { if (e.persisted) { stopped = false; ping(); } });
  ping();
})();
