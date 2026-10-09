/* TapTap — Clean router memory (core/router_cleanup.py). Scan → plan → clean, with the storage ring telling the story. */
(function () {
  var btn = document.getElementById('cleanBtn'), modalEl = document.getElementById('cleanModal');
  if (!btn || !modalEl || !window.bootstrap) return;
  var U = btn.dataset, LINK = U.via === 'link';
  var csrf = (document.querySelector('#csrf-holder [name=csrfmiddlewaretoken]') || document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  var modal = new bootstrap.Modal(modalEl);
  var $ = function (id) { return document.getElementById(id); };
  var hero = $('rcHero'), body = $('rcBody'), foot = $('rcFoot'), go = $('rcGo'), cleanBtn = $('rcClean');
  var state = { scan: null, plan: null, busy: false, before: null };

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function size(b) {
    b = +b || 0; if (b < 1024) return b + ' B';
    var u = ['KB', 'MB', 'GB'], i = -1; do { b /= 1024; i++; } while (b >= 1024 && i < 2);
    return (b >= 100 ? b.toFixed(0) : b >= 10 ? b.toFixed(1) : b.toFixed(2)) + ' ' + u[i];
  }
  function age(d) { return d == null ? '' : d < 1 ? 'today' : d === 1 ? '1 day old' : d < 60 ? d + ' days old' : Math.round(d / 30) + ' months old'; }
  function post(url, data) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: JSON.stringify(data || {}) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || !j.ok) throw new Error(j.message || 'TapTap could not do that.'); return j; }); });
  }
  function wait(job, onWait) {
    if (job.status !== 'waiting') return Promise.resolve(job);
    var t0 = Date.now();
    return new Promise(function (resolve, reject) {
      (function poll() {
        setTimeout(function () {
          fetch(U.job.replace(/0\/$/, job.id + '/'), { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (j) {
            if (j.job.status === 'waiting' && Date.now() - t0 < 450000) { onWait && onWait(Math.round((Date.now() - t0) / 1000)); poll(); }
            else resolve(j.job);
          }).catch(function () { Date.now() - t0 < 450000 ? poll() : reject(new Error('Lost contact with TapTap.')); });
        }, 2000);
      })();
    });
  }
  function done(job) { if (job.status === 'failed') throw new Error((job.result || {}).error || 'The router could not do it.'); return job; }

  // ── the ring and the numbers ──
  function ring(usedPct, ghostPct) {
    var u = $('rcUsed'), g = $('rcGhost');
    if (usedPct == null) { u.setAttribute('stroke-dasharray', '0 100'); $('rcPct').textContent = '—'; return; }
    u.setAttribute('stroke-dasharray', Math.max(0.5, usedPct).toFixed(1) + ' 100');
    u.classList.toggle('hot', usedPct >= 85);
    // the slice that will be freed: from "after" up to "now", drawn over the used arc
    var slice = ghostPct == null ? 0 : Math.max(0, usedPct - ghostPct);
    g.setAttribute('stroke-dasharray', slice < 0.3 ? '0 100' : '0 ' + ghostPct.toFixed(1) + ' ' + slice.toFixed(1) + ' 100');
    count($('rcPct'), usedPct, function (v) { return Math.round(v) + '%'; });
  }
  function count(el, to, fmt, ms) {
    var from = parseFloat(el.dataset.v || '0') || 0, t0 = performance.now(); ms = ms || 1200; el.dataset.v = to;
    (function step(t) { var k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 3); el.textContent = fmt(from + (to - from) * e); if (k < 1) requestAnimationFrame(step); })(t0);
  }
  function stats(free, total, ram, ramTotal, log) {
    if (total) { $('rcFree').textContent = size(free); $('rcTotal').textContent = 'of ' + size(total); }
    if (ramTotal) { $('rcRam').textContent = size(ram); $('rcRamTotal').textContent = 'of ' + size(ramTotal); }
    if (log != null) $('rcLog').textContent = log;
  }
  function phase(p, title, text) { hero.dataset.phase = p; if (title) $('rcTitle').textContent = title; if (text != null) $('rcText').textContent = text; }

  // ── scan ──
  function scan() {
    if (state.busy) return; state.busy = true; go.disabled = true; foot.hidden = true;
    phase('scan', 'Scanning the router…', LINK ? 'Asked through TapTap Link — the router answers at its next check-in (usually under 30 seconds).' : 'Reading storage, memory and every file…');
    body.innerHTML = '<div class="rc-scan"><div></div><div></div><div></div><div></div></div>';
    post(U.scan).then(function (j) {
      var h = j.health || {};
      if (h.total) { stats(h.free, h.total, h.free_mem, h.total_mem); ring((h.total - h.free) * 100 / h.total); }
      return wait(j.job, function (s) { $('rcText').textContent = 'Waiting for the router (TapTap Link)… ' + s + ' s'; });
    }).then(done).then(function (job) {
      state.scan = job; state.plan = job.result.plan; drawPlan();
    }).catch(function (e) {
      phase('idle', 'The scan did not finish', e.message); body.innerHTML = '<div class="rc-err">' + esc(e.message) + '</div>';
    }).then(function () { state.busy = false; go.disabled = false; go.querySelector('span').textContent = 'Scan again'; });
  }

  // ── plan ──
  function drawPlan() {
    var p = state.plan, st = p.storage;
    stats(st.free, st.total, p.ram.free, p.ram.total, p.log.lines);
    state.before = { free: st.free, total: st.total, ram: p.ram.free };
    var html = '';
    p.groups.forEach(function (g, gi) {
      var removable = g.level !== 'kept';
      html += '<div class="rc-group ' + g.level + '" data-g="' + gi + '" style="animation-delay:' + (gi * 60) + 'ms">'
        + '<div class="rc-gh"><input class="form-check-input" type="checkbox" data-gall="' + gi + '" ' + (g.level === 'safe' ? 'checked' : '') + (removable ? '' : ' disabled') + ' aria-label="All in ' + esc(g.label) + '">'
        + '<span class="ic"><i class="bi ' + esc(g.icon) + '"></i></span><div><b>' + esc(g.label) + ' <small class="d-inline">· ' + g.count + ' file' + (g.count === 1 ? '' : 's') + '</small></b><small>' + esc(g.why) + '</small></div>'
        + '<span class="rc-chip ' + g.level + '">' + (g.level === 'safe' ? 'Safe to remove' : g.level === 'check' ? 'Check first' : 'Kept') + '</span><span class="rc-size">' + size(g.bytes) + '</span></div>'
        + '<ul class="rc-files">' + g.files.map(function (f, fi) {
          return '<li><input class="form-check-input" type="checkbox" data-f="' + gi + ':' + fi + '" ' + (f.selected ? 'checked' : '') + (removable ? '' : ' disabled') + '>'
            + '<code title="' + esc(f.name) + '">' + esc(f.name) + '</code><span class="age">' + age(f.age) + '</span><span class="sz">' + size(f.size) + '</span></li>';
        }).join('') + (g.count > g.files.length ? '<li><span></span><small class="text-secondary">and ' + (g.count - g.files.length) + ' more (the largest are shown)</small></li>' : '') + '</ul></div>';
    });
    html += '<div class="rc-group rc-logcard ' + (p.log.lines ? 'safe' : 'kept') + '"><div class="rc-gh"><input class="form-check-input" type="checkbox" id="rcLogChk" ' + (p.log.suggest ? 'checked' : '') + (p.log.lines ? '' : ' disabled') + '>'
      + '<span class="ic"><i class="bi bi-journal-x"></i></span><div><b>Clear the memory log <small class="d-inline">· ' + p.log.lines + ' lines</small></b><small>Frees a little RAM. The router keeps logging new events right away.</small></div>'
      + '<span class="rc-chip safe">Safe</span><span class="rc-size">RAM</span></div></div>';
    if (p.protected && p.protected.count) html += '<p class="small text-secondary mt-2"><i class="bi bi-shield-check"></i> ' + p.protected.count + ' files in hotspot, skins and other system folders (' + size(p.protected.bytes) + ') are never touched.</p>';
    if (p.truncated) html += '<p class="small text-warning">The router has a lot of files — the first 400 were checked. Scan again after cleaning to see the rest.</p>';
    body.innerHTML = html;
    foot.hidden = false;
    body.querySelectorAll('.rc-gh').forEach(function (h) { h.addEventListener('click', function (e) { if (e.target.tagName !== 'INPUT') h.parentNode.classList.toggle('open'); }); });
    body.querySelectorAll('[data-gall]').forEach(function (c) { c.addEventListener('change', function () {
      body.querySelectorAll('[data-f^="' + c.dataset.gall + ':"]').forEach(function (x) { x.checked = c.checked; }); update(); }); });
    body.querySelectorAll('[data-f], #rcLogChk').forEach(function (c) { c.addEventListener('change', update); });
    update();                                    // also draws the ring with the slice to be freed
    var safe = p.safe_bytes;
    phase('plan', safe ? size(safe) + ' can be freed safely' : 'Your router is already tidy',
      safe ? 'Ticked items are safe. Open a group to see every file; “Check first” groups are never ticked for you.' : 'Nothing obvious to remove. You can still look through the groups below.');
  }
  function chosen() {
    var files = [], bytes = 0;
    body.querySelectorAll('[data-f]:checked').forEach(function (c) {
      var a = c.dataset.f.split(':'), f = state.plan.groups[+a[0]].files[+a[1]]; files.push(f.name); bytes += f.size;
    });
    return { files: files, bytes: bytes, log: !!($('rcLogChk') || {}).checked };
  }
  function update() {
    var c = chosen(), st = state.plan.storage;
    $('rcSel').textContent = size(c.bytes);
    $('rcSelTxt').textContent = c.files.length + ' file' + (c.files.length === 1 ? '' : 's') + (c.log ? ' + memory log' : '') + ' selected';
    cleanBtn.disabled = !c.files.length && !c.log;
    cleanBtn.querySelector('span').textContent = c.files.length ? 'Clean ' + size(c.bytes) : (c.log ? 'Clear the log' : 'Clean');
    if (st.total) {
      var used = (st.total - st.free) * 100 / st.total, after = Math.max(0, (st.total - st.free - c.bytes) * 100 / st.total);
      ring(used, after);
      $('rcPctLbl').textContent = c.bytes ? 'used → ' + Math.round(after) + '% after' : 'storage used';
    }
    body.querySelectorAll('[data-gall]').forEach(function (g) {
      var all = body.querySelectorAll('[data-f^="' + g.dataset.gall + ':"]'), on = body.querySelectorAll('[data-f^="' + g.dataset.gall + ':"]:checked');
      g.checked = all.length && on.length === all.length; g.indeterminate = on.length > 0 && on.length < all.length;
    });
  }

  // ── clean ──
  function clean() {
    var c = chosen(); if (state.busy || (!c.files.length && !c.log)) return;
    if (body.querySelector('.rc-group.check [data-f]:checked') && !confirm('You ticked files marked “Check first”. Remove them anyway?')) return;
    state.busy = true; cleanBtn.disabled = true; go.disabled = true; foot.hidden = true;
    phase('clean', 'Cleaning…', LINK ? 'The router removes the files at its next check-in — keep this window open.' : 'Removing the files from the router…');
    body.innerHTML = '<div class="rc-vac"><div class="rc-stream" id="rcStream">' + c.files.slice(0, 80).map(function (n) { return '<span class="rc-fchip">' + esc(n) + '</span>'; }).join('')
      + (c.files.length > 80 ? '<span class="rc-fchip">+' + (c.files.length - 80) + ' more</span>' : '') + (c.log ? '<span class="rc-fchip">memory log</span>' : '') + '</div>'
      + '<div><div class="rc-bin"><i class="bi bi-trash3"></i></div><div class="rc-counter"><b id="rcFreed">0 B</b><small>freed</small></div></div></div><p class="rc-wait" id="rcWait"></p>';
    var chips = [].slice.call(document.querySelectorAll('#rcStream .rc-fchip')), bin = document.querySelector('.rc-bin').getBoundingClientRect();
    var est = c.bytes, shown = 0, flying = true, total = state.before.total, usedBefore = total - state.before.free;
    // chips fly into the bin while the router works; the ring drains in step
    (function fly(i) {
      if (!flying || i >= chips.length) return;
      var r = chips[i].getBoundingClientRect(); chips[i].style.setProperty('--dx', (bin.left - r.left + 20) + 'px'); chips[i].classList.add('go');
      shown = est * (i + 1) / Math.max(1, chips.length) * 0.9; $('rcFreed').textContent = size(shown);
      if (total) ring((usedBefore - shown) * 100 / total);
      setTimeout(function () { fly(i + 1); }, Math.max(60, 1600 / Math.max(1, chips.length)));
    })(0);
    var freedTotal = 0, removed = 0, failed = 0, logCleared = null, last = null;
    function batch(req) {
      return post(U.clean, req).then(function (j) {
        return wait(j.job, function (s) { $('rcWait').textContent = 'Waiting for the router (TapTap Link)… ' + s + ' s' + (j.left ? ' · ' + j.left + ' more files after this batch' : ''); })
          .then(done).then(function (job) {
            var r = job.result; last = r; freedTotal += r.freed || 0; removed += r.removed_count || 0; failed += r.failed_count || 0;
            if (r.log_cleared != null) logCleared = r.log_cleared;
            return fetch(U.job.replace(/0\/$/, job.id + '/'), { credentials: 'same-origin' }).then(function (x) { return x.json(); })
              .then(function (jj) { return jj.left ? batch({ 'continue': job.id }) : null; });
          });
      });
    }
    batch({ scan: state.scan.id, files: c.files, clear_log: c.log }).then(function () {
      flying = false; finish({ freed: freedTotal, removed: removed, failed: failed, log: logCleared, after: last });
    }).catch(function (e) {
      flying = false; phase('idle', 'Cleaning stopped', e.message); body.innerHTML = '<div class="rc-err">' + esc(e.message) + '</div>';
    }).then(function () { state.busy = false; go.disabled = false; });
  }
  function finish(res) {
    var a = res.after || {}, b = state.before, saved = a.free_hdd && b.free ? Math.max(0, a.free_hdd - b.free) : res.freed;
    saved = Math.max(saved, res.freed || 0);
    if (a.total_hdd) { stats(a.free_hdd, a.total_hdd, a.free_mem, null, res.log ? 0 : null); ring((a.total_hdd - a.free_hdd) * 100 / a.total_hdd, null); }
    $('rcPctLbl').textContent = 'storage used';
    phase('done', 'Done — ' + size(saved) + ' freed', res.removed + ' file' + (res.removed === 1 ? '' : 's') + ' removed' + (res.failed ? ', ' + res.failed + ' could not be removed' : '') + (res.log ? ' · memory log cleared' : '') + '.');
    body.innerHTML = '<div class="rc-done"><div class="big" id="rcBig">0 B</div><div class="sub">freed on the router</div><div class="rc-ba">'
      + '<div><small>Free storage</small><b>' + size(b.free) + ' → ' + size(a.free_hdd || b.free + saved) + '</b> <em>+' + size(saved) + '</em></div>'
      + '<div><small>Storage used</small><b>' + (b.total ? Math.round((b.total - b.free) * 100 / b.total) : '—') + '% → ' + (a.total_hdd ? Math.round((a.total_hdd - a.free_hdd) * 100 / a.total_hdd) : '—') + '%</b></div>'
      + '<div><small>Free RAM</small><b>' + size(b.ram) + ' → ' + size(a.free_mem || b.ram) + '</b>' + (a.free_mem > b.ram ? ' <em>+' + size(a.free_mem - b.ram) + '</em>' : '') + '</div></div></div>';
    var big = $('rcBig'); count(big, saved, size, 1400);
    var box = body.querySelector('.rc-done'), colors = ['#22c55e', '#60a5fa', '#fbbf24', '#a78bfa', '#f472b6'];
    for (var i = 0; i < 36; i++) {
      var s = document.createElement('i'); s.className = 'rc-spark'; var ang = Math.random() * Math.PI * 2, dist = 120 + Math.random() * 220;
      s.style.cssText = 'background:' + colors[i % colors.length] + ';--x:' + Math.cos(ang) * dist + 'px;--y:' + Math.sin(ang) * dist * 0.6 + 'px;animation-delay:' + (Math.random() * 0.25) + 's';
      box.appendChild(s);
    }
    go.querySelector('span').textContent = 'Scan again';
  }

  btn.addEventListener('click', function () { modal.show(); if (!state.scan && !state.busy) scan(); });
  go.addEventListener('click', scan);
  cleanBtn.addEventListener('click', clean);
})();
