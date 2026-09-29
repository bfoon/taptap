/* TapTap live chat — floating chat on every page.
   Team room · direct messages · TapTap support. Polls /chat/state/ (fast while open, slower
   in the background), shows on-screen toasts with a chime, desktop alerts when the tab is
   hidden, a counter in the tab title, and "share this page" links. */
(function () {
  'use strict';
  var root = document.getElementById('ttChat'); if (!root) return;
  var CSRF = root.dataset.csrf || '';
  var FULL = root.dataset.full === '1';
  var S = { open: false, view: 'list', thread: null, threads: [], people: [], me: {}, prefs: {}, top: 0, msgs: [], seen: 0,
            attach: null, first: true, lastTyping: 0, timer: null, big: FULL, filter: '' };
  var COLORS = ['#1769e0', '#7c3aed', '#18a66a', '#e5484d', '#f59e0b', '#0ea5a4', '#db2777', '#65a30d', '#2563eb', '#ea580c'];
  var EMOJI = ['👍', '🙏', '😀', '😂', '🎉', '✅', '❤️', '🔥', '👀', '💰', '📶', '⚠️'];
  var ICON = {
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/><path d="M8 11h.01M12 11h.01M16 11h.01"/></svg>',
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    back: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>',
    gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>',
    smile: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M8 14s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01"/></svg>',
    link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/></svg>',
    bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0"/></svg>',
    bellx: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13.7 21a2 2 0 0 1-3.4 0M18.6 13A17 17 0 0 1 18 8M6.3 6.3A6 6 0 0 0 6 8c0 7-3 9-3 9h14M18 8a6 6 0 0 0-9.3-5M1 1l22 22"/></svg>',
    expand: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>'
  };

  // ───────── helpers ─────────
  function $(s, el) { return (el || root).querySelector(s); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function color(s) { var h = 0; s = String(s || ''); for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return COLORS[h % COLORS.length]; }
  function av(ini, name, on, cls) { return '<span class="ttc-av' + (on ? ' on' : '') + (cls ? ' ' + cls : '') + '" style="background:' + color(name) + '">' + esc(ini) + '</span>'; }
  function linkify(t) {
    return esc(t).replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|\s)(\/[a-z0-9][\w\-\/?=&.%]*)/gi, '$1<a href="$2">$2</a>')
      .replace(/@([\w.\-]+)/g, '<b>@$1</b>');
  }
  function when(iso, full) {
    if (!iso) return ''; var d = new Date(iso), n = new Date(), sameDay = d.toDateString() === n.toDateString();
    var hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (full || sameDay) return hm;
    var y = new Date(n); y.setDate(n.getDate() - 1);
    return d.toDateString() === y.toDateString() ? 'Yesterday' : d.toLocaleDateString([], { day: 'numeric', month: 'short' });
  }
  function dayLabel(iso) {
    var d = new Date(iso), n = new Date(), y = new Date(n); y.setDate(n.getDate() - 1);
    if (d.toDateString() === n.toDateString()) return 'Today';
    if (d.toDateString() === y.toDateString()) return 'Yesterday';
    return d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' });
  }
  function api(url, body) {
    var o = body === undefined ? { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' } }
      : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }, body: JSON.stringify(body) };
    return fetch(url, o).then(function (r) { return r.json().catch(function () { return { ok: false, message: 'Chat is not available right now.' }; }); });
  }
  function store(k, v) { try { if (v === undefined) return JSON.parse(localStorage.getItem('ttc:' + k)); localStorage.setItem('ttc:' + k, JSON.stringify(v)); } catch (e) { return null; } }

  // ───────── sound: a soft two-note chime made on the fly (no audio file needed) ─────────
  var AC = null;
  function chime(kind) {
    if (!S.prefs.sound) return;
    try {
      AC = AC || new (window.AudioContext || window.webkitAudioContext)();
      var notes = kind === 'support' ? [660, 880, 1175] : kind === 'mention' ? [880, 660, 880] : [740, 988];
      notes.forEach(function (f, i) {
        var o = AC.createOscillator(), g = AC.createGain(), t = AC.currentTime + i * 0.13;
        o.type = 'sine'; o.frequency.value = f; o.connect(g); g.connect(AC.destination);
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.18, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.35);
        o.start(t); o.stop(t + 0.4);
      });
    } catch (e) {}
  }
  document.addEventListener('pointerdown', function unlock() { try { AC = AC || new (window.AudioContext || window.webkitAudioContext)(); AC.resume(); } catch (e) {} document.removeEventListener('pointerdown', unlock); });

  // ───────── skeleton ─────────
  root.className = 'ttc' + (S.big ? ' big' : '');
  root.innerHTML = '<div class="ttc-toasts" aria-live="polite"></div>' +
    '<div class="ttc-panel ttc-hidden" role="dialog" aria-label="Chat"><div class="ttc-head"></div><div class="ttc-body"></div><div class="ttc-typing ttc-hidden"></div><div class="ttc-foot"></div></div>' +
    '<button class="ttc-launch" type="button" aria-label="Open chat">' + ICON.chat + '<span class="ttc-badge ttc-hidden">0</span></button>';
  var panel = $('.ttc-panel'), head = $('.ttc-head'), body = $('.ttc-body'), foot = $('.ttc-foot'), typing = $('.ttc-typing'), launch = $('.ttc-launch'), badge = $('.ttc-badge'), toasts = $('.ttc-toasts');
  if (FULL) { launch.classList.add('ttc-hidden'); }

  function totalUnread() { return S.threads.reduce(function (a, t) { return a + (t.muted ? 0 : t.unread); }, 0); }
  var baseTitle = document.title.replace(/^\(\d+\)\s*/, '');
  function updateBadge() {
    var n = totalUnread();
    badge.textContent = n > 99 ? '99+' : n; badge.classList.toggle('ttc-hidden', !n);
    document.title = (n ? '(' + n + ') ' : '') + baseTitle;
  }

  function setOpen(v) {
    S.open = v || FULL; panel.classList.toggle('ttc-hidden', !S.open); root.classList.toggle('is-open', S.open);
    launch.setAttribute('aria-label', S.open ? 'Close chat' : 'Open chat'); launch.innerHTML = (S.open ? ICON.x : ICON.chat) + badge.outerHTML;
    badge = $('.ttc-badge'); updateBadge(); store('open', S.open);
    if (S.open) { if (S.view === 'thread' && S.thread) openThread(S.thread.id, true); else render(); }
    schedule(200);
  }
  launch.addEventListener('click', function () { setOpen(!S.open); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && S.open && !FULL) { if ($('.ttc-pop')) { $('.ttc-pop').remove(); return; } setOpen(false); } });

  // ───────── rendering ─────────
  function headHTML(title, sub, left, right) {
    return (left || '') + '<h4>' + esc(title) + (sub ? '<small>' + esc(sub) + '</small>' : '') + '</h4>' + (right || '');
  }
  function render() {
    if (S.view === 'settings') return renderSettings();
    if (S.view === 'thread') return renderThread();
    typing.classList.add('ttc-hidden'); foot.innerHTML = '';
    head.innerHTML = headHTML('Chat', S.me.agent ? 'Team · Support inbox' : 'Your team and TapTap support', '',
      '<button class="ttc-ib" data-a="settings" title="Chat settings">' + ICON.gear + '</button>' +
      (FULL ? '' : '<button class="ttc-ib" data-a="big" title="Bigger">' + ICON.expand + '</button><button class="ttc-ib" data-a="close" title="Close">' + ICON.x + '</button>'));
    var f = S.filter.toLowerCase(), h = '<div class="ttc-search"><input type="search" placeholder="Search chats or people" value="' + esc(S.filter) + '" aria-label="Search chats"></div>';
    var ppl = S.people.filter(function (p) { return !f || p.name.toLowerCase().indexOf(f) >= 0; });
    if (ppl.length) h += '<div class="ttc-people">' + ppl.map(function (p) { return '<button class="ttc-person" data-u="' + p.id + '" title="Message ' + esc(p.name) + '">' + av(p.initials, p.name, p.online) + '<span>' + esc(p.name.split(' ')[0]) + '</span></button>'; }).join('') + '</div>';
    var mine = S.threads.filter(function (t) { return !t.inbox && (!f || t.title.toLowerCase().indexOf(f) >= 0); });
    var inbox = S.threads.filter(function (t) { return t.inbox && (!f || t.title.toLowerCase().indexOf(f) >= 0); });
    function row(t) {
      var ini = t.kind === 'team' ? '👥' : t.initials;
      return '<button class="ttc-row' + (t.unread ? ' unread' : '') + '" data-t="' + t.id + '">' +
        (t.kind === 'support' && !t.inbox ? '<span class="ttc-av' + (t.online ? ' on' : '') + '" style="background:linear-gradient(135deg,#7c3aed,#1769e0)">TT</span>' : av(ini, t.title, t.online)) +
        '<span class="ttc-mid"><b>' + esc(t.title) + (t.inbox && t.status ? ' <span class="ttc-tag ' + t.status + '">' + (t.status === 'solved' ? 'Solved' : 'Open') + '</span>' : '') + '</b><small>' + esc(t.last || t.sub || 'Say hello 👋') + '</small></span>' +
        '<span class="ttc-end">' + esc(when(t.last_at)) + (t.unread ? '<span class="ttc-count' + (t.muted ? ' muted' : '') + '">' + t.unread + '</span>' : '') + '</span></button>';
    }
    if (mine.length || !S.me.agent) h += '<div class="ttc-sec">Conversations</div>' + (mine.length ? mine.map(row).join('') : '<div class="ttc-empty">No conversations.</div>');
    if (S.me.agent) h += '<div class="ttc-sec">Support inbox · all businesses</div>' + (inbox.length ? inbox.map(row).join('') : '<div class="ttc-empty">No support conversations yet.</div>');
    body.innerHTML = h;
    var inp = $('.ttc-search input', body);
    inp.addEventListener('input', function () { S.filter = inp.value; var pos = inp.selectionStart; render(); var n = $('.ttc-search input', body); n.focus(); n.setSelectionRange(pos, pos); });
  }

  function renderThread() {
    var t = S.thread; if (!t) { S.view = 'list'; return render(); }
    var info = S.threads.filter(function (x) { return x.id === t.id; })[0] || {};
    var right = '<button class="ttc-ib" data-a="mute" title="' + (info.muted ? 'Unmute' : 'Mute') + ' this chat">' + (info.muted ? ICON.bellx : ICON.bell) + '</button>';
    if (t.agent_view) right = '<button class="ttc-ib" data-a="' + (t.status === 'solved' ? 'reopen' : 'solve') + '" title="' + (t.status === 'solved' ? 'Reopen' : 'Mark solved') + '">' + ICON.check + '</button>' + right;
    if (!FULL) right += '<button class="ttc-ib" data-a="close" title="Close">' + ICON.x + '</button>';
    head.innerHTML = headHTML(t.title, (info.sub || t.sub || ''), '<button class="ttc-ib" data-a="back" title="Back">' + ICON.back + '</button>', right);
    var h = '<div class="ttc-msgs">', prev = null, lastDay = '';
    if (S.more) h += '<button class="ttc-sys" data-a="older" style="border:0;background:none;cursor:pointer;text-decoration:underline">Show older messages</button>';
    if (!S.msgs.length) h += '<div class="ttc-empty">' + (t.kind === 'support' && !t.agent_view ? 'Ask TapTap anything — routers, vouchers, billing. We usually reply within minutes during the day.' : 'No messages yet. Say hello 👋') + '</div>';
    var lastMine = 0; S.msgs.forEach(function (m) { if (m.mine) lastMine = m.id; });
    S.msgs.forEach(function (m) {
      var day = dayLabel(m.at); if (day !== lastDay) { h += '<div class="ttc-day">' + day + '</div>'; lastDay = day; prev = null; }
      if (m.system) { h += '<div class="ttc-sys">' + linkify(m.body) + '</div>'; prev = null; return; }
      var cont = prev && prev.sender === m.sender && prev.mine === m.mine && (new Date(m.at) - new Date(prev.at)) < 5 * 60000;
      if (!m.mine && !cont && t.kind !== 'direct') h += '<div class="ttc-who">' + esc(m.sender) + '</div>';
      h += '<div class="ttc-m' + (m.mine ? ' me' : '') + (m.agent ? ' agent' : '') + (m.mention_me ? ' hot' : '') + (cont ? ' cont' : '') + '" data-id="' + m.id + '">' +
        (m.mine ? '' : av(m.initials, m.sender)) + '<div class="ttc-b">' + linkify(m.body) +
        (m.page_url ? '<a class="ttc-page" href="' + esc(m.page_url) + '">🔗 <span><b>' + esc(m.page_title || 'Shared page') + '</b><br><small>' + esc(m.page_url) + '</small></span></a>' : '') +
        '</div></div>';
      var next = S.msgs[S.msgs.indexOf(m) + 1];
      if (!next || next.sender !== m.sender || next.system || (new Date(next.at) - new Date(m.at)) >= 5 * 60000)
        h += '<div class="ttc-meta' + (m.mine ? ' me' : '') + '">' + when(m.at, true) + (m.mine && m.id === lastMine && t.kind === 'direct' ? (S.seen >= m.id ? ' · Seen' : ' · Sent') : '') + '</div>';
      prev = m;
    });
    body.innerHTML = h + '</div>';
    if (!$('.ttc-comp', foot)) {
      foot.innerHTML = '<div class="ttc-attach ttc-hidden"></div><div class="ttc-comp">' +
        '<button class="ttc-tb" data-a="emoji" title="Emoji">' + ICON.smile + '</button>' +
        '<button class="ttc-tb" data-a="share" title="Share the page you are on">' + ICON.link + '</button>' +
        '<textarea rows="1" placeholder="' + (t.kind === 'team' ? 'Message the team (@name)' : 'Write a message') + '" aria-label="Message"></textarea>' +
        '<button class="ttc-tb ttc-send" data-a="send" title="Send (Enter)">' + ICON.send + '</button></div>';
      wireComposer();
    }
    body.scrollTop = body.scrollHeight;
  }

  function renderSettings() {
    typing.classList.add('ttc-hidden'); foot.innerHTML = '';
    head.innerHTML = headHTML('Chat settings', 'Sounds, pop-ups and email', '<button class="ttc-ib" data-a="back" title="Back">' + ICON.back + '</button>', FULL ? '' : '<button class="ttc-ib" data-a="close" title="Close">' + ICON.x + '</button>');
    var p = S.prefs, perm = window.Notification ? Notification.permission : 'unsupported';
    body.innerHTML = '<div class="ttc-set">' +
      '<label><span>Sound<small>A soft chime for new messages</small></span><input type="checkbox" data-p="sound"' + (p.sound ? ' checked' : '') + '></label>' +
      '<label><span>Pop-ups on screen<small>Show new messages in the corner</small></span><input type="checkbox" data-p="popups"' + (p.popups ? ' checked' : '') + '></label>' +
      '<label><span>Email me unread messages<small>If I haven\'t read them after <input type="number" min="2" max="240" data-p="email_after" value="' + (p.email_after || 10) + '" style="width:56px"> minutes</small></span><input type="checkbox" data-p="email_missed"' + (p.email_missed ? ' checked' : '') + '></label>' +
      '<label><span>Desktop alerts<small>' + (perm === 'granted' ? 'On — you get alerts even when this tab is in the background' : perm === 'denied' ? 'Blocked in your browser settings' : perm === 'unsupported' ? 'Not supported by this browser' : 'Get alerts when this tab is in the background') + '</small></span>' +
      (perm === 'default' ? '<button class="btn btn-sm btn-primary" data-a="notif">Turn on</button>' : '') + '</label>' +
      '<p class="mt-3 mb-0" style="font-size:12px;color:var(--c-mut)">Tip: <b>Enter</b> sends, <b>Shift+Enter</b> adds a line. Type <b>@name</b> in the team room to alert someone even if they muted it.</p>' +
      '<button class="btn btn-sm btn-outline-secondary mt-3" data-a="test">Play test sound</button></div>';
    body.querySelectorAll('[data-p]').forEach(function (i) {
      i.addEventListener('change', function () { var d = {}; d[i.dataset.p] = i.type === 'checkbox' ? i.checked : +i.value; S.prefs[i.dataset.p] = d[i.dataset.p]; api('/chat/settings/', d); });
    });
  }

  // ───────── actions ─────────
  panel.addEventListener('click', function (e) {
    var b = e.target.closest('[data-a],[data-t],[data-u]'); if (!b) return;
    if (b.dataset.t) return openThread(+b.dataset.t);
    if (b.dataset.u) return api('/chat/direct/', { user: +b.dataset.u }).then(function (d) { if (d.ok) openThread(d.thread); else toast({ sender: 'Chat', body: d.message }); });
    var a = b.dataset.a;
    if (a === 'close') setOpen(false);
    else if (a === 'big') { S.big = !S.big; root.classList.toggle('big', S.big); }
    else if (a === 'back') { S.view = 'list'; S.thread = null; store('thread', null); render(); poll(); }
    else if (a === 'settings') { S.view = 'settings'; render(); }
    else if (a === 'test') { var s = S.prefs.sound; S.prefs.sound = true; chime('mention'); S.prefs.sound = s; }
    else if (a === 'notif') Notification.requestPermission().then(render);
    else if (a === 'mute') { var info = S.threads.filter(function (x) { return x.id === S.thread.id; })[0] || {}; info.muted = !info.muted; api('/chat/settings/', { thread: S.thread.id, mute: info.muted }); renderThread(); }
    else if (a === 'solve' || a === 'reopen') api('/chat/support/', { thread: S.thread.id, action: a }).then(function () { openThread(S.thread.id, true); });
    else if (a === 'older') loadOlder();
    else if (a === 'emoji') togglePop('emoji');
    else if (a === 'share') { S.attach = { url: location.pathname + location.search, title: document.title.replace(/^\(\d+\)\s*/, '').replace(/\s*—\s*TapTap$/, '') }; showAttach(); }
    else if (a === 'unattach') { S.attach = null; showAttach(); }
    else if (a === 'send') send();
  });

  function showAttach() {
    var el = $('.ttc-attach', foot); if (!el) return;
    el.classList.toggle('ttc-hidden', !S.attach);
    el.innerHTML = S.attach ? '<span>🔗 Sharing: <b>' + esc(S.attach.title) + '</b></span><button class="btn btn-sm btn-link p-0" data-a="unattach">Remove</button>' : '';
  }

  function togglePop(kind, items) {
    var old = $('.ttc-pop', foot); if (old) { var same = old.dataset.k === kind; old.remove(); if (same && kind === 'emoji') return; }
    var comp = $('.ttc-comp', foot), ta = $('textarea', foot), pop = document.createElement('div');
    pop.className = 'ttc-pop' + (kind === 'ment' ? ' ment' : ''); pop.dataset.k = kind;
    if (kind === 'emoji') pop.innerHTML = EMOJI.map(function (e) { return '<button type="button" data-e="' + e + '">' + e + '</button>'; }).join('');
    else { if (!items.length) return; pop.innerHTML = items.map(function (p) { return '<button type="button" data-m="' + esc(p.name.split(' ')[0]) + '">' + av(p.initials, p.name) + esc(p.name) + '</button>'; }).join(''); }
    comp.appendChild(pop);
    pop.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.e) insert(b.dataset.e + ' ');
      else { ta.value = ta.value.replace(/@([\w.\-]*)$/, '@' + b.dataset.m + ' '); ta.focus(); }
      pop.remove();
    });
  }
  function insert(txt) { var ta = $('textarea', foot), s = ta.selectionStart || ta.value.length; ta.value = ta.value.slice(0, s) + txt + ta.value.slice(ta.selectionEnd || s); ta.focus(); ta.selectionStart = ta.selectionEnd = s + txt.length; }

  function wireComposer() {
    var ta = $('textarea', foot);
    ta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); var pop = $('.ttc-pop.ment button', foot); if (pop) { pop.click(); return; } send(); }
    });
    ta.addEventListener('input', function () {
      ta.style.height = 'auto'; ta.style.height = Math.min(120, ta.scrollHeight) + 'px';
      var m = ta.value.slice(0, ta.selectionStart).match(/@([\w.\-]*)$/);
      if (m && S.thread && S.thread.kind !== 'direct') {
        var q = m[1].toLowerCase(); togglePop('ment', S.people.filter(function (p) { return p.name.toLowerCase().indexOf(q) >= 0; }).slice(0, 6));
      } else { var p = $('.ttc-pop.ment', foot); if (p) p.remove(); }
      if (Date.now() - S.lastTyping > 3000 && ta.value.trim()) { S.lastTyping = Date.now(); api('/chat/state/?open=' + S.thread.id + '&typing=1&since=' + S.top); }
    });
    setTimeout(function () { if (!('ontouchstart' in window)) ta.focus(); }, 50);
  }

  function send() {
    var ta = $('textarea', foot), txt = ta.value.trim(); if (!txt && !S.attach) return;
    var payload = { thread: S.thread.id, body: txt, page_url: S.attach ? S.attach.url : '', page_title: S.attach ? S.attach.title : '' };
    ta.value = ''; ta.style.height = 'auto'; S.attach = null; showAttach();
    var tmp = { id: 1e15, body: txt, mine: true, sender: S.me.name, at: new Date().toISOString(), page_url: payload.page_url, page_title: payload.page_title };
    S.msgs.push(tmp); renderThread();
    api('/chat/send/', payload).then(function (d) {
      S.msgs = S.msgs.filter(function (m) { return m !== tmp; });
      if (d.ok) { if (!S.msgs.some(function (m) { return m.id === d.message.id; })) S.msgs.push(d.message); S.top = Math.max(S.top, d.message.id); }
      else { ta.value = txt; toast({ sender: 'Not sent', body: d.message || 'Try again.' }); }
      renderThread(); poll();
    });
  }

  function openThread(id, keep) {
    S.view = 'thread'; store('thread', id); S.more = false;
    if (!keep || !S.thread || S.thread.id !== id) { S.msgs = []; foot.innerHTML = ''; }
    return api('/chat/t/' + id + '/').then(function (d) {
      if (!d.ok) { S.view = 'list'; render(); return; }
      S.thread = d.thread; S.msgs = d.messages; S.more = d.more;
      var info = S.threads.filter(function (x) { return x.id === id; })[0]; if (info) info.unread = 0;
      updateBadge(); renderThread(); schedule(300);
    });
  }
  function loadOlder() {
    var first = S.msgs[0]; if (!first) return;
    api('/chat/t/' + S.thread.id + '/?before=' + first.id).then(function (d) {
      if (!d.ok) return; var h0 = body.scrollHeight; S.msgs = d.messages.concat(S.msgs); S.more = d.more; renderThread(); body.scrollTop = body.scrollHeight - h0;
    });
  }

  // ───────── notifications ─────────
  function toast(m, kind) {
    if (!S.prefs.popups && m.id) return;
    var el = document.createElement('div');
    el.className = 'ttc-toast ' + (kind || '');
    el.innerHTML = av(m.initials || '💬', m.sender || 'Chat') + '<div><b>' + esc(m.sender) + (m.threadTitle ? ' · ' + esc(m.threadTitle) : '') + '</b><p>' + esc(m.body || m.page_title || '') + '</p></div>';
    el.addEventListener('click', function () { el.remove(); if (m.thread) { if (!S.open) setOpen(true); openThread(m.thread); } });
    toasts.appendChild(el); while (toasts.children.length > 3) toasts.firstChild.remove();
    setTimeout(function () { el.style.transition = 'opacity .4s'; el.style.opacity = '0'; setTimeout(function () { el.remove(); }, 450); }, 7000);
  }
  function desktop(m, title) {
    if (!window.Notification || Notification.permission !== 'granted' || !document.hidden) return;
    try { var n = new Notification(m.sender + ' · ' + title, { body: m.body || m.page_title || '', tag: 'ttc-' + m.thread, silent: !S.prefs.sound });
      n.onclick = function () { window.focus(); setOpen(true); openThread(m.thread); n.close(); }; } catch (e) {}
  }

  // ───────── polling ─────────
  function schedule(ms) {
    clearTimeout(S.timer);
    var wait = ms != null ? ms : (document.hidden ? 30000 : S.open ? 3500 : 12000);
    S.timer = setTimeout(poll, wait);
  }
  var polling = false;
  function poll() {
    if (polling) return; polling = true;
    var q = '/chat/state/?since=' + (S.first ? -1 : S.top) + (S.view === 'thread' && S.thread && S.open ? '&open=' + S.thread.id : '');
    api(q).then(function (d) {
      polling = false; if (!d.ok) { schedule(60000); return; }
      S.me = d.me; S.prefs = d.prefs; S.people = d.people; S.threads = d.threads;
      var incoming = S.first ? [] : d.new;
      S.first = false; S.top = Math.max(S.top, d.top);
      var viewing = S.open && S.view === 'thread' && S.thread && !document.hidden ? S.thread.id : 0, changed = false, maxHere = 0;
      incoming.forEach(function (m) {
        var t = S.threads.filter(function (x) { return x.id === m.thread; })[0] || {};
        if (m.thread === viewing) {
          if (!S.msgs.some(function (x) { return x.id === m.id; })) { S.msgs.push(m); changed = true; }
          if (m.id > maxHere) maxHere = m.id; t.unread = 0;
        }
        if (!m.notify) return;
        var kind = m.mention_me ? 'mention' : (t.kind === 'support' || t.inbox) ? 'support' : '';
        if (t.muted && !m.mention_me) return;
        if (m.thread !== viewing) { m.threadTitle = t.kind === 'direct' ? '' : t.title; toast(m, kind); desktop(m, t.title || 'Chat'); }
        chime(kind);
        if (!S.open) { launch.classList.remove('ping'); void launch.offsetWidth; launch.classList.add('ping'); }
      });
      if (maxHere) api('/chat/read/', { thread: viewing, upto: maxHere });
      if (d.seen_upto != null && d.seen_upto !== S.seen) { S.seen = d.seen_upto; changed = true; }
      updateBadge();
      if (S.open) {
        if (S.view === 'thread') {
          if (changed) renderThread();
          var names = d.typing || []; typing.classList.toggle('ttc-hidden', !names.length);
          typing.innerHTML = names.length ? '<i></i><i></i><i></i> ' + esc(names.join(', ')) + (names.length > 1 ? ' are' : ' is') + ' typing…' : '';
          var info = S.threads.filter(function (x) { return x.id === (S.thread && S.thread.id); })[0];
          var sub = head.querySelector('small'); if (info && sub) sub.textContent = info.sub || '';
        } else if (S.view === 'list' && !(document.activeElement && document.activeElement.closest && document.activeElement.closest('.ttc-search'))) render();
      }
      schedule();
    }).catch(function () { polling = false; schedule(20000); });
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { updateBadge(); schedule(100); } });

  // restore
  var wasOpen = store('open'), th = store('thread');
  if (th) { S.view = 'thread'; S.thread = { id: th }; }
  poll();
  setTimeout(function () { if (wasOpen || FULL) setOpen(true); }, 400);
  window.TapChat = { open: function (id) { setOpen(true); if (id) openThread(id); } };
})();
