/* Speed per device (core/device_lanes.py, core/views_flow.py):
 *   <div data-lanes-voucher="12">   voucher / member page: one lane per device slot, live, check + guarantee
 *   <div data-lanes-tab>            Traffic → Speed per device: the always-on guarantee, router check + fix, lanes in force
 *   any form with [name=speed_limit] + [name=max_devices]: "2M/2M each · 3 devices → up to 6M" under the speed box
 */
(function () {
  'use strict';
  var U = { speed: '/flow/speed/', check: '/flow/speed/check/', fix: '/flow/speed/fix/', lane: '/flow/speed/lane/', test: '/flow/test/' };
  var STATE = { own: 'own lane', taptap: 'own lane (TapTap)', shared: 'sharing', none: 'no limit', old: 'old speed', parent: 'squeezed', free: 'no plan limit' };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    var i = document.querySelector('[name=csrfmiddlewaretoken]');
    return i ? i.value : '';
  }
  function api(url, body) {
    var o = { credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-CSRFToken': csrf() } };
    if (body !== undefined) { o.method = 'POST'; o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
    return fetch(url, o).then(function (r) {
      return r.json().catch(function () {
        return { ok: false, message: r.status === 403 ? 'You do not have access to this.' : r.status === 404 ? 'TapTap does not know this page — the update may not be installed yet.'
          : 'TapTap had an error (' + r.status + '). The details are in the server log (docker compose logs web).' };
      });
    }, function () { return { ok: false, message: 'TapTap could not be reached.' }; });
  }
  function follow(t, cb) {
    return new Promise(function (resolve) {
      var n = 0;
      (function step(x) {
        if (cb) cb(x);
        if (x.status === 'done' || x.status === 'failed') return resolve(x);
        if (++n > 150) return resolve(Object.assign({}, x, { status: 'failed', result: { error: 'No answer from the router yet — it stays queued; look again in a minute.' } }));
        setTimeout(function () { api(U.test + x.id + '/').then(function (d) { step(d.ok ? d.test : x); }); }, x.status === 'waiting' ? 3000 : 1500);
      })(t);
    });
  }
  function bps(v) { v = +v || 0; return v >= 1e9 ? (v / 1e9).toFixed(2) + ' Gb/s' : v >= 1e6 ? (v / 1e6).toFixed(1) + ' Mb/s' : v >= 1e3 ? Math.round(v / 1e3) + ' kb/s' : Math.round(v) + ' b/s'; }
  function ago(iso) { if (!iso) return ''; var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000); return s < 50 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : Math.round(s / 3600) + ' h ago'; }
  function when(iso) { if (!iso) return ''; var d = new Date(iso); return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }); }
  function fill(pct) { return pct >= 90 ? 'full' : pct >= 60 ? 'mid' : ''; }
  function note(level, html) { return '<div class="dh-res ' + level + '">' + html + '</div>'; }
  function failText(t) { return (t.result && (t.result.error || t.result.note)) || 'The router did not answer.'; }

  /* The road: one lane per allowed device. Online devices carry their live speed against the plan; free slots are dashed. */
  function road(devices, allowed, downLimit) {
    var lanes = devices.map(function (d, i) {
      var pct = d.pct != null ? d.pct : (downLimit ? Math.min(100, Math.round((d.down_bps || 0) * 100 / downLimit)) : 0);
      var name = d.label || d.ip;
      return '<div class="sp-lane' + (d.down_bps || d.up_bps ? '' : ' idle') + (d.state ? ' st-' + esc(d.state) : '') + '" title="' + esc(d.text || '') + '">' +
        '<i class="flow ' + fill(pct) + '" style="width:' + pct + '%"></i><b>' + esc(name) + '</b>' +
        '<span>↓ ' + esc(d.down || bps(d.down_bps)) + (downLimit ? ' <small>of ' + esc(bps(downLimit)) + '</small>' : '') + '</span>' +
        (d.state ? '<em>' + esc(STATE[d.state] || d.state) + '</em>' : '') + '</div>';
    });
    for (var i = devices.length; i < Math.min(allowed, 8); i++) lanes.push('<div class="sp-lane free"><b>Device ' + (i + 1) + '</b><span>free — gets its own ' + esc(downLimit ? bps(downLimit) : 'lane') + ' when it signs in</span></div>');
    if (allowed > 8 && devices.length < allowed) lanes.push('<div class="sp-lane free"><b>…</b><span>' + (allowed - Math.max(8, devices.length)) + ' more free lanes</span></div>');
    return '<div class="sp-road">' + lanes.join('') + '</div>';
  }

  // ═══════════════════════════ voucher / member page ═══════════════════════════
  function VoucherLanes(el) { this.el = el; this.id = el.getAttribute('data-lanes-voucher'); this.load(); var self = this; setInterval(function () { if (!document.hidden && !self.busy) self.load(true); }, 15000); }
  VoucherLanes.prototype.load = function (quiet) {
    var self = this;
    if (!quiet) this.el.innerHTML = '<div class="text-secondary small"><span class="spinner-border spinner-border-sm"></span> Reading its devices…</div>';
    api(U.speed + '?voucher=' + encodeURIComponent(this.id)).then(function (d) {
      if (!d.ok) { if (!quiet) self.el.innerHTML = '<div class="small text-secondary">' + esc(d.message) + '</div>'; return; }
      self.d = d; self.draw();
    });
  };
  VoucherLanes.prototype.draw = function () {
    var d = this.d, v = d.voucher, keepOut = this.out ? this.out.innerHTML : '', self = this;
    var lane = d.lane, guard = d.guaranteed;
    this.el.innerHTML =
      '<div class="sp-head"><h3><i class="bi bi-signpost-2"></i> Speed per device</h3>' +
      '<span class="sp-guard' + (guard ? '' : ' off') + '" title="' + (guard ? 'TapTap gives each device its own queue at the plan speed' : 'The router gives each device its own queue from its profile') + '">' +
      '<i class="bi ' + (guard ? 'bi-shield-check' : 'bi-shield') + '"></i> ' + (guard ? 'Guaranteed by TapTap' + (lane && lane.until ? ' until ' + esc(when(lane.until)) : d.shared_on && !lane ? ' (all shared vouchers)' : '') : 'From the router profile') + '</span></div>' +
      '<div class="sp-math">' + (v.rate ? 'Plan speed <b>' + esc(v.rate) + '</b> is per device. ' : '') + esc(d.math.text) + '</div>' +
      '<div class="sp-roadbox">' + road(d.devices, v.devices_allowed, v.down_bps) + '</div>' +
      (d.setup ? '<div class="fl-warnline"><i class="bi bi-tools"></i> ' + esc(d.setup) + '</div>' : '') +
      '<div class="sp-acts">' +
        (d.devices.length ? '<button type="button" class="btn btn-sm btn-outline-primary" data-a="check"><i class="bi bi-radar"></i> Check on the router</button>' : '<small class="text-secondary">Not online right now — lanes appear as devices sign in.</small>') +
        (d.can_lane && !lane ? '<button type="button" class="btn btn-sm btn-primary" data-a="add" title="TapTap puts each device in its own queue at the plan speed — at once, even for devices already online"><i class="bi bi-shield-check"></i> Guarantee each device its own lane</button>' : '') +
        (lane ? '<button type="button" class="btn btn-sm btn-outline-secondary" data-a="stop"><i class="bi bi-x-circle"></i> Stop the guarantee</button>' : '') +
      '</div><div class="sp-out"></div>';
    this.out = this.el.querySelector('.sp-out');
    if (keepOut) this.out.innerHTML = keepOut;
    [].forEach.call(this.el.querySelectorAll('[data-a]'), function (b) { b.addEventListener('click', function () { self.act(b.dataset.a, b); }); });
  };
  VoucherLanes.prototype.act = function (a, btn) {
    var self = this, d = this.d;
    if (a === 'stop' && !window.confirm('Stop the guarantee for ' + d.voucher.code + '? Its devices keep the queue their router profile gives them.')) return;
    btn.disabled = true; this.busy = true;
    if (a === 'check') {
      this.out.innerHTML = note('wait', '<span class="spinner-border spinner-border-sm"></span> Asking the router which queue carries each device…');
      api(U.check, { voucher: this.id }).then(function (r) {
        if (!r.ok) { self.busy = false; btn.disabled = false; self.out.innerHTML = note('bad', esc(r.message)); return; }
        Promise.all(r.tests.map(function (t) {
          return follow(t, function (x) { if (x.status === 'waiting') self.out.innerHTML = note('wait', '<span class="spinner-border spinner-border-sm"></span> Waiting for ' + esc(x.router) + '’s next check-in…'); });
        })).then(function (done) {
          self.busy = false;
          var bad = done.filter(function (t) { return t.status !== 'done'; });
          var mine = [];
          done.forEach(function (t) { ((t.result || {}).vouchers || []).forEach(function (x) { if (x.code.toUpperCase() === d.voucher.code.toUpperCase()) mine.push(x); }); });
          var ok = mine.length && mine.every(function (x) { return x.ok; });
          var fasttrack = done.some(function (t) { return ((t.result || {}).findings || []).some(function (f) { return f.key === 'fasttrack'; }); });
          self.out.innerHTML = bad.length ? note('bad', esc(failText(bad[0]))) :
            note(ok && !fasttrack ? 'ok' : 'bad', '<b>' + esc(mine.map(function (x) { return x.verdict; }).join(' ') || 'Its devices were not online when the router answered.') + '</b>' +
              (fasttrack ? '<br>FastTrack on ' + esc(done[0].router) + ' skips every speed limit — fix it in Traffic → Speed per device.' : '') +
              (!ok && d.can_lane && !d.lane ? '<br>Press <b>Guarantee each device its own lane</b> — TapTap puts each one in its own queue at once.' : ''));
          self.load(true);
        });
      });
      return;
    }
    api(U.lane, { action: a, voucher: this.id, lane: d.lane ? d.lane.id : 0 }).then(function (r) {
      self.busy = false;
      self.out.innerHTML = r.ok ? note('ok', a === 'add' ? '<b>Guaranteed.</b> Each device of ' + esc(d.voucher.code) + ' gets its own ' + esc(bps(d.voucher.down_bps)) +
        ' lane — within seconds on Direct API routers, at the next check-in on TapTap Link. It ends when the voucher’s time ends.' : 'The guarantee is off.') : note('bad', esc(r.message));
      self.load(true);
    });
  };

  // ═══════════════════════════ Traffic → Speed per device ═══════════════════════════
  function Tab(el) { this.el = el; this.q = el.getAttribute('data-query') || ''; this.load(); }
  Tab.prototype.url = function (u) { return u + (this.q ? '?' + this.q : ''); };
  Tab.prototype.load = function () {
    var self = this;
    if (!this.d) this.el.innerHTML = '<div class="lab-busy"><span class="spinner-border spinner-border-sm"></span> Loading…</div>';
    api(this.url(U.speed)).then(function (d) {
      if (!d.ok) { self.el.innerHTML = '<div class="alert alert-danger">' + esc(d.message) + '</div>'; return; }
      self.d = d; self.draw();
    });
  };
  Tab.prototype.draw = function () {
    var self = this, d = this.d, demo = [{ label: 'Phone', down_bps: 1.6e6, pct: 80 }, { label: 'Laptop', down_bps: 1.1e6, pct: 55 }, { label: 'Tablet', down_bps: 0.4e6, pct: 20 }];
    var hero = '<div class="sp-hero"><div><h3>Every device gets the whole plan speed — its own lane</h3>' +
      '<p>A 2 Mb/s plan with 3 devices means each device has its own 2 Mb/s, not 2 Mb/s split three ways. The router does this from the plan’s profile — when nothing on the router gets in the way. The check below finds what does; the guarantee makes TapTap enforce it itself.</p>' +
      (d.can_fix ? '<div class="form-check form-switch"><input class="form-check-input" type="checkbox" id="spShared"' + (d.shared_on ? ' checked' : '') + (d.setup ? ' disabled' : '') + '>' +
        '<label class="form-check-label" for="spShared"><b>Always guarantee it</b> — TapTap puts every device of a voucher or member allowed 2+ devices in its own queue at the plan speed, on every router, all the time.</label></div>' +
        '<small style="color:#9fb3c8">One small queue per device online. Your own speed rules and fair usage still win.' + (d.shared && d.shared.by ? ' On since ' + esc(when(d.shared.since)) + ' by ' + esc(d.shared.by) + '.' : '') + '</small>' : '') +
      '</div><div><div class="sp-math"><b>2M/2M plan · 3 devices</b> → each device 2 Mb/s · up to 6 Mb/s together</div>' + road(demo, 4, 2e6) + '</div></div>';
    var routers = d.routers.map(function (r) { return self.router(r); }).join('');
    var lanes = d.lanes.length ? '<section class="panel mb-3"><div class="panel-head"><div><h3><i class="bi bi-shield-check"></i> Vouchers guaranteed one by one (' + d.lanes.length + ')</h3>' +
      '<p>Each device of these gets its own queue at the plan speed until the voucher’s time ends.</p></div></div>' + d.lanes.map(function (l) {
        return '<div class="ln-pin"><span><a class="fl-code" href="/vouchers/' + l.voucher_id + '/">' + esc(l.voucher) + '</a> <small class="text-secondary">' + esc(l.reason) +
          (l.until ? ' · until ' + esc(when(l.until)) : '') + (l.by ? ' · by ' + esc(l.by) : '') + '</small></span>' +
          '<button type="button" class="btn btn-sm btn-outline-secondary" data-stop="' + l.id + '"><i class="bi bi-x-circle"></i> Stop</button></div>';
      }).join('') + '</section>' : '';
    var plans = d.plans.length ? '<section class="panel mb-3"><div class="panel-head"><div><h3><i class="bi bi-calculator"></i> What each shared plan really gives</h3>' +
      '<p>Plan speed is per device. A voucher with all its devices busy can use the speed times the devices — worth knowing when you price a plan.</p></div></div>' +
      '<div class="table-responsive"><table class="table align-middle sp-plans"><thead><tr><th>Plan</th><th>Devices</th><th>Each device</th><th>Whole voucher, all busy</th></tr></thead><tbody>' +
      d.plans.map(function (p) { return '<tr><td><b>' + esc(p.name) + '</b><small class="d-block text-secondary">' + esc(p.kind) + '</small></td><td>' + p.devices + '</td><td>' + esc(p.each) + '</td><td>' + esc(p.total || '—') + '</td></tr>'; }).join('') +
      '</tbody></table></div></section>' : '';
    this.el.innerHTML = (d.setup ? '<div class="fl-warnline"><i class="bi bi-tools"></i> ' + esc(d.setup) + '</div>' : '') + hero + routers + lanes + plans;
    var sw = this.el.querySelector('#spShared');
    if (sw) sw.addEventListener('change', function () {
      if (sw.checked && !window.confirm('Guarantee every device of every shared voucher its own lane, on every router?\n\nTapTap adds one small queue per device online and keeps it right on every live-sync pass.')) { sw.checked = false; return; }
      sw.disabled = true;
      api(U.lane, { action: sw.checked ? 'shared_on' : 'shared_off' }).then(function (r) { if (!r.ok) window.alert(r.message); self.load(); });
    });
    [].forEach.call(this.el.querySelectorAll('[data-stop]'), function (b) {
      b.addEventListener('click', function () { b.disabled = true; api(U.lane, { action: 'stop', lane: +b.dataset.stop }).then(function () { self.load(); }); });
    });
    [].forEach.call(this.el.querySelectorAll('[data-check]'), function (b) { b.addEventListener('click', function () { self.check(b); }); });
    [].forEach.call(this.el.querySelectorAll('[data-fix]'), function (b) { b.addEventListener('click', function () { self.fix(b); }); });
  };
  Tab.prototype.router = function (r) {
    var L = r.last, head = '<section class="panel mb-3 sp-router" data-router="' + r.router_id + '"><div class="ln-head"><div><h3><i class="bi bi-router"></i> ' + esc(r.router) + '</h3>' +
      '<small class="fl-note">' + esc(r.via) + ' · ' + r.shared_online + ' device' + (r.shared_online === 1 ? '' : 's') + ' of shared vouchers online' +
      (r.last_at ? ' · checked ' + esc(ago(r.last_at)) : ' · not checked yet') + '</small></div>' +
      '<button type="button" class="btn btn-sm btn-primary" data-check="' + r.router_id + '"' + (r.shared_online && this.d.can_fix ? '' : ' disabled') + '><i class="bi bi-radar"></i> Check every shared voucher</button></div>';
    if (!L) return head + '<p class="fl-note mb-0">' + (r.shared_online ? 'Press Check: the router says which queue really carries each device.' : 'Nobody with a shared voucher is online right now.') + '</p><div class="sp-out"></div></section>';
    var s = L.summary;
    var tiles = '<div class="fl-tiles"><div class="fl-tile ' + (s.problems ? '' : 'good') + '"><span>Own lane</span><b>' + s.own + '</b><small>of ' + s.devices + ' devices checked</small></div>' +
      '<div class="fl-tile ' + (s.shared ? 'bad' : '') + '"><span>Sharing a queue</span><b>' + s.shared + '</b><small>split one speed</small></div>' +
      '<div class="fl-tile ' + (s.none ? 'bad' : '') + '"><span>No limit at all</span><b>' + s.none + '</b><small>get the whole line</small></div>' +
      '<div class="fl-tile ' + (s.old + s.parent ? 'warn' : '') + '"><span>Old speed / squeezed</span><b>' + (s.old + s.parent) + '</b><small>own lane, wrong speed</small></div></div>';
    var findings = (L.findings || []).map(function (f) {
      return '<li class="lab-f f-' + esc(f.level) + '"><i class="bi ' + (f.level === 'bad' ? 'bi-x-octagon-fill' : 'bi-exclamation-triangle-fill') + '"></i><div><b>' + esc(f.title) + '</b><small>' + esc(f.text) + '</small></div></li>';
    }).join('');
    var vrows = (L.vouchers || []).map(function (x) {
      return '<div class="sp-vrow' + (x.ok ? '' : ' bad') + '"><div class="sp-vhead"><span>' + (x.id ? '<a class="fl-code" href="/vouchers/' + x.id + '/">' + esc(x.code) + '</a>' : '<span class="fl-code">' + esc(x.code) + '</span>') +
        ' <small class="text-secondary">' + esc(x.plan) + (x.rate ? ' · ' + esc(x.rate) + ' each' : '') + ' · ' + x.devices.length + ' of ' + x.devices_allowed + ' devices online</small></span>' +
        '<small class="' + (x.ok ? 'text-success' : 'text-danger') + ' fw-bold">' + esc(x.verdict) + '</small></div>' +
        x.devices.map(function (dv) {
          return '<div class="sp-dev"><span class="sp-dot st-' + esc(dv.state) + '"></span><div><b>' + esc(dv.label || dv.ip) + '</b> <small class="text-secondary font-monospace">' + esc(dv.ip) + '</small>' +
            ' · ' + esc(STATE[dv.state] || dv.state) + (dv.limit ? ' · ' + esc(dv.limit) : '') + (dv.fasttrack ? ' · <span class="text-danger">FastTrack skips it</span>' : '') +
            '<small class="d-block text-secondary">' + esc(dv.text) + '</small></div></div>';
        }).join('') + '</div>';
    }).join('');
    var fx = L.fix || {}, can = fx.rates.length || fx.unparent.length || fx.guard || fx.lanes.length;
    var fixbox = can && this.d.can_fix ? '<div class="sp-fixbox"><b><i class="bi bi-magic"></i> Give every device its own lane</b>' +
      (fx.lanes.length ? '<div class="form-check"><input class="form-check-input" type="checkbox" id="fxL' + r.router_id + '" checked><label class="form-check-label" for="fxL' + r.router_id + '">Guarantee the ' + fx.lanes.length + ' voucher' + (fx.lanes.length > 1 ? 's' : '') + ' above — each device its own queue now, until their time ends</label></div>' : '') +
      (fx.rates.length || fx.unparent.length ? '<div class="form-check"><input class="form-check-input" type="checkbox" id="fxP' + r.router_id + '" checked><label class="form-check-label" for="fxP' + r.router_id + '">Fix the router profiles (' +
        [fx.rates.length ? fx.rates.length + ' speed' + (fx.rates.length > 1 ? 's' : '') : '', fx.unparent.length ? fx.unparent.length + ' parent queue' + (fx.unparent.length > 1 ? 's' : '') : ''].filter(Boolean).join(', ') + ') — every new sign-in gets its own lane</label></div>' : '') +
      (fx.guard ? '<div class="form-check"><input class="form-check-input" type="checkbox" id="fxG' + r.router_id + '" checked><label class="form-check-label" for="fxG' + r.router_id + '">Keep hotspot customers out of FastTrack — without it no speed limit works (the router works a little harder)</label></div>' : '') +
      '<button type="button" class="btn btn-sm btn-success mt-1" data-fix="' + r.router_id + '"><i class="bi bi-magic"></i> Fix it</button></div>' : '';
    return head + '<div class="lab-headline h-' + esc(L.level === 'idle' ? 'ok' : L.level) + '"><i class="bi ' + (L.level === 'ok' ? 'bi-check-circle-fill' : 'bi-exclamation-octagon-fill') + '"></i> <span>' + esc(L.title) + '</span></div>' +
      tiles + (findings ? '<ul class="lab-find mb-2">' + findings + '</ul>' : '') + fixbox + '<div class="sp-out"></div>' + vrows +
      '<p class="fl-note mb-0">' + L.queues_read + ' queues read on the router.</p></section>';
  };
  Tab.prototype.check = function (btn) {
    var self = this, sec = btn.closest('.sp-router'), out = sec.querySelector('.sp-out');
    btn.disabled = true;
    out.innerHTML = note('wait', '<span class="spinner-border spinner-border-sm"></span> Asking the router which queue carries each device…');
    api(this.url(U.check), { router: +btn.dataset.check }).then(function (r) {
      if (!r.ok) { btn.disabled = false; out.innerHTML = note('bad', esc(r.message)); return; }
      var t = r.tests.filter(function (x) { return x.router_id === +btn.dataset.check; })[0] || r.tests[0];
      if (!t) { btn.disabled = false; out.innerHTML = note('bad', esc((r.notes || []).join(' · ') || 'Nothing to check.')); return; }
      follow(t, function (x) { if (x.status === 'waiting') out.innerHTML = note('wait', '<span class="spinner-border spinner-border-sm"></span> Waiting for the router’s next check-in…'); })
        .then(function (done) { if (done.status !== 'done') { btn.disabled = false; out.innerHTML = note('bad', esc(failText(done))); return; } self.load(); });
    });
  };
  Tab.prototype.fix = function (btn) {
    var self = this, rid = +btn.dataset.fix, sec = btn.closest('.sp-router'), out = sec.querySelector('.sp-out');
    var box = function (p) { var e = document.getElementById(p + rid); return e ? e.checked : false; };
    var body = { router: rid, lanes: box('fxL'), profiles: box('fxP'), guard: box('fxG') };
    if (!body.lanes && !body.profiles && !body.guard) return;
    btn.disabled = true;
    out.innerHTML = note('wait', '<span class="spinner-border spinner-border-sm"></span> Fixing…');
    api(U.fix, body).then(function (r) {
      if (!r.ok) { btn.disabled = false; out.innerHTML = note('bad', esc(r.message)); return; }
      var lanesText = r.lanes_made ? r.lanes_made + ' voucher' + (r.lanes_made > 1 ? 's' : '') + ' now guaranteed — each device its own lane. ' : '';
      if (!r.test) { out.innerHTML = note('ok', esc(lanesText || r.message || 'Done.')); setTimeout(function () { self.load(); }, 1500); return; }
      follow(r.test, function (x) { if (x.status === 'waiting') out.innerHTML = note('wait', '<span class="spinner-border spinner-border-sm"></span> ' + esc(lanesText) + 'Waiting for the router’s next check-in…'); })
        .then(function (t) {
          var res = t.result || {};
          self.fixNote = { router: rid, html: t.status === 'done' ? note(res.failed_step ? 'bad' : 'ok', '<b>' + esc(lanesText + (res.fix_title || 'Done')) + '</b><br>' + esc(res.fix_note || '')) : note('bad', esc(failText(t))) };
          self.load();
        });
    });
  };
  var _draw = Tab.prototype.draw;
  Tab.prototype.draw = function () {
    _draw.call(this);
    if (this.fixNote) { var o = this.el.querySelector('.sp-router[data-router="' + this.fixNote.router + '"] .sp-out'); if (o) o.innerHTML = this.fixNote.html; this.fixNote = null; }
  };

  // ═══════════════════════════ plan forms: the per-device math ═══════════════════════════
  function parse(t) {
    var m = String(t || '').trim().split(/\s+/)[0].match(/^(\d+(?:\.\d+)?)([kKmMgG]?)(?:\/(\d+(?:\.\d+)?)([kKmMgG]?))?$/);
    if (!m) return null;
    var mul = { '': 1, k: 1e3, m: 1e6, g: 1e9 }, up = +m[1] * mul[m[2].toLowerCase()], dn = m[3] ? +m[3] * mul[(m[4] || '').toLowerCase()] : up;
    return { up: up, down: dn };
  }
  function planHints() {
    [].forEach.call(document.querySelectorAll('form'), function (f) {
      var sp = f.querySelector('[name=speed_limit]'), dv = f.querySelector('[name=max_devices]');
      if (!sp || !dv || sp._lane) return;
      sp._lane = 1;
      var hint = document.createElement('small'); hint.className = 'd-block text-secondary mt-1'; hint.setAttribute('aria-live', 'polite');
      sp.insertAdjacentElement('afterend', hint);
      function show() {
        var r = parse(sp.value), n = Math.max(1, parseInt(dv.value, 10) || 1);
        if (!sp.value.trim()) { hint.innerHTML = '<i class="bi bi-signpost-2"></i> Empty = full speed for every device.'; return; }
        if (!r) { hint.textContent = 'Write it like 2M/2M (upload/download) or 512k/1M.'; return; }
        hint.innerHTML = '<i class="bi bi-signpost-2"></i> Per device: each device gets ↓ ' + esc(bps(r.down)) + ' · ↑ ' + esc(bps(r.up)) + ' of its own' +
          (n > 1 ? ' — <b>up to ↓ ' + esc(bps(r.down * n)) + '</b> for the whole voucher when all ' + n + ' devices are busy.' : '.');
      }
      sp.addEventListener('input', show); dv.addEventListener('input', show); dv.addEventListener('change', show);
      var modal = f.closest('.modal'); if (modal) modal.addEventListener('shown.bs.modal', show);
      show();
    });
  }

  function mount() {
    [].forEach.call(document.querySelectorAll('[data-lanes-voucher]'), function (el) { if (!el._l) el._l = new VoucherLanes(el); });
    [].forEach.call(document.querySelectorAll('[data-lanes-tab]:not([data-lazy])'), function (el) { if (!el._l) el._l = new Tab(el); });
    planHints();
  }
  window.TTLanes = { tab: function (el) { if (!el._l) { el.removeAttribute('data-lazy'); el._l = new Tab(el); } else el._l.load(); }, mount: mount };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
