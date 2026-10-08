/* TapTap — My connection: from this device through every Wi-Fi box to the MikroTik and on to the Internet.
   The router measures each stop toward you (core/pathtrace.py); this page measures the device's own speed and,
   while it downloads, asks the router to measure every stop again — the stop where replies turn slow marks the slow link. */
(function () {
  var mp = document.getElementById('mp'), root = document.getElementById('nt'); if (!mp || !root) return;
  var RUN = root.dataset.run, TEST = root.dataset.test, ROUTER = root.dataset.router, VIA = root.dataset.via;
  var U = mp.dataset, LINK = VIA === 'TapTap Link';
  var csrf = (root.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  var $ = function (id) { return document.getElementById(id); };
  var go = $('mpGo'), body = $('mpBody'), chain = $('mpChain'), side = $('mpSide'), pick = $('mpPick'), verdictBox = $('mpVerdict');
  var CF = 'https://speed.cloudflare.com';
  var busy = false, state = {};

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function ms(v) { return v == null ? '—' : (v < 10 ? (+v).toFixed(1) : Math.round(v)) + ' ms'; }
  function sleep(t) { return new Promise(function (r) { setTimeout(r, t); }); }
  function emit(t) { document.dispatchEvent(new CustomEvent('nt:test', { detail: t })); }

  // ── talking to TapTap ──
  function post(url, data) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: JSON.stringify(data) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || !j.ok) throw new Error(j.message || 'TapTap could not do that.'); return j; }); });
  }
  function get(url) { return fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json().then(function (j) { if (!r.ok || !j.ok) throw new Error(j.message || 'TapTap could not do that.'); return j; }); }); }
  function run(kind, data, onWait) {
    data.router = ROUTER; data.kind = kind;
    return post(RUN, data).then(function (j) { emit(j.test); return wait(j.test, onWait); }).then(function (t) {
      emit(t);
      if (t.status === 'failed') throw new Error((t.result || {}).error || 'The router could not run the check.');
      return t;
    });
  }
  function wait(t, onWait) {
    if (t.status !== 'waiting') return Promise.resolve(t);
    var started = Date.now();
    return new Promise(function (resolve, reject) {
      (function poll() {
        setTimeout(function () {
          get(TEST.replace(/0\/$/, t.id + '/')).then(function (j) {
            if (j.test.status === 'waiting' && Date.now() - started < 400000) { onWait && onWait(Math.round((Date.now() - started) / 1000)); poll(); }
            else resolve(j.test);
          }).catch(function () { Date.now() - started < 400000 ? poll() : reject(new Error('Lost contact with TapTap.')); });
        }, 1500);
      })();
    });
  }

  // ── steps / messages ──
  function step(s, st) { var li = document.querySelector('#mpSteps li[data-s="' + s + '"]'); if (li) li.className = st || ''; }
  function say(title, text, st) { $('mpTitle').textContent = title; $('mpText').textContent = text || ''; if (st) mp.dataset.state = st; }
  function resetSteps() { document.querySelectorAll('#mpSteps li').forEach(function (li) { li.className = ''; }); }

  // ── the browser's own private IP, when it shares it (only used to choose between candidates) ──
  function localIp() {
    if (!window.RTCPeerConnection) return Promise.resolve('');
    return new Promise(function (resolve) {
      var pc, found = '', done = false;
      function fin() { if (done) return; done = true; try { pc && pc.close(); } catch (e) {} resolve(found); }
      try {
        pc = new RTCPeerConnection({ iceServers: [] }); pc.createDataChannel('taptap');
        pc.onicecandidate = function (ev) {
          var m = ev.candidate && /(\d{1,3}(?:\.\d{1,3}){3})/.exec(ev.candidate.candidate);
          if (m && /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)/.test(m[1])) { found = m[1]; fin(); }
        };
        pc.createOffer().then(function (o) { return pc.setLocalDescription(o); }).catch(fin);
      } catch (e) { fin(); }
      setTimeout(fin, 1200);
    });
  }

  // ── this device's own measurements ──
  function latency() {
    var url = CF + '/__down?bytes=0', own = false, times = [];
    function one() {
      var t0 = performance.now();
      return fetch((own ? U.blob + '?bytes=0' : url) + '&r=' + Math.random(), { cache: 'no-store', credentials: own ? 'same-origin' : 'omit' })
        .then(function (r) { return r.arrayBuffer(); }).then(function () { times.push(performance.now() - t0); });
    }
    var p = one().catch(function () { own = true; return one(); });
    for (var i = 0; i < 7; i++) p = p.then(one).catch(function () {});
    return p.then(function () { var t = times.slice(1).sort(function (a, b) { return a - b; }); return { rtt: t.length ? Math.round(t[Math.floor(t.length / 2)]) : null, server: own ? 'TapTap' : 'Cloudflare' }; });
  }

  // Keeps several downloads running until stop(); measures Mbit/s after a one-second ramp-up.
  function startDownload(maxSeconds, capBytes) {
    var active = true, bytes = 0, startAt = 0, startBytes = 0, own = false, ctrls = [];
    var t0 = performance.now();
    function url() { return own ? U.blob + '?bytes=25000000' : CF + '/__down?bytes=25000000'; }
    function stream() {
      if (!active) return Promise.resolve();
      var c = new AbortController(); ctrls.push(c);
      return fetch(url() + '&r=' + Math.random(), { cache: 'no-store', signal: c.signal, credentials: own ? 'same-origin' : 'omit' }).then(function (r) {
        if (!r.ok || !r.body) throw new Error('blocked');
        var rd = r.body.getReader();
        function pump() {
          return rd.read().then(function (x) {
            if (x.done || !active) return;
            bytes += x.value.length;
            var now = performance.now();
            if (!startAt && now - t0 > 1000) { startAt = now; startBytes = bytes; }
            if (bytes > capBytes || now - t0 > maxSeconds * 1000) { stop(); return; }
            return pump();
          });
        }
        return pump();
      }).then(stream, function (e) {
        if (!active) return;
        if (!own) { own = true; return stream(); }
        throw e;
      });
    }
    var runs = [stream(), stream(), stream(), stream()];
    function mbps() {
      var now = performance.now();
      var from = startAt || t0, base = startAt ? startBytes : 0;
      var secs = (now - from) / 1000;
      return secs > 0.3 ? Math.round((bytes - base) * 8 / secs / 1e5) / 10 : null;
    }
    function stop() { if (!active) return; active = false; ctrls.forEach(function (c) { try { c.abort(); } catch (e) {} }); }
    return { stop: function () { var m = mbps(); stop(); return { down: m, bytes: bytes, server: own ? 'TapTap' : 'Cloudflare' }; }, mbps: mbps,
             bytes: function () { return bytes; }, active: function () { return active; }, done: Promise.all(runs).catch(function () {}) };
  }

  function upload(seconds) {
    var blob = new Blob([new Uint8Array(1900000)]), sent = 0, own = false, t0 = performance.now(), end = t0 + seconds * 1000;
    function one() {
      if (performance.now() > end) return Promise.resolve();
      return fetch(own ? U.sink : CF + '/__up', { method: 'POST', body: blob, cache: 'no-store', credentials: own ? 'same-origin' : 'omit',
                                                  headers: own ? { 'X-CSRFToken': csrf } : {} })
        .then(function (r) { if (!r.ok) throw new Error('up'); sent += blob.size; return one(); },
              function () { if (!own) { own = true; return one(); } });
    }
    return Promise.all([one(), one()]).then(function () {
      var secs = (performance.now() - t0) / 1000;
      return sent ? Math.round(sent * 8 / secs / 1e5) / 10 : null;
    });
  }

  // ── drawing the line from this device down to the Internet ──
  var ICON = { device: 'bi-phone', box: 'bi-router', mikrotik: 'bi-hdd-rack', gw: 'bi-hdd-network', inet: 'bi-globe2' };
  function metric(st) {
    if (!st) return '<span class="mp-m pending">waiting…</span>';
    if (st.received === 0) return '<span class="mp-m bad">no reply</span>';
    return '<span class="mp-m">' + ms(st.med) + (st.loss ? ' · ' + st.loss + '% lost' : '') + '</span>';
  }
  function node(n, o) {
    o = o || {};
    var meta = [n.ip, n.mac].filter(Boolean).map(esc).join(' · ');
    var make = [n.brand, n.model].filter(Boolean).map(esc).join(' ');
    var badges = (n.nat ? '<span class="mp-b warn">hides devices (NAT)</span>' : '') + (n.source ? '<span class="mp-b">' + esc(n.source) + '</span>' : '') +
                 (n.kind === 'device' ? '<span class="mp-b you">you</span>' : '') + (n.silent ? '<span class="mp-b warn">does not answer ping</span>' : '') +
                 (n.kind === 'box' && !n.ip ? '<span class="mp-b warn">no IP — cannot be measured</span>' : '');
    var nums = '';
    if (o.measured) {
      nums = '<div class="mp-nums"><div><small>quiet</small>' + metric(n.idle) + '</div>' +
             (o.loaded ? '<div><small>under load</small>' + metric(n.load) + '</div>' : '') + '</div>';
    }
    return '<li class="mp-node ' + (n.kind || 'box') + '"><span class="mp-ic"><i class="bi ' + (ICON[n.kind] || ICON.box) + '"></i></span>'
      + '<div class="mp-info"><b>' + esc(n.name || n.ip || 'Unknown box') + '</b>' + (make ? '<em>' + make + '</em>' : '')
      + (meta ? '<code>' + meta + '</code>' : '') + (o.extra || '') + '<div class="mp-badges">' + badges + '</div></div>' + nums + '</li>';
  }
  function link(l, label) {
    var st = l ? l.state : 'pending';
    var bits = [];
    if (l && l.idle_add != null) bits.push('+' + ms(Math.max(0, l.idle_add)) + ' quiet');
    if (l && l.load_add != null) bits.push('+' + ms(Math.max(0, l.load_add)) + ' loaded');
    return '<li class="mp-link ' + st + '"><span class="mp-wire"></span><div>' + (label ? '<small>' + esc(label) + '</small>' : '')
      + (bits.length ? '<span>' + bits.join(' · ') + '</span>' : '') + (l && l.why ? '<p>' + esc(l.why) + '</p>' : '') + '</div></li>';
  }
  function linkLabel(path, i, stops) {
    // label of the link leading to stops[i] from the stop nearer the MikroTik
    if (i === 0) return path.wifi_direct ? 'Wi-Fi into ' + path.router.name + (path.port ? ' (' + path.port + ')' : '') : (path.port ? 'Cable / link into ' + path.router.name + ' › ' + path.port : '');
    return '';
  }
  function drawPath(path, v) {
    var stops = v ? v.stops : path.hops.concat(path.behind_nat || !path.device.ip ? [] : [path.device]);
    var links = v ? v.links : [];
    var html = '';
    if (path.behind_nat) html += node(path.device, { extra: '<p class="mp-hidden">Behind ' + esc((path.hops[path.hops.length - 1] || {}).name || 'a NAT router') + ' — measured by this device’s speed test only.</p>' }) + link(null, 'hidden behind NAT');
    for (var i = stops.length - 1; i >= 0; i--) {
      html += node(stops[i], { measured: true, loaded: !!(v && v.stops[i] && v.stops[i].load) });
      html += link(links[i] || null, linkLabel(path, i, stops));
    }
    var r = path.router;
    var eth = v && v.eth ? '<p class="mp-port">' + esc(r.port) + ': ' + esc(v.eth.rate || '?') + (String(v.eth.full_duplex).match(/false|no/i) ? ' half duplex' : (v.eth.full_duplex ? ' full duplex' : '')) + '</p>' : '';
    var wifi = v && v.wifi ? '<p class="mp-port">Wi-Fi: ' + esc(v.wifi.signal || '?') + ' dBm · ' + esc(v.wifi.tx || '?') + (v.wifi.ccq ? ' · CCQ ' + esc(v.wifi.ccq) : '') + '</p>' : '';
    html += node({ kind: 'mikrotik', name: r.name, ip: r.ip, source: 'your MikroTik' + (path.port ? ' › ' + path.port : '') }, { extra: eth + wifi });
    var up = v ? v.upstream : null;
    html += link(up ? upLink(up.gw_idle, up.gw_load, 'Internet port') : null, 'Internet port');
    html += node({ kind: 'gw', name: 'ISP gateway', ip: up ? up.gateway : '', idle: up ? up.gw_idle : null, load: up ? up.gw_load : null }, { measured: !!up, loaded: !!(up && up.gw_load) });
    html += link(up ? upLink(up.inet_idle, up.inet_load, 'ISP line') : null, 'ISP line');
    html += node({ kind: 'inet', name: 'Internet', ip: '1.1.1.1', idle: up ? up.inet_idle : null, load: up ? up.inet_load : null }, { measured: !!up, loaded: !!(up && up.inet_load) });
    chain.innerHTML = html;
  }
  function upLink(i, l) {
    if (!i) return null;
    var st = i.received === 0 ? 'bad' : 'ok', why = '';
    if (l && l.med != null && i.med != null && l.med - i.med >= 80) { st = 'warn'; why = 'Slows down while you download: the line from the ISP is full.'; }
    return { state: st, why: why, idle_add: null, load_add: null };
  }
  function drawSide(path, v, extra) {
    var h = '';
    if (path.note) h += '<p class="mp-note">' + esc(path.note) + '</p>';
    if (v) {
      var d = v.device || {};
      h += '<div class="mp-speeds">' + tile('This device ↓', d.down != null ? d.down + ' Mbit/s' : '—') + tile('This device ↑', d.up != null ? d.up + ' Mbit/s' : '—')
        + tile('Router ↓', v.router_speed != null ? v.router_speed + ' Mbit/s' : 'not measured') + tile('Device latency', d.rtt != null ? d.rtt + ' ms' : '—') + '</div>';
      if (v.findings && v.findings.length) h += '<ul class="mp-findings">' + v.findings.map(function (f) { return '<li class="' + esc(f.level) + '">' + esc(f.text) + '</li>'; }).join('') + '</ul>';
      if (v.ordered_by_time && v.stops.filter(function (s) { return s.kind === 'box'; }).length > 1) {
        h += '<div class="mp-save"><p>The order of the boxes was worked out from their response times. If it matches what is on the ground, save it — future checks (and Topology) will use it.</p>'
          + '<button type="button" class="btn btn-outline-primary btn-sm" id="mpSave"><i class="bi bi-diagram-3"></i> Save this order to Topology</button></div>';
      }
    }
    side.innerHTML = h + (extra || '');
    var sv = $('mpSave'); if (sv) sv.onclick = function () { saveChain(v, path); };
  }
  function tile(k, v) { return '<div class="mp-tile"><small>' + esc(k) + '</small><b>' + esc(v) + '</b></div>'; }
  function drawVerdict(v) {
    verdictBox.hidden = false; verdictBox.className = 'mp-verdict ' + v.level;
    verdictBox.innerHTML = '<i class="bi ' + (v.level === 'ok' ? 'bi-check-circle-fill' : v.level === 'warn' ? 'bi-exclamation-triangle-fill' : 'bi-x-octagon-fill') + '"></i><div><b>' + esc(v.title) + '</b><p>' + esc(v.detail) + '</p></div>';
  }

  function saveChain(v, path) {
    var keys = v.stops.filter(function (s) { return s.kind === 'box' && s.key && s.key.indexOf('nb:') !== 0; }).map(function (s) { return s.key; });
    post(U.save, { router: ROUTER, keys: keys, port: path.port }).then(function (j) {
      $('mpSave').outerHTML = '<p class="mp-ok"><i class="bi bi-check2"></i> Saved ' + j.saved + ' boxes as a chain on Topology.</p>';
    }).catch(function (e) { alert(e.message); });
  }

  // ── choosing the device ──
  function showPick(cands, note) {
    pick.hidden = false;
    var h = '<p>' + esc(note || 'Which device is this?') + '</p><div class="mp-cands">';
    (cands || []).forEach(function (c) {
      h += '<button type="button" data-ip="' + esc(c.ip) + '" data-mac="' + esc(c.mac) + '"><i class="bi bi-phone"></i><b>' + esc(c.name || 'Unnamed device') + '</b><code>' + esc(c.ip) + (c.mac ? ' · ' + esc(c.mac) : '') + '</code>' + (c.port ? '<small>on ' + esc(c.port) + '</small>' : '') + '</button>';
    });
    h += '</div><div class="mp-search"><input class="form-control form-control-sm" id="mpQ" placeholder="Or search any online device by name, IP or MAC…"><div class="mp-cands" id="mpFound"></div></div>';
    pick.innerHTML = h;
    var q = $('mpQ'), timer;
    q.oninput = function () { clearTimeout(timer); timer = setTimeout(function () { search(q.value); }, 250); };
    search('');
    return new Promise(function (resolve) {
      pick.onclick = function (e) { var b = e.target.closest('button[data-ip]'); if (!b) return; pick.hidden = true; resolve({ ip: b.dataset.ip, mac: b.dataset.mac, name: b.querySelector('b').textContent }); };
    });
  }
  function search(q) {
    get(U.devices + '?router=' + ROUTER + '&q=' + encodeURIComponent(q)).then(function (j) {
      var f = $('mpFound'); if (!f) return;
      f.innerHTML = j.devices.slice(0, 24).map(function (d) {
        return '<button type="button" data-ip="' + esc(d.ip) + '" data-mac="' + esc(d.mac) + '"><i class="bi ' + (d.type === 'wifi' ? 'bi-wifi' : 'bi-pc-display') + '"></i><b>' + esc(d.name || 'Unnamed device') + '</b><code>' + esc(d.ip) + (d.mac ? ' · ' + esc(d.mac) : '') + '</code></button>';
      }).join('') || '<small class="text-secondary">No online devices match.</small>';
    }).catch(function () {});
  }

  // ── the whole check ──
  function recentRouterSpeed() {
    var items = (window.ntItems && window.ntItems()) || [];
    var t = items.find(function (x) { return x.kind === 'speed' && x.status === 'done' && String(x.router_id) === String(ROUTER) && x.result && x.result.ok; });
    return t && t.ts && Date.now() / 1000 - t.ts < 1800 ? t : null;          // a router speed test from the last 30 minutes
  }

  function trace(customer) {
    if (busy) return; busy = true; go.disabled = true; $('mpCustomer').disabled = true;
    resetSteps(); verdictBox.hidden = true; body.hidden = true; pick.hidden = true; mp.dataset.state = 'running';
    var me, path, idle, load, dev = {}, speedTest = null;
    var chosen = Promise.resolve();
    if (customer) {
      step('find', 'run'); say('Trace a customer’s device', 'Pick the device. Only quiet checks run for a customer device — the speed test needs to run on the device itself.', 'running');
      chosen = showPick([], 'Which customer device?');
    } else {
      step('find', 'run'); say('Finding this device…', 'Looking for this device’s connection to TapTap on the router.', 'running');
      chosen = localIp().then(function (lip) {
        return run('whoami', { lip: lip }, function (s) { say('Finding this device…', 'Waiting for the router (TapTap Link)… ' + s + ' s'); });
      }).then(function (t) {
        var r = t.result || {};
        if (r.chosen) { var c = r.candidates.find(function (x) { return x.ip === r.chosen; }); return { ip: c.ip, mac: c.mac, name: c.name }; }
        say(r.candidates && r.candidates.length ? 'Which one is you?' : 'This device was not found on the router', r.note || '', 'warn');
        return showPick(r.candidates || [], r.note);
      });
    }
    chosen.then(function (c) {
      me = c; step('find', 'ok');
      step('map', 'run'); say('Mapping the way…', (c.name || c.ip) + ' · ' + c.ip + ' — collecting every box between it and the router.', 'running');
      return get(U.path + '?router=' + ROUTER + '&ip=' + encodeURIComponent(c.ip) + '&mac=' + encodeURIComponent(c.mac || ''));
    }).then(function (j) {
      path = j.path; state.path = path; step('map', 'ok');
      body.hidden = false; drawPath(path, null); drawSide(path, null);
      step('idle', 'run');
      say('Checking every stop…', path.measure.length + ' stops on your side, plus the ISP gateway and the Internet, measured from ' + path.router.name + ' while the line is quiet.', 'running');
      return run('hops', { hops: path.measure, port: path.port, mac: firstMac(path), phase: 'idle' }, function (s) { say('Checking every stop…', 'Waiting for the router (TapTap Link)… ' + s + ' s'); });
    }).then(function (t) {
      idle = t; step('idle', 'ok');
      return post(U.verdict, { router: ROUTER, ip: me.ip, mac: me.mac, idle: idle.id, via: VIA, preview: true });
    }).then(function (j) {
      var v = j.test.result; drawPath(path, v); drawSide(path, v);
      if (customer) return null;
      step('load', 'run');
      say('Speed under load…', 'This device downloads as fast as it can while ' + path.router.name + ' checks every stop again. Keep this page open.', 'running');
      return latency().then(function (lat) {
        dev.rtt = lat.rtt;
        var dl = startDownload(LINK ? 50 : 16, 120e6);
        var tick = setInterval(function () { var m = dl.mbps(); if (m != null) $('mpText').textContent = 'Downloading at ' + m + ' Mbit/s while ' + path.router.name + ' checks every stop again…'; }, 700);
        return sleep(900).then(function () {
          return run('hops', { hops: path.measure, port: path.port, mac: firstMac(path), phase: 'load', rounds: LINK ? 8 : 10 }, function (s) {
            $('mpText').textContent = 'Keeping the line busy while the router measures (TapTap Link)… ' + s + ' s · ' + (dl.mbps() || 0) + ' Mbit/s';
          });
        }).then(function (t) { load = t; clearInterval(tick); var d = dl.stop(); dev.down = d.down; dev.server = d.server; },
                function (e) { clearInterval(tick); dl.stop(); throw e; });
      }).then(function () {
        say('Speed under load…', 'Measuring upload…', 'running');
        return upload(6).then(function (up) { dev.up = up; });
      }).then(function () {
        step('load', 'ok');
        if (!$('mpRouterSpeed').checked) { speedTest = recentRouterSpeed(); return null; }
        say('Router speed…', path.router.name + ' measures its own Internet speed to compare.', 'running');
        return run('speed', {}, function (s) { $('mpText').textContent = 'Waiting for the router (TapTap Link)… ' + s + ' s'; })
          .then(function (t) { speedTest = t; }, function (e) {
            var recent = recentRouterSpeed(); speedTest = recent;                    // spaced out by the router — use the recent one
            if (!recent) $('mpText').textContent = e.message;
          });
      });
    }).then(function () {
      step('verdict', 'run');
      return post(U.verdict, { router: ROUTER, ip: me.ip, mac: me.mac, idle: idle.id, load: load ? load.id : null, speed: speedTest ? speedTest.id : null, device: dev, via: VIA });
    }).then(function (j) {
      var v = j.test.result; emit(j.test); state.v = v;
      step('verdict', 'ok'); drawPath(path, v); drawSide(path, v); drawVerdict(v);
      say(v.title, (me.name || me.ip) + ' → ' + path.router.name + ' → Internet' + (customer ? ' (quiet check)' : ''), v.level);
    }).catch(function (e) {
      say('The check stopped', e.message, 'bad');
      document.querySelectorAll('#mpSteps li.run').forEach(function (li) { li.className = 'bad'; });
    }).then(function () { busy = false; go.disabled = false; $('mpCustomer').disabled = false; go.querySelector('span').textContent = 'Trace again'; });
  }
  function firstMac(path) {
    if (path.wifi_direct) return path.device.mac || '';
    return (path.hops[0] && path.hops[0].mac) || '';
  }

  go.addEventListener('click', function () { trace(false); });
  $('mpCustomer').addEventListener('click', function () { trace(true); });

  // A "My connection" result picked from Recent tests.
  document.addEventListener('nt:show', function (e) {
    var t = e.detail;
    if (t.kind !== 'mypath' || !t.result || !t.result.stops) return;
    var v = t.result, stops = v.stops;
    var dev = stops.filter(function (s) { return s.kind === 'device'; })[0] || { kind: 'device', name: t.target, ip: (t.params || {}).ip };
    var p = { hops: stops.filter(function (s) { return s.kind === 'box'; }), device: dev, router: v.router || { name: t.router }, port: v.port || '',
              wifi_direct: false, behind_nat: !stops.some(function (s) { return s.kind === 'device'; }), note: v.path_note || '' };
    body.hidden = false; drawPath(p, v); drawSide(p, v); drawVerdict(v);
    say(v.title, t.target + ' · ' + t.at, v.level);
    mp.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
})();
