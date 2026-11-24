/* TapTap Line Hub — the control room on the Internet lines page (core/line_hub.py).
 *
 *  Stage    customers → router → one pod per gateway, with live traffic flowing along the wires
 *  Drawer   one gateway: switch on/off, in/out of use, NAT, TapTap's line, settings, customers, delete
 *  Tabs     Customers (drag-and-drop board) · Gateways (WAN) · NAT · Firewall · DHCP · TapTap line · Load sharing
 */
(function () {
  'use strict';
  var cfg = document.getElementById('lhUrls');
  if (!cfg || !window.TTBoard) return;
  var U = JSON.parse(cfg.textContent), B = window.TTBoard, esc = B.esc, bps = B.bps, COLORS = B.COLORS, ask = B.ask, toast = B.toast;
  var root = document.getElementById('lineHub');
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var S = { st: null, rates: {}, tab: 'people', board: null, open: '', loading: false, flows: [], raf: 0 };
  try { S.tab = sessionStorage.getItem('lh-tab-' + U.router_id) || 'people'; } catch (e) {}

  /* ───────── server ───────── */
  function csrf() { var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
  function call(url, body) {
    var o = { credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-CSRFToken': csrf() } };
    if (body !== undefined) { o.method = 'POST'; o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
    return fetch(url, o).then(function (r) {
      return r.json().then(function (d) { d.ok = d.ok !== undefined ? d.ok : !!d.success; return d; }, function () {
        return { ok: false, message: r.status === 403 ? 'You do not have access to this.' : r.status === 404 ? 'This TapTap update is not installed on the server yet.' : 'TapTap answered ' + r.status + '.' };
      });
    }, function () { return { ok: false, message: 'TapTap could not be reached.' }; });
  }

  /* ───────── skeleton ───────── */
  root.innerHTML =
    '<div class="lh-head"><div><h3><span class="lh-beacon"></span> Gateway control room</h3><p>Every internet line of ' + esc(U.router) + ', who is on it right now, and every switch in one place.</p></div>' +
    '<div class="lh-head-r"><span class="lh-chip" id="lhVia">…</span><button type="button" class="btn btn-sm btn-light" id="lhRefresh" title="Read the router again"><i class="bi bi-arrow-repeat"></i></button></div></div>' +
    '<div class="lh-stage" id="lhStage"><svg class="lh-wires" id="lhWires" aria-hidden="true"></svg>' +
    '<div class="lh-orb" id="lhOrb"><div class="lh-orb-ring"></div><b id="lhOnline">–</b><span>online</span></div>' +
    '<div class="lh-router" id="lhRouter"><i class="bi bi-router"></i><span>' + esc(U.router) + '</span></div>' +
    '<div class="lh-pods" id="lhPods"><div class="lh-skel"></div><div class="lh-skel"></div></div></div>' +
    '<div class="lh-tiles" id="lhTiles"></div>' +
    '<nav class="lh-tabs" role="tablist" aria-label="Line Hub">' + [
      ['people', 'bi-people', 'Customers on lines'], ['wan', 'bi-hdd-network', 'Gateways'], ['nat', 'bi-shuffle', 'NAT'], ['fw', 'bi-shield-lock', 'Firewall'],
      ['dhcp', 'bi-diagram-3', 'DHCP'], ['link', 'bi-broadcast', 'TapTap line'], ['share', 'bi-sliders', 'Load sharing']
    ].map(function (t) { return '<button type="button" role="tab" data-tab="' + t[0] + '"><i class="bi ' + t[1] + '"></i><span>' + t[2] + '</span></button>'; }).join('') + '<i class="lh-ink" aria-hidden="true"></i></nav>' +
    '<div class="lh-panes">' + ['people', 'wan', 'nat', 'fw', 'dhcp', 'link', 'share'].map(function (t) { return '<div class="lh-pane" data-pane="' + t + '" hidden></div>'; }).join('') + '</div>' +
    '<aside class="lh-drawer" id="lhDrawer" aria-hidden="true"><div class="lh-drawer-card" role="dialog" aria-modal="true" aria-label="Gateway"></div></aside>';
  var pane = function (t) { return root.querySelector('[data-pane="' + t + '"]'); };
  S.board = B.mount(pane('people'), { move: function (p) { return call(U.move, p); }, onDone: function () { later(pulse, 4000); later(pulse, 12000); } });

  [].forEach.call(root.querySelectorAll('.lh-tabs button'), function (b) { b.addEventListener('click', function () { show(b.dataset.tab); }); });
  root.querySelector('#lhRefresh').onclick = function () { load(true); };
  function show(t) {
    S.tab = t; try { sessionStorage.setItem('lh-tab-' + U.router_id, t); } catch (e) {}
    [].forEach.call(root.querySelectorAll('.lh-tabs button'), function (b) {
      var on = b.dataset.tab === t; b.classList.toggle('on', on); b.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on) { var ink = root.querySelector('.lh-ink'); ink.style.width = b.offsetWidth + 'px'; ink.style.transform = 'translateX(' + b.offsetLeft + 'px)'; }
    });
    [].forEach.call(root.querySelectorAll('.lh-pane'), function (p) { var on = p.dataset.pane === t; p.hidden = !on; if (on) { p.classList.remove('lh-in'); void p.offsetWidth; p.classList.add('lh-in'); } });
  }

  /* ───────── loading ───────── */
  var timers = {};
  function later(fn, ms) { clearTimeout(timers[fn.name + ms]); timers[fn.name + ms] = setTimeout(fn, ms); }
  function load(loud) {
    if (S.loading) return; S.loading = true;
    var btn = root.querySelector('#lhRefresh'); btn.classList.add('spin');
    call(U.state).then(function (d) {
      S.loading = false; btn.classList.remove('spin');
      if (!d.ok) { if (loud || !S.st) root.querySelector('#lhPods').innerHTML = '<div class="lh-err"><i class="bi bi-exclamation-octagon"></i> ' + esc(d.message) + '</div>'; return; }
      S.st = d; S.rates = {}; d.lines.forEach(function (l) { S.rates[l.interface] = { rx_bps: l.rx_bps, tx_bps: l.tx_bps }; });
      renderAll();
      if (loud) toast('Read ' + U.router + ' again (' + (d.live ? 'live' : 'from the last sync') + ').', 'ok');
    });
  }
  function pulse() {
    if (!S.st || document.hidden) return;
    call(U.pulse).then(function (d) {
      if (!d.ok) return;
      S.st.board = d.board; S.rates = d.rates || S.rates;
      d.board.columns.forEach(function (c) { var l = line(c.interface); if (l) { l.users = c.users; l.real = c.real; } });
      renderPods(true); renderTiles(); S.board.render(d.board); wires();
      if (S.open) renderDrawer();
    });
  }
  setInterval(pulse, 8000);
  setInterval(function () { if (!document.hidden) load(false); }, 90000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden && S.st) pulse(); });

  function line(iface) { return S.st && S.st.lines.filter(function (l) { return l.interface === iface; })[0]; }
  function lineState(l) {
    if (l.missing) return ['missing', 'Port missing', 'bi-question-octagon'];
    if (l.client_off) return ['off', 'Switched off', 'bi-power'];
    if (!l.up) return ['down', l.status === 'searching...' ? 'Searching…' : 'Not connected', 'bi-plug'];
    if (l.use === 'out') return ['out', 'Out of use', 'bi-pause-circle'];
    if (l.use === 'plain') return ['plain', 'In use (own route)', 'bi-check-circle'];
    if (l.use === 'standby') return ['standby', 'Standing by', 'bi-hourglass-split'];
    return ['up', 'In use', 'bi-check-circle-fill'];
  }

  /* ───────── stage ───────── */
  function renderAll() {
    var st = S.st;
    root.querySelector('#lhVia').innerHTML = '<i class="bi ' + (st.live ? 'bi-lightning-charge-fill' : 'bi-cloud-arrow-down') + '"></i> ' + esc(st.live ? st.via + ' · live' : st.via + ' · ' + 'last sync');
    if ((st.stale || []).length) root.querySelector('#lhVia').innerHTML += ' <span class="lh-warn-dot" title="Old copy: ' + esc(st.stale.join(', ')) + '"></span>';
    renderPods(false); renderTiles(); S.board.render(st.board);
    renderWan(); renderNat(); renderFw(); renderDhcp(); renderLink(); renderShare();
    show(S.tab);
    requestAnimationFrame(wires);
    if (S.open) renderDrawer();
  }

  function renderPods(soft) {
    var box = root.querySelector('#lhPods'), st = S.st, lines = st.lines;
    root.querySelector('#lhOnline').textContent = st.board.online;
    if (!lines.length) { box.innerHTML = '<div class="lh-err"><i class="bi bi-info-circle"></i> No internet line found. Add one in the Gateways tab below.</div>'; return; }
    var html = lines.map(function (l) {
      var s = lineState(l), r = S.rates[l.interface] || {}, color = COLORS[l.color % 6];
      var badges = [];
      if (l.carries_link) badges.push('<span class="lh-badge link" title="TapTap Link and Tunnel go through this line"><i class="bi bi-broadcast"></i> TapTap</span>');
      if (!l.missing && !l.nat.on && l.up) badges.push('<span class="lh-badge warn" title="Customers are not hidden behind this line (NAT off)"><i class="bi bi-exclamation-triangle"></i> NAT off</span>');
      if (l.routes_off && l.use === 'out') badges.push('<span class="lh-badge" title="Its routes are switched off">held</span>');
      if (l.type === 'dhcp' && /^(renew|rebind)/i.test(l.status || '')) badges.push('<span class="lh-badge warn" title="Its modem has not answered the address renewal. TapTap ' + (l.dhcp_fixing ? 'is now making sure renewals get through' : 'keeps renewals out of the line marks, NAT and firewall') + ' — the line works meanwhile."><i class="bi bi-arrow-repeat"></i> renewing</span>');
      if (!l.in_design && !l.missing) badges.push('<span class="lh-badge" title="Not in your design yet — tick it in step 1">new</span>');
      return '<button type="button" class="lh-pod ' + s[0] + '" data-if="' + esc(l.interface) + '" style="--c:' + color + '">' +
        '<span class="lh-pod-top"><span class="lh-pod-dot"></span><b>' + esc(l.label) + '</b><em>' + esc(l.interface) + ' · ' + esc(l.type.toUpperCase()) + '</em></span>' +
        '<span class="lh-pod-mid"><span class="lh-pod-users"><b data-v="' + (l.users || 0) + '">' + (l.users || 0) + '</b><small>customers</small></span>' +
        '<span class="lh-pod-rate"><span>↓ ' + bps(r.rx_bps) + '</span><span>↑ ' + bps(r.tx_bps) + '</span></span></span>' +
        '<span class="lh-pod-bar" title="Share of customers now' + (l.share != null ? ' — designed ' + l.share + '%' : '') + '"><i style="width:' + (l.real || 0) + '%"></i>' + (l.share != null ? '<u style="left:calc(' + l.share + '% - 1px)"></u>' : '') + '</span>' +
        '<span class="lh-pod-foot"><span class="lh-state"><i class="bi ' + s[2] + '"></i> ' + s[1] + '</span>' + badges.join('') + '</span></button>';
    }).join('');
    if (soft && box.children.length === lines.length) {       // keep the pods (and their hover / focus), update their insides
      var tmp = document.createElement('div'); tmp.innerHTML = html;
      [].forEach.call(tmp.children, function (n, i) {
        var old = box.children[i], ob = old.querySelector('.lh-pod-users b'), nb = n.querySelector('.lh-pod-users b');
        var changed = ob && nb && ob.textContent !== nb.textContent;
        old.className = n.className; old.innerHTML = n.innerHTML; old.style.cssText = n.style.cssText; old.dataset.if = n.dataset.if;
        if (changed) { var b = old.querySelector('.lh-pod-users b'); b.classList.add('lh-bump'); }
      });
    } else {
      box.innerHTML = html;
      [].forEach.call(box.children, function (p, i) { p.style.animationDelay = (i * 70) + 'ms'; p.classList.add('lh-pop'); });
    }
    [].forEach.call(box.querySelectorAll('.lh-pod'), function (p) { p.onclick = function () { openDrawer(p.dataset.if); }; });
  }

  function renderTiles() {
    var st = S.st, down = 0, up = 0;
    Object.keys(S.rates).forEach(function (k) { down += +S.rates[k].rx_bps || 0; up += +S.rates[k].tx_bps || 0; });
    var inUse = st.lines.filter(function (l) { return !l.missing && l.up && l.use !== 'out'; }).length;
    var link = st.link.interface ? (line(st.link.interface) || {}).label || st.link.interface : 'Automatic';
    var t = [['Online now', st.board.online, st.board.placed + ' placed on a line'], ['Lines in use', inUse + ' / ' + st.lines.filter(function (l) { return !l.missing; }).length, st.lines.some(function (l) { return l.missing; }) ? 'a deleted port needs tidying' : 'connected and carrying customers'],
      ['Download now', bps(down), 'all lines together'], ['Upload now', bps(up), 'all lines together'],
      ['Sharing', st.setup.strategy_label || 'Not set by TapTap', st.setup.status === 'active' ? 'applied' + (st.setup.applied_at ? ' ' + new Date(st.setup.applied_at).toLocaleDateString() : '') : st.setup.status === 'pending' ? 'waiting for “Keep”' : 'design below'],
      ['TapTap goes via', link, st.link.channel]];
    root.querySelector('#lhTiles').innerHTML = t.map(function (x) { return '<div class="lh-tile"><span>' + esc(x[0]) + '</span><b>' + esc(x[1]) + '</b><small>' + esc(x[2]) + '</small></div>'; }).join('');
  }

  /* wires + particles: drawn between the real positions of the orb, the router and each pod */
  function wires() {
    var svg = root.querySelector('#lhWires'), stage = root.querySelector('#lhStage');
    if (!svg || !S.st || getComputedStyle(svg).display === 'none') { S.flows = []; return; }
    var R = stage.getBoundingClientRect(), W = R.width, H = R.height;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H); svg.setAttribute('width', W); svg.setAttribute('height', H);
    var c = function (el, side) { var b = el.getBoundingClientRect(); return [side === 'r' ? b.right - R.left : side === 'l' ? b.left - R.left : b.left - R.left + b.width / 2, b.top - R.top + b.height / 2]; };
    var orb = c(root.querySelector('#lhOrb'), 'r'), rin = c(root.querySelector('#lhRouter'), 'l'), rout = c(root.querySelector('#lhRouter'), 'r');
    var out = '<defs><filter id="lhGlow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3"/></filter></defs>';
    var flows = [], total = 0;
    var feed = 'M' + orb[0] + ' ' + orb[1] + ' C' + (orb[0] + 60) + ' ' + orb[1] + ' ' + (rin[0] - 60) + ' ' + rin[1] + ' ' + rin[0] + ' ' + rin[1];
    out += '<path class="lh-w" d="' + feed + '"/><path class="lh-w-live" id="lhFeed" d="' + feed + '" stroke="#9cc8ff"/>';
    [].forEach.call(root.querySelectorAll('.lh-pod'), function (pod) {
      var l = line(pod.dataset.if); if (!l) return;
      var p = c(pod, 'l'), s = lineState(l)[0], color = COLORS[l.color % 6], mid = (rout[0] + p[0]) / 2;
      var d = 'M' + rout[0] + ' ' + rout[1] + ' C' + mid + ' ' + rout[1] + ' ' + mid + ' ' + p[1] + ' ' + (p[0] - 4) + ' ' + p[1];
      var dead = s === 'down' || s === 'missing' || s === 'off';
      var calm = s === 'out' || (s === 'standby' && !l.users);
      out += '<path class="lh-w' + (dead ? ' dead' : calm ? ' idle' : '') + '" d="' + d + '"/>';
      if (!dead) out += '<path class="lh-w-live' + (calm ? ' idle' : '') + '" data-if="' + esc(l.interface) + '" d="' + d + '" stroke="' + color + '"/>';
      var r = S.rates[l.interface] || {}, load = (+r.rx_bps || 0) + (+r.tx_bps || 0);
      if (!dead && s !== 'out') { flows.push({ id: l.interface, color: color, users: l.users || 0, load: load }); total += l.users || 0; }
    });
    svg.innerHTML = out + '<g id="lhDots"></g>';
    S.flows = flows.map(function (f) { return { id: f.id, color: f.color, path: svg.querySelector('.lh-w-live[data-if="' + (window.CSS && CSS.escape ? CSS.escape(f.id) : f.id) + '"]'), users: f.users, load: f.load, dots: [], acc: 0 }; });
    S.feed = { path: svg.querySelector('#lhFeed'), dots: [], acc: 0, users: total, load: S.flows.reduce(function (a, f) { return a + f.load; }, 0), color: '#cfe4ff' };
    if (!S.raf && !reduce) { S.last = performance.now(); S.raf = requestAnimationFrame(tick); }
  }
  function rate(f) {   // dots per second: busier lines stream more, a quiet line still breathes
    var u = Math.min(40, f.users), mbps = (f.load || 0) / 1e6;
    return f.users || mbps > .05 ? Math.min(9, .6 + u * .18 + Math.log10(1 + mbps) * 2.2) : 0;
  }
  function speed(f) { var mbps = (f.load || 0) / 1e6; return .22 + Math.min(.55, Math.log10(1 + mbps) * .22); }
  function tick(now) {
    S.raf = 0;
    if (document.hidden || !S.flows) { S.raf = requestAnimationFrame(tick); return; }
    var dt = Math.min(.1, (now - S.last) / 1000); S.last = now;
    var g = root.querySelector('#lhDots'); if (!g) { S.raf = requestAnimationFrame(tick); return; }
    var all = S.flows.concat(S.feed ? [S.feed] : []);
    var html = '';
    all.forEach(function (f) {
      if (!f.path) return;
      var len = f.len || (f.len = f.path.getTotalLength()); if (!len) return;
      f.acc += rate(f) * dt;
      while (f.acc >= 1 && f.dots.length < 26) { f.acc -= 1; f.dots.push({ t: 0, s: speed(f) * (.85 + Math.random() * .3), r: 1.6 + Math.random() * 1.6 }); }
      f.dots = f.dots.filter(function (d) { d.t += d.s * dt; return d.t < 1; });
      f.dots.forEach(function (d) {
        var pt = f.path.getPointAtLength(d.t * len), a = d.t < .1 ? d.t * 10 : d.t > .9 ? (1 - d.t) * 10 : 1;
        html += '<circle cx="' + pt.x.toFixed(1) + '" cy="' + pt.y.toFixed(1) + '" r="' + (d.r + 2.5).toFixed(1) + '" fill="' + f.color + '" opacity="' + (a * .25).toFixed(2) + '" filter="url(#lhGlow)"/>' +
          '<circle cx="' + pt.x.toFixed(1) + '" cy="' + pt.y.toFixed(1) + '" r="' + d.r.toFixed(1) + '" fill="' + f.color + '" opacity="' + a.toFixed(2) + '"/>';
      });
    });
    g.innerHTML = html;
    S.raf = requestAnimationFrame(tick);
  }
  var rz; window.addEventListener('resize', function () { clearTimeout(rz); rz = setTimeout(function () { wires(); show(S.tab); }, 120); });

  /* ───────── doing things ───────── */
  function act(payload, opts) {
    opts = opts || {};
    var btn = opts.btn; if (btn) { btn.disabled = true; btn.classList.add('lh-busy'); }
    var p = payload.step ? call(U.doctor, { interface: payload.interface, step: payload.step }) : call(U.action, payload);
    return p.then(function (r) {
      if (btn) { btn.disabled = false; btn.classList.remove('lh-busy'); }
      if (!r.ok) { toast(r.message || 'That did not work.', 'bad'); return r; }
      toast(r.message, r.via_link ? 'wait' : 'ok');
      if (opts.lines) document.dispatchEvent(new CustomEvent('tt:wan-redetect', { detail: { config: r.config || null, removed: opts.removed || '' } }));
      later(function refreshA() { load(false); }, r.via_link ? 9000 : 900);
      if (r.via_link) later(function refreshB() { load(false); }, 25000);
      return r;
    });
  }
  function confirmThen(q, payload, opts) { return ask(q).then(function (yes) { return yes ? act(payload, opts) : { ok: false, cancelled: true }; }); }

  /* ───────── drawer: one gateway ───────── */
  var drawer = root.querySelector('#lhDrawer');
  drawer.addEventListener('click', function (e) { if (e.target === drawer) closeDrawer(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && S.open && !document.querySelector('.lh-modal')) closeDrawer(); });
  function openDrawer(iface) { S.open = iface; renderDrawer(); drawer.classList.add('on'); drawer.setAttribute('aria-hidden', 'false'); setTimeout(function () { var f = drawer.querySelector('.lh-x'); if (f) f.focus(); }, 60); }
  function closeDrawer() { S.open = ''; drawer.classList.remove('on'); drawer.setAttribute('aria-hidden', 'true'); }
  document.addEventListener('tt:hub-open', function (e) { if (e.detail && e.detail.interface) openDrawer(e.detail.interface); });

  function sw(id, on, label, sub, disabled, tip) {
    return '<label class="lh-sw' + (disabled ? ' dis' : '') + '"' + (tip ? ' title="' + esc(tip) + '"' : '') + '><input type="checkbox" data-sw="' + id + '"' + (on ? ' checked' : '') + (disabled ? ' disabled' : '') + '><span class="lh-sw-t"></span><span class="lh-sw-l"><b>' + esc(label) + '</b>' + (sub ? '<small>' + esc(sub) + '</small>' : '') + '</span></label>';
  }

  function renderDrawer() {
    var l = line(S.open), card = drawer.querySelector('.lh-drawer-card');
    if (!l) { closeDrawer(); return; }
    var s = lineState(l), r = S.rates[l.interface] || {}, col = (S.st.board.columns || []).filter(function (c) { return c.interface === l.interface; })[0] || { people: [] };
    var others = S.st.lines.filter(function (x) { return x.interface !== l.interface && !x.missing && x.up && x.use !== 'out'; });
    var keep = card.querySelector('[data-keep-scroll]'), scroll = keep ? keep.scrollTop : 0;
    var h = '<header class="lh-dr-head" style="--c:' + COLORS[l.color % 6] + '"><div><span class="lh-dr-state ' + s[0] + '"><i></i>' + s[1] + '</span><h3>' + esc(l.label) + '</h3>' +
      '<p>' + esc(l.interface) + ' · ' + esc(l.type.toUpperCase()) + (l.address ? ' · ' + esc(l.address) : '') + (l.gateway && l.type !== 'pppoe' ? ' · gateway ' + esc(l.gateway) : '') + '</p></div>' +
      '<button type="button" class="lh-x" aria-label="Close"><i class="bi bi-x-lg"></i></button></header><div class="lh-dr-body" data-keep-scroll>';
    if (l.missing) {
      h += '<div class="lh-callout bad"><i class="bi bi-question-octagon"></i><div><b>This line’s port no longer exists.</b><p>RouterOS shows the port as “' + esc(l.interface) + '”: the DHCP / PPPoE client was left behind when its port was removed (a USB modem unplugged, a VLAN deleted…). It can never carry customers. Delete it to tidy up — nothing else changes.</p></div></div>' +
        '<button type="button" class="btn btn-danger w-100" data-del><i class="bi bi-trash3"></i> Delete this leftover client</button>';
    } else {
      h += '<div class="lh-dr-stats"><div><b>' + (l.users || 0) + '</b><span>customers</span></div><div><b>' + bps(r.rx_bps) + '</b><span>download</span></div><div><b>' + bps(r.tx_bps) + '</b><span>upload</span></div>' +
        '<div><b>' + (l.real || 0) + '%</b><span>of customers' + (l.share != null ? ' · designed ' + l.share + '%' : '') + '</span></div></div>' +
        ((!l.up || l.client_off || l.port_off || (l.use === 'out' && l.has_routes)) ? '<button type="button" class="btn btn-danger w-100 mb-2 lh-big" data-revive><i class="bi bi-lightning-charge-fill"></i> Bring ' + esc(l.label) + ' online</button>' : '') +
        (l.up && !l.client_off && (l.use === 'standby' || l.use === 'out' || !l.selected || S.st.setup.strategy !== 'balance') ? '<button type="button" class="btn btn-primary w-100 mb-2 lh-big" data-share><i class="bi bi-shuffle"></i> Share the load with ' + esc(l.label) + '</button>' : '') +
        '<h4>Switches</h4><div class="lh-sws">' +
        sw('power', !l.client_off, 'Line switched on', l.switchable ? 'Turns the ' + l.type.toUpperCase() + ' client on or off' : 'Static lines are switched by their port', !l.switchable) +
        sw('use', l.use !== 'out', 'Customers use this line', l.use === 'out' ? 'Out of use — it stays connected so you can test it' : 'Take it out of use to move everyone to the other lines', l.client_off || (!l.has_routes && l.use === 'out' && l.type !== 'dhcp')) +
        sw('nat', l.nat.on, 'NAT (hide customers behind it)', l.nat.on ? l.nat.rules.filter(function (x) { return !x.disabled; }).map(function (x) { return x.action + ' · ' + (x.via === 'port' ? 'this port' : x.via); }).join(', ') : 'Off — customers on this line get no internet') +
        sw('link', l.carries_link, 'Carries TapTap Link', l.carries_link ? (l.link_route_ok === false ? 'Chosen, but its route is not active now — TapTap uses the other lines' : 'TapTap’s own traffic goes through this line') : 'Choose this line for TapTap’s connection to the router', !l.up || l.use === 'out') +
        '</div>' +
        '<h4>Customers on this line <small>' + col.people.length + '</small></h4>' +
        (col.people.length ? '<div class="lh-dr-people">' + col.people.slice(0, 60).map(function (p) {
          return '<label class="lh-dr-p"><input type="checkbox" data-pk="' + esc(JSON.stringify({ ip: p.ip, mac: p.mac, code: p.code })) + '"><b>' + esc(p.code || p.ip) + '</b>' + (p.pinned ? ' <i class="bi bi-pin-angle-fill"></i>' : '') + '<small>' + esc(p.ip) + '</small><span>↓ ' + bps(p.down_bps) + '</span></label>';
        }).join('') + '</div>' +
          (S.st.board.can_move && others.length ? '<div class="lh-dr-move"><select class="form-select form-select-sm" aria-label="Move to">' + others.filter(function (o) { return o.movable; }).map(function (o) { return '<option value="' + esc(o.interface) + '">to ' + esc(o.label) + '</option>'; }).join('') + '</select>' +
            '<button type="button" class="btn btn-sm btn-primary" data-move-sel><i class="bi bi-arrow-right-circle"></i> Move selected</button><button type="button" class="btn btn-sm btn-outline-primary" data-move-all>Move everyone</button></div>'
            : '<p class="lh-mini">' + esc(S.st.board.why || 'Moving needs another line in use.') + '</p>')
          : '<p class="lh-mini">Nobody on this line right now.</p>') +
        '<h4>Settings</h4><form class="lh-form" data-edit>' +
        '<label>Name<input name="label" value="' + esc(l.label) + '" maxlength="40" list="isps"></label>' +
        '<label>Speed (Mbps)<input name="speed" type="number" min="1" max="10000" value="' + esc(l.speed) + '"></label>' +
        (l.type === 'static' ? '<label>Gateway<input name="gateway" value="' + esc(l.gateway) + '" placeholder="192.168.1.1"></label>' : '') +
        (l.type === 'dhcp' ? '<label>DNS from the modem<select name="use_peer_dns"><option value="no"' + (l.use_peer_dns !== 'yes' && l.use_peer_dns !== 'true' ? ' selected' : '') + '>No (recommended)</option><option value="yes"' + (l.use_peer_dns === 'yes' || l.use_peer_dns === 'true' ? ' selected' : '') + '>Yes</option></select></label>' : '') +
        (l.type === 'pppoe' ? '<label>PPPoE user<input name="user" value="' + esc(l.user) + '" autocomplete="off"></label><label>New password<input name="password" type="password" autocomplete="new-password" placeholder="unchanged"></label>' : '') +
        '<button class="btn btn-sm btn-primary" type="submit"><i class="bi bi-check2"></i> Save</button></form>' +
        '<div class="lh-dr-actions"><button type="button" class="btn btn-sm btn-light" data-design><i class="bi bi-pencil-square"></i> Open in the designer</button>' +
        (S.st.setup.strategy === 'failover' && l.selected && l.order !== 0 ? '<button type="button" class="btn btn-sm btn-light" data-main><i class="bi bi-star"></i> Make it the main line</button>' : '') + '</div>' +
        '<details class="lh-danger"><summary><i class="bi bi-exclamation-diamond"></i> Delete this line</summary><p>Removes the ' + (l.type === 'static' ? 'line from TapTap (and the address TapTap gave the port)' : l.type.toUpperCase() + ' client from the router') +
        ' and from your design. ' + (l.use === 'out' || l.client_off ? 'It is out of use, so nobody is affected.' : 'Take it out of use first, so the internet never drops while it goes.') + '</p>' +
        '<button type="button" class="btn btn-outline-danger btn-sm" data-del' + (l.use === 'out' || l.client_off ? '' : ' disabled') + '><i class="bi bi-trash3"></i> Delete ' + esc(l.label) + '</button></details>';
    }
    card.innerHTML = h + '</div>';
    var body = card.querySelector('[data-keep-scroll]'); if (body) body.scrollTop = scroll;
    card.querySelector('.lh-x').onclick = closeDrawer;
    wireDrawer(card, l, col);
  }

  function wireDrawer(card, l, col) {
    var q = function (s) { return card.querySelector(s); };
    var del = card.querySelectorAll('[data-del]');
    [].forEach.call(del, function (b) {
      b.onclick = function () {
        confirmThen({ title: 'Delete ' + l.label + '?', tone: 'danger', icon: 'bi-trash3', typeToConfirm: l.missing ? '' : l.label, ok: 'Delete',
          html: '<p>' + (l.missing ? 'The leftover client on “' + esc(l.interface) + '” is removed from the router.' : 'The ' + esc(l.type.toUpperCase()) + ' client on ' + esc(l.interface) + ' is removed from the router and the line leaves your design.') + '</p>' },
        { action: 'delete', interface: l.interface }, { btn: b, lines: true, removed: l.interface }).then(function (r) { if (r && r.ok) closeDrawer(); });
      };
    });
    var reverted = function (el) { el.checked = !el.checked; };
    [].forEach.call(card.querySelectorAll('[data-sw]'), function (el) {
      el.addEventListener('change', function () {
        var on = el.checked, k = el.dataset.sw, q2, payload;
        if (k === 'power') { payload = { interface: l.interface, step: on ? 'turn_on' : 'turn_off' }; q2 = { title: (on ? 'Switch on ' : 'Switch off ') + l.label + '?', icon: 'bi-power', tone: on ? '' : 'danger', text: on ? 'Give the modem about 10 seconds, then check the line before putting it in use.' : 'The line disconnects. Customers on it move to the other lines.' }; }
        if (k === 'use') { payload = { interface: l.interface, step: on ? 'use' : 'unuse' }; q2 = { title: on ? 'Put ' + l.label + ' in use?' : 'Take ' + l.label + ' out of use?', icon: on ? 'bi-play-circle' : 'bi-pause-circle', text: on ? 'Customers are shared across it again.' : 'Everyone on it moves to the other lines; it stays connected so you can test it.' }; }
        if (k === 'nat') { payload = { action: 'nat', interface: l.interface, on: on }; q2 = { title: (on ? 'Turn NAT on for ' : 'Turn NAT off for ') + l.label + '?', icon: 'bi-shuffle', tone: on ? '' : 'danger', text: on ? 'Customers are hidden behind this line’s address again.' : 'Only for lines whose provider routes your own addresses. Without NAT, customers on this line get no internet.' }; }
        if (k === 'link') { payload = { action: 'link', interface: on ? l.interface : '' }; q2 = { title: on ? 'Send TapTap through ' + l.label + '?' : 'Let TapTap choose its line automatically?', icon: 'bi-broadcast', text: on ? 'TapTap Link and Tunnel use this line. If it stops, they move to the other lines by themselves.' : 'TapTap follows the customers’ lines again.' }; }
        confirmThen(q2, payload, { btn: null }).then(function (r) { if (!r || !r.ok) reverted(el); });
      });
    });
    var form = q('[data-edit]');
    if (form) form.addEventListener('submit', function (e) {
      e.preventDefault();
      var fd = new FormData(form), p = { action: 'edit', interface: l.interface };
      fd.forEach(function (v, k) { if (v !== '') p[k] = v; });
      act(p, { btn: form.querySelector('button'), lines: true });
    });
    var rv = q('[data-revive]');
    if (rv) rv.onclick = function () {
      confirmThen({ title: 'Bring ' + l.label + ' online?', icon: 'bi-lightning-charge-fill', text: 'TapTap does what it takes: switches it on, asks the modem for a fresh address, or puts it back in use. Other lines are not touched.', ok: 'Bring it online' },
        { action: 'revive', interface: l.interface }, { btn: rv });
    };
    var shb = q('[data-share]');
    if (shb) shb.onclick = function () {
      confirmThen({ title: 'Share the load with ' + l.label + '?', icon: 'bi-shuffle', ok: 'Share the load',
        html: '<p>Customers are shared over every working line by speed, one line per customer, with health checks.</p><p class="small text-secondary mb-0">A safety timer goes on the router first; TapTap confirms it by itself once the router is still reachable — otherwise the router undoes it.</p>' },
        { action: 'share', interface: l.interface }, { btn: shb, lines: true });
    };
    var dz = q('[data-design]'); if (dz) dz.onclick = function () { closeDrawer(); var c = document.querySelector('.line-card[data-if="' + (window.CSS && CSS.escape ? CSS.escape(l.interface) : l.interface) + '"]') || document.getElementById('step1'); if (c) { c.scrollIntoView({ behavior: 'smooth', block: 'center' }); c.classList.add('lh-flash'); setTimeout(function () { c.classList.remove('lh-flash'); }, 1800); } };
    var mn = q('[data-main]'); if (mn) mn.onclick = function () { closeDrawer(); document.dispatchEvent(new CustomEvent('tt:wan-main', { detail: { interface: l.interface } })); };
    var sel = function () { return [].map.call(card.querySelectorAll('[data-pk]:checked'), function (c) { return JSON.parse(c.dataset.pk); }); };
    var mv = function (devs, b) {
      var to = q('.lh-dr-move select').value, target = line(to);
      if (!devs.length) { toast('Tick the customers to move first.', 'bad'); return; }
      ask({ title: 'Move ' + devs.length + ' customer' + (devs.length > 1 ? 's' : '') + ' to ' + target.label + '?', icon: 'bi-signpost-split', text: 'Their open connections are closed so they switch at once.', ok: 'Move them' }).then(function (y) {
        if (!y) return; b.disabled = true;
        call(U.move, { interface: to, devices: devs.map(function (d) { d.from = l.label; return d; }) }).then(function (r) {
          b.disabled = false; toast(r.message || 'Move failed.', r.ok ? (r.via_link ? 'wait' : 'ok') : 'bad'); if (r.ok) { later(pulse, 2500); later(pulse, 10000); }
        });
      });
    };
    var ms = q('[data-move-sel]'); if (ms) ms.onclick = function () { mv(sel(), ms); };
    var ma = q('[data-move-all]'); if (ma) ma.onclick = function () { mv(col.people.map(function (p) { return { ip: p.ip, mac: p.mac, code: p.code }; }), ma); };
  }

  /* ───────── tabs ───────── */
  function renderWan() {
    var st = S.st, p = pane('wan');
    var rows = st.lines.map(function (l) {
      var s = lineState(l);
      return '<tr><td><span class="lh-cdot" style="--c:' + COLORS[l.color % 6] + '"></span><b>' + esc(l.label) + '</b><small>' + esc(l.interface) + '</small></td><td>' + esc(l.type.toUpperCase()) + '</td>' +
        '<td><span class="lh-state-pill ' + s[0] + '">' + s[1] + '</span></td><td class="font-monospace small">' + esc(l.address || '—') + '</td><td class="font-monospace small">' + esc(l.type === 'pppoe' ? '(PPPoE)' : l.gateway || '—') + '</td>' +
        '<td class="text-end"><button type="button" class="btn btn-sm btn-outline-primary" data-open="' + esc(l.interface) + '"><i class="bi bi-sliders"></i> Manage</button></td></tr>';
    }).join('');
    var ports = st.ports || [];
    p.innerHTML = '<div class="lh-card"><div class="lh-card-h"><h4><i class="bi bi-hdd-network"></i> Internet lines (WAN)</h4><small>Open one to switch it, change it or delete it.</small></div>' +
      '<div class="table-responsive"><table class="table align-middle lh-table"><thead><tr><th>Line</th><th>Type</th><th>State</th><th>Address</th><th>Gateway</th><th></th></tr></thead><tbody>' + (rows || '<tr><td colspan="6" class="text-secondary">No line yet.</td></tr>') + '</tbody></table></div></div>' +
      '<div class="lh-card lh-add"><div class="lh-card-h"><h4><i class="bi bi-plus-circle"></i> Add an internet line</h4><small>Plug the modem into a free port, then choose how it connects.</small></div>' +
      (ports.length ? '<form class="lh-form wide" data-add><label>Port<select name="port" required>' + ports.map(function (x) { return '<option value="' + esc(x.name) + '">' + esc(x.name) + (x.running ? ' · cable in' : ' · no cable') + (x.comment ? ' · ' + esc(x.comment) : '') + '</option>'; }).join('') + '</select></label>' +
        '<div class="lh-seg" role="radiogroup" aria-label="Connection type"><label><input type="radio" name="type" value="dhcp" checked><span><i class="bi bi-magic"></i> DHCP<small>modem gives the address (Starlink, 4G, most fibre)</small></span></label>' +
        '<label><input type="radio" name="type" value="static"><span><i class="bi bi-pin-map"></i> Static<small>the provider gave you an address</small></span></label>' +
        '<label><input type="radio" name="type" value="pppoe"><span><i class="bi bi-key"></i> PPPoE<small>user name and password</small></span></label></div>' +
        '<label>Name<input name="label" maxlength="40" placeholder="e.g. Gamtel fibre" list="isps" required></label><label>Speed (Mbps)<input name="speed" type="number" min="1" max="10000" value="50"></label>' +
        '<label data-for="static" hidden>Address<input name="address" placeholder="192.168.20.2/24"></label><label data-for="static" hidden>Gateway<input name="gateway" placeholder="192.168.20.1"></label>' +
        '<label data-for="pppoe" hidden>PPPoE user<input name="user" autocomplete="off"></label><label data-for="pppoe" hidden>Password<input name="password" type="password" autocomplete="new-password"></label>' +
        '<div class="lh-form-foot"><button class="btn btn-primary" type="submit"><i class="bi bi-plus-lg"></i> Add the line</button><small>' + (st.setup.status === 'active' ? 'It connects at once but carries customers only after you tick it in step 1 and apply.' : 'It starts as a backup line (it never takes customers from your main line).') + '</small></div></form>'
        : '<p class="lh-mini mb-0">No free port: every port is in a bridge, carries a LAN or is already an internet line. Free one in Router control first.</p>') + '</div>';
    [].forEach.call(p.querySelectorAll('[data-open]'), function (b) { b.onclick = function () { openDrawer(b.dataset.open); }; });
    var f = p.querySelector('[data-add]');
    if (f) {
      var sync = function () { var t = f.querySelector('input[name=type]:checked').value; [].forEach.call(f.querySelectorAll('[data-for]'), function (x) { x.hidden = x.dataset.for !== t; x.querySelector('input').required = x.dataset.for === t; }); };
      [].forEach.call(f.querySelectorAll('input[name=type]'), function (r) { r.onchange = sync; }); sync();
      f.addEventListener('submit', function (e) {
        e.preventDefault();
        var fd = new FormData(f), d = { action: 'add' }; fd.forEach(function (v, k) { d[k] = v; });
        confirmThen({ title: 'Make ' + d.port + ' an internet line?', icon: 'bi-plus-circle', text: d.label + ' (' + d.type.toUpperCase() + ') on ' + d.port + '. NAT is added for it too.', ok: 'Add it' }, d, { btn: f.querySelector('button[type=submit]'), lines: true });
      });
    }
  }

  function renderNat() {
    var p = pane('nat'), st = S.st;
    p.innerHTML = '<div class="lh-card"><div class="lh-card-h"><h4><i class="bi bi-shuffle"></i> NAT per line</h4><small>NAT hides customers behind the line’s own address. Every normal internet line needs it on.</small></div><div class="lh-list">' +
      st.lines.filter(function (l) { return !l.missing; }).map(function (l) {
        return '<div class="lh-row"><span class="lh-cdot" style="--c:' + COLORS[l.color % 6] + '"></span><div class="lh-row-main"><b>' + esc(l.label) + '</b><small>' +
          (l.nat.rules.length ? l.nat.rules.map(function (x) { return (x.disabled ? 'off: ' : '') + x.action + ' via ' + (x.via === 'port' ? 'port ' + l.interface : x.via) + (x.comment ? ' — ' + x.comment : ''); }).map(esc).join(' · ') : 'no NAT rule yet') + '</small></div>' +
          sw('nat:' + l.interface, l.nat.on, l.nat.on ? 'On' : 'Off', '', false) + '</div>';
      }).join('') + '</div></div>';
    [].forEach.call(p.querySelectorAll('[data-sw^="nat:"]'), function (el) {
      el.addEventListener('change', function () {
        var iface = el.dataset.sw.slice(4), l = line(iface), on = el.checked;
        confirmThen({ title: (on ? 'Turn NAT on for ' : 'Turn NAT off for ') + l.label + '?', icon: 'bi-shuffle', tone: on ? '' : 'danger', text: on ? 'Customers are hidden behind this line’s address.' : 'Only for a provider that routes your own addresses. Customers on this line would get no internet.' },
          { action: 'nat', interface: iface, on: on }).then(function (r) { if (!r || !r.ok) el.checked = !on; });
      });
    });
  }

  function renderFw() {
    var p = pane('fw'), fw = S.st.firewall;
    p.innerHTML = '<div class="lh-card"><div class="lh-card-h"><h4><i class="bi bi-shield-lock"></i> Protection</h4><small>One switch each. Every rule is marked “TapTap Line Hub” on the router and goes away when switched off.</small></div>' +
      '<div class="lh-presets">' + fw.presets.map(function (x) {
        return '<div class="lh-preset' + (x.on ? ' on' : '') + '"><div class="lh-preset-ic"><i class="bi ' + (x.on ? 'bi-shield-fill-check' : 'bi-shield') + '"></i></div><div><b>' + esc(x.title) + '</b><p>' + esc(x.about) + '</p></div>' + sw('pre:' + x.key, x.on, x.on ? 'On' : 'Off', '', false) + '</div>';
      }).join('') + '</div></div>' +
      '<div class="lh-card"><div class="lh-card-h"><h4><i class="bi bi-list-check"></i> Firewall rules on the router</h4><small>Switch a rule off or on. TapTap’s own rules are locked here — they are managed by the page that made them.</small></div>' +
      (fw.rules.length ? '<div class="table-responsive"><table class="table align-middle lh-table small"><thead><tr><th>#</th><th>Chain</th><th>Action</th><th>Matches</th><th>Comment</th><th class="text-end">Traffic</th><th></th></tr></thead><tbody>' +
        fw.rules.map(function (r, i) {
          return '<tr class="' + (r.disabled ? 'off' : '') + '"><td class="text-secondary">' + i + '</td><td>' + esc(r.chain) + '</td><td><span class="lh-act ' + esc(r.action) + '">' + esc(r.action) + '</span></td><td class="font-monospace">' + esc(r.what || 'everything') + '</td><td>' + esc(r.comment) + '</td><td class="text-end">' + (r.bytes ? (r.bytes >= 1073741824 ? (r.bytes / 1073741824).toFixed(1) + ' GB' : (r.bytes / 1048576).toFixed(1) + ' MB') : '—') + '</td>' +
            '<td>' + (r.protected ? '<i class="bi bi-lock-fill text-secondary" title="Managed by TapTap"></i>' : sw('rule:' + r.id, !r.disabled, '', '', false)) + '</td></tr>';
        }).join('') + '</tbody></table></div>' : '<p class="lh-mini mb-0">No firewall rules found.</p>') + '</div>';
    [].forEach.call(p.querySelectorAll('[data-sw^="pre:"]'), function (el) {
      el.addEventListener('change', function () {
        var key = el.dataset.sw.slice(4), x = fw.presets.filter(function (y) { return y.key === key; })[0], on = el.checked;
        confirmThen({ title: (on ? 'Turn on: ' : 'Turn off: ') + x.title + '?', icon: 'bi-shield-lock', text: x.about }, { action: 'firewall', preset: key, on: on }).then(function (r) { if (!r || !r.ok) el.checked = !on; });
      });
    });
    [].forEach.call(p.querySelectorAll('[data-sw^="rule:"]'), function (el) {
      el.addEventListener('change', function () {
        var id = el.dataset.sw.slice(5), r0 = fw.rules.filter(function (y) { return y.id === id; })[0], on = el.checked;
        confirmThen({ title: (on ? 'Switch on' : 'Switch off') + ' this rule?', icon: 'bi-list-check', tone: on ? '' : 'danger', html: '<p class="font-monospace small">' + esc(r0.chain + ' ' + r0.action + ' ' + (r0.what || '') + ' ' + (r0.comment ? '— ' + r0.comment : '')) + '</p><p class="small text-secondary mb-0">' + (r0.chain === 'input' ? 'Rules on the router’s own input are protected by an undo timer: if TapTap loses the router, it is put back by itself.' : 'Takes effect at once.') + '</p>' },
          { action: 'rule', id: id, on: on }).then(function (r) { if (!r || !r.ok) el.checked = !on; });
      });
    });
  }

  function renderDhcp() {
    var p = pane('dhcp'), list = S.st.dhcp, opts = ['10m', '30m', '1h', '2h', '6h', '12h', '1d', '3d'];
    p.innerHTML = '<div class="lh-card"><div class="lh-card-h"><h4><i class="bi bi-diagram-3"></i> DHCP servers</h4><small>Who hands out addresses on your networks. Short leases free addresses of people who left; long ones keep sleeping phones signed in.</small></div>' +
      (list.length ? '<div class="lh-dhcp">' + list.map(function (s) {
        var lt = opts.indexOf(s.lease_time) < 0 ? [s.lease_time].concat(opts) : opts;
        return '<form class="lh-dh' + (s.disabled ? ' off' : '') + '" data-srv="' + esc(s.name) + '"><div class="lh-dh-h"><div><b>' + esc(s.name) + '</b>' + (s.hotspot ? ' <span class="lh-badge link">HotSpot</span>' : '') + '<small>' + esc(s.interface) + (s.network ? ' · ' + esc(s.network) : '') + '</small></div>' + sw('dh:' + s.name, !s.disabled, '', '', false) + '</div>' +
          '<div class="lh-dh-fig"><div><b>' + s.leases + '</b><span>addresses given</span></div><div><b class="font-monospace">' + esc(s.ranges || '—') + '</b><span>pool ' + esc(s.pool) + '</span></div></div>' +
          '<label>Lease time<select name="lease_time">' + lt.map(function (o) { return '<option' + (o === s.lease_time ? ' selected' : '') + '>' + esc(o) + '</option>'; }).join('') + '</select></label>' +
          '<label>DNS servers<input name="dns" value="' + esc(s.dns) + '" placeholder="empty = the router itself"></label>' +
          '<button class="btn btn-sm btn-primary" type="submit"><i class="bi bi-check2"></i> Save</button></form>';
      }).join('') + '</div>' : '<p class="lh-mini mb-0">No DHCP server on this router.</p>') + '</div>';
    [].forEach.call(p.querySelectorAll('form[data-srv]'), function (f) {
      f.addEventListener('submit', function (e) {
        e.preventDefault();
        act({ action: 'dhcp', server: f.dataset.srv, lease_time: f.lease_time.value, dns: f.dns.value }, { btn: f.querySelector('button') });
      });
    });
    [].forEach.call(p.querySelectorAll('[data-sw^="dh:"]'), function (el) {
      el.addEventListener('change', function () {
        var name = el.dataset.sw.slice(3), s = list.filter(function (x) { return x.name === name; })[0], on = el.checked;
        confirmThen({ title: (on ? 'Switch on ' : 'Switch off ') + name + '?', icon: 'bi-diagram-3', tone: on ? '' : 'danger',
          text: on ? 'Devices on ' + s.interface + ' get addresses again.' : 'New devices on ' + s.interface + ' get no address' + (s.hotspot ? ' — HotSpot customers cannot connect' : '') + '. Devices that already have one keep it until the lease ends.' },
        { action: 'dhcp', server: name, enabled: on, sure: true }).then(function (r) { if (!r || !r.ok) el.checked = !on; });
      });
    });
  }

  function renderLink() {
    var p = pane('link'), st = S.st, cur = st.link.interface;
    var opts = [{ v: '', t: 'Automatic', s: 'TapTap follows the customers’ lines and moves when one fails.', ok: true, icon: 'bi-magic' }].concat(
      st.lines.filter(function (l) { return !l.missing; }).map(function (l) { var ok = l.up && l.use !== 'out'; return { v: l.interface, t: l.label, s: ok ? l.interface + ' · ' + l.type.toUpperCase() : 'not available — ' + lineState(l)[1].toLowerCase(), ok: ok, icon: 'bi-hdd-network', color: COLORS[l.color % 6] }; }));
    p.innerHTML = '<div class="lh-card"><div class="lh-card-h"><h4><i class="bi bi-broadcast"></i> Which line carries TapTap</h4><small>' + esc(st.link.channel) + '. Pick the steadiest line for TapTap’s own connection to this router; customers are not affected.</small></div>' +
      '<div class="lh-linkopts" role="radiogroup">' + opts.map(function (o) {
        return '<label class="lh-lo' + (o.v === cur ? ' on' : '') + (o.ok ? '' : ' dis') + '"' + (o.color ? ' style="--c:' + o.color + '"' : '') + '><input type="radio" name="lhlink" value="' + esc(o.v) + '"' + (o.v === cur ? ' checked' : '') + (o.ok ? '' : ' disabled') + '>' +
          '<i class="bi ' + o.icon + '"></i><b>' + esc(o.t) + '</b><small>' + esc(o.s) + '</small>' + (o.v === cur && cur ? '<span class="lh-badge ' + (st.link.active ? 'link' : 'warn') + '">' + (st.link.active ? 'active' : 'route waiting') + '</span>' : '') + '</label>';
      }).join('') + '</div>' +
      '<p class="lh-mini">TapTap’s addresses: <span class="font-monospace">' + esc((st.link.addresses || []).join(', ') || 'unknown (SITE_URL not set)') + '</span>. A route per address is added with a check: if the chosen line stops, TapTap’s traffic simply takes the other lines.</p></div>';
    [].forEach.call(p.querySelectorAll('input[name=lhlink]'), function (r) {
      r.addEventListener('change', function () {
        var v = r.value, l = line(v);
        confirmThen({ title: v ? 'Send TapTap through ' + l.label + '?' : 'Let TapTap choose automatically?', icon: 'bi-broadcast', text: v ? 'TapTap Link and Tunnel use this line; customers stay where they are.' : 'TapTap follows the customers’ lines again.' },
          { action: 'link', interface: v }).then(function (res) { if (!res || !res.ok) renderLink(); });
      });
    });
  }

  function renderShare() {
    var p = pane('share'), st = S.st, sel = st.lines.filter(function (l) { return l.selected; });
    p.innerHTML = '<div class="lh-card"><div class="lh-card-h"><h4><i class="bi bi-sliders"></i> Load sharing</h4><small>' + esc(st.setup.strategy_label ? 'Now: ' + st.setup.strategy_label + (st.setup.status === 'active' ? ' (applied)' : st.setup.status === 'pending' ? ' (waiting for “Keep”)' : ' (designed, not applied)') : 'Not designed yet') + '</small></div>' +
      (sel.length ? '<div class="lh-sharegrid">' + sel.map(function (l) {
        return '<div class="lh-sh" style="--c:' + COLORS[l.color % 6] + '"><b>' + esc(l.label) + '</b><div class="lh-sh-bars"><div><span>Designed</span><i style="width:' + (l.share || 0) + '%"></i><em>' + (l.share != null ? l.share + '%' : 'by network') + '</em></div>' +
          '<div><span>Real now</span><i class="real" style="width:' + (l.real || 0) + '%"></i><em>' + (l.real || 0) + '%</em></div></div>' +
          (st.setup.strategy === 'failover' && l.order !== 0 ? '<button type="button" class="btn btn-sm btn-light" data-main="' + esc(l.interface) + '"><i class="bi bi-star"></i> Make main</button>' : '') + '</div>';
      }).join('') + '</div>' : '<p class="lh-mini">No line is in your design yet.</p>') +
      '<div class="d-flex gap-2 flex-wrap mt-2"><button type="button" class="btn btn-primary btn-sm" data-go="step2"><i class="bi bi-sliders"></i> Change how the lines work together</button><button type="button" class="btn btn-light btn-sm" data-go="step1"><i class="bi bi-ui-checks"></i> Choose lines and speeds</button></div></div>';
    [].forEach.call(p.querySelectorAll('[data-go]'), function (b) { b.onclick = function () { var t = document.getElementById(b.dataset.go); if (t) { t.scrollIntoView({ behavior: 'smooth', block: 'start' }); t.classList.add('lh-flash'); setTimeout(function () { t.classList.remove('lh-flash'); }, 1800); } }; });
    [].forEach.call(p.querySelectorAll('[data-main]'), function (b) { b.onclick = function () { document.dispatchEvent(new CustomEvent('tt:wan-main', { detail: { interface: b.dataset.main } })); }; });
  }

  load(false);
})();
