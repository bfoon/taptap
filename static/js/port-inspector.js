/* TapTap front-panel port inspector.
 * Click a port → configuration, devices on it, data counters and live speed.
 */
(function () {
  'use strict';
  var me = document.currentScript, PORT_URL = me.dataset.portUrl, CONTROL_URL = me.dataset.controlUrl;
  var drawer, bs, state = null, timer = null, hist = [];

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function bytes(n) { n = Number(n) || 0; var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2) : n) + ' ' + u[i]; }
  function bps(v) { v = Number(v) || 0; if (v >= 1e9) return (v / 1e9).toFixed(2) + ' Gb/s'; if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e8 ? 0 : 1) + ' Mb/s'; if (v >= 1e3) return Math.round(v / 1e3) + ' kb/s'; return Math.round(v) + ' b/s'; }
  function num(n) { return (Number(n) || 0).toLocaleString(); }
  function url(base, id) { return base.replace('/0/', '/' + id + '/'); }

  function build() {
    drawer = document.createElement('div');
    drawer.className = 'offcanvas offcanvas-end pi'; drawer.tabIndex = -1; drawer.setAttribute('aria-labelledby', 'piTitle');
    drawer.innerHTML = '<div class="offcanvas-header"><div class="pi-title"><small id="piRouter"></small><h5 id="piTitle"></h5><div class="pi-pills" id="piPills"></div></div>' +
      '<button type="button" class="btn-close" data-bs-dismiss="offcanvas" aria-label="Close"></button></div>' +
      '<div class="pi-live"><div class="in"><span id="piInL">In</span><b id="piIn">—</b></div><div class="out"><span id="piOutL">Out</span><b id="piOut">—</b></div>' +
      '<div class="pi-spark"><svg viewBox="0 0 300 50" preserveAspectRatio="none"><polyline id="piSpIn" fill="none" stroke="#44c2ff" stroke-width="2" vector-effect="non-scaling-stroke"/><polyline id="piSpOut" fill="none" stroke="#b69cff" stroke-width="2" vector-effect="non-scaling-stroke"/></svg></div>' +
      '<div class="pi-rate" id="piRate">Waiting for the router…</div></div>' +
      '<div class="pi-tabs" role="tablist"><button class="on" data-t="overview">Overview</button><button data-t="devices">Devices <span id="piDevN"></span></button><button data-t="config">Configuration</button><button data-t="control"><i class="bi bi-sliders"></i> Control</button></div>' +
      '<div id="piErr"></div><div class="offcanvas-body pi-body" id="piBody"></div>';
    document.body.appendChild(drawer);
    bs = new bootstrap.Offcanvas(drawer);
    drawer.addEventListener('hidden.bs.offcanvas', function () { clearTimeout(timer); state = null; });
    [].forEach.call(drawer.querySelectorAll('[data-t]'), function (b) {
      b.addEventListener('click', function () { [].forEach.call(drawer.querySelectorAll('[data-t]'), function (x) { x.classList.toggle('on', x === b); }); if (state) { state.tab = b.dataset.t; body(); } });
    });
  }

  function open(routerId, name, routerName) {
    if (!drawer) build();
    clearTimeout(timer); hist = [];
    state = { router: routerId, name: name, tab: 'overview', data: null, live: null };
    [].forEach.call(drawer.querySelectorAll('[data-t]'), function (x) { x.classList.toggle('on', x.dataset.t === 'overview'); });
    drawer.querySelector('#piRouter').textContent = routerName || 'Router';
    drawer.querySelector('#piTitle').textContent = name;
    drawer.querySelector('#piPills').innerHTML = '';
    drawer.querySelector('#piBody').innerHTML = '<div class="pi-empty"><span class="spinner-border spinner-border-sm"></span> Loading…</div>';
    drawer.querySelector('#piIn').textContent = drawer.querySelector('#piOut').textContent = '—';
    drawer.querySelector('#piErr').innerHTML = '';
    bs.show();
    load(false);
  }

  function load(live) {
    var s = state; if (!s) return;
    var u = url(PORT_URL, s.router) + '?name=' + encodeURIComponent(s.name) + (live ? '&live=1' : '');
    fetch(u, { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
      if (state !== s) return;
      if (!d.success) { drawer.querySelector('#piBody').innerHTML = '<div class="pi-empty">' + esc(d.message || 'Could not load this port.') + '</div>'; return; }
      s.data = d;
      if (d.live) {
        if (d.live.error) drawer.querySelector('#piErr').innerHTML = '<div class="pi-err"><i class="bi bi-wifi-off"></i> Live data unavailable: ' + esc(d.live.error) + '. Showing the last saved values.</div>';
        else { drawer.querySelector('#piErr').innerHTML = ''; s.live = d.live; hist.push([d.live.rx_bps || 0, d.live.tx_bps || 0]); hist = hist.slice(-60); }
      }
      header(); liveStrip(); if (s.tab !== 'control' || !drawer.querySelector('#piBody .pc-card')) body();
      var delay = d.router.status === 'Online' ? 3000 : 0;
      if (delay) timer = setTimeout(function () { load(true); }, live ? delay : 150);
      else drawer.querySelector('#piRate').textContent = 'Router is offline — showing the last saved values.';
    }).catch(function (e) {
      if (state !== s) return;
      drawer.querySelector('#piErr').innerHTML = '<div class="pi-err">Connection problem: ' + esc(e.message) + '. Retrying…</div>';
      timer = setTimeout(function () { load(true); }, 6000);
    });
  }

  function isWan(d) { return d.role === 'wan' || !!d.wan; }

  function header() {
    var d = state.data, L = state.live || {}, up = L.running != null ? L.running : d.running;
    var pills = ['<span class="' + (d.disabled ? 'down' : up ? 'up' : 'down') + '">' + (d.disabled ? 'Disabled' : up ? 'Link up' : 'No link') + '</span>', '<span>' + esc(d.role_label) + '</span>'];
    if (d.bridge) pills.push('<span>Bridge ' + esc(d.bridge) + '</span>');
    if (d.wan) pills.push('<span>Internet · ' + esc(d.wan.state) + (d.wan.expected_share != null ? ' · ' + d.wan.expected_share + '%' : '') + '</span>');
    if (d.comment) pills.push('<span>' + esc(d.comment) + '</span>');
    drawer.querySelector('#piPills').innerHTML = pills.join('');
    drawer.querySelector('#piDevN').textContent = d.devices.length ? '(' + d.devices.length + ')' : '';
  }

  function liveStrip() {
    var d = state.data, L = state.live, wan = isWan(d);
    drawer.querySelector('#piInL').textContent = wan ? 'Download (from Internet)' : 'In — from devices (their upload)';
    drawer.querySelector('#piOutL').textContent = wan ? 'Upload (to Internet)' : 'Out — to devices (their download)';
    if (L) { drawer.querySelector('#piIn').textContent = bps(L.rx_bps); drawer.querySelector('#piOut').textContent = bps(L.tx_bps); }
    var max = Math.max(1, Math.max.apply(null, hist.map(function (h) { return Math.max(h[0], h[1]); })));
    function pts(i) { return hist.map(function (h, k) { return (hist.length > 1 ? k * 300 / (hist.length - 1) : 0).toFixed(1) + ',' + (48 - h[i] / max * 44).toFixed(1); }).join(' '); }
    drawer.querySelector('#piSpIn').setAttribute('points', pts(0)); drawer.querySelector('#piSpOut').setAttribute('points', pts(1));
    var e = (L && L.ethernet) || {}, bits = [];
    if (e.rate) bits.push('Link speed <b>' + esc(e.rate) + '</b>' + (e['full-duplex'] != null ? ' · ' + (String(e['full-duplex']) === 'true' ? 'full duplex' : 'half duplex') : ''));
    if (e['sfp-module-present'] === 'true' || e['sfp-type']) bits.push('SFP ' + esc([e['sfp-vendor-name'], e['sfp-type'], e['sfp-wavelength']].filter(Boolean).join(' ')) + (e['sfp-rx-power'] ? ' · RX ' + esc(e['sfp-rx-power']) : ''));
    if (e['poe-out'] && e['poe-out'] !== 'off') bits.push('PoE ' + esc(e['poe-out']));
    if (L && !bits.length) bits.push('Live every 3 seconds');
    if (bits.length) drawer.querySelector('#piRate').innerHTML = bits.join(' · ');
  }

  function body() {
    var d = state.data, box = drawer.querySelector('#piBody');
    if (state.tab === 'devices') return devices(box, d);
    if (state.tab === 'config') return config(box, d);
    if (state.tab === 'control') return control(box, d);
    var c = (state.live && state.live.counters) || d.counters, wan = isWan(d);
    var rx = c['rx-byte'], tx = c['tx-byte'];
    var h = '<div class="pi-stats">' +
      '<div class="pi-stat"><span>' + (wan ? 'Downloaded' : 'Received from devices') + '</span><b>' + bytes(rx) + '</b><small>' + num(c['rx-packet']) + ' packets</small></div>' +
      '<div class="pi-stat"><span>' + (wan ? 'Uploaded' : 'Sent to devices') + '</span><b>' + bytes(tx) + '</b><small>' + num(c['tx-packet']) + ' packets</small></div>' +
      '<div class="pi-stat"><span>Link up since</span><b style="font-size:.9rem">' + esc(c['last-link-up-time'] || (d.running ? 'unknown' : 'not connected')) + '</b><small>' + num(c['link-downs']) + ' link drop' + (Number(c['link-downs']) === 1 ? '' : 's') + (c['last-link-down-time'] ? ' · last down ' + esc(c['last-link-down-time']) : '') + '</small></div>' +
      '<div class="pi-stat"><span>Errors &amp; drops</span><b>' + num((+c['rx-error'] || 0) + (+c['tx-error'] || 0) + (+c['rx-drop'] || 0) + (+c['tx-drop'] || 0) + (+c['tx-queue-drop'] || 0)) + '</b><small>rx err ' + num(c['rx-error']) + ' · tx err ' + num(c['tx-error']) + ' · drops ' + num((+c['rx-drop'] || 0) + (+c['tx-drop'] || 0) + (+c['tx-queue-drop'] || 0)) + '</small></div>' +
      '</div><p class="pi-note">Totals are RouterOS interface counters: they count from the last router restart or counter reset, not only from the last link-up.</p>';
    h += '<div class="pi-stats"><div class="pi-stat"><span>Devices on this port</span><b>' + d.devices.filter(function (x) { return x.online; }).length + '</b><small>' + d.devices.length + ' remembered</small></div>' +
      '<div class="pi-stat"><span>Switches / APs / routers</span><b>' + d.neighbors.length + '</b><small>' + esc(d.neighbors.map(function (n) { return n.identity || n.board || n.address; }).slice(0, 3).join(', ') || 'none detected') + '</small></div>' +
      '<div class="pi-stat"><span>MAC address</span><b style="font-size:.85rem;font-family:ui-monospace,monospace">' + esc(d.mac || '—') + '</b><small>MTU ' + esc(d.mtu || '—') + ' · ' + esc(d.type || '') + '</small></div>' +
      '<div class="pi-stat"><span>Saved by discovery</span><b style="font-size:.85rem">' + new Date(d.saved_at).toLocaleString() + '</b><small><a href="' + url(CONTROL_URL, d.router.id) + '">Open Control Center</a></small></div></div>';
    if (d.neighbors.length) h += '<b class="small d-block mb-2">Network gear on this port</b>' + d.neighbors.map(function (n) { return '<div class="pi-dev"><i class="bi bi-hdd-network"></i><div><b>' + esc(n.identity || n.board || 'Device') + '</b><small>' + esc([n.address, n.mac, n.board, n.version && 'v' + n.version].filter(Boolean).join(' · ')) + '</small></div><span class="dot' + (n.online ? '' : ' off') + '"></span></div>'; }).join('');
    box.innerHTML = h;
  }

  function devices(box, d) {
    if (!d.devices.length) { box.innerHTML = '<div class="pi-empty">No devices learned on this port yet.</div>'; return; }
    box.innerHTML = '<input class="form-control form-control-sm pi-search" placeholder="Filter by name, IP or MAC" aria-label="Filter devices"><div id="piDevs">' + d.devices.map(function (x) {
      return '<div class="pi-dev" data-s="' + esc([x.name, x.ip, x.mac, x.identified].join(' ').toLowerCase()) + '"><i class="bi ' + (x.kind === 'wifi' ? 'bi-wifi' : 'bi-pc-display') + '"></i><div><b>' + esc(x.name || x.ip || x.mac || 'Device') + '</b>' +
        '<small>' + esc([x.ip, x.mac, x.via && 'via ' + x.via].filter(Boolean).join(' · ')) + '</small><small>' + esc(x.sources || '') + '</small>' + (x.identified ? '<span class="id"><i class="bi bi-fingerprint"></i> ' + esc(x.identified) + '</span>' : '') + '</div><span class="dot' + (x.online ? '' : ' off') + '" title="' + (x.online ? 'Online' : 'Offline') + '"></span></div>';
    }).join('') + '</div>';
    var q = box.querySelector('.pi-search');
    q.addEventListener('input', function () { var v = q.value.toLowerCase(); [].forEach.call(box.querySelectorAll('[data-s]'), function (r) { r.hidden = r.dataset.s.indexOf(v) < 0; }); });
  }

  function config(box, d) {
    if (!d.sections.length) { box.innerHTML = '<div class="pi-empty">No configuration saved. Run discovery or refresh the configuration.</div>'; return; }
    box.innerHTML = d.sections.map(function (sec, i) {
      return '<details class="pi-sec"' + (i < 2 ? ' open' : '') + '><summary>' + esc(sec.title) + '<small>' + sec.rows.length + '</small></summary>' + sec.rows.map(function (row) {
        return '<div class="pi-kv">' + Object.keys(row).sort().map(function (k) { var v = row[k]; if (typeof v === 'object') v = JSON.stringify(v); return '<div>' + esc(k) + '</div><div>' + esc(v) + '</div>'; }).join('') + '</div>';
      }).join('') + '</details>';
    }).join('') + '<p class="pi-note">Change these settings in the <a href="' + url(CONTROL_URL, d.router.id) + '">Control Center</a>.</p>';
  }


  /* ───────────── Control: power, speed limit, traffic guard, router ───────────── */
  var ACTION_URL = me.dataset.actionUrl, REBOOT_URL = me.dataset.rebootUrl, BACKUPS_URL = me.dataset.backupsUrl;
  var token = (document.cookie.match(/(?:^|; )csrftoken=([^;]+)/) || [])[1] || '';
  function post(u, fields) {
    var fd = new FormData(); Object.keys(fields).forEach(function (k) { if (fields[k] != null) fd.append(k, fields[k]); });
    return fetch(u, { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'X-CSRFToken': decodeURIComponent(token) } })
      .then(function (r) { return r.json().then(function (j) { j.status = r.status; return j; }); });
  }
  function note(box, msg, bad) { var n = box.querySelector('.pc-note'); n.className = 'pc-note ' + (bad ? 'bad' : 'good'); n.textContent = msg; n.hidden = !msg; }
  function until(iso) { if (!iso) return ''; var d = new Date(iso), m = Math.max(0, Math.round((d - Date.now()) / 60000)); return 'until ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + (m ? ' (' + m + ' min)' : ''); }
  function portAction(box, d, action, extra, btn) {
    var fields = Object.assign({ name: d.name, action: action }, extra || {});
    var old = btn ? btn.innerHTML : ''; if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>'; }
    return post(url(ACTION_URL, d.router.id), fields).then(function (r) {
      if (r.needs_confirm) {
        var typed = prompt(r.message, '');
        if (typed === d.name) return portAction(box, d, action, Object.assign({}, extra, { confirm: typed }), btn);
        note(box, 'Cancelled — nothing was changed.', true); return;
      }
      note(box, r.message, !r.ok);
      if (r.control) { d.control = r.control; }
      if (r.ok && ['enable', 'disable', 'restart', 'off_for'].indexOf(action) >= 0) setTimeout(function () { load(true); }, 800);
      control(box, d, true);
    }).catch(function (e) { note(box, 'Could not reach TapTap: ' + e.message, true); })
      .then(function () { if (btn) { btn.disabled = false; btn.innerHTML = old; } });
  }
  function control(box, d, keepNote) {
    var c = d.control || { rules: [] }, L = state.live || {}, up = L.running != null ? L.running : d.running;
    var byKind = {}; c.rules.forEach(function (r) { byKind[r.kind] = r; });
    var lim = byKind.limit, g = byKind.guard, off = byKind.timed_off;
    var prevNote = keepNote && box.querySelector('.pc-note') ? box.querySelector('.pc-note').outerHTML : '<div class="pc-note" hidden></div>';
    var h = prevNote;
    if (c.risk_text) h += '<div class="pc-risk"><i class="bi bi-exclamation-triangle"></i> ' + esc(c.risk_text) + '</div>';
    // power
    h += '<section class="pc-card"><h6><i class="bi bi-power"></i> Port power <span class="pc-state ' + (d.disabled ? 'off' : up ? 'on' : 'idle') + '">' + (d.disabled ? 'Off' : up ? 'On · link up' : 'On · no cable/device') + '</span></h6>' +
      (off ? '<p class="pc-sub"><i class="bi bi-clock-history"></i> Turned off for a while — comes back on by itself ' + until(off.restore_at) + '.</p>' : '') +
      '<div class="pc-row">' + (d.disabled ? '<button class="btn btn-sm btn-success" data-a="enable"><i class="bi bi-play-fill"></i> Turn on</button>' : '<button class="btn btn-sm btn-outline-danger" data-a="disable"><i class="bi bi-stop-fill"></i> Turn off</button>') +
      '<div class="pc-inline"><button class="btn btn-sm btn-outline-primary" data-a="restart"><i class="bi bi-arrow-clockwise"></i> Restart</button><select class="form-select form-select-sm" id="pcRestartSecs" aria-label="Seconds off"><option value="5">5 s off</option><option value="15">15 s off</option><option value="30">30 s off</option></select></div>' +
      '<div class="pc-inline"><button class="btn btn-sm btn-outline-warning" data-a="off_for"><i class="bi bi-hourglass-split"></i> Off for</button><select class="form-select form-select-sm" id="pcOffMins" aria-label="How long"><option value="15">15 min</option><option value="60">1 hour</option><option value="240">4 hours</option><option value="720">12 hours</option><option value="1440">1 day</option></select></div></div>' +
      '<p class="pc-hint">Restart and “off for” are also scheduled on the router itself, so the port comes back even if TapTap loses the connection.</p></section>';
    // limit
    h += '<section class="pc-card"><h6><i class="bi bi-speedometer"></i> Speed limit' + (lim ? ' <span class="pc-state on">' + lim.limit_down + ' ↓ / ' + lim.limit_up + ' ↑ Mb/s' + (lim.restore_at ? ' · ' + until(lim.restore_at) : '') + '</span>' : ' <span class="pc-state idle">No limit</span>') + '</h6>' +
      '<div class="pc-grid"><label>Download to devices<div class="input-group input-group-sm"><input class="form-control" id="pcDown" inputmode="decimal" value="' + (lim ? lim.limit_down : '') + '" placeholder="e.g. 10"><span class="input-group-text">Mb/s</span></div></label>' +
      '<label>Upload from devices<div class="input-group input-group-sm"><input class="form-control" id="pcUp" inputmode="decimal" value="' + (lim ? lim.limit_up : '') + '" placeholder="e.g. 5"><span class="input-group-text">Mb/s</span></div></label>' +
      '<label>For<select class="form-select form-select-sm" id="pcLimMins"><option value="0">Until I remove it</option><option value="60">1 hour</option><option value="240">4 hours</option><option value="720">12 hours</option><option value="1440">1 day</option></select></label></div>' +
      '<div class="pc-row"><button class="btn btn-sm btn-primary" data-a="limit">' + (lim ? 'Update limit' : 'Apply limit') + '</button>' + (lim ? '<button class="btn btn-sm btn-light" data-a="unlimit">Remove limit</button>' : '') + '</div>' +
      '<p class="pc-hint">A RouterOS simple queue on this port. Traffic bridged between two devices on the same switch is not limited.</p></section>';
    // guard
    h += '<section class="pc-card"><h6><i class="bi bi-shield-shaded"></i> Traffic guard ' + (g ? (g.active ? '<span class="pc-state off">Triggered · ' + until(g.restore_at) + '</span>' : g.enabled ? '<span class="pc-state on">Watching</span>' : '<span class="pc-state idle">Paused</span>') : '<span class="pc-state idle">Off</span>') + '</h6>' +
      '<p class="pc-sub">When this port stays above a speed, slow it down or switch it off for a while, then put it back automatically.' + (g && g.times ? ' Triggered ' + g.times + ' time' + (g.times > 1 ? 's' : '') + '.' : '') + (g && g.error ? ' <span class="text-danger">Last problem: ' + esc(g.error) + '</span>' : '') + '</p>' +
      '<div class="pc-grid"><label>If speed reaches<div class="input-group input-group-sm"><input class="form-control" id="pcThr" inputmode="decimal" value="' + (g ? g.threshold : 50) + '"><span class="input-group-text">Mb/s</span></div></label>' +
      '<label>Direction<select class="form-select form-select-sm" id="pcDir"><option value="down">Download</option><option value="up">Upload</option><option value="any">Either</option></select></label>' +
      '<label>For at least<select class="form-select form-select-sm" id="pcSus"><option value="30">30 s</option><option value="60">1 min</option><option value="180">3 min</option><option value="300">5 min</option><option value="900">15 min</option></select></label>' +
      '<label>Then<select class="form-select form-select-sm" id="pcAct"><option value="throttle">Slow it down</option><option value="shutdown"' + (c.risk ? ' disabled' : '') + '>Turn the port off' + (c.risk ? ' (not allowed here)' : '') + '</option></select></label>' +
      '<label id="pcThrottleWrap">Slow to<div class="input-group input-group-sm"><input class="form-control" id="pcThrottle" inputmode="decimal" value="' + (g ? g.throttle : 2) + '"><span class="input-group-text">Mb/s</span></div></label>' +
      '<label>For<select class="form-select form-select-sm" id="pcHold"><option value="5">5 min</option><option value="10">10 min</option><option value="30">30 min</option><option value="60">1 hour</option><option value="240">4 hours</option></select></label></div>' +
      '<div class="pc-row"><button class="btn btn-sm btn-primary" data-a="guard_save">' + (g ? 'Save guard' : 'Turn guard on') + '</button>' + (g ? '<button class="btn btn-sm btn-light" data-a="guard_delete">Remove guard</button>' : '') + '</div></section>';
    // router
    h += '<section class="pc-card"><h6><i class="bi bi-router"></i> ' + esc(d.router.name) + '</h6>' +
      '<div class="pc-row"><button class="btn btn-sm btn-outline-primary" data-a="backup"><i class="bi bi-cloud-arrow-down"></i> Back up now</button><button class="btn btn-sm btn-outline-danger" data-a="reboot"><i class="bi bi-bootstrap-reboot"></i> Reboot router</button></div>' +
      '<label class="form-check form-switch mt-2"><input class="form-check-input" type="checkbox" id="pcAuto"' + (c.auto_backup ? ' checked' : '') + '> <span class="form-check-label">Back up automatically every night (02:00–05:00)</span></label>' +
      '<div class="pc-backups" id="pcBackups"><small class="text-secondary">' + (c.backups ? 'Loading backups…' : 'No backups yet.') + '</small></div></section>';
    box.innerHTML = h;
    function sel(id, v) { var e = box.querySelector('#' + id); if (e && v != null) e.value = String(v); }
    if (g) { sel('pcDir', g.direction); sel('pcSus', g.sustain); sel('pcAct', g.action); sel('pcHold', g.hold); }
    var act = box.querySelector('#pcAct'), tw = box.querySelector('#pcThrottleWrap');
    function syncAct() { tw.style.display = act.value === 'throttle' ? '' : 'none'; } act.onchange = syncAct; syncAct();
    box.querySelectorAll('[data-a]').forEach(function (b) {
      b.onclick = function () {
        var a = b.dataset.a;
        if (a === 'disable' && !confirm('Turn off ' + d.name + '? Devices on it lose their connection until you turn it back on.')) return;
        if (a === 'restart') return portAction(box, d, 'restart', { seconds: box.querySelector('#pcRestartSecs').value }, b);
        if (a === 'off_for') return portAction(box, d, 'off_for', { minutes: box.querySelector('#pcOffMins').value }, b);
        if (a === 'limit') return portAction(box, d, 'limit', { down: box.querySelector('#pcDown').value || 0, up: box.querySelector('#pcUp').value || 0, minutes: box.querySelector('#pcLimMins').value }, b);
        if (a === 'guard_save') return portAction(box, d, 'guard_save', { threshold: box.querySelector('#pcThr').value, direction: box.querySelector('#pcDir').value, sustain: box.querySelector('#pcSus').value,
          guard_action: act.value, throttle: box.querySelector('#pcThrottle').value, hold: box.querySelector('#pcHold').value }, b);
        if (a === 'guard_delete' && !confirm('Remove the traffic guard from ' + d.name + '?')) return;
        if (a === 'reboot') {
          var typed = prompt('Reboot ' + d.router.name + '? Every customer is disconnected for 1–2 minutes.\nType the router name to confirm:', '');
          if (typed == null) return;
          b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Rebooting…';
          return post(url(REBOOT_URL, d.router.id), { confirm: typed }).then(function (r) { note(box, r.message, !r.ok); b.disabled = false; b.innerHTML = '<i class="bi bi-bootstrap-reboot"></i> Reboot router'; });
        }
        if (a === 'backup') {
          b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Backing up…';
          return post(url(BACKUPS_URL, d.router.id), {}).then(function (r) { note(box, r.message, !r.ok); loadBackups(box, d); })
            .catch(function (e) { note(box, e.message, true); }).then(function () { b.disabled = false; b.innerHTML = '<i class="bi bi-cloud-arrow-down"></i> Back up now'; });
        }
        portAction(box, d, a, {}, b);
      };
    });
    box.querySelector('#pcAuto').onchange = function () { var on = this.checked; post(url(BACKUPS_URL, d.router.id), { action: 'auto', on: on ? '1' : '0' }).then(function (r) { note(box, r.message, !r.ok); if (d.control) d.control.auto_backup = on; }); };
    if (c.backups) loadBackups(box, d);
  }
  function loadBackups(box, d) {
    var el = box.querySelector('#pcBackups'); if (!el) return;
    fetch(url(BACKUPS_URL, d.router.id), { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
      el.innerHTML = j.items.length ? '<div class="pc-blist">' + j.items.slice(0, 8).map(function (b) {
        return '<div class="pc-b"><i class="bi ' + (b.automatic ? 'bi-moon-stars' : 'bi-archive') + '"></i><div><b>' + new Date(b.at).toLocaleString() + '</b><small>' +
          esc([b.backup_file && 'binary backup', b.export_file && 'text export', b.version && 'RouterOS ' + b.version].filter(Boolean).join(' · ')) + (b.error ? ' · <span class="text-danger">' + esc(b.error) + '</span>' : '') + '</small></div>' +
          '<a class="btn btn-sm btn-light" href="' + url(BACKUPS_URL, d.router.id) + b.id + '/download/" title="' + (b.downloadable ? 'Download the .rsc export' : 'Download TapTap’s configuration snapshot (the backup files are on the router under Files)') + '"><i class="bi bi-download"></i> ' + (b.downloadable ? '.rsc' : '.json') + '</a></div>';
      }).join('') + '</div><p class="pc-hint">The .backup and .rsc files are also kept on the router (WinBox › Files). A .backup restores only on the same router model.</p>' : '<small class="text-secondary">No backups yet.</small>';
    }).catch(function () { el.innerHTML = '<small class="text-danger">Could not load backups.</small>'; });
  }

  document.addEventListener('click', function (e) {
    var p = e.target.closest('.router-port[data-port]'); if (!p) return;
    open(p.dataset.router, p.dataset.port, p.dataset.routerName);
  });
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('.router-port[data-port]')) { e.preventDefault(); e.target.click(); }
  });
})();
