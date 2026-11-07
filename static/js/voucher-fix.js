/* Voucher fix — "Why can't it connect?" + Fix it (core/voucher_unstick.py).
   Mount: <div data-vfix data-url="/vouchers/12/fix/">            (a known voucher)
          <div data-vfix data-find="/vouchers/fix/find/">         (Network tools: type a code first) */
(function () {
  'use strict';
  var ICON = { bad: 'bi-x-octagon-fill', warn: 'bi-exclamation-triangle-fill', ok: 'bi-check-circle-fill' };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function csrf() {
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (m) return decodeURIComponent(m[1]);
    var i = document.querySelector('[name=csrfmiddlewaretoken]');
    return i ? i.value : '';
  }
  function getJSON(url, opts) {
    return fetch(url, Object.assign({ credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-CSRFToken': csrf(), 'Content-Type': 'application/json' } }, opts || {}))
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'TapTap answered ' + r.status }; }); });
  }

  function Fix(root) {
    this.root = root;
    this.url = root.getAttribute('data-url') || '';
    this.find = root.getAttribute('data-find') || '';
    this.timer = null;
    root.innerHTML =
      (this.find ? '<form class="vfx-find"><input class="form-control" name="code" placeholder="Voucher code or member username" autocomplete="off" required>' +
        '<button class="btn btn-primary"><i class="bi bi-search"></i> Check</button></form>' : '') +
      '<div class="vfx-head"></div><ul class="vfx-list"></ul><div class="vfx-go"></div><div class="vfx-result"></div>';
    this.head = root.querySelector('.vfx-head');
    this.list = root.querySelector('.vfx-list');
    this.go = root.querySelector('.vfx-go');
    this.result = root.querySelector('.vfx-result');
    var self = this, form = root.querySelector('.vfx-find');
    if (form) form.addEventListener('submit', function (e) {
      e.preventDefault();
      var code = form.code.value.trim();
      if (!code) return;
      self.busy('Checking ' + code + '…');
      getJSON(self.find + '?code=' + encodeURIComponent(code)).then(function (d) {
        if (!d.ok) return self.error(d.error);
        self.url = '/vouchers/' + d.voucher.id + '/fix/';
        self.show(d);
      }).catch(function () { self.error('TapTap could not be reached.'); });
    });
  }
  Fix.prototype.busy = function (text) {
    this.head.innerHTML = '<div class="vfx-busy"><span class="spinner-border spinner-border-sm"></span> ' + esc(text) + '</div>';
    this.list.innerHTML = ''; this.go.innerHTML = ''; this.result.innerHTML = '';
  };
  Fix.prototype.error = function (text) {
    this.head.innerHTML = '<div class="alert alert-danger mb-0">' + esc(text || 'Something went wrong.') + '</div>';
  };
  Fix.prototype.load = function () {
    var self = this;
    if (!this.url) return;
    this.busy('Looking at everything that can stop it…');
    getJSON(this.url).then(function (d) { d.ok ? self.show(d) : self.error(d.error); })
      .catch(function () { self.error('TapTap could not be reached.'); });
  };
  Fix.prototype.items = function (found) {
    if (!found || !found.length) return '<li class="vfx-i vfx-ok"><i class="bi ' + ICON.ok + '"></i><div><b>TapTap sees nothing stopping it</b>' +
      '<small>If the customer still cannot connect, press Fix it: the router is checked and read back.</small></div></li>';
    return found.map(function (f) {
      var tag = f.fix === 'auto' ? '<span class="vfx-tag vfx-auto">Fix it does this</span>' : f.fix === 'you' ? '<span class="vfx-tag vfx-you">You decide</span>' : '';
      return '<li class="vfx-i vfx-' + esc(f.level) + '"><i class="bi ' + (ICON[f.level] || 'bi-dot') + '"></i><div><b>' + esc(f.title) + '</b>' + tag +
        (f.text ? '<small>' + esc(f.text) + '</small>' : '') + '</div></li>';
    }).join('');
  };
  Fix.prototype.show = function (d) {
    var v = d.voucher || {}, self = this;
    this.head.innerHTML = '<div class="vfx-who"><a href="' + esc(v.url) + '"><b>' + esc(v.code) + '</b></a> <span class="badge text-bg-light">' + esc(v.state_label) + '</span>' +
      (v.router ? ' <span class="text-secondary small"><i class="bi bi-router"></i> ' + esc(v.router) + ' · ' + esc(v.via) + '</span>' : '') + '</div>';
    this.list.innerHTML = this.items(d.findings);
    this.result.innerHTML = '';
    if (d.fixable) {
      this.go.innerHTML = '<button class="btn btn-danger vfx-btn" type="button"><i class="bi bi-magic"></i> Fix it</button>' +
        '<small class="text-secondary">Resets its devices, switches it on, frees it for any phone, sets its time from the router\'s own counter, ends stuck sessions — then reads the router back.</small>';
      this.go.querySelector('button').addEventListener('click', function () { self.fix(this); });
    } else {
      this.go.innerHTML = '<div class="alert alert-warning mb-0 small"><i class="bi bi-info-circle"></i> Not fixable yet: ' + esc(d.blocked) + '.</div>';
    }
  };
  Fix.prototype.fix = function (btn) {
    var self = this;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Fixing…';
    getJSON(this.url, { method: 'POST', body: JSON.stringify({ reason: 'Customer could not connect' }) }).then(function (d) {
      btn.disabled = false; btn.innerHTML = '<i class="bi bi-magic"></i> Fix it again';
      self.list.innerHTML = self.items(d.findings_after || d.findings);
      var notes = (d.notes || []).map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('');
      self.result.innerHTML = '<div class="vfx-res ' + (d.ok ? 'is-ok' : (d.waiting ? 'is-wait' : 'is-bad')) + '"><b>' + esc(d.message || '') + '</b>' +
        (notes ? '<ul class="vfx-notes">' + notes + '</ul>' : '') + '<div class="vfx-router"></div></div>';
      if (d.lines && d.lines.length) self.router(d.ok, d.lines);
      if (d.cmd) self.poll(d.cmd, 0);
    }).catch(function () { btn.disabled = false; btn.innerHTML = '<i class="bi bi-magic"></i> Fix it'; self.error('TapTap could not be reached.'); });
  };
  Fix.prototype.router = function (ok, lines, waiting) {
    var box = this.result.querySelector('.vfx-router');
    if (!box) return;
    box.innerHTML = '<div class="vfx-rt"><i class="bi ' + (waiting ? 'bi-hourglass-split' : ok ? 'bi-router-fill' : 'bi-router') + '"></i> ' +
      (waiting ? 'Router' : ok ? 'The router has it right now' : 'The router says') + '</div><ul>' +
      lines.map(function (l) { return '<li>' + esc(l) + '</li>'; }).join('') + '</ul>';
    var res = this.result.querySelector('.vfx-res');
    if (res && !waiting) res.className = 'vfx-res ' + (ok ? 'is-ok' : 'is-bad');
  };
  Fix.prototype.poll = function (cmd, n) {
    var self = this;
    clearTimeout(this.timer);
    if (n > 60) return this.router(false, ['No answer from the router after 3 minutes. The fix stays queued; look at the TapTap Link page.']);
    this.timer = setTimeout(function () {
      getJSON(self.url + '?cmd=' + encodeURIComponent(cmd)).then(function (d) {
        if (d.status === 'waiting') { self.router(false, d.lines || [], true); return self.poll(cmd, n + 1); }
        self.router(d.status === 'done' && d.ok !== false, d.lines || []);
      }).catch(function () { self.poll(cmd, n + 1); });
    }, 3000);
  };

  function mount() {
    [].forEach.call(document.querySelectorAll('[data-vfix]'), function (el) {
      if (el._vfix) return;
      el._vfix = new Fix(el);
      var modal = el.closest('.modal');
      if (modal) modal.addEventListener('show.bs.modal', function () { el._vfix.load(); });
      else if (el._vfix.url) el._vfix.load();
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
