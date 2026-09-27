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
      '<div class="pi-tabs" role="tablist"><button class="on" data-t="overview">Overview</button><button data-t="devices">Devices <span id="piDevN"></span></button><button data-t="config">Configuration</button></div>' +
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
      header(); liveStrip(); body();
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

  document.addEventListener('click', function (e) {
    var p = e.target.closest('.router-port[data-port]'); if (!p) return;
    open(p.dataset.router, p.dataset.port, p.dataset.routerName);
  });
  document.addEventListener('keydown', function (e) {
    if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('.router-port[data-port]')) { e.preventDefault(); e.target.click(); }
  });
})();
