/* TapTap live-sync heartbeat: keeps pages fresh and, if no scheduler is running, drives the sync itself. */
(function () {
  'use strict';
  var pill = document.getElementById('livePill'); if (!pill) return;
  var TICK = pill.dataset.tick, NOW = pill.dataset.now, lastIds = '', state = null, fail = 0, timer = null;
  var lastAlert = +(sessionStorageGet('tt-last-alert') || 0), firstTick = true, alertsSeen = [];
  var lastEv = +(sessionStorageGet('tt-last-ev') || 0), evSeen = [], evUnread = 0, devUnread = 0;
  function sessionStorageGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function sessionStorageSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }
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
  function chime(level) {   // business alerts: a two/three-note chime by importance
    try { var A = window.AudioContext || window.webkitAudioContext; if (!A) return; var c = new A();
      var notes = level === 'danger' ? [880, 660, 880, 660] : level === 'info' ? [660, 880] : [740, 988, 740];
      notes.forEach(function (f, i) { var o = c.createOscillator(), g = c.createGain(), t = c.currentTime + i * .16; o.type = 'triangle'; o.frequency.value = f;
        g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(.12, t + .02); g.gain.exponentialRampToValueAtTime(.0001, t + .3); o.connect(g); g.connect(c.destination); o.start(t); o.stop(t + .32); });
    } catch (e) {}
  }
  function evToast(a) {
    var box = document.querySelector('.tt-toasts'); if (!box) { box = document.createElement('div'); box.className = 'tt-toasts'; box.setAttribute('aria-live', 'polite'); document.body.appendChild(box); }
    var el = document.createElement('div'); el.className = 'tt-toast ' + (a.level === 'danger' ? 'off' : a.level === 'info' ? 'on' : 'warn');
    el.innerHTML = '<i class="bi ' + (a.level === 'danger' ? 'bi-exclamation-octagon text-danger' : a.level === 'info' ? 'bi-info-circle text-primary' : 'bi-exclamation-triangle text-warning') + '"></i><div><b>' + esc(a.title) + '</b><br><small>' + esc(a.body) + '</small></div><button aria-label="Dismiss">✕</button>';
    el.querySelector('button').onclick = function (e) { e.stopPropagation(); el.remove(); };
    if (a.link) { el.style.cursor = 'pointer'; el.onclick = function () { location.href = a.link; }; }
    box.appendChild(el); setTimeout(function () { el.remove(); }, a.level === 'danger' ? 30000 : 15000);
  }
  function evDesktop(a) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try { var n = new Notification((a.level === 'danger' ? '🚨 ' : a.level === 'info' ? 'ℹ ' : '⚠ ') + a.title, { body: a.body, tag: 'tt-ev-' + a.id, requireInteraction: a.level === 'danger' });
      if (a.link) n.onclick = function () { window.focus(); location.href = a.link; n.close(); }; } catch (e) {}
  }
  function paintBell() {
    var bell = document.getElementById('alertBell'); if (!bell) return;
    var n = devUnread + evUnread, cnt = bell.querySelector('.ab-count'); cnt.hidden = !n; cnt.textContent = n > 99 ? '99+' : n; bell.classList.toggle('has', !!n);
    var list = document.querySelector('#alertPanel .ab-ev'); if (!list) { var host = document.querySelector('#alertPanel .ab-list'); if (!host) return;
      list = document.createElement('div'); list.className = 'ab-ev'; host.parentNode.insertBefore(list, host); }
    list.innerHTML = evSeen.map(function (a) { return '<a class="ab-item ' + (a.level === 'danger' ? 'off' : 'on') + '" href="' + esc(a.link || '/alerts/?f=business') + '" style="text-decoration:none;color:inherit"><i class="bi ' +
      (a.level === 'danger' ? 'bi-exclamation-octagon' : a.level === 'info' ? 'bi-info-circle' : 'bi-exclamation-triangle') + '"></i><div><b>' + esc(a.title) + '</b><small>' + esc(a.body) + ' · ' + new Date(a.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + '</small></div></a>'; }).join('');
  }
  function paintEvents(ev) {
    if (!ev) return;
    evUnread = ev.unread || 0;
    (ev.fresh || []).forEach(function (a) { if (!evSeen.some(function (x) { return x.id === a.id; })) evSeen.push(a); });
    evSeen.sort(function (a, b) { return b.id - a.id; }); evSeen = evSeen.slice(0, 10);
    var newer = (ev.fresh || []).filter(function (a) { return a.id > lastEv; });
    if (newer.length && !firstEv) {
      newer.slice().reverse().forEach(function (a) { evToast(a); if (a.desktop) evDesktop(a); });
      var loud = newer.filter(function (a) { return a.sound; });
      if (loud.length) { chime(loud.some(function (a) { return a.level === 'danger'; }) ? 'danger' : loud[0].level); var bell = document.getElementById('alertBell'); if (bell) { bell.classList.remove('ring'); void bell.offsetWidth; bell.classList.add('ring'); } }
    }
    if (ev.last_id > lastEv) { lastEv = ev.last_id; sessionStorageSet('tt-last-ev', lastEv); }
    firstEv = false; paintBell();
  }
  var firstEv = true;
  function beep() {
    try { var A = window.AudioContext || window.webkitAudioContext; if (!A) return; var c = new A(), o = c.createOscillator(), g = c.createGain();
      o.type = 'sine'; o.frequency.value = 660; g.gain.setValueAtTime(.08, c.currentTime); g.gain.exponentialRampToValueAtTime(.0001, c.currentTime + .5);
      o.connect(g); g.connect(c.destination); o.start(); o.stop(c.currentTime + .5); } catch (e) {}
  }
  function toast(a) {
    var box = document.querySelector('.tt-toasts'); if (!box) { box = document.createElement('div'); box.className = 'tt-toasts'; box.setAttribute('aria-live', 'polite'); document.body.appendChild(box); }
    var el = document.createElement('div'); el.className = 'tt-toast ' + (a.event === 'offline' ? 'off' : 'on');
    el.innerHTML = '<i class="bi ' + (a.event === 'offline' ? 'bi-exclamation-octagon text-danger' : 'bi-check-circle text-success') + '"></i><div><b>' + esc(a.name) + '</b> ' + (a.event === 'offline' ? 'went offline' : 'is back online') +
      '<br><small>' + esc([a.ip, a.router].filter(Boolean).join(' · ')) + '</small></div><button aria-label="Dismiss">✕</button>';
    el.querySelector('button').onclick = function () { el.remove(); };
    box.appendChild(el); setTimeout(function () { el.remove(); }, 12000);
  }
  function desktop(a) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try { new Notification((a.event === 'offline' ? '⚠ ' : '✓ ') + a.name + (a.event === 'offline' ? ' went offline' : ' is back online'), { body: [a.ip, a.router].filter(Boolean).join(' · '), tag: 'tt-' + a.id }); } catch (e) {}
  }
  function paintAlerts(al) {
    var bell = document.getElementById('alertBell'); if (!bell || !al) return;
    devUnread = al.unread || 0;
    (al.fresh || []).forEach(function (a) { if (!alertsSeen.some(function (x) { return x.id === a.id; })) alertsSeen.push(a); });
    alertsSeen.sort(function (a, b) { return b.id - a.id; }); alertsSeen = alertsSeen.slice(0, 20);
    var list = document.querySelector('#alertPanel .ab-list');
    if (list) list.innerHTML = alertsSeen.length ? alertsSeen.map(function (a) { return '<div class="ab-item ' + (a.event === 'offline' ? 'off' : 'on') + '"><i class="bi ' + (a.event === 'offline' ? 'bi-exclamation-octagon' : 'bi-check-circle') + '"></i><div><b>' + esc(a.name) + '</b> ' + (a.event === 'offline' ? 'went offline' : 'back online') + '<small>' + esc([a.ip, a.router, new Date(a.at).toLocaleString()].filter(Boolean).join(' · ')) + '</small></div></div>'; }).join('') :
      '<small class="text-secondary">' + (al.unread ? al.unread + ' unread — open All alerts to see them.' : 'No new alerts. You are told here when watched devices go offline.') + '</small>';
    var newer = (al.fresh || []).filter(function (a) { return a.id > lastAlert; });
    if (newer.length && !firstTick) {
      newer.slice().reverse().forEach(function (a) { toast(a); desktop(a); });
      if (newer.some(function (a) { return a.event === 'offline'; })) { beep(); bell.classList.remove('ring'); void bell.offsetWidth; bell.classList.add('ring'); }
      window.dispatchEvent(new CustomEvent('taptap:alerts', { detail: newer }));
    }
    if (al.last_id > lastAlert) { lastAlert = al.last_id; sessionStorageSet('tt-last-alert', lastAlert); }
    firstTick = false; paintBell();
  }
  function apply(d) {
    state = d; fail = 0; paintAlerts(d.alerts); paintEvents(d.biz_alerts);
    var ids = (d.events || []).map(function (e) { return e.t; }).join('|');
    if (lastIds && ids !== lastIds) window.dispatchEvent(new CustomEvent('taptap:live', { detail: d }));
    lastIds = ids; paint();
  }
  function tick() {
    clearTimeout(timer);
    if (document.hidden) { timer = setTimeout(tick, 30000); return; }
    fetch(TICK + '?since=' + (firstTick ? 0 : lastAlert) + '&esince=' + (firstEv ? 0 : lastEv), { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.json(); }).then(apply)
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
  var bell = document.getElementById('alertBell'), apanel = document.getElementById('alertPanel');
  if (bell) {
    bell.addEventListener('click', function (e) { e.preventDefault(); apanel.classList.toggle('show'); bell.setAttribute('aria-expanded', apanel.classList.contains('show')); });
    document.addEventListener('click', function (e) { if (apanel.classList.contains('show') && !apanel.contains(e.target) && !bell.contains(e.target)) { apanel.classList.remove('show'); bell.setAttribute('aria-expanded', 'false'); } });
    document.getElementById('alertRead').onclick = function () {
      fetch('/alerts/read/', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': decodeURIComponent(token), 'x-requested-with': 'fetch' } })
        .then(function () { alertsSeen = []; evSeen = []; evUnread = 0; paintAlerts({ unread: 0, fresh: [], last_id: lastAlert }); });
    };
    var desk = document.getElementById('alertDesk');
    function deskLabel() { desk.textContent = !('Notification' in window) ? 'Desktop alerts unsupported' : Notification.permission === 'granted' ? 'Desktop alerts on' : Notification.permission === 'denied' ? 'Desktop alerts blocked' : 'Turn on desktop alerts'; desk.disabled = !('Notification' in window) || Notification.permission !== 'default'; }
    deskLabel(); desk.onclick = function () { Notification.requestPermission().then(deskLabel); };
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) tick(); });
  setInterval(paint, 1000);
  tick();
})();
