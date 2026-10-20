/* TapTap Internet Doctor card (Internet lines page) — logic lives in core/wan_doctor.py.
   Live routers: real tests from the router. TapTap Link routers: the last sync, and every action is a Link command. */
(function () {
  var $ = function (s) { return document.querySelector(s); };
  var urlsEl = $('#doctorUrls');
  if (!urlsEl) return;
  var U = JSON.parse(urlsEl.textContent);
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var D = { installed: null, viaLink: false, busy: false, link: null, timers: [] };

  var FIX = { refresh: ['bi-arrow-repeat', 'Renew connection'], new_mac: ['bi-shuffle', 'Give the port a new MAC'], reset_mac: ['bi-arrow-counterclockwise', 'Put original MAC back'],
    turn_on: ['bi-power', 'Switch on'], turn_off: ['bi-power', 'Switch off'], use: ['bi-people', 'Put in use'], unuse: ['bi-sign-stop', 'Take out of use'] };
  var USE = { managed: ['on', 'Customers use it'], plain: ['plain', 'Customers use it (emergency route)'], out: ['out', 'Customers don’t use it'] };
  var LABEL = { ok: 'Working', connected: 'Connected', holding: 'Modem answers, no internet', down: 'Modem not answering', off: 'Switched off', error: 'Could not check' };
  var WAIT = { turn_on: 12000, turn_off: 3000, use: 4000, unuse: 3000 };
  var ASK = { turn_off: 'Switch off {l}? It stops getting an address from its modem until you switch it on again.',
              unuse: 'Take {l} out of use? Customers move to the other line(s). It stays connected so you can test it.',
              new_mac: 'Give {l} a new MAC address? The modem will see the router as a new device. Customers on this line pause for about 20 seconds.' };

  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ''; }
  function req(url, body) {
    var opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(body || {}) };
    return fetch(url, opt).then(function (r) { return r.json().then(function (d) { d._ok = r.ok; return d; }, function () { return { _ok: false, message: 'Unexpected reply (' + r.status + ').' }; }); },
                                function () { return { _ok: false, message: 'TapTap could not be reached.' }; });
  }
  function msg(text, good) { $('#docMsgs').insertAdjacentHTML('afterbegin', '<div class="doc-msg ' + (good ? 'good' : 'bad') + '">' + esc(text) + '</div>'); }
  function later(fn, ms) { D.timers.push(setTimeout(fn, ms)); }
  function clearLater() { D.timers.forEach(clearTimeout); D.timers = []; }

  function renderState() {
    var el = $('#docState'), tg = $('#docToggle'), ck = $('#docCheck');
    if (D.installed === undefined) return;
    var via = D.viaLink ? '<span class="pill link">Through TapTap Link</span> ' : '';
    var doc = D.installed === true ? '<span class="pill on">Doctor on</span>' : D.installed === false ? '<span class="pill off">Doctor off</span>' : '<span class="pill off">Doctor: not known</span>';
    var linkTxt = D.viaLink && D.link ? ' Last report from the router: ' + esc(D.link.synced) + '.' + (D.link.pending ? ' ' + D.link.pending + ' Doctor action(s) waiting for the router.' : '') +
      ' Buttons are sent to the router and done at its next check-in.' : '';
    el.innerHTML = via + doc + ' ' + (D.installed ? 'Watching every line on ' + esc(U.router) + ' each minute.' : 'Turn it on so a broken line is noticed and fixed without you.') + linkTxt;
    tg.hidden = false;
    tg.className = 'btn btn-sm ' + (D.installed ? 'btn-outline-secondary' : 'btn-success');
    tg.innerHTML = D.installed ? '<i class="bi bi-power"></i> Turn off' : '<i class="bi bi-shield-check"></i> Turn on Internet Doctor';
    $('#docMacWrap').hidden = !!D.installed;
    ck.disabled = false;
    ck.innerHTML = D.viaLink ? '<i class="bi bi-arrow-clockwise"></i> Refresh from router' : '<i class="bi bi-activity"></i> Check my lines now';
  }

  function renderRecent(rows) {
    var box = $('#docRecent');
    if (!rows || !rows.length) { box.innerHTML = ''; return; }
    box.innerHTML = '<div class="doc-recent"><h4>What the Doctor did lately</h4><ul class="list-unstyled mb-0">' +
      rows.map(function (r) { return '<li><b>' + esc(r.time) + '</b>' + esc(r.message) + '</li>'; }).join('') + '</ul></div>';
  }

  function actionsFor(l) {
    var acts = [], use = l.use || { state: 'out' }, add = function (a) { if (acts.indexOf(a) < 0) acts.push(a); };
    if (l.state === 'off') { if (l.switchable) add('turn_on'); return acts; }
    if ((l.state === 'ok' || l.state === 'connected') && use.state !== 'managed') add('use');
    if (l.fix) add(l.fix);
    (l.extra_fixes || []).forEach(add);
    if (l.mac_changed) add('reset_mac');
    if (l.state === 'holding') add('refresh');
    if (use.state !== 'out') add('unuse');
    if (l.switchable) add('turn_off');
    return acts;
  }

  function renderLines(headline, lines) {
    var html = headline ? '<div class="doc-head">' + esc(headline) + '</div>' : '';
    (lines || []).forEach(function (l) {
      var use = l.use || { state: 'out' }, acts = actionsFor(l);
      var tested = (l.reached || []).length + (l.missed || []).length;
      var info = [];
      if (tested) info.push('Internet test: ' + (l.reached || []).length + ' of 3 answered');
      if (l.gateway) info.push('modem ' + esc(l.gateway));
      if (l.mac) info.push('MAC ' + esc(l.mac) + (l.mac_changed ? ' (changed)' : ''));
      html += '<div class="doc-line ' + esc(l.state) + '"><span class="dot"></span><div class="body">' +
        '<div class="t">' + esc(l.label) + ' <small>' + esc(l.interface) + ' · ' + esc(LABEL[l.state] || l.state) + '</small>' +
          (l.state !== 'off' ? ' <span class="use ' + USE[use.state][0] + '">' + USE[use.state][1] + '</span>' : '') + '</div>' +
        '<p class="f">' + esc(l.finding) + '</p>' + (info.length ? '<div class="probes">' + info.join(' · ') + '</div>' : '') +
        (acts.length ? '<div class="acts">' + acts.map(function (a, i) {
          var main = i === 0 && a !== 'unuse' && a !== 'turn_off';
          return '<button class="btn btn-sm ' + (main ? (a === 'use' || a === 'turn_on' ? 'btn-success' : 'btn-warning') : 'btn-light') + '" data-fix="' + a + '" data-if="' + esc(l.interface) +
            '" data-label="' + esc(l.label) + '"><i class="bi ' + FIX[a][0] + '"></i> ' + FIX[a][1] + '</button>';
        }).join('') + '</div>' : '') + '</div></div>';
    });
    if (!(lines || []).length) html += '<p class="small text-secondary mt-2">No internet lines found on ' + esc(U.router) + ' yet.</p>';
    $('#docResult').innerHTML = html;
  }

  function linkHeadline(lines) {
    var idle = (lines || []).filter(function (l) { return l.state === 'connected' && (l.use || {}).state === 'out'; });
    if (idle.length) return idle.map(function (l) { return l.label; }).join(', ') + ' is connected but customers don’t use it — press “Put in use”.';
    return 'What the router reported at its last check-in.';
  }

  function applyStatus(d) {
    D.viaLink = !!d.via_link; D.installed = d.installed; D.link = d.link || null;
    renderState();
    if (D.viaLink) renderLines(linkHeadline(d.lines_state), d.lines_state);
    renderRecent(d.recent);
  }

  function loadStatus() {
    return req(U.status).then(function (d) {
      if (!d._ok) { $('#docState').textContent = d.message || 'Could not read the Doctor’s state.'; return d; }
      $('#docSteps').innerHTML = (d.escalation || []).map(function (s) { return '<li>After ' + s[0] + ' minutes: ' + esc(s[1]) + '</li>'; }).join('') +
        '<li>From 3 minutes, if another line works, customers are moved off the broken line — and moved back after two good checks.</li>' +
        '<li>A line counts as working only when 2 of 3 independent internet addresses answer through it.</li>';
      applyStatus(d);
      return d;
    });
  }

  function refreshSoon() { clearLater(); later(loadStatus, 20000); later(loadStatus, 45000); later(loadStatus, 90000); }

  function check() {
    if (D.busy) return; D.busy = true;
    var b = $('#docCheck'); b.disabled = true;
    b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> ' + (D.viaLink ? 'Asking the router…' : 'Testing every line from ' + esc(U.router) + '…');
    req(U.check, {}).then(function (d) {
      D.busy = false;
      if (!d._ok) { renderState(); msg(d.message || 'The check failed.'); return; }
      if (d.via_link) { applyStatus(d); if (d.message) msg(d.message, true); refreshSoon(); return; }
      D.viaLink = false; D.installed = d.installed; renderState(); renderLines(d.headline, d.lines); renderRecent(d.recent);
    });
  }

  $('#docCheck').addEventListener('click', check);

  $('#docToggle').addEventListener('click', function () {
    var on = !D.installed, b = this;
    if (!on && !confirm('Turn off the Internet Doctor on ' + U.router + '? Any line it took out of use is put back.')) return;
    b.disabled = true;
    req(on ? U.install : U.remove, on ? { allow_mac: $('#docMac').checked } : {}).then(function (d) {
      b.disabled = false;
      if (!d._ok) { msg(d.message || 'That did not work.'); return; }
      D.installed = d.installed; renderState();
      if (d.via_link) { msg((on ? 'Internet Doctor is being installed. ' : 'Internet Doctor is being removed. ') + d.message, true); refreshSoon(); return; }
      msg(on ? 'Internet Doctor is on. It checks ' + (d.lines || []).join(', ') + ' every minute, on the router itself.' : 'Internet Doctor is off.', true);
    });
  });

  $('#docResult').addEventListener('click', function (e) {
    var b = e.target.closest('[data-fix]');
    if (!b) return;
    var step = b.getAttribute('data-fix'), iface = b.getAttribute('data-if'), label = b.getAttribute('data-label') || iface;
    if (ASK[step] && !confirm(ASK[step].replace('{l}', label + ' (' + iface + ')'))) return;
    var html = b.innerHTML;
    b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> ' + (D.viaLink ? 'Sending…' : 'Working…');
    req(U.fix, { interface: iface, step: step }).then(function (d) {
      if (!d._ok) { b.disabled = false; b.innerHTML = html; msg(d.message || 'That did not work.'); return; }
      if (d.via_link) { b.innerHTML = '<i class="bi bi-hourglass-split"></i> Sent'; msg(d.message, true); refreshSoon(); return; }
      var wait = WAIT[step] || 25000;
      msg(d.message + ' Checking again in ' + Math.round(wait / 1000) + ' seconds…', true);
      later(check, wait);
    });
  });

  loadStatus();
})();
