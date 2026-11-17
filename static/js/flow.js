/* TapTap flow tools (core/views_flow.py):
 *   Connection lab   — <button data-lab-open='{"voucher":12}'> opens the lab in a modal
 *                      <div data-lab-mini='{"voucher":12}'>    a one-line live strip with the same buttons
 *   DHCP lease       — <div data-dhcp data-router="3">          lease time per network, one click to change
 *   Traffic tabs     — #flTabs on the Traffic page: Idle vouchers · Slow vouchers · Internet lines · DHCP lease
 */
(function () {
  'use strict';
  var U = { lab: '/flow/lab/', run: '/flow/lab/run/', verdict: '/flow/lab/verdict/', live: '/flow/live/', test: '/flow/test/', line: '/flow/line/',
            lines: '/flow/lines/', idle: '/flow/idle/', slow: '/flow/slow/', scan: '/flow/slow/scan/', dhcp: '/flow/dhcp/' };
  var ICON = { bad: 'bi-x-octagon-fill', warn: 'bi-exclamation-triangle-fill', ok: 'bi-check-circle-fill', info: 'bi-info-circle-fill' };

  // ─────────── helpers ───────────
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    var i = document.querySelector('[name=csrfmiddlewaretoken]');
    return i ? i.value : '';
  }
  function api(url, body) {
    var opts = { credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-CSRFToken': csrf() } };
    if (body !== undefined) { opts.method = 'POST'; opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, message: r.status === 403 ? 'You do not have access to this.'
          : r.status === 404 ? 'TapTap does not know this page — the update may not be installed on the server yet.'
          : r.status >= 500 ? 'TapTap had an error (' + r.status + '). The details are in the server log (docker compose logs web).'
          : 'TapTap answered ' + r.status + '.' };
      });
    }, function () { return { ok: false, message: 'TapTap could not be reached.' }; });
  }
  function qs(o) { return Object.keys(o).filter(function (k) { return o[k] !== '' && o[k] != null; }).map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(o[k]); }).join('&'); }
  function bps(v) { v = +v || 0; return v >= 1e9 ? (v / 1e9).toFixed(2) + ' Gb/s' : v >= 1e6 ? (v / 1e6).toFixed(1) + ' Mb/s' : v >= 1e3 ? Math.round(v / 1e3) + ' kb/s' : Math.round(v) + ' b/s'; }
  function mins(s) { s = +s || 0; return s < 90 ? s + ' s' : s < 5400 ? Math.round(s / 60) + ' min' : Math.round(s / 360) / 10 + ' h'; }
  function ago(iso) { if (!iso) return ''; var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000); return s < 50 ? 'just now' : mins(Math.round(s)) + ' ago'; }
  function cls(pct) { return pct >= 90 ? 'full' : pct >= 60 ? 'mid' : ''; }
  function attr(o) { return esc(JSON.stringify(o)); }
  function spin(text) { return '<div class="lab-busy"><span class="spinner-border spinner-border-sm"></span> ' + esc(text) + '</div>'; }

  /* Follow one router test until it is done. cb(test) is called on every change; resolves with the final test. */
  function follow(test, cb) {
    return new Promise(function (resolve) {
      var n = 0;
      (function step(t) {
        if (cb) cb(t);
        if (t.status === 'done' || t.status === 'failed') return resolve(t);
        if (++n > 150) return resolve(Object.assign({}, t, { status: 'failed', result: { error: 'No answer from the router yet. It stays queued — look again in a minute.' } }));
        setTimeout(function () {
          api(U.test + t.id + '/').then(function (d) { step(d.ok ? d.test : t); });
        }, t.status === 'waiting' ? 3000 : 1500);
      })(test);
    });
  }
  function failText(t) { return (t.result && (t.result.error || t.result.note)) || 'The router did not answer.'; }

  // ═══════════════════════════ Connection lab ═══════════════════════════
  var STATIONS = [['device', 'bi-phone', 'Phone'], ['link', 'bi-wifi', 'Wi-Fi / cable'], ['limit', 'bi-speedometer2', 'Speed limit'],
                  ['line', 'bi-signpost-split', 'Internet line'], ['internet', 'bi-globe2', 'Internet']];

  function Lab(root, query) {
    this.root = root; this.query = query; this.i = 0; this.res = {}; this.series = []; this.timers = []; this.alive = true;
    root.innerHTML = spin('Looking up who is online…');
    this.load();
  }
  Lab.prototype.stop = function () { this.alive = false; this.timers.forEach(clearInterval); this.timers = []; };
  Lab.prototype.load = function () {
    var self = this;
    api(U.lab + '?' + qs(this.query)).then(function (d) {
      if (!self.alive) return;
      if (!d.ok) { self.root.innerHTML = '<div class="alert alert-danger mb-0">' + esc(d.message) + '</div>'; return; }
      self.d = d;
      if (!d.targets.length) return self.offline();
      self.frame(); self.pick(0);
      if (d.setup) self.root.insertAdjacentHTML('afterbegin', '<div class="fl-warnline"><i class="bi bi-tools"></i> ' + esc(d.setup) + '</div>');
    });
  };
  Lab.prototype.who = function () {
    var d = this.d, v = d.voucher, dev = d.device;
    return '<h4>' + (v ? '<a href="' + esc(v.url) + '">' + esc(v.code) + '</a> <span class="badge text-bg-light">' + esc(v.state_label) + '</span>' +
      (v.plan ? ' <small class="text-secondary fw-normal">' + esc(v.plan) + (v.left ? ' · ' + esc(v.left) + ' left' : '') + '</small>' : '')
      : dev ? esc(dev.name) : esc((d.targets[0] || {}).ip || 'Device')) + '</h4>';
  };
  Lab.prototype.offline = function () {
    var d = this.d;
    this.root.innerHTML = '<div class="lab-who">' + this.who() + '</div><div class="fl-empty"><i class="bi bi-moon-stars" style="color:#94a3b8"></i>' +
      '<b>Not online right now</b><p class="mb-2">' + esc(d.offline || '') + '</p>' +
      (d.can_fix && d.voucher ? '<a class="btn btn-outline-danger btn-sm" href="' + esc(d.voucher.url) + '#fix"><i class="bi bi-wifi-off"></i> Can\'t connect?</a>' : '') + '</div>';
  };
  Lab.prototype.frame = function () {
    var d = this.d, self = this;
    var chips = d.targets.length > 1 ? '<div class="lab-targets">' + d.targets.map(function (t, i) {
      return '<button type="button" data-i="' + i + '"><b>' + esc(t.ip) + '</b><small>' + esc(t.mac || t.code) + ' · ' + esc(t.router) + '</small></button>';
    }).join('') + '</div>' : '';
    this.root.innerHTML = '<div class="lab-who">' + this.who() + chips + '</div>' +
      '<ol class="lab-road">' + STATIONS.map(function (s) {
        return '<li class="lab-st" data-st="' + s[0] + '"><span class="lab-dot"><i class="bi ' + s[1] + '"></i></span><b>' + s[2] + '</b><small></small></li>';
      }).join('') + '</ol>' +
      '<div class="lab-waitbox"></div>' +
      '<div class="lab-cols"><div>' +
        '<div class="lab-headline"><i class="bi bi-hourglass-split"></i> <span>Checking…</span></div><ul class="lab-find"></ul>' +
        '<div class="lab-tools mt-3"></div>' +
      '</div><div>' +
        '<div class="lab-box lab-speed"></div><div class="lab-box lab-linebox" hidden></div><div class="lab-box lab-facts" hidden></div>' +
      '</div></div>';
    [].forEach.call(this.root.querySelectorAll('.lab-targets button'), function (b) {
      b.addEventListener('click', function () { self.pick(+b.dataset.i); });
    });
  };
  Lab.prototype.$ = function (sel) { return this.root.querySelector(sel); };
  Lab.prototype.t = function () { return this.d.targets[this.i]; };
  Lab.prototype.body = function (extra) { var t = this.t(); return Object.assign({ router: t.router_id, ip: t.ip, mac: t.mac, code: t.code }, extra || {}); };
  Lab.prototype.pick = function (i) {
    var self = this, t;
    this.i = i; this.res = {}; this.series = []; this.timers.forEach(clearInterval); this.timers = [];
    t = this.t();
    [].forEach.call(this.root.querySelectorAll('.lab-targets button'), function (b) { b.classList.toggle('on', +b.dataset.i === i); });
    this.speedBox(); this.lineBox(t.lines.where, null); this.tools();
    this.point(t.down_bps, t.up_bps);
    this.timers.push(setInterval(function () {
      api(U.live + '?' + qs({ router: t.router_id, ip: t.ip, code: t.code })).then(function (d) {
        if (d.ok && self.alive) { t.down_bps = d.down_bps; t.up_bps = d.up_bps; t.quiet_s = d.quiet_s; self.point(d.down_bps, d.up_bps, !d.online); }
      });
    }, 5000));
    this.check();
  };
  // ── speed now, against the limit ──
  Lab.prototype.speedBox = function () {
    var t = this.t(), L = t.limits || {}, list = (L.limits || []).map(function (l) {
      return '<li class="' + (l.source === L.limit_source ? 'on' : '') + '"><span>' + esc(l.label) + '</span><span>↓ ' + esc(l.down) + (l.up ? ' · ↑ ' + esc(l.up) : '') + '</span></li>';
    }).join('');
    this.$('.lab-speed').innerHTML = '<h5><span><i class="bi bi-activity"></i> Speed right now</span><small class="text-secondary fw-normal" data-f="fresh"></small></h5>' +
      '<div class="lab-meter"><div class="dn"><span>Download</span><b data-f="dn">—</b></div><div class="up"><span>Upload</span><b data-f="up">—</b></div>' +
      '<div><span>This session</span><b style="font-size:1rem">' + esc(t.data || '—') + '</b></div></div>' +
      '<div class="lab-spark"><canvas></canvas></div>' +
      (L.limit_down ? '<div class="lab-limit"><div class="bar"><i></i></div><small><span data-f="pct"></span><span>limit ↓ ' + esc(bps(L.limit_down)) + '</span></small></div>' : '<small class="fl-note">No speed limit known for it.</small>') +
      (list ? '<ul class="lab-limits">' + list + '</ul>' : '');
  };
  Lab.prototype.point = function (dn, up, off) {
    var t = this.t(), L = (t.limits || {}).limit_down, box = this.$('.lab-speed'), pct, bar;
    if (!box) return;
    this.series.push([+dn || 0, +up || 0]); if (this.series.length > 60) this.series.shift();
    box.querySelector('[data-f=dn]').textContent = bps(dn); box.querySelector('[data-f=up]').textContent = bps(up);
    box.querySelector('[data-f=fresh]').textContent = off ? 'not online now' : (t.quiet_s >= 120 ? 'quiet for ' + mins(t.quiet_s) : 'live · every 5 s');
    if (L) {
      pct = Math.min(100, Math.round((+dn || 0) * 100 / L)); bar = box.querySelector('.lab-limit .bar i');
      bar.style.width = pct + '%'; bar.className = cls(pct);
      box.querySelector('[data-f=pct]').textContent = pct + '% of the limit in use';
    }
    this.spark(box.querySelector('canvas'), L);
  };
  Lab.prototype.spark = function (cv, limit) {
    var w = cv.clientWidth, h = cv.clientHeight, r = window.devicePixelRatio || 1, c = cv.getContext('2d'), s = this.series, max = limit || 0, i;
    if (!w || !h) return;
    cv.width = w * r; cv.height = h * r; c.setTransform(r, 0, 0, r, 0, 0); c.clearRect(0, 0, w, h);
    for (i = 0; i < s.length; i++) max = Math.max(max, s[i][0], s[i][1]);
    max = max * 1.15 || 1;
    function y(v) { return h - 3 - (v / max) * (h - 8); }
    function line(k, col, fill) {
      var step = w / 59, x0 = w - (s.length - 1) * step;
      c.beginPath();
      for (i = 0; i < s.length; i++) c[i ? 'lineTo' : 'moveTo'](x0 + i * step, y(s[i][k]));
      c.strokeStyle = col; c.lineWidth = 2; c.lineJoin = 'round'; c.stroke();
      if (fill && s.length > 1) { c.lineTo(w, h); c.lineTo(x0, h); c.closePath(); c.fillStyle = fill; c.fill(); }
    }
    if (limit) { c.setLineDash([5, 4]); c.strokeStyle = '#ef444499'; c.lineWidth = 1; c.beginPath(); c.moveTo(0, y(limit)); c.lineTo(w, y(limit)); c.stroke(); c.setLineDash([]); }
    line(1, '#7c3aed'); line(0, '#0b84c6', 'rgba(11,132,198,.12)');
  };
  // ── the road: TapTap's own knowledge first (dashed), the router's measurement replaces it (solid) ──
  var STEPS = { labq: ['Quick check', 'the phone, its speed limits, its internet line'], labd: ['Details', 'Wi-Fi signal, address lease, the internet, the log'] };
  Lab.prototype.station = function (key, state, detail, src) {
    var li = this.$('[data-st=' + key + ']'); if (!li) return;
    li.className = 'lab-st' + (state && state !== 'idle' ? ' s-' + state : '') + (src ? ' src-' + src : '');
    li.title = src === 'measured' ? 'Measured by the router just now' : src === 'known' ? 'From TapTap’s own data (live sync, the traffic sample, the last router sync)' : '';
    if (detail !== undefined) { li.querySelector('small').textContent = detail || ''; }
  };
  Lab.prototype.check = function () {
    var self = this, t = this.t();
    this.res = {}; this.tries = {};
    this.head('idle', 'Reading what TapTap already knows…'); this.$('.lab-find').innerHTML = '';
    this.run(['labq', 'labd'], t);
    this.verdict();                                   // TapTap's own knowledge, at once — never an empty lab
  };
  Lab.prototype.run = function (kinds, t) {
    var self = this;
    kinds.forEach(function (k) { self.res[k] = { status: 'starting', kind: k }; });
    this.track();
    api(U.run, this.body({ kinds: kinds })).then(function (d) {
      if (!self.alive || self.t() !== t) return;
      if (!d.ok) { kinds.forEach(function (k) { self.res[k] = { status: 'failed', kind: k, result: { error: d.message } }; }); return self.track(); }
      d.tests.forEach(function (test) { self.watch(test, t); });
      if (d.message) self.note(d.message);
    });
  };
  Lab.prototype.watch = function (test, t) {
    var self = this, k = test.kind;
    follow(test, function (x) { if (self.alive && self.t() === t) { self.res[k] = x; self.track(); } }).then(function (done) {
      if (!self.alive || self.t() !== t) return;
      if (done.status === 'failed' && !self.tries[k] && done.via === 'TapTap Link') {
        self.tries[k] = 1;                                         // once more, automatically: a busy router usually takes the 2nd
        self.res[k] = { status: 'retry', kind: k, result: done.result };
        self.track();
        return setTimeout(function () { if (self.alive && self.t() === t) self.run([k], t); }, 1500);
      }
      self.res[k] = done;
      if (k === 'labq' && done.status === 'done' && done.result.lane) self.lineBox(done.result.lane, null);
      self.facts(); self.verdict(); self.track();
    });
  };
  /* The router check strip: where each step is, in facts — never a bare spinner, never a red headline. */
  Lab.prototype.track = function () {
    var self = this, t = this.t(), box = this.$('.lab-waitbox'), rows = [];
    ['labq', 'labd'].forEach(function (k) {
      var x = self.res[k]; if (!x) return;
      var dv = x.delivery || {}, st = x.status, icon, text, cls = '';
      if (st === 'done') { icon = 'bi-check-circle-fill text-success'; text = 'Measured' + (x.secs != null ? ' in ' + Math.round(x.secs) + ' s' : '') + (self.tries[k] ? ' (2nd try)' : ''); }
      else if (st === 'retry') { icon = 'bi-arrow-repeat text-warning'; text = 'The answer did not arrive — trying once more…'; cls = 'warn'; }
      else if (st === 'failed') { icon = 'bi-exclamation-circle text-warning'; text = failText(x); cls = 'warn'; }
      else if (dv.stage === 'running') { icon = 'bi-broadcast text-primary'; text = 'Handed to ' + t.router + (dv.sent_s != null ? ' ' + dv.sent_s + ' s ago' : '') + ' — measuring now'; }
      else if (dv.link) {
        icon = 'bi-hourglass-split text-primary';
        text = 'Waiting for ' + t.router + '’s next check-in' +
          (dv.heard_s != null ? ' — last one ' + (dv.heard_s < 50 ? dv.heard_s + ' s' : Math.round(dv.heard_s / 60) + ' min') + ' ago' : '') +
          (dv.usual_s ? ', usually every ~' + dv.usual_s + ' s' : '') +
          (dv.vouchers_ahead ? ' · ' + dv.vouchers_ahead + ' voucher change' + (dv.vouchers_ahead > 1 ? 's' : '') + ' go first' : '') +
          (dv.express ? ' · express seat' : '') + (dv.age ? ' · ' + dv.age + ' s' : '');
      } else { icon = 'bi-broadcast text-primary'; text = 'Measuring…'; }
      rows.push('<div class="lab-step ' + cls + '"><i class="bi ' + icon + '"></i><div><b>' + STEPS[k][0] + '</b> <small class="text-secondary">' + STEPS[k][1] + '</small>' +
        '<div class="lab-step-t">' + esc(text) + '</div></div>' +
        (st === 'failed' ? '<button type="button" class="btn btn-sm btn-outline-primary" data-retry="' + k + '"><i class="bi bi-arrow-repeat"></i> Try again</button>' : '') + '</div>');
    });
    box.innerHTML = rows.length ? '<div class="lab-steps"><div class="lab-steps-h"><span><i class="bi bi-router"></i> Router check on ' + esc(t.router) + '</span>' +
      '<span class="lab-legend"><i class="lg-m"></i> measured <i class="lg-k"></i> known to TapTap</span></div>' + rows.join('') + '</div>' : '';
    [].forEach.call(box.querySelectorAll('[data-retry]'), function (b) {
      b.addEventListener('click', function () { b.disabled = true; self.tries[b.dataset.retry] = 1; self.run([b.dataset.retry], t); });
    });
  };
  Lab.prototype.note = function (text) { this.$('.lab-waitbox').insertAdjacentHTML('beforeend', '<div class="fl-warnline">' + esc(text) + '</div>'); };
  Lab.prototype.head = function (level, text, sub) {
    var h = this.$('.lab-headline');
    h.className = 'lab-headline h-' + level;
    h.innerHTML = '<i class="bi ' + (level === 'idle' ? 'bi-hourglass-split' : ICON[level] || 'bi-dot') + '"></i> <span>' + esc(text) +
      (sub ? '<small class="d-block fw-normal">' + esc(sub) + '</small>' : '') + '</span>';
  };
  Lab.prototype.verdict = function () {
    var self = this, t = this.t(), r = this.res, id = function (k) { return r[k] && r[k].status === 'done' ? r[k].id : ''; };
    api(U.verdict, this.body({ labq: id('labq'), labd: id('labd') })).then(function (d) {
      if (!d.ok || !self.alive || self.t() !== t) return;
      var v = d.verdict, pending = ['labq', 'labd'].some(function (k) { return r[k] && ['done', 'failed'].indexOf(r[k].status) < 0; }),
        failed = ['labq', 'labd'].filter(function (k) { return r[k] && r[k].status === 'failed'; });
      Object.keys(v.stations).forEach(function (k) {
        var s = v.stations[k];
        self.station(k, s.state === 'idle' && pending ? 'run' : s.state, s.detail, s.src);
      });
      self.head(v.head.level, v.head.title, v.head.measured ? (pending ? 'The router is still measuring the rest.' : '') :
        failed.length ? 'From TapTap’s own data — the router’s measurement did not arrive (see the router check above).' :
          pending ? 'From TapTap’s own data so far — the router is measuring.' : '');
      self.$('.lab-find').innerHTML = v.findings.map(function (f) {
        return '<li class="lab-f f-' + esc(f.level) + '"><i class="bi ' + (ICON[f.level] || 'bi-dot') + '"></i><div><b>' + esc(f.title) + '</b>' +
          (f.text ? '<small>' + esc(f.text) + '</small>' : '') + self.action(f.action) + '</div></li>';
      }).join('');
    });
  };
  Lab.prototype.action = function (a) {
    var d = this.d, t = this.t();
    if (a === 'switch' && t.lines.can_switch) return '<button type="button" class="btn btn-sm btn-primary" data-act="next"><i class="bi bi-shuffle"></i> Switch its internet line</button>';
    if (a === 'dhcp' && d.can_dhcp) return '<a class="btn btn-sm btn-outline-primary" href="/traffic/?router=' + t.router_id + '#dhcp"><i class="bi bi-hdd-network"></i> DHCP lease time</a>';
    if (a === 'fix' && d.can_fix && d.voucher) return '<a class="btn btn-sm btn-outline-danger" href="' + esc(d.voucher.url) + '#fix"><i class="bi bi-wifi-off"></i> Can\'t connect?</a>';
    if (a === 'lift' && d.can_lift && d.voucher) return '<a class="btn btn-sm btn-outline-primary" href="' + esc(d.voucher.url) + '#fair-usage"><i class="bi bi-lightning-charge"></i> Give full speed back</a>';
    return '';
  };
  // ── numbers behind the verdict ──
  Lab.prototype.facts = function () {
    var box = this.$('.lab-facts'), q = this.res.labq && this.res.labq.status === 'done' ? this.res.labq.result : null,
      dd = this.res.labd && this.res.labd.status === 'done' ? this.res.labd.result : null, html = '', kv = [];
    if (q && q.ping) {
      var pg = q.ping, times = pg.times || [], top = Math.max.apply(null, times.map(function (x) { return typeof x === 'number' ? x : 0; }).concat([20]));
      html += '<h5><span><i class="bi bi-broadcast"></i> Router → phone</span><small class="text-secondary fw-normal">' + (pg.received || 0) + ' of ' + (pg.sent || 0) + ' replies' + (pg.avg != null ? ' · ' + pg.avg + ' ms' : '') + '</small></h5>' +
        '<div class="lab-pings" title="One bar per ping: taller is slower, red is lost">' + times.map(function (x) {
          return x == null ? '<i class="lost"></i>' : '<i class="' + (x > 150 ? 'slow' : '') + '" style="height:' + (typeof x === 'number' ? Math.max(12, Math.round(x * 100 / top)) : 40) + '%"></i>';
        }).join('') + '</div>';
    }
    if (q) {
      if (q.active) kv.push(['Session', 'online ' + q.active.uptime + (q.active.idle ? ' · idle ' + q.active.idle : '')]);
      (q.queues || []).forEach(function (x) {
        kv.push([{ fup: 'Fair usage limit', rule: 'Speed rule', plan: 'Plan limit' }[x.kind] || 'Limit',
          '↓ ' + bps(x.rate_down) + ' of ' + bps(x.max_down) + (x.drop_down ? ' · ' + x.drop_down.toLocaleString() + ' packets dropped (' + x.drop_pct + '%)' : ' · no drops')]);
      });
      if (q.queues && !q.queues.length) kv.push(['Speed limit on the router', 'none found for this address']);
    }
    if (dd) {
      if (dd.wifi) kv.push(['Wi-Fi', (dd.wifi.signal != null ? dd.wifi.signal + ' dBm' : '') + (dd.wifi.tx ? ' · ' + dd.wifi.tx : '') + (dd.wifi.interface ? ' · ' + dd.wifi.interface : '')]);
      if (dd.port || (q && q.arp && q.arp.interface)) kv.push(['Comes in on', dd.port || q.arp.interface]);
      if (dd.lease) kv.push(['Address lease', dd.lease.status + (dd.lease.expires ? ' · renews within ' + dd.lease.expires : '') + (dd.lease.server ? ' · ' + dd.lease.server : '')]);
      if (dd.lease && dd.lease.host) kv.push(['Calls itself', dd.lease.host]);
      if (dd.internet) kv.push(['Router → internet', dd.internet.received ? (dd.internet.avg != null ? dd.internet.avg + ' ms' : 'answers') + (dd.internet.loss ? ' · ' + dd.internet.loss + '% lost' : '') : 'no ping reply']);
      if (dd.cpu != null) kv.push(['Router CPU', dd.cpu + '%']);
      if (dd.drops && dd.drops.total) kv.push(['Sign-outs in the log', dd.drops.rows.slice(0, 3).map(function (x) { return x.at + ' ' + x.why; }).join(' · ')]);
    }
    if (kv.length) html += '<dl class="lab-kv">' + kv.map(function (x) { return '<dt>' + esc(x[0]) + '</dt><dd>' + esc(x[1]) + '</dd>'; }).join('') + '</dl>';
    box.hidden = !html; box.innerHTML = html;
  };
  // ── internet line: see it, switch it ──
  Lab.prototype.lineBox = function (where, msg) {
    var self = this, t = this.t(), L = t.lines, box = this.$('.lab-linebox'), w = where || {}, lanes;
    if (!box) return;
    if (!L.multi) { box.hidden = true; return; }
    box.hidden = false;
    if (where) L.where = where;
    lanes = (w.lanes && w.lanes.length ? w.lanes : L.lanes.map(function (l) { return Object.assign({ count: 0, pct: 0 }, l); }));
    var pin = w.pin || (L.pin && L.pin.mark) || '';
    box.innerHTML = '<h5><span><i class="bi bi-signpost-split"></i> Internet line</span><small class="text-secondary fw-normal">' + esc(w.at ? 'seen ' + ago(w.at) : '') + '</small></h5>' +
      '<div class="fl-note mb-2">' + esc(msg || w.title || (w.state === 'one' ? 'Going through ' + w.label : w.state === 'mixed' ? 'Mostly ' + w.label + ' — its connections are spread over the lines' :
        'No traffic seen from it in the last minute, so its line is not known yet.')) + (w.note && !msg ? ' ' + esc(w.note) : '') + '</div>' +
      '<div class="lab-lanes">' + lanes.map(function (l) {
        var on = l.current || (w.lane === l.mark && w.state !== 'none'), pinned = pin === l.mark;
        return '<button type="button" class="lab-lane' + (on ? ' is-on' : '') + (pinned ? ' is-pin' : '') + '" data-mark="' + esc(l.mark) + '"' + (L.can_switch ? '' : ' disabled') +
          ' title="' + (on ? 'It is on this line' : 'Move it to ' + esc(l.label)) + '"><b>' + esc(l.label) + '</b><small>' + l.share + '% of customers by design' +
          (l.count ? ' · ' + l.count + ' of its connections' : '') + '</small>' + (pinned ? '<em><i class="bi bi-pin-angle-fill"></i> switched here</em>' : on ? '<em>now</em>' : '') +
          '<span class="fill" style="width:' + (l.pct || 0) + '%"></span></button>';
      }).join('') + '</div>' +
      (L.can_switch ? '<div class="lab-acts"><button type="button" class="btn btn-sm btn-primary" data-act="next"><i class="bi bi-shuffle"></i> Switch to the ' + (lanes.length > 2 ? 'next' : 'other') + ' line</button>' +
        (pin ? '<button type="button" class="btn btn-sm btn-outline-secondary" data-act="auto"><i class="bi bi-arrow-counterclockwise"></i> Back to automatic</button>' : '') +
        '<button type="button" class="btn btn-sm btn-outline-secondary" data-act="look"><i class="bi bi-arrow-repeat"></i> Check now</button></div>' : '') +
      (!L.can_switch && this.d.setup ? '<div class="fl-note mt-2"><i class="bi bi-tools"></i> Switching is off until the server runs its database step (see the note at the top).</div>' : '') +
      (!L.sticky ? '<div class="fl-note mt-2">This router shares connection by connection, so a customer normally uses every line. A switch keeps this one on a single line.</div>' : '');
    [].forEach.call(box.querySelectorAll('.lab-lane'), function (b) {
      b.addEventListener('click', function () { if (!b.classList.contains('is-on') || pin !== b.dataset.mark) self.line('switch', b.dataset.mark, b.querySelector('b').textContent); });
    });
  };
  Lab.prototype.line = function (action, mark, label) {
    var self = this, t = this.t(), box = this.$('.lab-linebox');
    if (action === 'switch' && mark !== 'next' && !window.confirm('Move ' + (t.code || t.ip) + ' to ' + label + '?\n\nIts open connections are closed so it changes line at once (a call or download in progress restarts).')) return;
    if (action === 'switch' && mark === 'next' && !window.confirm('Move ' + (t.code || t.ip) + ' to the other internet line?\n\nIts open connections are closed so it changes line at once (a call or download in progress restarts).')) return;
    [].forEach.call(box.querySelectorAll('button'), function (b) { b.disabled = true; });
    box.querySelector('.fl-note').innerHTML = '<span class="spinner-border spinner-border-sm"></span> ' +
      (action === 'look' ? 'Counting its connections on the router…' : action === 'auto' ? 'Removing the switch…' : 'Switching…') + (t.via === 'TapTap Link' ? ' (at the router\'s next check-in)' : '');
    api(U.line, this.body({ action: action, mark: mark, current: (t.lines.where || {}).lane || '' })).then(function (d) {
      if (!d.ok) return self.lineBox(t.lines.where, d.message);
      if (action === 'auto') t.lines.pin = null;
      follow(d.test).then(function (done) {
        if (!self.alive || self.t() !== t) return;
        if (done.status !== 'done') return self.lineBox(t.lines.where, failText(done));
        if (action === 'switch' && !done.result.failed_step) t.lines.pin = { mark: d.lane.mark, label: d.lane.label };
        self.lineBox(done.result, null);
        self.station('line', done.result.failed_step ? 'warn' : 'ok', done.result.label || (d.lane && d.lane.label) || '');
        document.dispatchEvent(new CustomEvent('tt:line-changed'));
      });
    });
  };
  Lab.prototype.tools = function () {
    var self = this, t = this.t(), box = this.$('.lab-tools');
    box.innerHTML = '<button type="button" class="btn btn-sm btn-outline-primary" data-act="again"><i class="bi bi-arrow-repeat"></i> Check again</button>' +
      (this.d.can_dhcp ? '<a class="btn btn-sm btn-outline-secondary" href="/network-tools/?router=' + t.router_id + '"><i class="bi bi-tools"></i> Network tools</a>' +
        '<a class="btn btn-sm btn-outline-secondary" href="/traffic/?router=' + t.router_id + '#dhcp"><i class="bi bi-hdd-network"></i> DHCP lease</a>' : '') +
      (this.d.voucher ? '<a class="btn btn-sm btn-outline-secondary" href="' + esc(this.d.voucher.url) + '"><i class="bi bi-ticket-perforated"></i> Voucher page</a>' : '');
    if (!this._bound) {
      this._bound = true;
      this.root.addEventListener('click', function (e) {
        var b = e.target.closest('[data-act]'); if (!b || !self.root.contains(b)) return;
        var a = b.dataset.act;
        if (a === 'again') self.check();
        else if (a === 'next') self.line('switch', 'next');
        else if (a === 'auto' || a === 'look') self.line(a);
      });
    }
  };

  // ── modal ──
  var modalEl = null, current = null;
  function openLab(query) {
    if (!modalEl) {
      modalEl = document.createElement('div');
      modalEl.className = 'modal fade'; modalEl.id = 'ttLabModal'; modalEl.tabIndex = -1; modalEl.setAttribute('aria-labelledby', 'ttLabTitle');
      modalEl.innerHTML = '<div class="modal-dialog modal-xl modal-dialog-scrollable"><div class="modal-content"><div class="modal-header">' +
        '<h5 class="modal-title" id="ttLabTitle"><i class="bi bi-activity"></i> Connection lab <small class="text-secondary fw-normal fs-6">— measured from the router it is connected to</small></h5>' +
        '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button></div><div class="modal-body"><div class="lab"></div></div></div></div>';
      document.body.appendChild(modalEl);
      modalEl.addEventListener('hidden.bs.modal', function () { if (current) current.stop(); current = null; });
    }
    if (current) current.stop();
    current = new Lab(modalEl.querySelector('.lab'), query);
    if (window.bootstrap && bootstrap.Modal) bootstrap.Modal.getOrCreateInstance(modalEl).show();
    else { modalEl.classList.add('show'); modalEl.style.display = 'block'; }
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-lab-open]'); if (!b) return;
    e.preventDefault();
    try { openLab(JSON.parse(b.getAttribute('data-lab-open'))); } catch (err) { /* malformed attribute */ }
  });

  // ── the one-line strip on voucher / device pages ──
  function mini(el) {
    var query; try { query = JSON.parse(el.getAttribute('data-lab-mini')); } catch (e) { return; }
    function draw() {
      api(U.lab + '?' + qs(query)).then(function (d) {
        if (!d.ok) { el.hidden = true; return; }
        var t = d.targets[0], w = t && t.lines.where || {}, pin = t && t.lines.pin;
        el.hidden = false;
        el.innerHTML = t
          ? '<span class="pulse"></span><span><b>Online</b>' + (d.targets.length > 1 ? ' on ' + d.targets.length + ' devices' : '') + ' · <span class="font-monospace">' + esc(t.ip) + '</span></span>' +
            '<span>↓ <b>' + esc(t.down) + '</b> · ↑ <b>' + esc(t.up) + '</b>' + (t.limits && t.limits.limit_down ? ' <small class="text-secondary">of ' + esc(bps(t.limits.limit_down)) + '</small>' : '') + '</span>' +
            (t.quiet_s >= 300 ? '<span class="fl-chip">no traffic for ' + esc(mins(t.quiet_s)) + '</span>' : '') +
            (t.lines.multi ? '<span class="fl-chip line"><i class="bi bi-signpost-split"></i> ' + esc(pin ? pin.label + ' (switched)' : w.state && w.state !== 'none' ? (w.state === 'mixed' ? 'mostly ' : '') + w.label : 'line not seen yet') + '</span>' : '') +
            '<span class="sp"></span><button type="button" class="btn btn-sm btn-primary" data-lab-open="' + attr(query) + '"><i class="bi bi-activity"></i> Connection lab</button>'
          : '<span class="pulse off"></span><span class="text-secondary">Not online right now — the connection lab works while a device is signed in.</span>';
      });
    }
    draw(); setInterval(function () { if (!document.hidden) draw(); }, 20000);
    document.addEventListener('tt:line-changed', draw);
  }

  // ═══════════════════════════ DHCP lease ═══════════════════════════
  function Dhcp(root) { this.root = root; this.router = root.getAttribute('data-router') || ''; this.load(); }
  Dhcp.prototype.load = function () {
    var self = this;
    this.root.innerHTML = spin('Reading DHCP servers…');
    api(U.dhcp + (this.router ? '?router=' + encodeURIComponent(this.router) : '')).then(function (d) {
      if (!d.ok) { self.root.innerHTML = '<div class="alert alert-danger mb-0">' + esc(d.message) + '</div>'; return; }
      self.presets = d.presets; self.routers = d.routers; self.draw();
    });
  };
  Dhcp.prototype.chips = function (cur, sug) {
    return this.presets.map(function (p) {
      return '<button type="button" data-min="' + p.minutes + '" class="' + (p.minutes === cur ? 'on' : p.minutes === sug && sug !== cur ? 'sug' : '') + '"' +
        (p.minutes === cur ? ' aria-pressed="true"' : '') + '>' + esc(p.label) + (p.minutes === sug && sug !== cur ? ' ✓' : '') + '</button>';
    }).join('') + '<button type="button" data-min="custom">Other…</button>';
  };
  Dhcp.prototype.draw = function () {
    var self = this;
    if (!this.routers.length) { this.root.innerHTML = '<div class="fl-empty">No router yet.</div>'; return; }
    this.root.innerHTML = this.routers.map(function (r) {
      var cards = r.servers.map(function (s) {
        var a = s.advice || {}, full = s.full;
        return '<div class="dh-card lv-' + esc(a.level || 'ok') + '" data-server="' + esc(s.name) + '"><div class="dh-top"><div><h4>' + esc(s.name) + (s.hotspot ? ' <span class="badge text-bg-primary">hotspot</span>' : '') +
          (s.disabled ? ' <span class="badge text-bg-secondary">off</span>' : '') + '</h4><small>on ' + esc(s.interface || '?') + '</small></div>' +
          '<div class="dh-now"><small>lease time</small>' + esc(s.human) + '</div></div>' +
          (s.leases != null ? '<div class="dh-pool">' + (s.pool ? '<div class="bar"><i class="' + cls(full) + '" style="width:' + Math.min(100, full || 0) + '%"></i></div>' : '') +
            '<small class="fl-note">' + s.leases + ' device' + (s.leases === 1 ? '' : 's') + ' hold an address' + (s.pool ? ' of ' + s.pool + ' (' + full + '% full)' : '') + '</small></div>' : '') +
          '<div class="dh-adv lv-' + esc(a.level || 'ok') + '">' + esc(a.text || '') + '</div>' +
          '<div class="dh-chips">' + self.chips(s.minutes, a.suggest) + '</div><div class="dh-out"></div></div>';
      }).join('');
      return '<section class="panel dh-router" data-router="' + r.router_id + '"><div class="ln-head"><div><h3><i class="bi bi-hdd-network"></i> ' + esc(r.router) + '</h3>' +
        '<small class="fl-note">' + esc(r.via) + ' · ' + (r.as_of ? (r.live ? 'read from the router ' : 'from the last sync, ') + esc(ago(r.as_of)) : 'never read') + '</small></div>' +
        '<button type="button" class="btn btn-sm btn-outline-secondary" data-read><i class="bi bi-arrow-repeat"></i> Read from the router</button></div>' +
        (r.servers.length > 1 ? '<div class="dh-all"><b>All ' + r.servers.length + ' networks at once:</b><span class="dh-chips">' + self.chips(-1, -1) + '</span></div>' : '') +
        (r.servers.length ? '<div class="dh-grid">' + cards + '</div>' : '<div class="fl-empty"><i class="bi bi-hdd-network" style="color:#94a3b8"></i>TapTap knows no DHCP server on this router yet. Press “Read from the router”.</div>') +
        '<div class="dh-out dh-rout"></div><p class="fl-note mt-2 mb-0">A change is read back from the router. Devices take the new lease time when they next renew their address ' +
        '(within half of the old time) — nobody is disconnected.</p></section>';
    }).join('');
    [].forEach.call(this.root.querySelectorAll('.dh-router'), function (sec) {
      var rid = sec.getAttribute('data-router');
      sec.querySelector('[data-read]').addEventListener('click', function () { self.send(sec, { router: rid, action: 'read' }, sec.querySelector('.dh-rout')); });
      [].forEach.call(sec.querySelectorAll('.dh-chips button'), function (b) {
        b.addEventListener('click', function () {
          var card = b.closest('.dh-card'), m = b.dataset.min, name = card ? card.getAttribute('data-server') : '';
          if (m === 'custom') {
            m = window.prompt('Lease time in minutes (2 – 43200).\n60 = 1 hour, 1440 = 1 day.', '60');
            if (m == null) return;
            m = parseInt(m, 10);
            if (!(m >= 2 && m <= 43200)) return window.alert('Type a number of minutes between 2 and 43200.');
          }
          if (!card && !window.confirm('Set the lease time of every network on this router?')) return;
          self.send(sec, { router: rid, action: 'set', minutes: +m, servers: name ? [name] : [] }, card ? card.querySelector('.dh-out') : sec.querySelector('.dh-rout'));
        });
      });
    });
  };
  Dhcp.prototype.send = function (sec, body, out) {
    var self = this, buttons = sec.querySelectorAll('button');
    [].forEach.call(buttons, function (b) { b.disabled = true; });
    out.innerHTML = '<div class="dh-res wait"><span class="spinner-border spinner-border-sm"></span> ' + (body.action === 'read' ? 'Reading the router…' : 'Sending to the router…') + '</div>';
    api(U.dhcp, body).then(function (d) {
      if (!d.ok) { [].forEach.call(buttons, function (b) { b.disabled = false; }); out.innerHTML = '<div class="dh-res bad">' + esc(d.message) + '</div>'; return; }
      follow(d.test, function (t) {
        if (t.status === 'waiting') out.innerHTML = '<div class="dh-res wait"><span class="spinner-border spinner-border-sm"></span> Waiting for the router\'s next check-in…</div>';
      }).then(function (t) {
        var r = t.result || {}, msg = t.status === 'done' ? '<div class="dh-res ' + (r.failed_step ? 'bad' : 'ok') + '"><b>' + esc(r.title || 'Done') + '</b>' + (r.note ? '<br>' + esc(r.note) : '') + '</div>'
          : '<div class="dh-res bad">' + esc(failText(t)) + '</div>';
        if (t.status === 'done' && r.servers) {
          self.routers.forEach(function (x) { if (String(x.router_id) === String(body.router)) { x.servers = r.servers; x.live = true; x.as_of = new Date().toISOString(); } });
          self.draw();
          var again = self.root.querySelector('.dh-router[data-router="' + body.router + '"] .dh-rout');
          if (again) again.innerHTML = msg;
        } else { [].forEach.call(buttons, function (b) { b.disabled = false; }); out.innerHTML = msg; }
      });
    });
  };

  // ═══════════════════════════ Traffic tabs ═══════════════════════════
  function Tabs(nav) {
    var self = this;
    this.nav = nav; this.q = nav.getAttribute('data-query') || ''; this.loaded = {}; this.state = { idleView: 'online', min: 10, level: 'calm' };
    [].forEach.call(nav.querySelectorAll('button'), function (b) { b.addEventListener('click', function () { self.show(b.dataset.tab, true); }); });
    window.addEventListener('hashchange', function () { self.show(location.hash.slice(1) || 'overview'); });
    this.show(location.hash.slice(1) || 'overview');
    this.badge();
    setInterval(function () { if (!document.hidden) { self.badge(); if (self.tab !== 'overview' && self.tab !== 'dhcp' && self.tab !== 'speed') self.load(self.tab, true); } }, 30000);
    document.addEventListener('tt:line-changed', function () { if (self.tab === 'lines') self.load('lines', true); });
  }
  Tabs.prototype.pane = function (name) { return document.querySelector('[data-pane="' + name + '"]'); };
  Tabs.prototype.url = function (base, extra) { return base + '?' + [this.q, extra || ''].filter(Boolean).join('&'); };
  Tabs.prototype.show = function (name, push) {
    if (!this.pane(name)) name = 'overview';
    this.tab = name;
    [].forEach.call(this.nav.querySelectorAll('button'), function (b) { var on = b.dataset.tab === name; b.classList.toggle('on', on); b.setAttribute('aria-selected', on ? 'true' : 'false'); });
    [].forEach.call(document.querySelectorAll('[data-pane]'), function (p) { p.hidden = p.getAttribute('data-pane') !== name; });
    if (push && history.replaceState) history.replaceState(null, '', name === 'overview' ? location.pathname + location.search : '#' + name);
    if (name === 'overview') window.dispatchEvent(new Event('resize'));
    else this.load(name);
  };
  Tabs.prototype.badge = function () {
    var n = document.getElementById('flIdleN'), self = this;
    if (n) api(this.url(U.idle, 'min=10')).then(function (d) {
      if (!d.ok) return;
      var tot = d.online_total + d.offline.total; n.textContent = tot; n.classList.toggle('hot', tot > 0); self.idleData = self.idleData || d;
    });
  };
  Tabs.prototype.load = function (name, quiet) {
    var self = this, pane = this.pane(name);
    if (name === 'dhcp') { if (!this.loaded.dhcp) { this.loaded.dhcp = 1; new Dhcp(pane.querySelector('[data-dhcp]')); } return; }
    if (name === 'speed') { if (window.TTLanes) TTLanes.tab(pane.querySelector('[data-lanes-tab]')); return; }   // static/js/lanes.js
    if (!quiet && !this.loaded[name]) pane.querySelector('.fl-body').innerHTML = spin('Loading…');
    this.loaded[name] = 1;
    var url = name === 'idle' ? this.url(U.idle, 'min=' + this.state.min + '&level=' + this.state.level) : this.url(name === 'slow' ? U.slow : U.lines);
    api(url).then(function (d) {
      if (!d.ok) { pane.querySelector('.fl-body').innerHTML = '<div class="alert alert-danger">' + esc(d.message) + '</div>'; return; }
      self[name](pane, d);
    });
  };
  function stale(list) { return list && list.length ? '<div class="fl-warnline"><i class="bi bi-exclamation-triangle"></i> No fresh session list from ' + esc(list.join(', ')) + ' (live sync has not reported in the last 5 minutes), so ' + (list.length > 1 ? 'they are' : 'it is') + ' left out.</div>' : ''; }
  function who(r) {
    return (r.id ? '<a class="fl-code" href="/vouchers/' + r.id + '/">' + esc(r.code) + '</a>' : '<span class="fl-code">' + esc(r.code) + '</span>') +
      '<small>' + esc([r.plan, r.name].filter(Boolean).join(' · ') || (r.bypass ? 'bypass device' : '')) + '</small>';
  }
  function devCell(r) {
    return (r.device_id ? '<a href="/devices/' + r.device_id + '/">' + esc(r.device || 'Device') + '</a>' : esc(r.device || '—')) +
      '<small class="font-monospace">' + esc(r.ip) + (r.mac ? ' · ' + esc(r.mac) : '') + '</small>';
  }
  function labBtn(r, text) { return '<button type="button" class="btn btn-sm btn-outline-primary" data-lab-open="' + attr({ router: r.router_id, ip: r.ip, mac: r.mac, code: r.code }) + '"><i class="bi bi-activity"></i> ' + (text || 'Check') + '</button>'; }

  Tabs.prototype.idle = function (pane, d) {
    var self = this, st = this.state, body = pane.querySelector('.fl-body'), rows, table;
    var n = document.getElementById('flIdleN'); if (n && st.min === 10 && st.level === 'calm') { n.textContent = d.online_total + d.offline.total; }
    if (st.idleView === 'online') {
      rows = d.online;
      table = rows.length ? '<div class="table-responsive"><table class="table align-middle fl-table"><thead><tr><th>Voucher</th><th>Device</th><th>No traffic for</th><th>Signed in</th><th>Session data</th><th>Time left</th><th>Router</th><th></th></tr></thead><tbody>' +
        rows.map(function (r) {
          return '<tr><td>' + who(r) + '</td><td>' + devCell(r) + '</td><td><b>' + esc(r.idle) + '</b><div class="fl-idlebar"><i style="width:' + Math.min(100, Math.round(r.idle_s / 36)) + '%"></i></div></td>' +
            '<td>' + esc(r.uptime || '—') + '</td><td>' + esc(r.data) + '<small>now ' + esc(r.now) + '</small></td><td>' + esc(r.left || '—') + '</td>' +
            '<td>' + esc(r.router) + (r.line ? '<small><span class="fl-chip line">' + esc(r.line) + '</span></small>' : '') + '</td><td class="text-end">' + labBtn(r) + '</td></tr>';
        }).join('') + '</tbody></table></div>'
        : '<div class="fl-empty"><i class="bi bi-emoji-smile"></i><b>Everyone who is signed in is using the internet</b><p class="mb-0">No session has been quiet for ' + st.min + ' minutes.</p></div>';
    } else {
      rows = d.offline.rows;
      table = rows.length ? '<div class="table-responsive"><table class="table align-middle fl-table"><thead><tr><th>Voucher</th><th>Away for</th><th>Time left</th><th>Time used</th><th>Data (14 days)</th><th>Router</th><th></th></tr></thead><tbody>' +
        rows.map(function (r) {
          return '<tr><td>' + who(r) + '</td><td><b>' + esc(r.never ? 'never seen using data' : r.away) + '</b></td><td>' + esc(r.left) + '</td>' +
            '<td><div class="fl-speed"><div class="bar"><i class="' + cls(r.used_pct) + '" style="width:' + r.used_pct + '%"></i></div><small>' + r.used_pct + '% of its time gone</small></div></td>' +
            '<td>' + esc(r.data) + '</td><td>' + esc(r.router) + '</td><td class="text-end"><a class="btn btn-sm btn-outline-danger" href="/vouchers/' + r.id + '/#fix"><i class="bi bi-wifi-off"></i> Can\'t connect?</a></td></tr>';
        }).join('') + '</tbody></table></div>' + (d.offline.total > rows.length ? '<p class="fl-note">Showing the ' + rows.length + ' that end soonest, of ' + d.offline.total + '.</p>' : '')
        : '<div class="fl-empty"><i class="bi bi-emoji-smile"></i><b>No started voucher is sitting unused</b><p class="mb-0">Every voucher whose time is running has a device online.</p></div>';
    }
    body.innerHTML = stale(d.stale) +
      '<div class="fl-tiles"><div class="fl-tile ' + (d.online_total ? 'warn' : 'good') + '"><span>Signed in, no traffic</span><b>' + d.online_total + '</b><small>of ' + d.sessions + ' sessions online</small></div>' +
      '<div class="fl-tile ' + (d.offline.total ? 'warn' : 'good') + '"><span>Time running, not online</span><b>' + d.offline.total + '</b><small>started vouchers with nobody signed in</small></div></div>' +
      '<div class="fl-bar"><div class="fl-seg" role="group" aria-label="Which idle vouchers"><button type="button" data-v="online" class="' + (st.idleView === 'online' ? 'on' : '') + '">Signed in, no traffic</button>' +
      '<button type="button" data-v="offline" class="' + (st.idleView === 'offline' ? 'on' : '') + '">Time running, not online</button></div>' +
      (st.idleView === 'online' ? '<div class="d-flex gap-2 flex-wrap"><select class="form-select form-select-sm w-auto" data-s="level" aria-label="How quiet"><option value="calm"' + (st.level === 'calm' ? ' selected' : '') + '>Only background chatter (under 10 kb/s)</option>' +
        '<option value="silent"' + (st.level === 'silent' ? ' selected' : '') + '>Nothing at all (under 1 kb/s)</option></select>' +
        '<select class="form-select form-select-sm w-auto" data-s="min" aria-label="For how long">' + [5, 10, 30, 60, 180].map(function (m) { return '<option value="' + m + '"' + (st.min === m ? ' selected' : '') + '>for ' + (m < 60 ? m + ' minutes' : m / 60 + ' hour' + (m > 60 ? 's' : '')) + ' or more</option>'; }).join('') + '</select></div>' : '') +
      '</div>' + table;
    [].forEach.call(body.querySelectorAll('.fl-seg button'), function (b) { b.addEventListener('click', function () { st.idleView = b.dataset.v; self.idle(pane, d); }); });
    [].forEach.call(body.querySelectorAll('select[data-s]'), function (s) { s.addEventListener('change', function () { st[s.dataset.s] = s.dataset.s === 'min' ? +s.value : s.value; self.load('idle'); }); });
  };

  Tabs.prototype.slow = function (pane, d) {
    var self = this, body = pane.querySelector('.fl-body'), c = d.counts, scans = Object.keys(d.scans), last = scans.length ? d.scans[scans[0]].at : null;
    var unread = scans.filter(function (k) { return !d.scans[k].readable; }).length;
    body.innerHTML = stale(d.stale) +
      '<div class="fl-tiles"><div class="fl-tile ' + (d.total ? 'warn' : 'good') + '"><span>Held back right now</span><b>' + d.total + '</b><small>of ' + d.sessions + ' sessions online</small></div>' +
      '<div class="fl-tile"><span>Fair usage</span><b>' + c.fup + '</b><small>passed a data step</small></div>' +
      '<div class="fl-tile"><span>Speed rules</span><b>' + c.rule + '</b><small>Traffic → Speed control</small></div>' +
      '<div class="fl-tile"><span>At their plan speed</span><b>' + c.limit + '</b><small>using 85% or more of it</small></div>' +
      '<div class="fl-tile ' + (c.drops ? 'bad' : '') + '"><span>Packets dropped</span><b>' + (scans.length ? c.drops : '—') + '</b><small>' + (last ? 'router scan ' + esc(ago(last)) : 'scan the routers to see') + '</small></div></div>' +
      '<div class="fl-bar"><span class="fl-note">Speed and limit come from live sync. <b>Scan</b> asks each router which speed limits are dropping packets — that is what a customer feels as “slow”.' +
      (unread ? ' ' + unread + ' router' + (unread > 1 ? 's' : '') + ' did not report drops (RouterOS version).' : '') + '</span>' +
      '<button type="button" class="btn btn-sm btn-primary" data-scan><i class="bi bi-radar"></i> Scan the routers for drops</button></div><div class="fl-scanout"></div>' +
      (d.rows.length ? '<div class="table-responsive"><table class="table align-middle fl-table"><thead><tr><th>Voucher</th><th>Device</th><th>Speed now</th><th>Limit</th><th>Drops</th><th>Why it is slow</th><th>Router</th><th></th></tr></thead><tbody>' +
        d.rows.map(function (r) {
          return '<tr><td>' + who(r) + '</td><td>' + devCell(r) + '</td>' +
            '<td><div class="fl-speed"><b>↓ ' + esc(r.down) + '</b> <small class="d-inline">↑ ' + esc(r.up) + '</small>' + (r.pct != null ? '<div class="bar"><i class="' + cls(r.pct) + '" style="width:' + r.pct + '%"></i></div><small>' + r.pct + '% of the limit</small>' : '') + '</div></td>' +
            '<td>' + (r.limit ? '<b>' + esc(r.limit) + '</b><small>' + esc({ fup: 'fair usage', rule: 'speed rule', plan: 'plan' }[r.source] || '') + '</small>' : '—') + '</td>' +
            '<td>' + (r.drops == null ? '<span class="text-secondary">not scanned</span>' : r.drops ? '<b class="text-danger">' + r.drops.toLocaleString() + '</b>' + (r.drop_pct ? '<small>' + r.drop_pct + '% of download</small>' : '') : '0') + '</td>' +
            '<td><div class="fl-why">' + r.reasons.map(function (x) { return '<span class="fl-chip ' + esc(x.key) + '">' + esc(x.text) + '</span>'; }).join('') + '</div></td>' +
            '<td>' + esc(r.router) + '</td><td class="text-end">' + labBtn(r, 'Diagnose') + '</td></tr>';
        }).join('') + '</tbody></table></div>'
        : '<div class="fl-empty"><i class="bi bi-lightning-charge"></i><b>Nobody is being held back</b><p class="mb-0">No online voucher is slowed by fair usage or a speed rule, or is using all of its plan speed' + (scans.length ? ', and no limiter is dropping packets' : '') + '.</p></div>');
    body.querySelector('[data-scan]').addEventListener('click', function () {
      var btn = this, out = body.querySelector('.fl-scanout');
      btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Scanning…';
      api(self.url(U.scan), {}).then(function (r) {
        if (!r.ok) { btn.disabled = false; btn.innerHTML = '<i class="bi bi-radar"></i> Scan the routers for drops'; out.innerHTML = '<div class="fl-warnline">' + esc(r.message) + '</div>'; return; }
        Promise.all(r.tests.map(function (t) { return follow(t); })).then(function (done) {
          var lines = done.map(function (t) { return '<b>' + esc(t.router) + ':</b> ' + esc(t.status === 'done' ? t.result.title : failText(t)); }).concat((r.notes || []).map(esc));
          self.scanNote = lines.length ? '<div class="fl-warnline">' + lines.join(' · ') + '</div>' : '';
          self.load('slow', true);
        });
      });
    });
    if (this.scanNote) { body.querySelector('.fl-scanout').innerHTML = this.scanNote; this.scanNote = ''; }
  };

  Tabs.prototype.lines = function (pane, d) {
    var self = this, body = pane.querySelector('.fl-body');
    body.innerHTML = (d.setup ? '<div class="fl-warnline"><i class="bi bi-tools"></i> ' + esc(d.setup) + '</div>' : '') + d.routers.map(function (r) {
      if (!r.multi) return '<section class="panel ln-router"><div class="ln-head"><h3><i class="bi bi-router"></i> ' + esc(r.router) + '</h3><a class="btn btn-sm btn-outline-primary" href="' + esc(r.design_url) + '"><i class="bi bi-diagram-3"></i> Internet lines</a></div>' +
        '<p class="fl-note mb-0">' + esc(r.why) + '</p></section>';
      var lines = r.lines.map(function (l, li) {
        var other = r.lines[(li + 1) % r.lines.length];
        return '<div class="ln-line"><h4><i class="bi bi-signpost-split"></i> ' + esc(l.label) + '</h4>' +
          '<div class="ln-fig"><div><b>' + l.customers + '</b><span>customers now</span></div><div><b>' + esc(l.down) + '</b><span>download</span></div><div><b>' + esc(l.up) + '</b><span>upload</span></div></div>' +
          '<div class="ln-share" title="Bar: share of customers now. Mark: the share you designed."><i style="width:' + l.real + '%"></i><u style="left:calc(' + l.share + '% - 1px)"></u></div>' +
          '<small class="fl-note">' + l.real + '% of customers now · designed for ' + l.share + '%</small>' +
          '<ul class="ln-people">' + (l.people.length ? l.people.map(function (p) {
            return '<li><span><b class="fl-code">' + esc(p.code) + '</b> <small>' + esc(p.ip) + ' · ↓ ' + esc(p.down) + (p.mixed ? ' · partly' : '') + '</small></span>' +
              '<span class="d-flex gap-1">' + (d.can_switch ? '<button type="button" class="btn btn-sm btn-outline-primary py-0" title="Move to ' + esc(other.label) + '" data-move="' + attr({ router: r.router_id, ip: p.ip, mac: p.mac, code: p.code, mark: other.mark, label: other.label, current: l.mark }) + '"><i class="bi bi-arrow-right-circle"></i> ' + esc(other.label) + '</button>' : '') +
              '<button type="button" class="btn btn-sm btn-light py-0" title="Connection lab" data-lab-open="' + attr({ router: r.router_id, ip: p.ip, mac: p.mac, code: p.code }) + '"><i class="bi bi-activity"></i></button></span></li>';
          }).join('') : '<li class="fl-note">Nobody seen on this line in the last minute.</li>') + '</ul></div>';
      }).join('');
      var pins = r.pins.length ? '<div class="ln-pins"><b><i class="bi bi-pin-angle-fill"></i> Switched by hand (' + r.pins.length + ')</b>' + r.pins.map(function (p) {
        return '<div class="ln-pin"><span><b class="fl-code">' + esc(p.code || p.mac || p.ip) + '</b> kept on <b>' + esc(p.label) + '</b>' + (p.from ? ' <small class="text-secondary">(was ' + esc(p.from) + ')</small>' : '') +
          ' <small class="text-secondary">· ' + esc(ago(p.since)) + (p.by ? ' by ' + esc(p.by) : '') + (p.confirmed ? '' : ' · waiting for the router') + '</small></span>' +
          '<button type="button" class="btn btn-sm btn-outline-secondary" data-auto="' + attr({ router: r.router_id, ip: p.ip, mac: p.mac, code: p.code }) + '"><i class="bi bi-arrow-counterclockwise"></i> Back to automatic</button></div>';
      }).join('') + '</div>' : '';
      return '<section class="panel ln-router" data-router="' + r.router_id + '"><div class="ln-head"><div><h3><i class="bi bi-router"></i> ' + esc(r.router) + '</h3><small class="fl-note">' + esc(r.via) + ' · ' + r.placed + ' of ' + r.online + ' customers placed on a line' +
        (r.seen_at ? ' · sampled ' + esc(ago(r.seen_at)) : ' · waiting for the first traffic sample (up to a minute)') + '</small></div>' +
        '<a class="btn btn-sm btn-outline-primary" href="' + esc(r.design_url) + '"><i class="bi bi-diagram-3"></i> Change the sharing</a></div>' +
        (r.stale ? stale([r.router]) : '') + '<div class="ln-grid">' + lines + '</div>' + pins + '<div class="ln-out mt-2"></div>' +
        (!r.sticky ? '<p class="fl-note mt-2 mb-0">This router shares connection by connection: each customer is shown on the line most of their connections use.</p>' : '') + '</section>';
    }).join('') || '<div class="fl-empty">No router yet.</div>';
    function act(btn, body2, question) {
      var sec = btn.closest('.ln-router'), out = sec.querySelector('.ln-out');
      if (question && !window.confirm(question)) return;
      [].forEach.call(sec.querySelectorAll('button'), function (b) { b.disabled = true; });
      out.innerHTML = '<div class="dh-res wait"><span class="spinner-border spinner-border-sm"></span> Sending to the router…</div>';
      api(U.line, body2).then(function (r) {
        if (!r.ok) { out.innerHTML = '<div class="dh-res bad">' + esc(r.message) + '</div>'; [].forEach.call(sec.querySelectorAll('button'), function (b) { b.disabled = false; }); return; }
        follow(r.test).then(function (t) {
          self.lineNote = t.status === 'done' ? '<div class="dh-res ' + (t.result.failed_step ? 'bad' : 'ok') + '"><b>' + esc(body2.code || body2.ip) + ': ' + esc(t.result.title) + '</b><br>' + esc(t.result.note || '') + '</div>' : '<div class="dh-res bad">' + esc(failText(t)) + '</div>';
          self.lineNoteFor = body2.router; self.load('lines', true);
        });
      });
    }
    [].forEach.call(body.querySelectorAll('[data-move]'), function (b) {
      b.addEventListener('click', function () { var p = JSON.parse(b.getAttribute('data-move')); act(b, Object.assign({ action: 'switch' }, p), 'Move ' + (p.code || p.ip) + ' to ' + p.label + '?\n\nIts open connections are closed so it changes line at once.'); });
    });
    [].forEach.call(body.querySelectorAll('[data-auto]'), function (b) {
      b.addEventListener('click', function () { act(b, Object.assign({ action: 'auto' }, JSON.parse(b.getAttribute('data-auto')))); });
    });
    if (this.lineNote) { var o = body.querySelector('.ln-router[data-router="' + this.lineNoteFor + '"] .ln-out') || body.querySelector('.ln-out'); if (o) o.innerHTML = this.lineNote; this.lineNote = ''; }
  };

  // ─────────── mount ───────────
  function mount() {
    [].forEach.call(document.querySelectorAll('[data-lab-mini]'), function (el) { if (!el._lab) { el._lab = 1; mini(el); } });
    [].forEach.call(document.querySelectorAll('[data-dhcp]:not([data-lazy])'), function (el) { if (!el._dhcp) el._dhcp = new Dhcp(el); });
    var nav = document.getElementById('flTabs'); if (nav && !nav._tabs) nav._tabs = new Tabs(nav);
    var find = document.querySelector('form[data-lab-find]');
    if (find && !find._lab) {
      find._lab = 1;
      find.addEventListener('submit', function (e) { e.preventDefault(); var code = find.code.value.trim(); if (code) openLab({ router: find.getAttribute('data-router'), code: code }); });
    }
  }
  window.TTLab = { open: openLab };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
