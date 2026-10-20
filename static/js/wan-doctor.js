/* TapTap Internet Doctor card (Internet lines page) — logic lives in core/wan_doctor.py */
(function () {
  var $ = function (s) { return document.querySelector(s); };
  var urlsEl = $('#doctorUrls');
  if (!urlsEl) return;
  var U = JSON.parse(urlsEl.textContent);
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var D = { installed: null, viaLink: false, busy: false };

  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ''; }
  function req(url, body) {
    var opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(body || {}) };
    return fetch(url, opt).then(function (r) { return r.json().then(function (d) { d._ok = r.ok; return d; }, function () { return { _ok: false, message: 'Unexpected reply (' + r.status + ').' }; }); },
                                function () { return { _ok: false, message: 'TapTap could not be reached.' }; });
  }
  function msg(text, good) { $('#docResult').insertAdjacentHTML('afterbegin', '<div class="doc-msg ' + (good ? 'good' : 'bad') + '">' + esc(text) + '</div>'); }

  function renderState() {
    var el = $('#docState'), tg = $('#docToggle');
    if (D.viaLink) {
      el.innerHTML = '<span class="pill link">TapTap Link</span> ' + esc(U.router) + ' can’t be reached live right now. Download the Doctor script and paste it into the router’s terminal — it then works by itself.';
      tg.hidden = true; $('#docMacWrap').hidden = true; $('#docCheck').disabled = true; return;
    }
    if (D.installed === null) return;
    el.innerHTML = D.installed
      ? '<span class="pill on">On</span> Watching every line on ' + esc(U.router) + ' each minute.'
      : '<span class="pill off">Off</span> Not watching yet. Turn it on so a broken line is noticed and fixed without you.';
    tg.hidden = false;
    tg.className = 'btn btn-sm ' + (D.installed ? 'btn-outline-secondary' : 'btn-success');
    tg.innerHTML = D.installed ? '<i class="bi bi-power"></i> Turn off' : '<i class="bi bi-shield-check"></i> Turn on Internet Doctor';
    $('#docMacWrap').hidden = !!D.installed;
  }

  function renderRecent(rows) {
    var box = $('#docRecent');
    if (!rows || !rows.length) { box.innerHTML = ''; return; }
    box.innerHTML = '<div class="doc-recent"><h4>What the Doctor did lately</h4><ul class="list-unstyled mb-0">' +
      rows.map(function (r) { return '<li><b>' + esc(r.time) + '</b>' + esc(r.message) + '</li>'; }).join('') + '</ul></div>';
  }

  var FIX = { refresh: ['bi-arrow-repeat', 'Renew connection'], new_mac: ['bi-shuffle', 'Give the port a new MAC'], reset_mac: ['bi-arrow-counterclockwise', 'Put original MAC back'] };
  var LABEL = { ok: 'Working', holding: 'Modem answers, no internet', down: 'Modem not answering', off: 'Switched off', error: 'Could not check' };

  function renderCheck(d) {
    var html = '<div class="doc-head">' + esc(d.headline) + '</div>';
    (d.lines || []).forEach(function (l) {
      var acts = [];
      if (l.fix) acts.push(l.fix);
      if (l.mac_changed && acts.indexOf('reset_mac') < 0) acts.push('reset_mac');
      if (l.state === 'holding' && acts.indexOf('refresh') < 0) acts.push('refresh');
      var probes = (l.reached || []).length + (l.missed || []).length
        ? '<div class="probes">Internet test: ' + (l.reached || []).length + ' of 3 answered' + (l.gateway ? ' · modem ' + esc(l.gateway) : '') + (l.mac ? ' · MAC ' + esc(l.mac) : '') + '</div>' : '';
      html += '<div class="doc-line ' + esc(l.state) + '"><span class="dot"></span><div class="body">' +
        '<div class="t">' + esc(l.label) + ' <small>' + esc(l.interface) + ' · ' + esc(LABEL[l.state] || l.state) + '</small></div>' +
        '<p class="f">' + esc(l.finding) + '</p>' + probes +
        (acts.length ? '<div class="acts">' + acts.map(function (a, i) {
          return '<button class="btn btn-sm ' + (i === 0 ? 'btn-warning' : 'btn-light') + '" data-fix="' + a + '" data-if="' + esc(l.interface) + '"><i class="bi ' + FIX[a][0] + '"></i> ' + FIX[a][1] + '</button>';
        }).join('') + '</div>' : '') + '</div></div>';
    });
    $('#docResult').innerHTML = html;
  }

  function check() {
    if (D.busy) return; D.busy = true;
    var b = $('#docCheck'); b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Testing every line from ' + esc(U.router) + '…';
    req(U.check, {}).then(function (d) {
      D.busy = false; b.disabled = false; b.innerHTML = '<i class="bi bi-activity"></i> Check my lines now';
      if (!d._ok) { if (d.via_link) { D.viaLink = true; renderState(); } msg(d.message || 'The check failed.'); return; }
      D.installed = d.installed; renderState(); renderCheck(d); renderRecent(d.recent);
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
      msg(on ? 'Internet Doctor is on. It checks ' + (d.lines || []).join(', ') + ' every minute, on the router itself.' : 'Internet Doctor is off.', true);
    });
  });

  $('#docResult').addEventListener('click', function (e) {
    var b = e.target.closest('[data-fix]');
    if (!b) return;
    var step = b.getAttribute('data-fix'), iface = b.getAttribute('data-if');
    if (step === 'new_mac' && !confirm('Give ' + iface + ' a new MAC address? The modem will see the router as a new device. Customers on this line pause for about 20 seconds.')) return;
    b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Working…';
    req(U.fix, { interface: iface, step: step }).then(function (d) {
      if (!d._ok) { b.disabled = false; b.textContent = 'Try again'; msg(d.message || 'The fix failed.'); return; }
      msg(d.message + ' Checking again in 25 seconds…', true);
      setTimeout(check, 25000);
    });
  });

  req(U.status).then(function (d) {
    if (!d._ok) { $('#docState').textContent = d.message || 'Could not read the Doctor’s state.'; return; }
    D.viaLink = !!d.via_link; D.installed = d.installed;
    $('#docSteps').innerHTML = (d.escalation || []).map(function (s) { return '<li>After ' + s[0] + ' minutes: ' + esc(s[1]) + '</li>'; }).join('') +
      '<li>From 3 minutes, if another line works, customers are moved off the broken line — and moved back after two good checks.</li>' +
      '<li>A line counts as working only when 2 of 3 independent internet addresses answer through it.</li>';
    renderState(); renderRecent(d.recent);
  });
})();
