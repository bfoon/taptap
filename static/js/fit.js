/* Fit — the TapTap AI technician.
   A small hovering robot with a screwdriver that lives above the chat button.
   It follows your pointer with its eyes, blinks, looks around, twirls its screwdriver,
   tightens a bolt on its own head when bored, falls asleep when you leave it alone,
   waves when you come near, flashes its antenna when something needs you, and hops
   when an action works. Click it to talk.

   Server: /fit/state/ · /fit/ask/ · /fit/action/<id>/ · /fit/settings/ · /fit/new/  (core/views_fit.py) */
(function () {
  'use strict';
  var root = document.getElementById('ttFit'); if (!root) return;
  var CSRF = root.dataset.csrf || '';
  var REDUCED = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var S = { open: false, state: null, busy: false, conv: null, view: 'chat', lastMove: Date.now(), mood: '', asleep: false, polls: {} };

  // ───────── the robot ─────────
  function botSVG() {
    return '' +
      '<svg class="fit-bot" viewBox="0 0 100 120" aria-hidden="true">' +
      '<defs><linearGradient id="fbSteel" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--f-steel)"/><stop offset="1" stop-color="var(--f-steel-2)"/></linearGradient>' +
      '<linearGradient id="fbFlame" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe9a8"/><stop offset=".55" stop-color="#f5a524"/><stop offset="1" stop-color="#f5a524" stop-opacity="0"/></linearGradient></defs>' +
      '<ellipse class="fb-shadow" cx="50" cy="114" rx="22" ry="3.5"/>' +
      '<g class="fb-float"><g class="fb-body">' +
        '<g class="fb-flame"><path d="M43 96 Q50 116 57 96 Z" fill="url(#fbFlame)"/><path d="M47 96 Q50 106 53 96 Z" fill="#fff6d8"/></g>' +
        '<rect x="41" y="89" width="18" height="8" rx="3" fill="var(--f-steel-3)"/>' +
        // left arm (waves)
        '<g class="fb-arm-l"><rect x="23" y="62" width="9" height="22" rx="4.5" fill="url(#fbSteel)" stroke="var(--f-steel-3)" stroke-width=".8"/>' +
          '<circle cx="27.5" cy="86" r="4.6" fill="var(--f-steel-2)" stroke="var(--f-steel-3)" stroke-width=".8"/></g>' +
        // torso
        '<rect x="30" y="58" width="40" height="34" rx="11" fill="url(#fbSteel)" stroke="var(--f-steel-3)" stroke-width=".8"/>' +
        '<rect x="38" y="63.5" width="24" height="18" rx="6" fill="var(--f-visor)"/>' +
        '<circle class="fb-lamp-glow" cx="50" cy="72.5" r="6.5"/><circle class="fb-lamp" cx="50" cy="72.5" r="3.6"/>' +
        '<rect x="30" y="84" width="40" height="5" fill="var(--f-amber)"/><rect x="46" y="83.2" width="8" height="6.6" rx="1.6" fill="var(--f-amber-2)"/>' +
        // right arm with the screwdriver
        '<g class="fb-arm-r"><rect x="68" y="62" width="9" height="22" rx="4.5" fill="url(#fbSteel)" stroke="var(--f-steel-3)" stroke-width=".8"/>' +
          '<g class="fb-driver"><line x1="81" y1="86" x2="93" y2="86" stroke="var(--f-steel-3)" stroke-width="1.8" stroke-linecap="round"/>' +
            '<path d="M93 84.6 L95.6 86 L93 87.4 Z" fill="var(--f-steel-3)"/>' +
            '<rect x="73" y="83.4" width="9.5" height="5.2" rx="2.2" fill="var(--f-amber)"/><rect x="75.4" y="83.4" width="1.3" height="5.2" fill="var(--f-amber-2)"/></g>' +
          '<circle cx="72.5" cy="86" r="4.6" fill="var(--f-steel-2)" stroke="var(--f-steel-3)" stroke-width=".8"/></g>' +
        '<rect x="44" y="54" width="12" height="6" rx="2" fill="var(--f-steel-3)"/>' +
        // head
        '<g class="fb-head">' +
          '<g class="fb-antenna"><line x1="50" y1="13" x2="50" y2="6" stroke="var(--f-steel-3)" stroke-width="2" stroke-linecap="round"/>' +
            '<circle class="fb-bulb-glow" cx="50" cy="5" r="4"/><circle class="fb-bulb" cx="50" cy="5" r="3.2"/></g>' +
          faceSVG() +
          '<g class="fb-spark"><line x1="76" y1="38" x2="71" y2="33" stroke="#ffd56a" stroke-width="1.6" stroke-linecap="round"/><line x1="76" y1="38" x2="82" y2="34" stroke="#ffd56a" stroke-width="1.6" stroke-linecap="round"/><line x1="76" y1="38" x2="83" y2="40" stroke="#fff" stroke-width="1.4" stroke-linecap="round"/></g>' +
        '</g>' +
        '<g class="fb-burst">' + burst() + '</g>' +
      '</g>' +
        '<g class="fb-gear"><circle cx="84" cy="14" r="4.2" fill="none" stroke="var(--f-amber)" stroke-width="3" stroke-dasharray="2.1 1.6"/><circle cx="84" cy="14" r="1.6" fill="var(--f-amber)"/></g>' +
        '<text class="fb-zzz" x="72" y="20">z</text><text class="fb-zzz" x="76" y="16">z</text><text class="fb-zzz" x="80" y="12">Z</text>' +
      '</g></svg>';
  }
  function faceSVG() {
    return '' +
      '<rect x="22" y="18" width="56" height="38" rx="13" fill="url(#fbSteel)" stroke="var(--f-steel-3)" stroke-width=".8"/>' +
      '<path d="M23 33 C23 19 33 12.5 50 12.5 C67 12.5 77 19 77 33 Z" fill="var(--f-amber)"/>' +
      '<rect x="47" y="12.8" width="6" height="20" rx="2" fill="var(--f-amber-2)" opacity=".45"/>' +
      '<rect x="19" y="30" width="62" height="5" rx="2.5" fill="var(--f-amber-2)"/>' +
      '<circle cx="22" cy="45" r="3.4" fill="var(--f-steel-2)" stroke="var(--f-steel-3)" stroke-width=".8"/><path d="M20.4 45h3.2" stroke="var(--f-steel-3)" stroke-width=".9"/>' +
      '<circle cx="78" cy="45" r="3.4" fill="var(--f-steel-2)" stroke="var(--f-steel-3)" stroke-width=".8"/><path d="M76.4 45h3.2M78 43.4v3.2" stroke="var(--f-steel-3)" stroke-width=".9"/>' +
      '<rect x="28" y="37" width="44" height="16" rx="8" fill="var(--f-visor)"/>' +
      '<g class="fb-eyes"><rect class="fb-eye fb-eye-l" x="36" y="40.5" width="7" height="8" rx="3.5"/><rect class="fb-eye" x="57" y="40.5" width="7" height="8" rx="3.5"/>' +
        '<path class="fb-happy" d="M35.8 46.5 Q39.5 40.5 43.2 46.5 M56.8 46.5 Q60.5 40.5 64.2 46.5"/>' +
        '<path class="fb-closed" d="M35.8 45 Q39.5 47.6 43.2 45 M56.8 45 Q60.5 47.6 64.2 45"/>' +
        '<path class="fb-sad" d="M36 43 L43 45.5 M64 43 L57 45.5"/></g>' +
      '<g class="fb-mouth"><rect x="44.6" y="50" width="2.6" height="1.5" rx=".6"/><rect x="48.7" y="50" width="2.6" height="1.5" rx=".6"/><rect x="52.8" y="50" width="2.6" height="1.5" rx=".6"/></g>';
  }
  function burst() {
    var s = '';
    for (var i = 0; i < 8; i++) {
      var a = i * Math.PI / 4, x1 = 50 + Math.cos(a) * 34, y1 = 50 + Math.sin(a) * 34, x2 = 50 + Math.cos(a) * 44, y2 = 50 + Math.sin(a) * 44;
      s += '<line x1="' + x1.toFixed(1) + '" y1="' + y1.toFixed(1) + '" x2="' + x2.toFixed(1) + '" y2="' + y2.toFixed(1) + '"/>';
    }
    return s;
  }
  function miniFace(cls) {
    return '<svg viewBox="17 4 66 54" aria-hidden="true"><defs><linearGradient id="fbSteelM' + cls + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#e3e9f1"/><stop offset="1" stop-color="#b7c3d2"/></linearGradient></defs>' +
      faceSVG().replace(/url\(#fbSteel\)/g, 'url(#fbSteelM' + cls + ')') + '</svg>';
  }

  var ICON = {
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h13M13 6l6 6-6 6"/></svg>',
    gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
    lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
    test: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a9 9 0 1 0 9 9"/><path d="M12 7v5l3 2"/></svg>'
  };

  // ───────── helpers ─────────
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function api(url, body) {
    var o = { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' } };
    if (body !== undefined) { o.method = 'POST'; o.headers['Content-Type'] = 'application/json'; o.headers['X-CSRFToken'] = CSRF; o.body = JSON.stringify(body); }
    return fetch(url, o).then(function (r) { return r.json().catch(function () { return { ok: false, message: 'Fit is not available right now.' }; }); })
      .catch(function () { return { ok: false, message: 'No connection to TapTap. Check your internet and try again.' }; });
  }
  function ss(k, v) { try { if (v === undefined) return JSON.parse(sessionStorage.getItem('fit:' + k)); sessionStorage.setItem('fit:' + k, JSON.stringify(v)); } catch (e) { return null; } }
  function safeHref(h) {
    h = String(h || '').replace(/&amp;/g, '&');
    if (/^\/(?!\/)/.test(h) || /^https?:\/\//i.test(h)) return h;
    return null;
  }

  // Small, safe markdown: paragraphs, **bold**, *italic*, `code`, [links](/path), lists, # headings, pipe tables.
  function md(src) {
    var lines = String(src || '').replace(/\r/g, '').split('\n'), out = [], list = null, para = [], i;
    function inline(t) {
      t = esc(t)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,!?:;]|$)/g, '$1<i>$2</i>')
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, txt, href) {
          var h = safeHref(href); if (!h) return txt;
          var ext = /^https?:/i.test(h) && h.indexOf(location.origin) !== 0;
          return '<a href="' + esc(h) + '"' + (ext ? ' target="_blank" rel="noopener"' : '') + '>' + txt + '</a>';
        });
      return t;
    }
    function flushP() { if (para.length) { out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; } }
    function flushL() { if (list) { out.push('<' + list.t + '>' + list.items.map(function (x) { return '<li>' + inline(x) + '</li>'; }).join('') + '</' + list.t + '>'); list = null; } }
    for (i = 0; i < lines.length; i++) {
      var l = lines[i], m;
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        flushP(); flushL();
        var rows = [l]; i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
        i--;
        var cells = function (r) { return r.trim().replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); }); };
        var h = cells(rows[0]);
        out.push('<div class="fit-tbl"><table><thead><tr>' + h.map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') + '</tr></thead><tbody>' +
          rows.slice(1).map(function (r) { return '<tr>' + cells(r).map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody></table></div>');
        continue;
      }
      if ((m = l.match(/^\s*#{1,4}\s+(.*)$/))) { flushP(); flushL(); out.push('<h4>' + inline(m[1]) + '</h4>'); continue; }
      if ((m = l.match(/^\s*[-*•]\s+(.*)$/))) { flushP(); if (!list || list.t !== 'ul') { flushL(); list = { t: 'ul', items: [] }; } list.items.push(m[1]); continue; }
      if ((m = l.match(/^\s*\d+[.)]\s+(.*)$/))) { flushP(); if (!list || list.t !== 'ol') { flushL(); list = { t: 'ol', items: [] }; } list.items.push(m[1]); continue; }
      if (!l.trim()) { flushP(); flushL(); continue; }
      flushL(); para.push(l);
    }
    flushP(); flushL();
    return out.join('');
  }

  // ───────── build the launcher ─────────
  root.className = 'fit';
  root.innerHTML = '<button class="fit-btn" type="button" aria-label="Open Fit, your AI technician" aria-expanded="false">' + botSVG() + '</button>';
  var btn = root.querySelector('.fit-btn');
  var brand = root.dataset.color; if (brand) document.documentElement.style.setProperty('--fit-brand', brand);

  function setMood(m, ms) {
    ['is-thinking', 'is-talking', 'is-happy', 'is-sad', 'is-alert', 'is-bad'].forEach(function (c) { if (c !== 'is-' + m) root.classList.remove(c); });
    S.mood = m || '';
    if (m) root.classList.add('is-' + m);
    clearTimeout(S.moodT);
    if (m && ms) S.moodT = setTimeout(function () { root.classList.remove('is-' + m); S.mood = ''; restAlert(); }, ms);
  }
  function play(cls, ms) {
    if (REDUCED) return;
    root.classList.remove(cls); void root.offsetWidth; root.classList.add(cls);
    setTimeout(function () { root.classList.remove(cls); }, ms);
  }
  function hop() { play('is-hop', 650); }
  function blink() {
    if (S.asleep || REDUCED) return;
    root.classList.add('blink'); setTimeout(function () { root.classList.remove('blink'); }, 170);
  }
  (function blinkLoop() {
    setTimeout(function () { blink(); if (Math.random() < .22) setTimeout(blink, 260); blinkLoop(); }, 2200 + Math.random() * 4200);
  })();

  // Eyes follow the pointer; the head leans a little towards it.
  var eyeRaf = 0, px = 0, py = 0;
  function aim() {
    eyeRaf = 0;
    var r = btn.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height * .36;
    var dx = px - cx, dy = py - cy, d = Math.max(1, Math.hypot(dx, dy)), k = Math.min(1, d / 160);
    root.style.setProperty('--ex', (dx / d * 2.6 * k).toFixed(2) + 'px');
    root.style.setProperty('--ey', (dy / d * 1.9 * k).toFixed(2) + 'px');
    root.style.setProperty('--tilt', (dx / d * 5 * k).toFixed(1) + 'deg');
  }
  document.addEventListener('pointermove', function (e) {
    px = e.clientX; py = e.clientY; S.lastMove = Date.now();
    if (S.asleep) wake();
    if (!eyeRaf && !REDUCED) eyeRaf = requestAnimationFrame(aim);
  }, { passive: true });

  btn.addEventListener('pointerenter', function () { if (!S.open && !S.busy) { play('is-wave', 1900); } });
  btn.addEventListener('click', function () { S.open ? close() : open(); });

  // A life of its own: little routines while nobody is talking to it.
  function idle() {
    var wait = 7000 + Math.random() * 9000;
    setTimeout(function () {
      if (!S.open && !S.busy && !S.asleep && !document.hidden && !S.mood) {
        var r = Math.random();
        if (r < .3) { play('is-look', 1300); root.style.setProperty('--ex', '-2.6px'); setTimeout(function () { root.style.setProperty('--ex', '2.6px'); }, 650); }
        else if (r < .55) play('is-twirl', 1450);
        else if (r < .78) play('is-fix', 2400);
        else if (r < .9) play('is-wave', 1900);
        else hop();
      }
      if (!S.open && !S.asleep && Date.now() - S.lastMove > 75000) sleep();
      idle();
    }, wait);
  }
  function sleep() { if (REDUCED) return; S.asleep = true; root.classList.add('is-sleep'); }
  function wake() { if (!S.asleep) return; S.asleep = false; root.classList.remove('is-sleep'); hop(); blink(); }
  idle();

  // ───────── insights bubble & badge ─────────
  function restAlert() {
    var ins = (S.state && S.state.insights) || [];
    var seen = ss('seen') || [];
    var fresh = ins.filter(function (x) { return seen.indexOf(x.text) < 0; });
    root.classList.toggle('is-alert', !S.open && fresh.length > 0 && !S.mood);
    root.classList.toggle('is-bad', !S.open && fresh.some(function (x) { return x.level === 'bad'; }) && !S.mood);
    var b = root.querySelector('.fit-badge');
    if (fresh.length && !S.open) {
      if (!b) { b = document.createElement('span'); b.className = 'fit-badge'; root.appendChild(b); }
      b.textContent = fresh.length;
    } else if (b) b.remove();
    return fresh;
  }
  function say(ins) {
    var old = root.querySelector('.fit-say'); if (old) old.remove();
    var el = document.createElement('div');
    el.className = 'fit-say ' + (ins.level || '');
    el.setAttribute('role', 'status');
    el.innerHTML = '<button class="fit-say-x" type="button" aria-label="Hide">×</button><b>Fit noticed</b>' + esc(ins.text);
    el.addEventListener('click', function (e) {
      if (e.target.classList.contains('fit-say-x')) { el.remove(); markSeen([ins]); restAlert(); return; }
      el.remove(); open(ins.ask);
    });
    root.appendChild(el);
    hop(); setMood('talking', 1600);
    setTimeout(function () { if (el.parentNode) el.remove(); }, 9000);
  }
  function markSeen(list) {
    var seen = ss('seen') || [];
    list.forEach(function (x) { if (seen.indexOf(x.text) < 0) seen.push(x.text); });
    ss('seen', seen.slice(-40));
  }

  function loadState() {
    return api('/fit/state/').then(function (d) {
      if (!d || !d.ok) return null;
      if (!d.enabled && !(d.settings && d.settings.can_manage)) { root.remove(); return null; }
      S.state = d; S.conv = d.conversation;
      if (d.business && d.business.color) document.documentElement.style.setProperty('--fit-brand', d.business.color);
      ss('state', { at: Date.now(), insights: d.insights || [] });
      return d;
    });
  }
  // First visit of this tab: fetch now; afterwards reuse what we know for 3 minutes.
  var cached = ss('state');
  if (cached && Date.now() - cached.at < 180000) { S.state = { insights: cached.insights }; setTimeout(function () { restAlert(); }, 400); }
  else setTimeout(function () {
    loadState().then(function (d) {
      if (!d) return;
      var fresh = restAlert();
      if (fresh.length && !S.open) setTimeout(function () { say(fresh[0]); }, 1800);
    });
  }, 1500);

  // ───────── the panel ─────────
  var P = null;
  function open(ask) {
    if (S.open) { if (ask) send(ask); return; }
    S.open = true; wake();
    btn.setAttribute('aria-expanded', 'true');
    var bub = root.querySelector('.fit-say'); if (bub) bub.remove();
    P = document.createElement('section');
    P.className = 'fitp'; P.setAttribute('role', 'dialog'); P.setAttribute('aria-label', 'Fit, your AI technician');
    P.innerHTML =
      '<header class="fitp-head"><div class="fitp-ava">' + miniFace('h') + '</div>' +
        '<div class="fitp-ttl"><b>Fit</b><small>AI technician for ' + esc((S.state && S.state.business && S.state.business.name) || 'your business') + '</small></div>' +
        '<button class="fitp-ib" data-a="new" type="button" title="New conversation" aria-label="New conversation">' + ICON.plus + '</button>' +
        '<button class="fitp-ib" data-a="set" type="button" title="What Fit may do" aria-label="What Fit may do" hidden>' + ICON.gear + '</button>' +
        '<button class="fitp-ib" data-a="close" type="button" title="Close" aria-label="Close Fit">' + ICON.x + '</button></header>' +
      '<div class="fitp-scope">' + ICON.lock + '<span>Fit only sees this business, and only what your role can open.</span></div>' +
      '<div class="fitp-body" aria-live="polite"></div>' +
      '<div class="fitp-chips"></div>' +
      '<form class="fitp-form"><textarea rows="1" placeholder="Ask Fit about vouchers, agents, money or your network…" aria-label="Message Fit"></textarea>' +
        '<button class="fitp-send" type="submit" aria-label="Send">' + ICON.send + '</button></form>';
    document.body.appendChild(P);
    var ta = P.querySelector('textarea');
    P.addEventListener('click', onPanelClick);
    P.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
    P.querySelector('form').addEventListener('submit', function (e) { e.preventDefault(); send(ta.value); });
    ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(ta.value); } });
    ta.addEventListener('input', function () { ta.style.height = 'auto'; ta.style.height = Math.min(120, ta.scrollHeight) + 'px'; });
    restAlert();
    body().innerHTML = '<div class="fit-work"><i></i><i></i><i></i><span>Getting ready…</span></div>';
    loadState().then(function (d) {
      if (!P) return;
      if (!d) { body().innerHTML = '<p>Fit is not available right now.</p>'; return; }
      markSeen(d.insights || []); restAlert();
      P.querySelector('[data-a="set"]').hidden = !(d.settings && d.settings.can_manage);
      render();
      if (ask) send(ask);
    });
    setTimeout(function () { ta.focus(); }, 60);
  }
  function close() {
    if (!P) return;
    P.remove(); P = null; S.open = false; S.view = 'chat';
    btn.setAttribute('aria-expanded', 'false'); btn.focus();
    Object.keys(S.polls).forEach(function (k) { clearTimeout(S.polls[k]); }); S.polls = {};
    restAlert();
  }
  function body() { return P.querySelector('.fitp-body'); }
  function scroll() { var b = body(); b.scrollTop = b.scrollHeight; }
  function face(m, ms) {
    var a = P && P.querySelector('.fitp-ava'); if (!a) return;
    a.className = 'fitp-ava' + (m ? ' is-' + m : '');
    if (ms) setTimeout(function () { if (a) a.className = 'fitp-ava'; }, ms);
  }

  function render() {
    var d = S.state, b = body();
    P.querySelector('[data-a="set"]').classList.toggle('on', S.view === 'settings');
    if (S.view === 'settings') { renderSettings(); return; }
    b.innerHTML = '';
    if (!d.ready) {
      b.innerHTML = '<div class="fit-hello"><h3>Fit is almost ready</h3><p>' + (d.settings.can_manage
        ? 'Add your Groq key to the server’s <code>.env</code> as <code>GROQ_API_KEY=…</code> and restart TapTap. Fit can already show you what needs attention:'
        : 'The business owner still needs to switch Fit on. Meanwhile, here is what needs attention:') + '</p></div>';
    } else if (!d.messages.length) {
      b.innerHTML = '<div class="fit-hello"><h3>Hi ' + esc(d.me.name) + ', I’m Fit.</h3><p>Ask me anything about your vouchers, agents, money or network. ' +
        'I can look things up, run tests on your routers and get changes ready — you press the button.</p></div>';
    }
    if (!d.messages.length || !d.ready) (d.insights || []).forEach(function (x) {
      var el = document.createElement('button');
      el.type = 'button'; el.className = 'fit-ins ' + (x.level || ''); el.dataset.ask = x.ask;
      el.innerHTML = '<s></s><span>' + esc(x.text) + '</span>' + (d.ready ? '<em>Ask Fit</em>' : '');
      if (!d.ready) el.disabled = true;
      b.appendChild(el);
    });
    d.messages.forEach(function (m) { addMsg(m.role, m.text, m.cards, false); });
    var chips = P.querySelector('.fitp-chips');
    chips.innerHTML = d.ready ? (d.suggestions || []).map(function (s) { return '<button type="button" data-ask="' + esc(s) + '">' + esc(s) + '</button>'; }).join('') : '';
    P.querySelector('textarea').disabled = !d.ready;
    P.querySelector('.fitp-send').disabled = !d.ready;
    scroll();
  }

  function addMsg(role, text, cards, animate) {
    var b = body(), el = document.createElement('div');
    el.className = 'fit-m ' + (role === 'user' ? 'me' : 'bot');
    if (role === 'user') { el.innerHTML = '<div class="fit-txt"></div>'; el.firstChild.textContent = text; b.appendChild(el); return el; }
    el.innerHTML = '<div class="fit-dot">' + miniFace('d') + '</div><div class="fit-txt"></div>';
    b.appendChild(el);
    var tx = el.querySelector('.fit-txt');
    var after = function () {
      var c = cards || {};
      (c.tests || []).forEach(function (t) { tx.appendChild(testCard(t)); });
      (c.actions || []).forEach(function (a) { tx.appendChild(actionCard(a)); });
      scroll();
    };
    if (!animate || REDUCED) { tx.innerHTML = md(text); after(); return el; }
    // Typewriter, while both faces talk.
    var n = 0, step = Math.max(2, Math.ceil(text.length / 70));
    setMood('talking'); face('talking');
    (function tick() {
      n = Math.min(text.length, n + step);
      tx.innerHTML = md(text.slice(0, n));
      scroll();
      if (n < text.length) setTimeout(tick, 16);
      else { setMood(''); face(''); after(); }
    })();
    return el;
  }

  function actionCard(a) {
    var el = document.createElement('div');
    el.className = 'fit-wo ' + (a.danger || 'ok') + ' s-' + a.status;
    el.dataset.id = a.id;
    var st = { proposed: 'Waiting for you', done: 'Done', failed: 'Did not work', dismissed: 'Dismissed', expired: 'Expired — ask again' }[a.status] || a.status;
    var can = S.state && S.state.settings && S.state.settings.can_manage;
    var h = '<div class="fit-wo-top"><span>' + esc(a.group_label) + '</span><span class="fit-wo-st">' + esc(st) + '</span></div>' +
      '<b>' + esc(a.title) + '</b><p>' + esc(a.detail) + '</p>';
    if (a.status === 'proposed' && !a.authorized) {
      h += '<div class="fit-wo-lock">' + ICON.lock + '<span>' + (can ? 'You have not allowed Fit to make “' + esc(a.group_label) + '” changes yet.'
        : 'The business owner has not allowed Fit to make “' + esc(a.group_label) + '” changes.') + '</span></div>';
    }
    h += '<div class="fit-wo-row">';
    if (a.status === 'proposed') {
      if (a.authorized) h += '<button class="fit-b go" type="button" data-run="' + a.id + '">Take action</button>';
      else if (can) h += '<button class="fit-b go" type="button" data-allow="' + esc(a.group) + '">Allow ' + esc(a.group_label) + '</button>';
      h += '<button class="fit-b" type="button" data-dismiss="' + a.id + '">Dismiss</button>';
    }
    var href = safeHref(a.link);
    if (href) h += '<a class="fit-b" href="' + esc(href) + '">Open</a>';
    h += '</div>';
    if (a.result) h += '<div class="fit-wo-res">' + esc(a.result) + '</div>';
    el.innerHTML = h;
    el._card = a;
    return el;
  }

  function testCard(t) {
    var el = document.createElement('div');
    el.className = 'fit-test ' + t.status;
    el.innerHTML = ICON.test + '<div><b>' + esc(t.kind) + (t.target ? ' · ' + esc(t.target) : '') + '</b><small>' + esc(t.router) + ' — ' +
      esc(t.summary || (t.status === 'waiting' ? 'Waiting for the router…' : t.status)) + '</small></div>';
    if (t.status === 'waiting' || t.status === 'running') pollTest(t, el, 0);
    return el;
  }
  function pollTest(t, el, n) {
    if (n > 40 || !P) return;
    S.polls[t.id] = setTimeout(function () {
      api('/network-tools/test/' + t.id + '/').then(function (d) {
        if (!d || !d.ok || !el.isConnected) return;
        var x = d.test;
        el.className = 'fit-test ' + x.status;
        el.querySelector('small').textContent = x.router + ' — ' + (x.summary || x.status);
        if (x.status === 'waiting' || x.status === 'running') pollTest(t, el, n + 1);
        else if (x.status === 'done') { hop(); setMood('happy', 2500); }
      });
    }, 3000);
  }

  var WORK = [
    [/agent|sales|sold/i, ['Reading agent sales…', 'Comparing with last period…']],
    [/financ|money|profit|revenue|expense|target/i, ['Opening the books…', 'Adding up profit and costs…']],
    [/router|network|offline|internet|ping|speed|test|wifi|wi-fi/i, ['Checking your routers…', 'Talking to the MikroTik…', 'Reading live data…']],
    [/report|customer|complain/i, ['Opening customer reports…', 'Re-checking each voucher…']],
    [/voucher|code|freeze|warn|block|time/i, ['Looking up the voucher…', 'Reading its history…']]
  ];
  function workText(q) {
    for (var i = 0; i < WORK.length; i++) if (WORK[i][0].test(q)) return WORK[i][1];
    return ['Thinking…', 'Checking your data…'];
  }

  function send(text) {
    text = String(text || '').trim();
    if (!text || S.busy || !P) return;
    if (!S.state || !S.state.ready) return;
    var ta = P.querySelector('textarea'); ta.value = ''; ta.style.height = 'auto';
    if (S.view !== 'chat') { S.view = 'chat'; render(); }
    var hello = body().querySelectorAll('.fit-hello, .fit-ins'); for (var i = 0; i < hello.length; i++) hello[i].remove();
    addMsg('user', text);
    var w = document.createElement('div'), phr = workText(text), k = 0;
    w.className = 'fit-work'; w.innerHTML = '<i></i><i></i><i></i><span>' + esc(phr[0]) + '</span>';
    body().appendChild(w); scroll();
    var rot = setInterval(function () { k = (k + 1) % phr.length; var s = w.querySelector('span'); if (s) s.textContent = phr[k]; }, 1800);
    S.busy = true; setMood('thinking'); face('thinking');
    P.querySelector('.fitp-send').disabled = true;
    api('/fit/ask/', { message: text, conversation: S.conv, page: { path: location.pathname, title: document.title } }).then(function (d) {
      clearInterval(rot); w.remove(); S.busy = false; setMood(''); face('');
      if (!P) return;
      P.querySelector('.fitp-send').disabled = false;
      if (!d || !d.ok) { addMsg('assistant', (d && d.message) || 'Something went wrong.', null, true); setMood('sad', 3000); return; }
      S.conv = d.conversation;
      S.state.messages.push({ role: 'user', text: text }, { role: 'assistant', text: d.reply, cards: d.cards });
      addMsg('assistant', d.reply, d.cards, true);
      var mood = d.mood === 'alert' ? 'alert' : d.mood;
      setTimeout(function () { if (mood) setMood(mood, 3500); if (mood === 'happy') hop(); }, 300);
      P.querySelector('textarea').focus();
    });
  }

  function onPanelClick(e) {
    var t = e.target.closest('button, a'); if (!t) return;
    var a = t.dataset.a;
    if (a === 'close') return close();
    if (a === 'new') {
      api('/fit/new/', {}).then(function (d) { if (d && d.ok) { S.conv = d.conversation; S.state.messages = []; S.view = 'chat'; render(); } });
      return;
    }
    if (a === 'set') { S.view = S.view === 'settings' ? 'chat' : 'settings'; render(); return; }
    if (t.dataset.ask) return send(t.dataset.ask);
    if (t.dataset.allow) return allowGroup(t.dataset.allow, t);
    if (t.dataset.dismiss) return decide(t, 'dismiss');
    if (t.dataset.run) {
      var card = t.closest('.fit-wo');
      if (card.classList.contains('danger') && !t.classList.contains('confirm')) {
        t.classList.add('confirm'); t.textContent = 'Yes, do it';
        setTimeout(function () { if (t.isConnected) { t.classList.remove('confirm'); t.textContent = 'Take action'; } }, 4000);
        return;
      }
      return decide(t, 'run');
    }
  }

  function decide(t, decision) {
    var card = t.closest('.fit-wo'), id = card.dataset.id;
    card.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    if (decision === 'run') { t.textContent = 'Working…'; setMood('thinking'); play('is-fix', 2400); }
    api('/fit/action/' + id + '/', { decision: decision }).then(function (d) {
      setMood('');
      var c = (d && d.card) || Object.assign({}, card._card, { status: 'failed', result: (d && d.message) || 'It did not work.' });
      if (d && !d.card && card._card) c.status = card._card.status;
      if (d && d.message && !c.result) c.result = d.message;
      var fresh = actionCard(c); card.replaceWith(fresh);
      remember(c);
      if (decision === 'run') {
        if (d && d.ok) { hop(); setMood('happy', 3000); face('happy', 2500); } else { setMood('sad', 3000); }
      }
    });
  }
  function remember(c) {
    (S.state.messages || []).forEach(function (m) {
      (m.cards && m.cards.actions || []).forEach(function (x, i, arr) { if (x.id === c.id) arr[i] = c; });
    });
  }

  function allowGroup(group, t) {
    t.disabled = true;
    var allowed = (S.state.settings.allowed || []).slice();
    if (allowed.indexOf(group) < 0) allowed.push(group);
    api('/fit/settings/', { allowed: allowed }).then(function (d) {
      if (!d || !d.ok) { t.disabled = false; return; }
      S.state.settings = d.settings;
      body().querySelectorAll('.fit-wo').forEach(function (el) {
        var c = el._card; if (!c || c.group !== group) return;
        c.authorized = true; var fresh = actionCard(c); el.replaceWith(fresh); remember(c);
      });
      blink(); setMood('happy', 1500);
    });
  }

  function renderSettings() {
    var s = S.state.settings, b = body();
    var h = '<div class="fit-set"><h3>What Fit may do</h3><p>Fit always asks first: every change shows a card and only happens when someone presses ' +
      '<b>Take action</b> — and only if their own role allows it. Switch off a group to make Fit read-only for it.</p>';
    h += tog('enabled', 'Fit is on for this business', 'Off hides Fit from your team. You can still open it here.', s.enabled);
    s.groups.forEach(function (g) { h += tog('g:' + g.key, g.label, g.help, s.allowed.indexOf(g.key) >= 0); });
    h += tog('auto_close_reports', 'Close fixed customer reports by itself', 'When a reported voucher works again after a fix, Fit marks the report resolved with a note. Needs “Customer reports”.', s.auto_close_reports);
    b.innerHTML = h + '</div>';
    P.querySelector('.fitp-chips').innerHTML = '';
    b.querySelectorAll('input').forEach(function (inp) { inp.addEventListener('change', saveSettings); });
  }
  function tog(key, label, help, on) {
    return '<label class="fit-tog"><input type="checkbox" data-k="' + esc(key) + '"' + (on ? ' checked' : '') + '><span class="fit-sw"></span>' +
      '<span><b>' + esc(label) + '</b><small>' + esc(help) + '</small></span></label>';
  }
  function saveSettings() {
    var body_ = { allowed: [] };
    body().querySelectorAll('input[data-k]').forEach(function (i) {
      var k = i.dataset.k;
      if (k.indexOf('g:') === 0) { if (i.checked) body_.allowed.push(k.slice(2)); }
      else body_[k] = i.checked;
    });
    api('/fit/settings/', body_).then(function (d) { if (d && d.ok) { S.state.settings = d.settings; S.state.enabled = d.settings.enabled; blink(); } });
  }

  // Keyboard: "/" then "f" opens Fit from anywhere (not while typing).
  var lastSlash = 0;
  document.addEventListener('keydown', function (e) {
    var tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
    if (e.key === '/') { lastSlash = Date.now(); return; }
    if ((e.key === 'f' || e.key === 'F') && Date.now() - lastSlash < 800) { e.preventDefault(); open(); }
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) S.lastMove = Date.now(); });
})();
