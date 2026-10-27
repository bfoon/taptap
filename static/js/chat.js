/* TapTap live chat — floating chat on every page.
   Team room · direct messages · TapTap support. Polls /chat/state/ (fast while open, slower
   in the background), shows on-screen toasts with a chime, desktop alerts when the tab is
   hidden, a counter in the tab title, and "share this page" links.
   Also: photos (deleted for good after 7 days, optional view-once), and voice/video calls with secure
   screen sharing — the call runs in its own window (static/js/chat-call.js) so you can keep using TapTap;
   this file rings, shows the call pill, and applies the privacy shield / pointer on the shared tab. */
(function () {
  'use strict';
  var root = document.getElementById('ttChat'); if (!root) return;
  var CSRF = root.dataset.csrf || '';
  var FULL = root.dataset.full === '1';
  var S = { open: false, view: 'list', thread: null, threads: [], people: [], me: {}, prefs: {}, top: 0, msgs: [], seen: 0,
            attach: null, first: true, lastTyping: 0, timer: null, big: FULL, filter: '', typers: [], typingThreads: {} };
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
    phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/></svg>',
    video: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="14" height="12" rx="2"/><path d="M16 10l6-3v10l-6-3z"/></svg>',
    image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/></svg>',
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
        '<span class="ttc-mid"><b>' + esc(t.title) + (t.inbox && t.status ? ' <span class="ttc-tag ' + t.status + '">' + (t.status === 'solved' ? 'Solved' : 'Open') + '</span>' : '') + '</b>' +
        (S.typingThreads[t.id] ? '<small class="ttc-tt"><span class="ttc-mini-dots"><i></i><i></i><i></i></span> ' + esc(typingText(S.typingThreads[t.id], t)) + '</small>' : '<small>' + esc(t.last || t.sub || 'Say hello 👋') + '</small>') + '</span>' +
        '<span class="ttc-end">' + esc(when(t.last_at)) + (t.unread ? '<span class="ttc-count' + (t.muted ? ' muted' : '') + '">' + t.unread + '</span>' : '') + '</span></button>';
    }
    if (mine.length || !S.me.agent) h += '<div class="ttc-sec">Conversations</div>' + (mine.length ? mine.map(row).join('') : '<div class="ttc-empty">No conversations.</div>');
    if (S.me.agent) h += '<div class="ttc-sec">Support inbox · all businesses</div>' + (inbox.length ? inbox.map(row).join('') : '<div class="ttc-empty">No support conversations yet.</div>');
    h += '<div class="ttc-sec">Help</div><a class="ttc-row" href="/support/"><span class="ttc-av" style="background:linear-gradient(135deg,#18a66a,#0ea5a4)">?</span><span class="ttc-mid"><b>How-to guides</b><small>Add a router, fix connections, fair usage, sharing…</small></span></a>';
    body.innerHTML = h;
    var inp = $('.ttc-search input', body);
    inp.addEventListener('input', function () { S.filter = inp.value; var pos = inp.selectionStart; render(); var n = $('.ttc-search input', body); n.focus(); n.setSelectionRange(pos, pos); });
  }

  function typingText(names, t) {
    if (t && t.kind === 'direct') return 'typing…';
    return names.slice(0, 2).join(' and ') + (names.length > 2 ? ' and others' : '') + (names.length > 1 ? ' are' : ' is') + ' typing…';
  }
  // The "someone is typing" bubble at the bottom of the conversation (three bouncing dots).
  function renderTyping() {
    var box = $('.ttc-msgs', body); if (!box) return;
    var nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 90;
    box.querySelectorAll('.ttc-typing-b').forEach(function (n) { n.remove(); });
    var t = S.thread || {};
    S.typers.slice(0, 3).forEach(function (p) {
      var asTapTap = p.agent && t.kind === 'support' && !t.agent_view, name = asTapTap ? p.name + ' · TapTap Support' : p.name;
      var el = document.createElement('div'); el.className = 'ttc-typing-b';
      el.innerHTML = (t.kind !== 'direct' ? '<div class="ttc-who">' + esc(name) + '</div>' : '') +
        '<div class="ttc-m' + (asTapTap ? ' agent' : '') + '">' + (asTapTap ? '<span class="ttc-av" style="background:linear-gradient(135deg,#7c3aed,#1769e0)">TT</span>' : av(p.initials, p.name)) +
        '<div class="ttc-b ttc-dots" role="status" aria-label="' + esc(p.name) + ' is typing"><i></i><i></i><i></i></div></div>';
      box.appendChild(el);
    });
    if (nearBottom) body.scrollTop = body.scrollHeight;
  }

  function renderThread() {
    var t = S.thread; if (!t) { S.view = 'list'; return render(); }
    var info = S.threads.filter(function (x) { return x.id === t.id; })[0] || {};
    var right = (t.can_call ? '<button class="ttc-ib" data-a="call" title="Voice call">' + ICON.phone + '</button><button class="ttc-ib" data-a="vcall" title="Video call">' + ICON.video + '</button>' : '') +
      '<button class="ttc-ib" data-a="mute" title="' + (info.muted ? 'Unmute' : 'Mute') + ' this chat">' + (info.muted ? ICON.bellx : ICON.bell) + '</button>';
    if (t.agent_view) right = '<button class="ttc-ib" data-a="' + (t.status === 'solved' ? 'reopen' : 'solve') + '" title="' + (t.status === 'solved' ? 'Reopen' : 'Mark solved') + '">' + ICON.check + '</button>' + right;
    if (!FULL) right += '<button class="ttc-ib" data-a="close" title="Close">' + ICON.x + '</button>';
    head.innerHTML = headHTML(t.title, (info.sub || t.sub || ''), '<button class="ttc-ib" data-a="back" title="Back">' + ICON.back + '</button>', right);
    var h = '<div class="ttc-msgs">', prev = null, lastDay = '';
    if (S.more) h += '<button class="ttc-sys" data-a="older" style="border:0;background:none;cursor:pointer;text-decoration:underline">Show older messages</button>';
    if (!S.msgs.length) h += '<div class="ttc-empty">' + (t.kind === 'support' && !t.agent_view ? 'Ask TapTap anything — routers, vouchers, billing. We usually reply within minutes during the day.' +
      '<div style="margin-top:14px;text-align:left"><b style="font-size:12px;text-transform:uppercase;letter-spacing:.06em">Quick answers</b>' +
      [['troubleshoot-link', 'Router not connecting?'], ['add-router-link', 'Add a MikroTik router'], ['fair-usage', 'Set up fair usage'], ['shared-auto-warn', 'Stop voucher sharing']].map(function (g) {
        return '<a class="ttc-page" href="/support/guides/' + g[0] + '/">📘 <span>' + g[1] + '</span></a>'; }).join('') + '</div>' : 'No messages yet. Say hello 👋') + '</div>';
    var lastMine = 0; S.msgs.forEach(function (m) { if (m.mine) lastMine = m.id; });
    S.msgs.forEach(function (m) {
      var day = dayLabel(m.at); if (day !== lastDay) { h += '<div class="ttc-day">' + day + '</div>'; lastDay = day; prev = null; }
      if (m.system) { h += '<div class="ttc-sys">' + linkify(m.body) + '</div>'; prev = null; return; }
      var cont = prev && prev.sender === m.sender && prev.mine === m.mine && (new Date(m.at) - new Date(prev.at)) < 5 * 60000;
      if (!m.mine && !cont && t.kind !== 'direct') h += '<div class="ttc-who">' + esc(m.sender) + '</div>';
      h += '<div class="ttc-m' + (m.mine ? ' me' : '') + (m.agent ? ' agent' : '') + (m.mention_me ? ' hot' : '') + (cont ? ' cont' : '') + '" data-id="' + m.id + '">' +
        (m.mine ? '' : av(m.initials, m.sender)) + '<div class="ttc-b' + (m.image ? ' has-img' : '') + '">' + imgHTML(m) + (m.body ? '<span class="ttc-txt">' + linkify(m.body) + '</span>' : '') +
        (m.page_url ? '<a class="ttc-page" href="' + esc(m.page_url) + '">🔗 <span><b>' + esc(m.page_title || 'Shared page') + '</b><br><small>' + esc(m.page_url) + '</small></span></a>' : '') +
        '</div></div>';
      var next = S.msgs[S.msgs.indexOf(m) + 1];
      if (!next || next.sender !== m.sender || next.system || (new Date(next.at) - new Date(m.at)) >= 5 * 60000)
        h += '<div class="ttc-meta' + (m.mine ? ' me' : '') + '">' + when(m.at, true) + (m.mine && m.id === lastMine && t.kind === 'direct' ? (S.seen >= m.id ? ' · Seen' : ' · Sent') : '') + '</div>';
      prev = m;
    });
    body.innerHTML = h + '</div>';
    renderTyping();
    if (!$('.ttc-comp', foot)) {
      foot.innerHTML = '<div class="ttc-attach ttc-hidden"></div><div class="ttc-comp">' +
        '<button class="ttc-tb" data-a="emoji" title="Emoji">' + ICON.smile + '</button>' +
        '<button class="ttc-tb" data-a="share" title="Share the page you are on">' + ICON.link + '</button>' +
        '<button class="ttc-tb" data-a="photo" title="Send a photo (deleted after ' + (t.photo_days || 7) + ' days)">' + ICON.image + '</button>' +
        '<input type="file" accept="image/*" class="ttc-file" hidden>' +
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
      '<p class="mt-3 mb-0" style="font-size:12px;color:var(--c-mut)">Tip: <b>Enter</b> sends, <b>Shift+Enter</b> adds a line. Type <b>@name</b> in the team room to alert someone even if they muted it. Paste or drop a photo to send it — photos are deleted for everyone after a week. Use 📞 in a direct or support chat to call; calls ring even when sounds are off.</p>' +
      '<button class="btn btn-sm btn-outline-secondary mt-3" data-a="test">Play test sound</button></div>';
    body.querySelectorAll('[data-p]').forEach(function (i) {
      i.addEventListener('change', function () { var d = {}; d[i.dataset.p] = i.type === 'checkbox' ? i.checked : +i.value; S.prefs[i.dataset.p] = d[i.dataset.p]; api('/chat/settings/', d); });
    });
  }

  // ───────── actions ─────────
  panel.addEventListener('click', function (e) {
    var b = e.target.closest('[data-a],[data-t],[data-u],[data-img],[data-once]'); if (!b) return;
    if (b.dataset.img) return lightbox(+b.dataset.img, false);
    if (b.dataset.once) return lightbox(+b.dataset.once, true);
    if (b.dataset.t) return openThread(+b.dataset.t);
    if (b.dataset.u) return api('/chat/direct/', { user: +b.dataset.u }).then(function (d) { if (d.ok) openThread(d.thread); else toast({ sender: 'Chat', body: d.message }); });
    var a = b.dataset.a;
    if (a === 'close') setOpen(false);
    else if (a === 'big') { S.big = !S.big; root.classList.toggle('big', S.big); }
    else if (a === 'back') { S.view = 'list'; S.thread = null; S.typers = []; store('thread', null); render(); poll(); }
    else if (a === 'settings') { S.view = 'settings'; render(); }
    else if (a === 'test') { var s = S.prefs.sound; S.prefs.sound = true; chime('mention'); S.prefs.sound = s; }
    else if (a === 'notif') Notification.requestPermission().then(render);
    else if (a === 'mute') { var info = S.threads.filter(function (x) { return x.id === S.thread.id; })[0] || {}; info.muted = !info.muted; api('/chat/settings/', { thread: S.thread.id, mute: info.muted }); renderThread(); }
    else if (a === 'solve' || a === 'reopen') api('/chat/support/', { thread: S.thread.id, action: a }).then(function () { openThread(S.thread.id, true); });
    else if (a === 'older') loadOlder();
    else if (a === 'emoji') togglePop('emoji');
    else if (a === 'share') { S.attach = { url: location.pathname + location.search, title: document.title.replace(/^\(\d+\)\s*/, '').replace(/\s*—\s*TapTap$/, '') }; showAttach(); }
    else if (a === 'unattach') { S.attach = null; showAttach(); }
    else if (a === 'photo') { var fi = $('.ttc-file', foot); fi && fi.click(); }
    else if (a === 'unphoto') { clearPhoto(); }
    else if (a === 'once') { if (S.photo) { S.photo.once = !S.photo.once; showAttach(); } }
    else if (a === 'call' || a === 'vcall') startCall(a === 'vcall');
    else if (a === 'send') send();
  });

  function showAttach() {
    var el = $('.ttc-attach', foot); if (!el) return;
    el.classList.toggle('ttc-hidden', !S.attach && !S.photo);
    var h = S.attach ? '<div class="ttc-att-row"><span>🔗 Sharing: <b>' + esc(S.attach.title) + '</b></span><button class="btn btn-sm btn-link p-0" data-a="unattach">Remove</button></div>' : '';
    if (S.photo) {
      var days = (S.thread && S.thread.photo_days) || 7, direct = S.thread && S.thread.kind === 'direct';
      h += '<div class="ttc-att-photo"><img src="' + S.photo.url + '" alt=""><div><b>Photo · ' + Math.max(1, Math.round(S.photo.blob.size / 1024)) + ' KB</b>' +
        '<small>' + (S.photo.once ? '👁 Deleted as soon as it is opened' : '⏳ Deleted for everyone after ' + days + ' days') + ' · location data removed</small>' +
        (direct ? '<label class="ttc-once"><input type="checkbox" data-a="once"' + (S.photo.once ? ' checked' : '') + '> View once</label>' : '') +
        '</div><button class="btn btn-sm btn-link p-0" data-a="unphoto">Remove</button></div>';
    }
    el.innerHTML = h;
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
    var ta = $('textarea', foot), fi = $('.ttc-file', foot);
    fi.addEventListener('change', function () { if (fi.files[0]) pickPhoto(fi.files[0]); fi.value = ''; });
    ta.addEventListener('paste', function (e) {
      var f = [].slice.call((e.clipboardData && e.clipboardData.files) || []).filter(function (x) { return /^image\//.test(x.type); })[0];
      if (f) { e.preventDefault(); pickPhoto(f); }
    });
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
    var ta = $('textarea', foot), txt = ta.value.trim();
    if (S.photo) return sendPhoto(ta, txt);
    if (!txt && !S.attach) return;
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
      S.me = d.me; S.prefs = d.prefs; S.people = d.people; S.threads = d.threads; S.typingThreads = d.typing_threads || {};
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
          var ty = d.typing || [], sig = JSON.stringify(ty), was = JSON.stringify(S.typers);
          S.typers = ty; if (changed || sig !== was) renderTyping();
          var info = S.threads.filter(function (x) { return x.id === (S.thread && S.thread.id); })[0];
          var sub = head.querySelector('small'); if (info && sub) sub.textContent = info.sub || '';
        } else if (S.view === 'list' && !(document.activeElement && document.activeElement.closest && document.activeElement.closest('.ttc-search'))) render();
      }
      schedule();
    }).catch(function () { polling = false; schedule(20000); });
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { updateBadge(); schedule(100); } });


  // ───────── photos: shrunk and cleaned in the browser, deleted for good after a week ─────────
  function timeLeft(iso) {
    var ms = new Date(iso) - new Date(); if (ms <= 0) return 'now';
    var d = Math.floor(ms / 864e5), h = Math.floor(ms / 36e5);
    return d >= 1 ? d + 'd' : h >= 1 ? h + 'h' : '<1h';
  }
  function imgHTML(m) {
    var im = m.image; if (!im) return '';
    var days = (S.thread && S.thread.photo_days) || 7;
    if (im.local) return '<span class="ttc-img sending" style="aspect-ratio:' + im.w + '/' + im.h + '"><img src="' + im.local + '" alt=""><i class="ttc-spin"></i></span>';
    if (im.gone) return '<span class="ttc-img-gone">' + (im.opened ? '👁 Opened' : '🖼 Photo expired') + '<small>' + (im.opened ? 'View-once photo' : 'Photos are deleted after ' + days + ' days') + '</small></span>';
    if (im.once) return m.mine ? '<span class="ttc-img-gone">👁 View-once photo<small>Not opened yet</small></span>'
      : '<button class="ttc-img-once" data-once="' + m.id + '">👁 Tap to view once<small>It is deleted as soon as you close it</small></button>';
    return '<button class="ttc-img" data-img="' + m.id + '" style="aspect-ratio:' + im.w + '/' + im.h + '" title="Open photo"><img src="/chat/img/' + m.id + '/" loading="lazy" alt="Photo">' +
      '<span class="ttc-exp" title="Deleted for everyone on ' + esc(new Date(im.expires).toLocaleString()) + '">⏳ ' + timeLeft(im.expires) + '</span></button>';
  }
  function decode(file) {
    if (window.createImageBitmap) return createImageBitmap(file, { imageOrientation: 'from-image' }).catch(function () { return createImageBitmap(file); }).catch(function () { return viaImg(file); });
    return viaImg(file);
  }
  function viaImg(file) {
    return new Promise(function (res, rej) { var u = URL.createObjectURL(file), i = new Image(); i.onload = function () { res(i); }; i.onerror = function () { rej(new Error('bad')); }; i.src = u; });
  }
  function encode(c, q) {
    return new Promise(function (res) {
      c.toBlob(function (b) { if (b && b.type === 'image/webp') res(b); else c.toBlob(res, 'image/jpeg', q); }, 'image/webp', q);
    });
  }
  // Re-drawing on a canvas drops EXIF (GPS location, phone model) and shrinks big phone photos.
  function compress(file) {
    if (file.type === 'image/gif' && file.size < 3e6) return viaImg(file).then(function (i) { return { blob: file, w: i.naturalWidth, h: i.naturalHeight }; });
    return decode(file).then(function (bmp) {
      var bw = bmp.width || bmp.naturalWidth, bh = bmp.height || bmp.naturalHeight, sc = Math.min(1, 1920 / Math.max(bw, bh));
      var w = Math.max(1, Math.round(bw * sc)), h = Math.max(1, Math.round(bh * sc)), c = document.createElement('canvas');
      c.width = w; c.height = h; c.getContext('2d').drawImage(bmp, 0, 0, w, h);
      return encode(c, 0.82).then(function (b) { return b && b.size > 900 * 1024 ? encode(c, 0.62) : b; }).then(function (b) { return { blob: b, w: w, h: h }; });
    });
  }
  function pickPhoto(file) {
    if (!S.thread) return;
    if (!/^image\//.test(file.type)) return toast({ sender: 'Photo', body: 'Pick a photo (JPEG, PNG, WebP or GIF).' });
    compress(file).then(function (r) {
      if (!r.blob) throw new Error('x');
      clearPhoto(); S.photo = { blob: r.blob, w: r.w, h: r.h, url: URL.createObjectURL(r.blob), once: false }; showAttach();
      var ta = $('textarea', foot); ta && ta.focus();
    }).catch(function () { toast({ sender: 'Photo', body: 'This photo format cannot be opened here. Try a JPEG or PNG.' }); });
  }
  function clearPhoto() { if (S.photo) { try { URL.revokeObjectURL(S.photo.url); } catch (e) {} } S.photo = null; showAttach(); }
  function sendPhoto(ta, txt) {
    var p = S.photo; S.photo = null; showAttach(); ta.value = ''; ta.style.height = 'auto';
    var tmp = { id: 1e15, body: txt, mine: true, sender: S.me.name, at: new Date().toISOString(), image: { local: p.url, w: p.w, h: p.h } };
    S.msgs.push(tmp); renderThread();
    var f = new FormData(); f.append('thread', S.thread.id); f.append('body', txt); f.append('once', p.once ? '1' : '0');
    f.append('file', p.blob, 'photo.' + ((p.blob.type.split('/')[1]) || 'jpg'));
    fetch('/chat/upload/', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }, body: f })
      .then(function (r) { return r.json(); }).catch(function () { return { ok: false, message: 'Could not send the photo.' }; })
      .then(function (d) {
        S.msgs = S.msgs.filter(function (m) { return m !== tmp; });
        if (d.ok) { if (!S.msgs.some(function (m) { return m.id === d.message.id; })) S.msgs.push(d.message); S.top = Math.max(S.top, d.message.id); }
        else { toast({ sender: 'Photo not sent', body: d.message || 'Try again.' }); ta.value = txt; }
        setTimeout(function () { try { URL.revokeObjectURL(p.url); } catch (e) {} }, 5000);
        renderThread(); poll();
      });
  }
  panel.addEventListener('dragover', function (e) { if (S.view === 'thread' && e.dataTransfer && [].indexOf.call(e.dataTransfer.types || [], 'Files') >= 0) { e.preventDefault(); panel.classList.add('ttc-drop'); } });
  panel.addEventListener('dragleave', function (e) { if (!panel.contains(e.relatedTarget)) panel.classList.remove('ttc-drop'); });
  panel.addEventListener('drop', function (e) {
    panel.classList.remove('ttc-drop'); if (S.view !== 'thread') return; e.preventDefault();
    var f = [].slice.call(e.dataTransfer.files || []).filter(function (x) { return /^image\//.test(x.type); })[0]; if (f) pickPhoto(f);
  });
  function lightbox(id, once) {
    var m = S.msgs.filter(function (x) { return x.id === id; })[0] || {}, im = m.image || {};
    var lb = document.createElement('div'); lb.className = 'ttc-lb' + (once ? ' once' : '');
    lb.innerHTML = '<div class="ttc-lb-top"><span>' + (once ? '👁 View once — this photo is deleted when you close it' : '⏳ Deleted for everyone ' + esc(new Date(im.expires).toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }))) + '</span>' +
      (once ? '' : '<a class="ttc-ib" href="/chat/img/' + id + '/" download title="Download">⬇</a>') + '<button class="ttc-ib" data-lb="x" title="Close (Esc)">' + ICON.x + '</button></div>' +
      '<img src="/chat/img/' + id + '/" alt="Photo">';
    var img = lb.querySelector('img');
    img.onerror = function () { img.replaceWith(Object.assign(document.createElement('p'), { className: 'ttc-lb-gone', textContent: once ? 'This view-once photo was already opened.' : 'This photo has expired.' })); };
    if (once) { lb.addEventListener('contextmenu', function (e) { e.preventDefault(); }); img.draggable = false; }
    function close() {
      lb.remove(); document.removeEventListener('keydown', key);
      if (once && m.image) { m.image = { gone: true, opened: true }; renderThread(); }
    }
    function key(e) { if (e.key === 'Escape') { e.stopPropagation(); close(); } }
    lb.addEventListener('click', function (e) { if (e.target === lb || e.target.closest('[data-lb]')) close(); });
    document.addEventListener('keydown', key, true);
    root.appendChild(lb);
  }

  // ───────── calls: ring, open the call window, call pill, privacy shield, pointer ─────────
  var bc = window.BroadcastChannel ? new BroadcastChannel('tt-call') : null;
  var TAB = Math.random().toString(36).slice(2, 12);
  var CALL = { live: null, seen: 0, ring: null, dismissed: {}, ringT: null, toneT: null };
  // Capture Handle: lets the call window know exactly which TapTap tab is being shared (Chrome/Edge).
  try { if (navigator.mediaDevices && navigator.mediaDevices.setCaptureHandleConfig && window.top === window) navigator.mediaDevices.setCaptureHandleConfig({ handle: TAB, exposeOrigin: true, permittedOrigins: [location.origin] }); } catch (e) {}

  function callLive() { return CALL.live && Date.now() - CALL.seen < 3500; }
  function openCall(url) {
    if (callLive()) { focusCall(); return toast({ sender: 'Call', body: 'You are already in a call.' }); }
    var w = null;
    try { w = window.open(url, 'ttcall', 'popup=yes,width=480,height=760,left=' + Math.max(0, (screen.availWidth || 1200) - 520) + ',top=40'); } catch (e) {}
    if (w) { try { w.focus(); } catch (e) {} return; }
    embedCall(url);                       // pop-ups blocked: run the call over this page instead
  }
  function embedCall(url) {
    var old = document.querySelector('.ttc-callframe'); if (old) old.remove();
    var f = document.createElement('div'); f.className = 'ttc-callframe';
    f.innerHTML = '<iframe allow="camera; microphone; display-capture; autoplay; fullscreen" src="' + esc(url) + '&embed=1" title="Call"></iframe>' +
      '<p>Stay on this page during the call. <b>Tip:</b> allow pop-ups for TapTap to keep browsing while you talk.</p>';
    document.body.appendChild(f);
  }
  window.addEventListener('message', function (e) {
    if (e.origin !== location.origin || !e.data || e.data.ttcall !== 'close') return;
    var f = document.querySelector('.ttc-callframe'); if (f) f.remove();
  });
  function focusCall() {
    var f = document.querySelector('.ttc-callframe'); if (f) { f.scrollIntoView(); return; }
    try { var w = window.open('', 'ttcall'); if (w) w.focus(); } catch (e) {}
    if (bc) bc.postMessage({ t: 'focus-call' });
  }
  function startCall(video) { if (S.thread) openCall('/chat/call/?thread=' + S.thread.id + '&video=' + (video ? 1 : 0)); }

  // The call pill: a small "on a call" reminder on every TapTap tab while the call window is open.
  var pill = document.createElement('button'); pill.type = 'button'; pill.className = 'ttc-callpill ttc-hidden'; document.body.appendChild(pill);
  pill.addEventListener('click', focusCall);
  setInterval(function () {
    if (!callLive()) { pill.classList.add('ttc-hidden'); return; }
    var c = CALL.live, s = Math.floor((Date.now() - c.since) / 1000);
    pill.innerHTML = '<i></i>' + esc(String(c.peer || 'Call').split(' ')[0]) + ' · ' + Math.floor(s / 60) + ':' + ('0' + s % 60).slice(-2) + (c.sharing ? ' · 🖥 Sharing' : c.viewing ? ' · 👀 Viewing' : '');
    pill.classList.remove('ttc-hidden');
  }, 1000);

  function shield(on, mine) {
    var h = document.documentElement; h.classList.toggle('tt-shield', !!on); h.classList.toggle('tt-shared', !!(on && mine));
  }
  function pointAt(m) {
    if (m.surface !== 'browser') return;
    if (m.handle ? m.handle !== TAB : document.hidden) return;   // only the tab that is actually being shared
    var d = document.createElement('div'); d.className = 'tt-ptr'; d.style.left = (m.x * innerWidth) + 'px'; d.style.top = (m.y * innerHeight) + 'px';
    d.innerHTML = '<span>' + esc(m.who || '') + '</span>'; document.body.appendChild(d); setTimeout(function () { d.remove(); }, 1700);
  }
  if (bc) {
    bc.onmessage = function (e) {
      var m = e.data || {};
      if (m.t === 'call') { if (m.state === 'live') { CALL.live = m; CALL.seen = Date.now(); } else { CALL.live = null; pill.classList.add('ttc-hidden'); } }
      else if (m.t === 'shield') shield(m.on, m.handle && m.handle === TAB);
      else if (m.t === 'ptr') pointAt(m);
      else if (m.t === 'ring-done') { CALL.dismissed[m.id] = 1; hideRing(); }
    };
    bc.postMessage({ t: 'hello?', tab: TAB });              // a call window already sharing will re-send its shield state
  }

  // Incoming calls: a cheap poll (cache only) every few seconds.
  function ringPoll() {
    clearTimeout(CALL.ringT);
    fetch('/chat/call/poll/', { credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' } })
      .then(function (r) { return r.json(); }).then(function (d) { if (d && d.ok) handleRings(d.rings || []); })
      .catch(function () {}).then(function () { CALL.ringT = setTimeout(ringPoll, document.hidden ? 8000 : 3000); });
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { clearTimeout(CALL.ringT); CALL.ringT = setTimeout(ringPoll, 150); } });
  function handleRings(list) {
    var r = list.filter(function (x) { return !CALL.dismissed[x.id]; })[0];
    if (!r) return hideRing();
    if (callLive()) return;                 // already talking: the caller gets "busy" next time
    if (CALL.ring && CALL.ring.id === r.id) return;
    showRing(r);
  }
  var QUICK = ['Can’t talk right now — I’ll call you back.', 'In a meeting. Can you text me?', 'Calling you back in 5 minutes.'];
  function showRing(r) {
    hideRing(); CALL.ring = r;
    var card = document.createElement('div'); card.className = 'ttc-ring' + (r.kind === 'support' ? ' support' : ''); card.setAttribute('role', 'alertdialog');
    card.innerHTML = '<div class="ttc-ring-av">' + esc(r.initials) + '</div><div class="ttc-ring-mid"><b>' + esc(r.from) + '</b><small>' + (r.video ? 'Incoming video call' : 'Incoming voice call') + ' · 🔒 encrypted</small></div>' +
      '<div class="ttc-ring-btns"><button class="ttc-rb no" data-r="decline" title="Decline">' + ICON.phone + '</button>' +
      '<button class="ttc-rb msg" data-r="msg" title="Decline with a message">' + ICON.chat + '</button>' +
      (r.video ? '<button class="ttc-rb yes" data-r="video" title="Answer with video">' + ICON.video + '</button>' : '') +
      '<button class="ttc-rb yes" data-r="voice" title="Answer">' + ICON.phone + '</button></div>' +
      '<div class="ttc-ring-quick ttc-hidden">' + QUICK.map(function (q, i) { return '<button data-q="' + i + '">' + esc(q) + '</button>'; }).join('') + '</div>' +
      '<span class="ttc-ring-time" style="animation-duration:' + (r.left || 40) + 's"></span>';
    card.addEventListener('click', function (e) {
      var b = e.target.closest('[data-r],[data-q]'); if (!b) return;
      var id = r.id;
      if (b.dataset.q) { callAct('reply', id, { text: QUICK[+b.dataset.q] }); done(); return; }
      var a = b.dataset.r;
      if (a === 'msg') { card.querySelector('.ttc-ring-quick').classList.toggle('ttc-hidden'); return; }
      if (a === 'decline') callAct('decline', id);
      else openCall('/chat/call/?join=' + encodeURIComponent(id) + (a === 'video' ? '&video=1' : ''));
      done();
      function done() { CALL.dismissed[id] = 1; if (bc) bc.postMessage({ t: 'ring-done', id: id }); hideRing(); }
    });
    document.body.appendChild(card);
    ringTone();
    if (document.hidden && window.Notification && Notification.permission === 'granted') {
      try { var n = new Notification((r.video ? 'Video call' : 'Call') + ' from ' + r.from, { body: 'Tap to answer on TapTap', tag: 'ttcall-' + r.id, requireInteraction: true });
        n.onclick = function () { window.focus(); n.close(); }; CALL.note = n; } catch (e) {}
    }
  }
  function hideRing() {
    clearTimeout(CALL.toneT); CALL.ring = null;
    var c = document.querySelector('.ttc-ring'); if (c) c.remove();
    if (CALL.note) { try { CALL.note.close(); } catch (e) {} CALL.note = null; }
  }
  function callAct(action, id, extra) {
    var b = extra || {}; b.action = action; b.id = id;
    return api('/chat/call/act/', b);
  }
  // One tab rings (the one you are looking at, or one background tab) — not every open TapTap tab at once.
  function ringer() {
    if (!document.hidden) return true;
    var l = store('ringer'); if (!l || l.until < Date.now() || l.tab === TAB) { store('ringer', { tab: TAB, until: Date.now() + 5000 }); return true; }
    return false;
  }
  function ringTone() {
    clearTimeout(CALL.toneT); if (!CALL.ring) return;
    if (ringer()) {
      try {
        AC = AC || new (window.AudioContext || window.webkitAudioContext)(); AC.resume && AC.resume();
        [0, 0.45].forEach(function (off) {
          [523, 659].forEach(function (f) {
            var o = AC.createOscillator(), g = AC.createGain(), t = AC.currentTime + off;
            o.frequency.value = f; o.connect(g); g.connect(AC.destination);
            g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.09, t + 0.03); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.38);
            o.start(t); o.stop(t + 0.42);
          });
        });
      } catch (e) {}
    }
    CALL.toneT = setTimeout(ringTone, 2600);
  }

  // restore
  var wasOpen = store('open'), th = store('thread');
  if (th) { S.view = 'thread'; S.thread = { id: th }; }
  poll();
  setTimeout(ringPoll, 1200);
  setTimeout(function () { if (wasOpen || FULL) setOpen(true); }, 400);
  function supportId() { var t = S.threads.filter(function (x) { return x.kind === 'support' && !x.inbox; })[0]; return t && t.id; }
  window.TapChat = {
    open: function (id) { setOpen(true); if (id) openThread(id); },
    call: function (threadId, video) { openCall('/chat/call/?thread=' + threadId + '&video=' + (video ? 1 : 0)); },
    // "Chat with TapTap support" buttons in the help center
    openSupport: function () {
      var id = supportId(); if (id) { setOpen(true); openThread(id); return; }
      api('/chat/state/?since=-1').then(function (d) { if (d.ok) { S.threads = d.threads; var i = supportId(); setOpen(true); if (i) openThread(i); } });
    }
  };
})();
