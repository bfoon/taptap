/* TapTap Line Board — customers grouped by the internet line they use; drag one or many onto another line.
 * Shared by the Internet lines page (static/js/line-hub.js) and Traffic → Internet lines (static/js/flow.js).
 *
 *   var b = TTBoard.mount(el, { move: function (payload) { return Promise<{ok, message, via_link}> }, onDone: fn });
 *   b.render(boardData);          // data from core/line_hub.board()
 *
 * Moving: drag a customer (or the selected ones) onto a line, or select and use the bar at the bottom. Touch screens
 * use the bar (and the ⋯ button on a card). Nothing moves on the router before the person confirms.
 */
(function () {
  'use strict';
  var COLORS = ['#5aa1ff', '#3ddc97', '#ffb547', '#c792ff', '#ff7a90', '#4fd1e0'];
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function bps(v) { v = +v || 0; return v >= 1e9 ? (v / 1e9).toFixed(2) + ' Gb/s' : v >= 1e6 ? (v / 1e6).toFixed(1) + ' Mb/s' : v >= 1e3 ? Math.round(v / 1e3) + ' kb/s' : Math.round(v) + ' b/s'; }
  function keyOf(p) { return (p.mac || '') + '|' + p.ip; }
  function ago(iso) { if (!iso) return ''; var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000); return s < 50 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : Math.round(s / 3600) + ' h ago'; }

  /* ───────── a small animated confirm dialog (also used by line-hub.js) ───────── */
  function ask(opts) {
    return new Promise(function (resolve) {
      var wrap = document.createElement('div');
      wrap.className = 'lh-modal';
      wrap.innerHTML = '<div class="lh-modal-card ' + esc(opts.tone || '') + '" role="dialog" aria-modal="true" aria-labelledby="lhmT">' +
        '<div class="lh-modal-icon"><i class="bi ' + esc(opts.icon || 'bi-question-circle') + '"></i></div>' +
        '<h3 id="lhmT">' + esc(opts.title) + '</h3><div class="lh-modal-body">' + (opts.html || '<p>' + esc(opts.text || '') + '</p>') + '</div>' +
        (opts.typeToConfirm ? '<label class="lh-type">Type <b>' + esc(opts.typeToConfirm) + '</b> to confirm<input type="text" autocomplete="off" spellcheck="false"></label>' : '') +
        '<div class="lh-modal-actions"><button type="button" class="btn btn-light" data-no>' + esc(opts.cancel || 'Cancel') + '</button>' +
        '<button type="button" class="btn ' + (opts.tone === 'danger' ? 'btn-danger' : 'btn-primary') + '" data-yes>' + esc(opts.ok || 'Continue') + '</button></div></div>';
      document.body.appendChild(wrap);
      var yes = wrap.querySelector('[data-yes]'), inp = wrap.querySelector('.lh-type input');
      if (inp) { yes.disabled = true; inp.addEventListener('input', function () { yes.disabled = inp.value.trim().toLowerCase() !== String(opts.typeToConfirm).trim().toLowerCase(); }); }
      requestAnimationFrame(function () { wrap.classList.add('on'); (inp || yes).focus(); });
      function close(v) { wrap.classList.remove('on'); setTimeout(function () { wrap.remove(); }, 220); document.removeEventListener('keydown', onKey); resolve(v); }
      function onKey(e) { if (e.key === 'Escape') close(false); if (e.key === 'Enter' && !yes.disabled) close(true); }
      document.addEventListener('keydown', onKey);
      wrap.addEventListener('click', function (e) { if (e.target === wrap) close(false); });
      wrap.querySelector('[data-no]').onclick = function () { close(false); };
      yes.onclick = function () { close(true); };
    });
  }

  function toast(message, tone) {
    var host = document.querySelector('.lh-toasts');
    if (!host) { host = document.createElement('div'); host.className = 'lh-toasts'; host.setAttribute('aria-live', 'polite'); document.body.appendChild(host); }
    var t = document.createElement('div');
    t.className = 'lh-toast ' + (tone || 'ok');
    t.innerHTML = '<i class="bi ' + (tone === 'bad' ? 'bi-x-octagon-fill' : tone === 'wait' ? 'bi-hourglass-split' : 'bi-check-circle-fill') + '"></i><span>' + esc(message) + '</span><button type="button" aria-label="Close">&times;</button>';
    host.appendChild(t);
    requestAnimationFrame(function () { t.classList.add('on'); });
    var gone = function () { t.classList.remove('on'); setTimeout(function () { t.remove(); }, 300); };
    t.querySelector('button').onclick = gone;
    setTimeout(gone, tone === 'bad' ? 12000 : 7000);
  }

  /* ───────── the board ───────── */
  function Board(el, opts) {
    this.el = el; this.opts = opts || {}; this.sel = {}; this.filter = ''; this.data = null; this.busy = false;
    this.el.classList.add('lb');
  }

  Board.prototype.people = function () {
    var all = [];
    (this.data.columns || []).forEach(function (c) { c.people.forEach(function (p) { all.push([c, p]); }); });
    (this.data.unplaced || []).forEach(function (p) { all.push([null, p]); });
    return all;
  };
  Board.prototype.selected = function () {
    var self = this;
    return this.people().filter(function (x) { return self.sel[keyOf(x[1])]; });
  };

  Board.prototype.render = function (data) {
    if (this.dragging) { this.pending = data; return; }
    var self = this, first = {};
    if (this.data && !reduce) [].forEach.call(this.el.querySelectorAll('.lb-chip'), function (c) { first[c.dataset.k] = c.getBoundingClientRect(); });
    this.data = data;
    var known = {}; this.people().forEach(function (x) { known[keyOf(x[1])] = 1; });
    Object.keys(this.sel).forEach(function (k) { if (!known[k]) delete self.sel[k]; });
    var cols = data.columns || [], movable = data.can_move, f = this.filter.toLowerCase();
    var head = '<div class="lb-top"><div class="lb-meta"><span class="lb-live' + (data.fresh ? '' : ' stale') + '"><i></i>' + (data.fresh ? 'Live' : 'Waiting for live sync') + '</span>' +
      '<b>' + data.placed + '</b> of <b>' + data.online + '</b> online customers placed on a line' + (data.nat_at ? ' · traffic sampled ' + esc(ago(data.nat_at)) : '') +
      (data.strategy_label ? ' · <span class="lb-strat">' + esc(data.strategy_label) + '</span>' : '') + '</div>' +
      '<label class="lb-search"><i class="bi bi-search"></i><input type="search" placeholder="Find a voucher, IP or MAC" value="' + esc(this.filter) + '" aria-label="Find a customer"></label></div>' +
      (!movable && data.why ? '<div class="lb-why"><i class="bi bi-info-circle"></i> ' + esc(data.why) + '</div>' : '') +
      (movable ? '<div class="lb-hint"><i class="bi bi-hand-index-thumb"></i> Drag a customer onto another line — or tick several and use the bar below. Their open connections are closed so they switch at once; apps reconnect by themselves.</div>' : '');
    var colHtml = cols.map(function (c, i) {
      var color = COLORS[(c.color != null ? c.color : i) % 6], drop = movable && c.mark && c.up && c.use !== 'out';
      var people = c.people.filter(function (p) { return !f || (p.code + ' ' + p.ip + ' ' + p.mac).toLowerCase().indexOf(f) >= 0; });
      var state = c.missing ? 'port missing' : !c.up ? 'not connected' : c.use === 'out' ? 'out of use' : '';
      return '<section class="lb-col' + (drop ? ' drop' : '') + (state ? ' dim' : '') + '" data-if="' + esc(c.interface) + '" style="--c:' + color + '" aria-label="' + esc(c.label) + '">' +
        '<header><div class="lb-name"><span class="lb-dot"></span><b>' + esc(c.label) + '</b><small>' + esc(c.interface) + (state ? ' · ' + state : '') + '</small></div>' +
        '<div class="lb-num"><b data-n="' + c.users + '">' + c.users + '</b><span>customers</span></div></header>' +
        '<div class="lb-speeds"><span>↓ ' + bps(c.down_bps) + '</span><span>↑ ' + bps(c.up_bps) + '</span></div>' +
        '<div class="lb-share" title="Bar: share of customers now' + (c.share != null ? ' · mark: the share you designed (' + c.share + '%)' : '') + '"><i style="width:' + (c.real || 0) + '%"></i>' + (c.share != null ? '<u style="left:calc(' + c.share + '% - 1px)"></u>' : '') + '</div>' +
        '<small class="lb-sharetxt">' + (c.real || 0) + '% now' + (c.share != null ? ' · designed ' + c.share + '%' : '') + '</small>' +
        (cols.length > 0 && c.people.length ? '<button type="button" class="lb-all" data-all="' + esc(c.interface) + '">Select all ' + c.people.length + '</button>' : '') +
        '<div class="lb-list">' + (people.length ? people.map(function (p) { return self.chip(p, c, movable); }).join('') : '<div class="lb-empty">' + (f ? 'No match' : drop ? 'Drop customers here' : 'Nobody on this line now') + '</div>') +
        (c.more ? '<div class="lb-empty">+ ' + c.more + ' more</div>' : '') + '</div></section>';
    }).join('');
    var un = data.unplaced || [];
    if (un.length) {
      colHtml += '<section class="lb-col unplaced" data-if="" style="--c:#94a3b8"><header><div class="lb-name"><span class="lb-dot"></span><b>Not placed yet</b><small>no traffic sampled in the last minute</small></div>' +
        '<div class="lb-num"><b>' + un.length + '</b><span>customers</span></div></header><div class="lb-list">' +
        un.filter(function (p) { return !f || (p.code + ' ' + p.ip + ' ' + p.mac).toLowerCase().indexOf(f) >= 0; }).map(function (p) { return self.chip(p, null, movable); }).join('') + '</div></section>';
    }
    var pins = (data.pins || []).length ? '<details class="lb-pins"><summary><i class="bi bi-pin-angle-fill"></i> Kept on a line by hand (' + data.pins.length + ')</summary>' + data.pins.map(function (p) {
      return '<div class="lb-pin"><span><b class="lb-code">' + esc(p.code || p.mac || p.ip) + '</b> → <b>' + esc(p.label) + '</b>' + (p.from ? ' <small>(was ' + esc(p.from) + ')</small>' : '') +
        ' <small>· ' + esc(ago(p.since)) + (p.by ? ' by ' + esc(p.by) : '') + (p.confirmed ? '' : ' · waiting for the router') + '</small></span>' +
        '<button type="button" class="btn btn-sm btn-outline-secondary" data-unpin="' + esc(JSON.stringify({ ip: p.ip, mac: p.mac, code: p.code })) + '"><i class="bi bi-arrow-counterclockwise"></i> Back to automatic</button></div>';
    }).join('') + '</details>' : '';
    this.el.innerHTML = head + '<div class="lb-cols" style="--n:' + Math.max(1, cols.length + (un.length ? 1 : 0)) + '">' + (colHtml || '<div class="lb-empty big">No internet line found on this router.</div>') + '</div>' + pins +
      '<div class="lb-bar" role="toolbar" aria-label="Selected customers"></div>';
    this.wire();
    this.bar();
    if (!reduce) {     // FLIP: chips glide from where they were to where they are now
      [].forEach.call(this.el.querySelectorAll('.lb-chip'), function (c) {
        var a = first[c.dataset.k]; if (!a) { c.classList.add('lb-new'); return; }
        var b = c.getBoundingClientRect(), dx = a.left - b.left, dy = a.top - b.top;
        if (Math.abs(dx) + Math.abs(dy) < 2) return;
        c.style.transform = 'translate(' + dx + 'px,' + dy + 'px)'; c.style.transition = 'none';
        requestAnimationFrame(function () { requestAnimationFrame(function () { c.style.transition = 'transform .65s cubic-bezier(.2,.8,.2,1)'; c.style.transform = ''; }); });
        if (Math.abs(dx) > 40) c.classList.add('lb-flew');
      });
    }
    // numbers that changed count up / down
    [].forEach.call(this.el.querySelectorAll('.lb-num b[data-n]'), function (b) {
      var col = b.closest('.lb-col'), prev = self.prevN && self.prevN[col.dataset.if];
      if (prev != null && prev !== +b.dataset.n) { b.classList.add('lb-bump'); }
    });
    this.prevN = {}; cols.forEach(function (c) { self.prevN[c.interface] = c.users; });
  };

  Board.prototype.chip = function (p, col, movable) {
    var k = keyOf(p), on = !!this.sel[k];
    var how = p.pinned ? '<i class="bi bi-pin-angle-fill" title="Kept on this line by hand' + (p.pin_confirmed ? '' : ' — waiting for the router') + '"></i>'
      : p.how === 'main' ? '<i class="bi bi-diagram-2" title="On the main line (the backup lines wait)"></i>' : '';
    return '<div class="lb-chip' + (on ? ' sel' : '') + (p.pinned ? ' pinned' : '') + '" data-k="' + esc(k) + '"' + (movable ? ' draggable="true"' : '') + ' tabindex="0">' +
      '<label class="lb-tick" title="Select"><input type="checkbox"' + (on ? ' checked' : '') + ' aria-label="Select ' + esc(p.code || p.ip) + '"><span></span></label>' +
      '<div class="lb-who"><b class="lb-code">' + esc(p.code || p.ip) + '</b>' + how + '<small>' + esc(p.ip) + (p.mac ? ' · ' + esc(p.mac.slice(-8)) : '') + '</small></div>' +
      '<div class="lb-spd"><span class="' + (p.down_bps > 1e6 ? 'hot' : '') + '">↓ ' + bps(p.down_bps) + '</span><small>↑ ' + bps(p.up_bps) + '</small></div>' +
      (movable ? '<button type="button" class="lb-more" aria-label="Move ' + esc(p.code || p.ip) + '"><i class="bi bi-three-dots"></i></button>' : '') + '</div>';
  };

  Board.prototype.byKey = function (k) {
    var hit = null; this.people().some(function (x) { if (keyOf(x[1]) === k) { hit = x; return true; } return false; }); return hit;
  };

  Board.prototype.wire = function () {
    var self = this, el = this.el;
    var s = el.querySelector('.lb-search input');
    if (s) s.addEventListener('input', function () { self.filter = s.value; var pos = s.selectionStart; self.render(self.data); var n = self.el.querySelector('.lb-search input'); n.focus(); try { n.setSelectionRange(pos, pos); } catch (e) {} });
    [].forEach.call(el.querySelectorAll('.lb-chip'), function (c) {
      var k = c.dataset.k, cb = c.querySelector('input');
      cb.addEventListener('change', function () { if (cb.checked) self.sel[k] = 1; else delete self.sel[k]; c.classList.toggle('sel', cb.checked); self.bar(); });
      c.addEventListener('keydown', function (e) { if (e.key === ' ' && e.target === c) { e.preventDefault(); cb.checked = !cb.checked; cb.dispatchEvent(new Event('change')); } });
      var more = c.querySelector('.lb-more');
      if (more) more.addEventListener('click', function (e) { e.stopPropagation(); if (!self.sel[k]) { self.sel = {}; self.sel[k] = 1; } self.render(self.data); self.bar(true); });
      c.addEventListener('dragstart', function (e) {
        if (!self.sel[k]) { self.sel = {}; self.sel[k] = 1; [].forEach.call(el.querySelectorAll('.lb-chip.sel'), function (x) { x.classList.remove('sel'); x.querySelector('input').checked = false; }); c.classList.add('sel'); cb.checked = true; }
        self.dragging = true;
        var n = Object.keys(self.sel).length;
        e.dataTransfer.effectAllowed = 'move';
        try { e.dataTransfer.setData('text/plain', k); } catch (x) {}
        var ghost = document.createElement('div'); ghost.className = 'lb-ghost'; ghost.textContent = n > 1 ? n + ' customers' : (self.byKey(k) || [0, {}])[1].code || 'customer';
        document.body.appendChild(ghost); try { e.dataTransfer.setDragImage(ghost, 20, 20); } catch (x) {}
        setTimeout(function () { ghost.remove(); }, 0);
        el.classList.add('dragging');
        [].forEach.call(el.querySelectorAll('.lb-chip.sel'), function (x) { x.classList.add('lifted'); });
      });
      c.addEventListener('dragend', function () {
        self.dragging = false; el.classList.remove('dragging');
        [].forEach.call(el.querySelectorAll('.lifted,.over'), function (x) { x.classList.remove('lifted'); x.classList.remove('over'); });
        if (self.pending) { var p = self.pending; self.pending = null; self.render(p); }
      });
    });
    [].forEach.call(el.querySelectorAll('.lb-col.drop'), function (col) {
      col.addEventListener('dragover', function (e) { if (!self.dragging) return; e.preventDefault(); e.dataTransfer.dropEffect = 'move'; col.classList.add('over'); });
      col.addEventListener('dragleave', function (e) { if (!col.contains(e.relatedTarget)) col.classList.remove('over'); });
      col.addEventListener('drop', function (e) { e.preventDefault(); col.classList.remove('over'); self.dragging = false; el.classList.remove('dragging'); self.moveTo(col.dataset.if); });
    });
    [].forEach.call(el.querySelectorAll('[data-all]'), function (b) {
      b.addEventListener('click', function () {
        var col = (self.data.columns || []).filter(function (c) { return c.interface === b.dataset.all; })[0]; if (!col) return;
        var all = col.people.every(function (p) { return self.sel[keyOf(p)]; });
        col.people.forEach(function (p) { if (all) delete self.sel[keyOf(p)]; else self.sel[keyOf(p)] = 1; });
        self.render(self.data);
      });
    });
    [].forEach.call(el.querySelectorAll('[data-unpin]'), function (b) {
      b.addEventListener('click', function () { self.send({ auto: true, devices: [JSON.parse(b.dataset.unpin)] }, 'Put this customer back on automatic sharing?', b); });
    });
  };

  Board.prototype.bar = function (focus) {
    var self = this, bar = this.el.querySelector('.lb-bar'), chosen = this.selected(), d = this.data;
    if (!bar) return;
    if (!chosen.length) { bar.classList.remove('on'); bar.innerHTML = ''; return; }
    var targets = (d.columns || []).filter(function (c) { return c.mark && c.up && c.use !== 'out'; });
    var pinned = chosen.filter(function (x) { return x[1].pinned; }).length;
    bar.innerHTML = '<span class="lb-count"><b>' + chosen.length + '</b> selected</span>' +
      (d.can_move ? '<span class="lb-to">Move to</span>' + targets.map(function (c) {
        return '<button type="button" class="btn btn-sm lb-target" data-to="' + esc(c.interface) + '" style="--c:' + COLORS[(c.color || 0) % 6] + '"><i class="bi bi-arrow-right-circle"></i> ' + esc(c.label) + '</button>';
      }).join('') : '<span class="lb-to text-warning">Moving is not available on this router</span>') +
      (pinned && d.can_move ? '<button type="button" class="btn btn-sm btn-outline-light" data-auto><i class="bi bi-arrow-counterclockwise"></i> Back to automatic</button>' : '') +
      '<button type="button" class="btn btn-sm btn-link text-light" data-clear>Clear</button>';
    bar.classList.add('on');
    [].forEach.call(bar.querySelectorAll('[data-to]'), function (b) { b.onclick = function () { self.moveTo(b.dataset.to); }; });
    var a = bar.querySelector('[data-auto]');
    if (a) a.onclick = function () {
      var devs = self.selected().filter(function (x) { return x[1].pinned; }).map(function (x) { return { ip: x[1].ip, mac: x[1].mac, code: x[1].code }; });
      self.send({ auto: true, devices: devs }, 'Put ' + devs.length + ' customer' + (devs.length > 1 ? 's' : '') + ' back on automatic sharing?', a);
    };
    bar.querySelector('[data-clear]').onclick = function () { self.sel = {}; self.render(self.data); };
    if (focus) { var t = bar.querySelector('[data-to]'); if (t) t.focus(); }
  };

  Board.prototype.moveTo = function (iface) {
    var self = this, d = this.data, col = (d.columns || []).filter(function (c) { return c.interface === iface; })[0];
    if (!col || !col.mark) return;
    var chosen = this.selected().filter(function (x) { return !x[0] || x[0].interface !== iface || x[1].how !== 'kept'; });
    if (!chosen.length) { toast('They are already kept on ' + col.label + '.', 'ok'); return; }
    var names = chosen.slice(0, 6).map(function (x) { return x[1].code || x[1].ip; }).join(', ') + (chosen.length > 6 ? ' and ' + (chosen.length - 6) + ' more' : '');
    var devs = chosen.map(function (x) { return { ip: x[1].ip, mac: x[1].mac, code: x[1].code, from: x[0] ? x[0].label : '' }; });
    this.send({ interface: iface, devices: devs },
      { title: 'Move ' + chosen.length + ' customer' + (chosen.length > 1 ? 's' : '') + ' to ' + col.label + '?', icon: 'bi-signpost-split',
        html: '<p><b>' + esc(names) + '</b></p><p class="small text-secondary mb-0">They stay on ' + esc(col.label) + ' until you put them back on automatic. ' +
          'Their open connections are closed so they switch at once — a video call may freeze for a second. If ' + esc(col.label) + ' fails, the other lines carry them.</p>', ok: 'Move them' },
      null, iface);
  };

  Board.prototype.send = function (payload, question, btn, iface) {
    var self = this;
    if (this.busy) return;
    var q = typeof question === 'string' ? { title: question, icon: 'bi-arrow-counterclockwise', ok: 'Yes' } : question;
    ask(q).then(function (yes) {
      if (!yes) return;
      self.busy = true;
      var keys = payload.devices.map(function (x) { return (x.mac || '') + '|' + x.ip; });
      keys.forEach(function (k) { var c = self.el.querySelector('.lb-chip[data-k="' + (window.CSS && CSS.escape ? CSS.escape(k) : k) + '"]'); if (c) c.classList.add('moving'); });
      if (btn) btn.disabled = true;
      Promise.resolve(self.opts.move(payload)).then(function (r) {
        self.busy = false;
        if (!r || !r.ok) { toast((r && r.message) || 'The move failed.', 'bad'); self.render(self.data); return; }
        toast(r.message, r.via_link ? 'wait' : 'ok');
        // show the result at once: move the chips into their new column, then let live data confirm it
        if (iface) {
          var data = JSON.parse(JSON.stringify(self.data)), target = data.columns.filter(function (c) { return c.interface === iface; })[0];
          var moving = {}; keys.forEach(function (k) { moving[k] = 1; });
          var lifted = [];
          data.columns.forEach(function (c) { c.people = c.people.filter(function (p) { if (moving[keyOf(p)]) { lifted.push(p); c.users--; return false; } return true; }); });
          data.unplaced = (data.unplaced || []).filter(function (p) { if (moving[keyOf(p)]) { lifted.push(p); return false; } return true; });
          lifted.forEach(function (p) { p.pinned = true; p.how = 'kept'; p.pin_confirmed = !r.via_link; target.people.unshift(p); target.users++; });
          var tot = data.columns.reduce(function (a, c) { return a + c.users; }, 0);
          data.columns.forEach(function (c) { c.real = tot ? Math.round(c.users * 100 / tot) : 0; });
          self.sel = {};
          self.render(data);
        } else { self.sel = {}; }
        if (self.opts.onDone) self.opts.onDone(r);
      }, function () { self.busy = false; toast('TapTap could not be reached.', 'bad'); self.render(self.data); });
    });
  };

  window.TTBoard = { mount: function (el, opts) { return new Board(el, opts); }, ask: ask, toast: toast, bps: bps, esc: esc, COLORS: COLORS };
})();
