/* TapTap Internet Lines designer */
(function () {
  var $ = function (s, r) { return (r || document).querySelector(s); }, $$ = function (s, r) { return [].slice.call((r || document).querySelectorAll(s)); };
  var U = JSON.parse($('#wanUrls').textContent), ST = JSON.parse($('#wanState').textContent);
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var COLORS = ['#5aa1ff', '#3ddc97', '#ffb547', '#c792ff', '#ff7a90', '#4fd1e0'];
  var HOSTS = [['8.8.8.8', 'Google'], ['1.1.1.1', 'Cloudflare'], ['9.9.9.9', 'Quad9'], ['208.67.222.222', 'OpenDNS'], ['8.8.4.4', 'Google 2'], ['1.0.0.1', 'Cloudflare 2']];
  var saved = ST.config || {};
  var S = { facts: null, manual: false, lines: [], strategy: saved.strategy || '', primary: saved.primary || '', sticky: saved.sticky || 'customer',
            weights: saved.weights || {}, splits: saved.splits || [], health: saved.health !== false, preview: null, status: ST.status, confirmBy: ST.confirm_by, touchedStrategy: !!saved.strategy };
  $('#health').checked = S.health;

  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ''; }
  function post(url, body) { return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(body || {}) }).then(function (r) { return r.json().then(function (d) { d._ok = r.ok; return d; }); }); }

  /* ───────── detection ───────── */
  function detect() {
    $('#lines').innerHTML = '<div class="detect-state"><span class="spinner-border spinner-border-sm"></span> Asking ' + esc(U.router) + ' which connections it has…</div>';
    fetch(U.detect).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.success) throw new Error(d.message);
      S.facts = d.facts; S.manual = false;
      if (d.current && d.current.method) { $('#nowMethod').textContent = 'Right now: ' + d.current.method; $('#nowDesc').textContent = d.current.description || ''; }
      var byIf = {}; (saved.links || []).forEach(function (l) { byIf[l.interface] = l; });
      S.lines = d.facts.links.map(function (l, i) {
        var s = byIf[l.interface] || {};
        return { interface: l.interface, type: l.type, gateway: l.type === 'static' ? (s.gateway || l.gateway) : l.gateway, label: s.label || l.label || (l.lte ? '4G / LTE' : l.interface),
                 speed: s.speed || (l.lte ? 20 : 50), check: s.check || HOSTS[i % HOSTS.length][0], enabled: saved.links ? !!byIf[l.interface] : (l.type !== 'static' || !!l.gateway),
                 running: l.running, status: l.status, detected: true };
      });
      (saved.links || []).forEach(function (l) { if (!S.lines.some(function (x) { return x.interface === l.interface; })) S.lines.push(Object.assign({ enabled: true, detected: false }, l)); });
      if (saved.links) { var order = saved.links.map(function (l) { return l.interface; }); S.lines.sort(function (a, b) { var ia = order.indexOf(a.interface), ib = order.indexOf(b.interface); return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib); }); }
      if (!S.splits.length) S.splits = (d.facts.lans || []).map(function (n) { return { network: n.network, label: n.label, interface: '' }; });
      renderAll();
    }).catch(function (e) {
      S.facts = { v7: true, links: [], lans: [], manual: true }; S.manual = true;
      if (!S.lines.length) S.lines = (saved.links || []).map(function (l) { return Object.assign({ enabled: true }, l); });
      $('#nowMethod').textContent = 'Router not reachable';
      $('#nowDesc').textContent = 'You can still design the setup and download a script to paste into WinBox → Terminal.';
      renderAll();
      $('#lines').insertAdjacentHTML('afterbegin', '<div class="alert alert-warning small">Could not reach ' + esc(U.router) + ': ' + esc(e.message) +
        '<br>Add your lines by hand below. <label class="ms-2">RouterOS <select id="rosVer" class="form-select form-select-sm d-inline-block w-auto"><option value="7">v7</option><option value="6">v6</option></select></label></div>');
      $('#rosVer').onchange = function () { S.facts.v7 = this.value === '7'; preview(); };
    });
  }

  /* ───────── step 1: lines ───────── */
  function renderLines() {
    var box = $('#lines');
    if (!S.lines.length) { box.innerHTML = '<div class="detect-state"><i class="bi bi-info-circle"></i> No internet connections found. Add one by hand, or set a port’s role to WAN in Router control.</div>'; return; }
    box.innerHTML = S.lines.map(function (l, i) {
      var st = l.detected === false ? '' : (l.running && (l.type !== 'dhcp' || /bound/i.test(l.status || '')) ? 'up' : (l.type === 'static' && l.running ? 'up' : 'down'));
      var hostOpts = HOSTS.map(function (h) { return '<option value="' + h[0] + '"' + (l.check === h[0] ? ' selected' : '') + '>' + h[0] + ' · ' + h[1] + '</option>'; }).join('');
      if (l.check && !HOSTS.some(function (h) { return h[0] === l.check; })) hostOpts += '<option selected>' + esc(l.check) + '</option>';
      return '<div class="line-card ' + (l.enabled ? 'on' : 'off') + '" data-i="' + i + '"><div class="tog"><input type="checkbox" data-f="enabled"' + (l.enabled ? ' checked' : '') + ' aria-label="Use ' + esc(l.label) + '"></div>' +
        '<div class="top"><input class="nm" data-f="label" value="' + esc(l.label) + '" list="isps" aria-label="Line name">' +
        (l.detected === false ? '<input class="form-control form-control-sm" style="max-width:140px" data-f="interface" value="' + esc(l.interface) + '" placeholder="interface e.g. ether2" aria-label="Interface">' +
          '<select class="form-select form-select-sm" style="max-width:110px" data-f="type" aria-label="Type"><option value="static"' + (l.type === 'static' ? ' selected' : '') + '>Static</option><option value="dhcp"' + (l.type === 'dhcp' ? ' selected' : '') + '>DHCP</option><option value="pppoe"' + (l.type === 'pppoe' ? ' selected' : '') + '>PPPoE</option></select>' +
          '<button class="btn btn-sm btn-link text-danger p-0" data-rm="1">Remove</button>'
          : '<span class="ifc">' + esc(l.interface) + '</span><span class="typ">' + ({ dhcp: 'DHCP', pppoe: 'PPPoE', static: 'Static' }[l.type]) + '</span><span class="dot ' + st + '" title="' + esc(l.status || '') + '"></span><small class="text-secondary">' + esc(l.status || '') + '</small>') + '</div>' +
        '<div class="fields"><div><label>Gateway</label><input data-f="gateway" value="' + esc(l.gateway) + '"' + (l.type !== 'static' && l.detected !== false ? ' readonly title="Taken from the ' + l.type.toUpperCase() + ' connection automatically"' : '') + ' placeholder="' + (l.type === 'pppoe' ? 'automatic' : 'e.g. 192.168.1.1') + '"></div>' +
        '<div><label>Speed (Mbps)</label><input type="number" min="1" max="10000" data-f="speed" value="' + esc(l.speed) + '"></div>' +
        '<div><label>Health check</label><select data-f="check">' + hostOpts + '</select></div></div></div>';
    }).join('');
    $$('.line-card', box).forEach(function (card) {
      var l = S.lines[+card.dataset.i];
      $$('[data-f]', card).forEach(function (el) {
        el.addEventListener(el.type === 'checkbox' || el.tagName === 'SELECT' ? 'change' : 'input', function () {
          var f = el.dataset.f; l[f] = el.type === 'checkbox' ? el.checked : (f === 'speed' ? +el.value : el.value.trim());
          if (f === 'speed') { S.weights = {}; }
          if (f === 'enabled' || f === 'type') { renderAll(); return; }
          renderStrats(); renderOpts(); preview();
        });
      });
      var rm = $('[data-rm]', card); if (rm) rm.onclick = function () { S.lines.splice(+card.dataset.i, 1); renderAll(); };
    });
  }
  $('#addLine').onclick = function () { var used = S.lines.map(function (l) { return l.check; }); S.lines.push({ interface: '', type: 'static', gateway: '', label: 'New line', speed: 20, enabled: true, detected: false, check: (HOSTS.filter(function (h) { return used.indexOf(h[0]) < 0; })[0] || HOSTS[0])[0] }); renderAll(); };
  $('#redetect').onclick = detect;
  $('#health').onchange = function () { S.health = this.checked; preview(); };

  function enabled() { return S.lines.filter(function (l) { return l.enabled; }); }

  /* ───────── step 2: strategy ───────── */
  var ICON = {
    single: '<path d="M10 23H150" stroke="#1769e0" stroke-width="5" stroke-linecap="round"/>',
    failover: '<path d="M10 16H150" stroke="#1769e0" stroke-width="5" stroke-linecap="round"/><path d="M10 32H150" stroke="#9aa8b8" stroke-width="3" stroke-dasharray="5 6" stroke-linecap="round"/>',
    balance: '<path d="M10 23C60 23 70 10 150 10" stroke="#1769e0" stroke-width="6" fill="none" stroke-linecap="round"/><path d="M10 23C60 23 70 36 150 36" stroke="#18a66a" stroke-width="3.5" fill="none" stroke-linecap="round"/>',
    ecmp: '<path d="M10 23C60 23 70 8 150 8M10 23H150M10 23C60 23 70 38 150 38" stroke="#7c3aed" stroke-width="3" fill="none" stroke-linecap="round"/>',
    split: '<path d="M10 10C70 10 80 36 150 36" stroke="#18a66a" stroke-width="4" fill="none" stroke-linecap="round"/><path d="M10 36C70 36 80 10 150 10" stroke="#1769e0" stroke-width="4" fill="none" stroke-linecap="round"/>'
  };
  var STRATS = [['single', 'One line', 'Everything uses one connection. Others stay idle.'], ['failover', 'Backup line', 'One main line; a backup takes over the moment it fails.'],
    ['balance', 'Share the load', 'Customers spread across lines by speed. Each customer keeps one line.'], ['ecmp', 'Combine lines', 'Every connection may use any line. Simplest spread.'],
    ['split', 'Split by network', 'Send HotSpot, office or staff networks down different lines.']];
  function recommend() {
    var e = enabled(); if (e.length < 2) return 'single';
    var sp = e.map(function (l) { return +l.speed || 1; }), mx = Math.max.apply(null, sp), mn = Math.min.apply(null, sp);
    return mx / mn >= 4 ? 'failover' : 'balance';
  }
  function renderStrats() {
    var n = enabled().length, rec = recommend();
    if (!S.touchedStrategy || (n < 2 && S.strategy !== 'single')) S.strategy = rec;
    $('#strats').innerHTML = STRATS.map(function (s) {
      var dis = s[0] !== 'single' && n < 2;
      return '<button type="button" class="strat' + (S.strategy === s[0] ? ' on' : '') + '" data-s="' + s[0] + '"' + (dis ? ' disabled title="Needs two or more lines"' : '') + '>' +
        (s[0] === rec && n ? '<span class="rec">Recommended</span>' : '') + '<svg viewBox="0 0 160 46" aria-hidden="true">' + ICON[s[0]] + '</svg><b>' + s[1] + '</b><small>' + s[2] + '</small></button>';
    }).join('');
    $$('#strats .strat').forEach(function (b) { b.onclick = function () { S.strategy = b.dataset.s; S.touchedStrategy = true; renderStrats(); renderOpts(); preview(); }; });
  }

  function weightsAuto() {
    var e = enabled(), sp = e.map(function (l) { return Math.max(.1, +l.speed || 1); }), tot = sp.reduce(function (a, b) { return a + b; }, 0), best = null;
    for (var n = e.length; n <= 12; n++) {
      var w = sp.map(function (s) { return Math.max(1, Math.round(s / tot * n)); }), sw = w.reduce(function (a, b) { return a + b; }, 0);
      var err = w.reduce(function (a, wi, i) { return a + Math.abs(wi / sw - sp[i] / tot); }, 0);
      if (!best || err < best[0] - 1e-9) best = [err, w];
    }
    var out = {}; e.forEach(function (l, i) { out[l.interface] = best[1][i]; }); return out;
  }
  function renderOpts() {
    var e = enabled(), box = $('#opts'), h = '';
    if (!e.length) { box.innerHTML = ''; return; }
    if (S.strategy === 'single') {
      if (!e.some(function (l) { return l.interface === S.primary; })) S.primary = e[0].interface;
      h = '<h4>Which line?</h4><div class="choice">' + e.map(function (l) { return '<label><input type="radio" name="primary" value="' + esc(l.interface) + '"' + (S.primary === l.interface ? ' checked' : '') + '><span><b>' + esc(l.label) + '</b><small>' + esc(l.interface) + ' · ' + l.speed + ' Mbps</small></span></label>'; }).join('') + '</div>';
    } else if (S.strategy === 'failover') {
      h = '<h4>Order of lines</h4><div class="prio">' + e.map(function (l, i) { return '<div><span class="rank">' + (i === 0 ? 'Main' : 'Backup ' + i) + '</span><b class="flex-grow-1">' + esc(l.label) + '</b><button data-up="' + esc(l.interface) + '" aria-label="Move up"' + (i === 0 ? ' disabled' : '') + '><i class="bi bi-arrow-up"></i></button><button data-down="' + esc(l.interface) + '" aria-label="Move down"' + (i === e.length - 1 ? ' disabled' : '') + '><i class="bi bi-arrow-down"></i></button></div>'; }).join('') + '</div>' +
        '<p class="small text-secondary mt-2 mb-0">Traffic returns to the main line on its own once it is healthy again.</p>';
    } else if (S.strategy === 'balance') {
      var auto = weightsAuto(); e.forEach(function (l) { if (!S.weights[l.interface]) S.weights[l.interface] = auto[l.interface]; });
      var tot = e.reduce(function (a, l) { return a + (+S.weights[l.interface] || 1); }, 0);
      h = '<h4>How much each line gets</h4><div class="share-bar">' + e.map(function (l, i) { return '<i style="width:' + (S.weights[l.interface] / tot * 100) + '%;background:' + COLORS[i % 6] + '"></i>'; }).join('') + '</div>' +
        e.map(function (l, i) { return '<div class="share-row"><span><i class="bi bi-circle-fill" style="color:' + COLORS[i % 6] + ';font-size:.6rem"></i> ' + esc(l.label) + '</span><input type="range" min="1" max="12" value="' + S.weights[l.interface] + '" data-w="' + esc(l.interface) + '" aria-label="Share for ' + esc(l.label) + '"><output>' + Math.round(S.weights[l.interface] / tot * 100) + '%</output></div>'; }).join('') +
        '<button class="btn btn-link btn-sm p-0" id="wReset">Match line speeds</button>' +
        '<h4 class="mt-3">When a customer browses</h4><div class="choice"><label><input type="radio" name="sticky" value="customer"' + (S.sticky === 'customer' ? ' checked' : '') + '><span><b>Each customer stays on one line</b> <span class="badge text-bg-success">Best for HotSpot</span><small>Logins, banking, WhatsApp calls and games keep working. Spreads by customer, so a heavy user fills one line.</small></span></label>' +
        '<label><input type="radio" name="sticky" value="connection"' + (S.sticky === 'connection' ? ' checked' : '') + '><span><b>Spread every connection</b><small>Evens out traffic best, but a customer may appear from two addresses and get logged out of some sites.</small></span></label></div>';
    } else if (S.strategy === 'ecmp') {
      h = '<div class="fit-note small text-secondary">All selected lines are used equally, whatever their speed. For unequal lines, “Share the load” gives better results.</div>';
    } else {
      h = '<h4>Which network uses which line</h4>' + S.splits.map(function (sp, i) {
        return '<div class="split-row"><input value="' + esc(sp.label || '') + '" data-sp="' + i + '" data-k="label" placeholder="Name e.g. HotSpot" aria-label="Network name"><input value="' + esc(sp.network) + '" data-sp="' + i + '" data-k="network" placeholder="10.5.50.0/24" aria-label="Network"><select data-sp="' + i + '" data-k="interface" aria-label="Line"><option value="">Normal (main + backup)</option>' +
          e.map(function (l) { return '<option value="' + esc(l.interface) + '"' + (sp.interface === l.interface ? ' selected' : '') + '>' + esc(l.label) + '</option>'; }).join('') + '</select><button class="btn btn-sm btn-link text-danger p-0" data-sprm="' + i + '" aria-label="Remove"><i class="bi bi-x-lg"></i></button></div>';
      }).join('') + '<button class="btn btn-sm btn-light" id="spAdd"><i class="bi bi-plus"></i> Add a network</button>' +
        '<p class="small text-secondary mt-2 mb-0">Networks left on “Normal” use ' + esc(e[0].label) + ' and fall back to the others.</p>';
    }
    box.innerHTML = h;
    $$('input[name=primary]', box).forEach(function (r) { r.onchange = function () { S.primary = r.value; preview(); }; });
    $$('input[name=sticky]', box).forEach(function (r) { r.onchange = function () { S.sticky = r.value; preview(); }; });
    $$('[data-up],[data-down]', box).forEach(function (b) { b.onclick = function () {
      var id = b.dataset.up || b.dataset.down, i = S.lines.findIndex(function (l) { return l.interface === id; }), d = b.dataset.up ? -1 : 1, j = i + d;
      while (S.lines[j] && !S.lines[j].enabled) j += d; if (!S.lines[j]) return;
      var t = S.lines[i]; S.lines[i] = S.lines[j]; S.lines[j] = t; renderLines(); renderOpts(); preview(); }; });
    $$('[data-w]', box).forEach(function (r) { r.oninput = function () { S.weights[r.dataset.w] = +r.value; renderOpts(); preview(); var n = $('[data-w="' + r.dataset.w + '"]'); if (n) n.focus(); }; });
    var wr = $('#wReset', box); if (wr) wr.onclick = function () { S.weights = {}; renderOpts(); preview(); };
    $$('[data-sp]', box).forEach(function (el) { el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', function () { S.splits[+el.dataset.sp][el.dataset.k] = el.value.trim(); preview(); }); });
    $$('[data-sprm]', box).forEach(function (b) { b.onclick = function () { S.splits.splice(+b.dataset.sprm, 1); renderOpts(); preview(); }; });
    var sa = $('#spAdd', box); if (sa) sa.onclick = function () { S.splits.push({ network: '', label: '', interface: '' }); renderOpts(); };
  }

  /* ───────── config + preview ───────── */
  function config() {
    return { strategy: S.strategy, primary: S.primary, sticky: S.sticky, health: S.health, weights: S.weights,
             splits: S.splits.filter(function (s) { return s.interface && s.network; }),
             links: S.lines.map(function (l) { return { interface: l.interface, type: l.type, gateway: l.gateway, label: l.label, speed: l.speed, check: l.check, enabled: l.enabled }; }) };
  }
  var pt, seq = 0;
  function preview() {
    clearTimeout(pt); pt = setTimeout(function () {
      var my = ++seq;
      post(U.preview, { config: config(), manual: S.manual, facts: S.manual ? S.facts : null }).then(function (d) {
        if (my !== seq) return; S.preview = d; renderReview(); drawFlow();
      });
    }, 300);
  }

  function renderReview() {
    var d = S.preview || {}, ok = d.errors && !d.errors.length;
    $('#applyBtn').disabled = !ok || S.manual;
    $('#applyNote').textContent = S.manual ? 'The router is not reachable from TapTap, so apply by pasting the downloaded script into WinBox → New Terminal (press Ctrl+X first for Safe Mode).'
      : 'With the safety timer on, the router puts everything back by itself if you don’t confirm — so a mistake can’t lock you out for good.';
    if (!ok) { $('#review').innerHTML = '<p class="text-secondary small mb-0">Fix the points shown in the preview to continue.</p>'; $('#chips').innerHTML = ''; return; }
    $('#review').innerHTML = '<ul class="small mb-2 ps-3">' + d.summary.map(function (s) { return '<li class="mb-1">' + esc(s) + '</li>'; }).join('') + '</ul>' +
      (d.untouched && d.untouched.length ? '<p class="small text-secondary">Not used: ' + d.untouched.map(function (l) { return esc(l.label || l.interface); }).join(', ') + ' — stays connected but stops adding its own default route.</p>' : '');
    var names = { lists: 'list entries', tables: 'routing tables', routes: 'routes', mangle: 'traffic marks', rules: 'network rules', nat: 'NAT rules', clients: 'client settings' };
    $('#chips').innerHTML = Object.keys(d.counts || {}).map(function (k) { return '<span>' + d.counts[k] + ' ' + (names[k] || k) + '</span>'; }).join('') + '<span>everything tagged “TapTap WAN”</span>';
    var sb = $('#script'); sb.innerHTML = esc(d.script).replace(/^(#.*)$/gm, '<span class="c">$1</span>');
  }
  $('#showScript').onclick = function () { var s = $('#script'); s.hidden = !s.hidden; };
  $('#dlScript').addEventListener('click', function (e) { if (!(S.preview && S.preview.errors && !S.preview.errors.length)) { e.preventDefault(); alert('Fix the problems in the preview first.'); } });

  /* ───────── diagram ───────── */
  function drawFlow(health) {
    var d = S.preview || {}, flows = d.flows || enabled().map(function (l) { return { id: l.interface, interface: l.interface, label: l.label, share: null, role: '' }; });
    var lans = (S.facts && S.facts.lans || []).slice(0, 3), n = Math.max(flows.length, 1), H = Math.max(220, 70 * n + 40), svg = $('#flow');
    svg.setAttribute('viewBox', '0 0 440 ' + H);
    var cy = H / 2, rx = 190, out = '';
    out += '<defs><filter id="glow"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>';
    out += '<circle class="node" cx="52" cy="' + cy + '" r="36"/><text class="big" x="52" y="' + (cy - 3) + '" text-anchor="middle">Customers</text><text class="small" x="52" y="' + (cy + 12) + '" text-anchor="middle">' + esc(lans.length ? lans.length + ' network' + (lans.length > 1 ? 's' : '') : 'your LAN') + '</text>';
    out += '<path class="wire" d="M88 ' + cy + 'H' + (rx - 26) + '"/><path class="flow" d="M88 ' + cy + 'H' + (rx - 26) + '" stroke="#5aa1ff" stroke-width="4" stroke-dasharray="8 4"/>';
    out += '<rect class="node router" x="' + (rx - 26) + '" y="' + (cy - 26) + '" width="52" height="52" rx="14"/><text class="big" x="' + rx + '" y="' + (cy + 5) + '" text-anchor="middle">⇄</text><text class="small" x="' + rx + '" y="' + (cy + 42) + '" text-anchor="middle">' + esc(U.router) + '</text>';
    flows.forEach(function (f, i) {
      var y = n === 1 ? cy : 36 + i * ((H - 72) / (n - 1)), color = COLORS[i % 6];
      var hs = health && health.filter(function (h) { return h.interface === (f.interface || f.id); })[0];
      var down = hs && hs.ping && hs.ping.ok === false, share = f.share, standby = share === 0;
      var w = share == null ? 3 : Math.max(2, 2 + share / 12);
      var path = 'M' + (rx + 26) + ' ' + cy + 'C' + (rx + 70) + ' ' + cy + ' ' + (rx + 50) + ' ' + y + ' ' + 300 + ' ' + y;
      out += '<path class="wire" d="' + path + '"/><path class="flow' + (down ? ' down' : (standby ? ' standby' : '')) + '" d="' + path + '" stroke="' + color + '" stroke-width="' + w + '" stroke-dasharray="' + (share > 50 ? '10 3' : '6 6') + '" style="animation-duration:' + (share ? Math.max(.5, 1.6 - share / 100) : 1.4) + 's"/>';
      out += '<rect class="node" x="300" y="' + (y - 22) + '" width="132" height="44" rx="12" style="stroke:' + (down ? '#e5484d' : (hs && hs.ping && hs.ping.ok ? '#3ddc97' : color)) + '"/>';
      var sub = hs && hs.ping && hs.ping.ok !== null && hs.ping.ok !== undefined ? (hs.ping.ok ? 'Online · ' + (hs.ping.rtt || '') : 'Not answering') : ((share != null ? share + '% · ' : '') + (f.role || ''));
      out += '<text x="312" y="' + (y - 4) + '">' + esc(String(f.label).slice(0, 17)) + '</text><text class="small" x="312" y="' + (y + 11) + '">' + esc(sub.slice(0, 24)) + '</text>';
    });
    if (!flows.length) out += '<text class="small" x="300" y="' + cy + '">Tick a line to begin</text>';
    svg.innerHTML = out;
    $('#flowSub').textContent = S.preview && S.preview.config ? (STRATS.filter(function (s) { return s[0] === S.strategy; })[0] || [])[1] || 'Live preview' : 'Live preview';
    $('#flowSum').innerHTML = (d.summary || []).slice(0, 4).map(function (s) { return '<li>' + esc(s) + '</li>'; }).join('');
    $('#flowMsgs').innerHTML = (d.errors || []).map(function (e) { return '<div class="flow-err"><i class="bi bi-exclamation-octagon"></i> ' + esc(e) + '</div>'; }).join('') +
      (d.warnings || []).map(function (w) { return '<div class="flow-warn"><i class="bi bi-info-circle"></i> ' + esc(w) + '</div>'; }).join('');
  }

  /* ───────── apply / banner ───────── */
  $('#applyBtn').onclick = function () {
    var mins = +$('#undoMin').value, d = S.preview;
    var msg = 'Apply “' + $('#flowSub').textContent + '” to ' + U.router + '?\n\n' + d.summary.slice(0, 3).join('\n') + '\n\n' +
      (mins ? 'Safety timer: if you don’t press “Keep” within ' + mins + ' minutes, the router undoes everything by itself.' : 'No safety timer: a mistake must be undone by hand.');
    if (!confirm(msg)) return;
    var b = this; b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Applying…';
    post(U.apply, { config: config(), undo_minutes: mins }).then(function (r) {
      b.innerHTML = '<i class="bi bi-lightning-charge"></i> Apply to ' + esc(U.router); b.disabled = false;
      if (!r.success) { banner('failed', r.message); return; }
      S.status = r.status; S.confirmBy = r.confirm_by; saved = config(); ST.applied_at = new Date().toISOString(); banner(r.status, null, r); drawFlow(r.health); window.scrollTo({ top: 0, behavior: 'smooth' });
      fetch(U.detect).then(function (x) { return x.json(); }).then(function (x) { if (x.success && x.current && x.current.method) { $('#nowMethod').textContent = 'Right now: ' + x.current.method; $('#nowDesc').textContent = x.current.description || ''; } });
    }).catch(function (e) { b.disabled = false; banner('failed', 'Lost contact with TapTap while applying (' + e.message + '). If the safety timer was on, the router will undo the change by itself.'); });
  };

  function healthHtml(list) {
    if (!list || !list.length) return '';
    return '<div class="health">' + list.map(function (h) {
      if (h.error) return '<div><i class="bi bi-exclamation-triangle"></i> ' + esc(h.error) + '</div>';
      var p = h.ping || {}, cls = p.ok === true ? 'ok' : (p.ok === false ? 'bad' : 'unk');
      var txt = p.ok === true ? 'Online · ' + (p.rtt || '') + (p.loss ? ' · ' + p.loss + '% loss' : '') : (p.ok === false ? 'No answer' : 'Not tested');
      return '<div><i class="bi bi-hdd-network"></i><b>' + esc(h.label) + '</b><small class="text-secondary">' + (h.route_active ? 'route active' : (h.has_route ? 'standing by' : 'no main route')) + '</small><span class="st ' + cls + '">' + txt + '</span></div>';
    }).join('') + '</div>';
  }
  var timer;
  function banner(status, message, data) {
    clearInterval(timer); var box = $('#banner'); data = data || {};
    if (status === 'pending' && S.confirmBy) {
      box.innerHTML = '<div class="wan-banner pending"><div class="cd-ring" id="cdRing"><b id="cdTxt">5:00</b></div><div class="flex-grow-1"><h3>Applied. Is the internet still working?</h3><p>Open a website on a customer device. If all is well, keep the changes. Otherwise do nothing — the router undoes them by itself when the timer runs out.</p>' + healthHtml(data.health) + '</div>' +
        '<div class="d-flex flex-column gap-2"><button class="btn btn-success" id="keepBtn"><i class="bi bi-check2-circle"></i> Keep changes</button><button class="btn btn-outline-danger btn-sm" id="undoBtn">Undo now</button></div></div>';
      var end = new Date(S.confirmBy).getTime(), total = Math.max(1, end - Date.now());
      var tick = function () { var left = Math.max(0, end - Date.now()), m = Math.floor(left / 60000), s = Math.floor(left % 60000 / 1000);
        var t = $('#cdTxt'); if (!t) return; t.textContent = m + ':' + String(s).padStart(2, '0'); $('#cdRing').style.setProperty('--p', left / total * 100);
        if (left <= 0) { clearInterval(timer); setTimeout(refreshStatus, 4000); } };
      tick(); timer = setInterval(tick, 1000);
      $('#keepBtn').onclick = function () { this.disabled = true; post(U.confirm).then(function (r) { if (r.success) { S.status = 'active'; banner('active', null, data); } else banner('failed', r.message); }); };
    } else if (status === 'active') {
      box.innerHTML = '<div class="wan-banner active"><i class="bi bi-shield-check fs-3 text-success"></i><div class="flex-grow-1"><h3>Your internet lines are set up by TapTap</h3><p>' + esc((STRATS.filter(function (s) { return s[0] === (saved.strategy || S.strategy); })[0] || [])[1] || '') + (ST.applied_at ? ' · applied ' + new Date(ST.applied_at).toLocaleString() : '') + '</p><div id="hBox">' + healthHtml(data.health) + '</div></div>' +
        '<div class="d-flex flex-column gap-2"><button class="btn btn-light btn-sm" id="checkBtn"><i class="bi bi-activity"></i> Check lines</button><button class="btn btn-outline-danger btn-sm" id="undoBtn">Undo TapTap setup</button></div></div>';
      $('#checkBtn').onclick = function () { var b = this; b.disabled = true; fetch(U.status).then(function (r) { return r.json(); }).then(function (r) { b.disabled = false; $('#hBox').innerHTML = r.success ? healthHtml(r.health) : '<p class="text-danger small">' + esc(r.message) + '</p>'; if (r.success) drawFlow(r.health); }); };
    } else if (status === 'failed') {
      box.innerHTML = '<div class="wan-banner failed"><i class="bi bi-x-octagon fs-3 text-danger"></i><div class="flex-grow-1"><h3>That didn’t work</h3><p>' + esc(message || ST.last_result && ST.last_result.error || '') + '</p></div><button class="btn btn-outline-danger btn-sm" id="undoBtn">Clean up</button></div>';
    } else if (status === 'undone') {
      box.innerHTML = '<div class="wan-banner" style="background:#f1f4f7;border:1px solid #dde4ec"><i class="bi bi-arrow-counterclockwise fs-4"></i><div><h3>The TapTap setup was undone</h3><p>The router is back to its own internet settings. Your design is kept below — apply it again when ready.</p></div></div>';
    } else box.innerHTML = '';
    var ub = $('#undoBtn'); if (ub) ub.onclick = function () {
      if (!confirm('Remove everything TapTap added and put the router’s own internet settings back?')) return;
      ub.disabled = true; post(U.undo).then(function (r) { if (r.success) { S.status = 'undone'; banner('undone'); drawFlow(); $('#nowMethod').textContent = 'Right now: the router’s own settings'; } else { ub.disabled = false; alert(r.message); } });
    };
  }
  function refreshStatus() { fetch(U.status).then(function (r) { return r.json(); }).then(function (r) { if (r.success) { S.status = r.status; S.confirmBy = r.confirm_by; banner(r.status, null, r); } }); }

  function renderAll() { renderLines(); renderStrats(); renderOpts(); preview(); drawFlow(); }
  banner(S.status, null, ST.last_result || {});
  if (S.status === 'pending') setTimeout(refreshStatus, 1500);
  detect();
})();
