/* Control Center › Router clock: see the MikroTik's time and correct it (core/router_clock.py).
   Over TapTap Link the router answers at its next check-in, so a request is polled until it arrives. */
(function () {
  'use strict';
  var box = document.getElementById('routerClock');
  if (!box) return;
  var $ = function (id) { return document.getElementById(id); };
  var csrf = (document.querySelector('#csrf-holder [name=csrfmiddlewaretoken]') || document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  var state = null, readAt = 0, poller = null, pollUntil = 0, zonesLoaded = false;

  function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function toast(msg, kind) {
    if (typeof window.showToast === 'function') { window.showToast(msg, kind || 'info'); return; }
    var n = $('rclkNote'); n.hidden = false; n.className = 'rclk-note' + (kind === 'danger' ? ' danger' : ''); n.textContent = msg;
  }
  function req(url, data) {
    var opt = { credentials: 'same-origin', headers: { 'X-CSRFToken': csrf } };
    if (data !== undefined) { opt.method = 'POST'; opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(data); }
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || j.success === false) throw new Error(j.message || ('Request failed (' + r.status + ')'));
        return j;
      });
    });
  }
  // "2026-10-15 14:30:05" → Date (treated as a plain wall clock; only differences matter)
  function wall(s) { if (!s) return null; var m = s.match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/); return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null; }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function hms(d) { return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }
  function day(d) { return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }); }
  function human(sec) {
    var a = Math.abs(sec), s = sec > 0 ? 'ahead' : 'behind';
    if (a < 60) return a + ' s ' + s;
    if (a < 3600) return Math.round(a / 60) + ' min ' + s;
    if (a < 172800) return (a / 3600).toFixed(1).replace('.0', '') + ' h ' + s;
    return Math.round(a / 86400) + ' days ' + s;
  }

  function tick() {
    if (!state || !readAt) return;
    var gone = Date.now() - readAt, r = wall(state.router_local), c = wall(state.correct_local);
    if (r) { r = new Date(r.getTime() + gone); $('rclkRouter').textContent = hms(r); $('rclkRouterDate').textContent = day(r); }
    if (c) { c = new Date(c.getTime() + gone); $('rclkCorrect').textContent = hms(c); $('rclkCorrectDate').textContent = day(c); }
  }

  function render(j) {
    var c = j.clock || {};
    state = c; readAt = Date.now();
    // Age of the reading by TapTap's clock (this computer's clock may be wrong too).
    if (c.at && j.server_utc) readAt -= Math.max(0, new Date(j.server_utc).getTime() - new Date(c.at).getTime());
    var v = $('rclkVerdict'), face = $('rclkRouterFace');
    var verdicts = {
      ok: ['bi-check-circle-fill', 'Correct'], drift: ['bi-exclamation-triangle-fill', 'Drifting'],
      wrong: ['bi-x-octagon-fill', 'Wrong'], unknown: ['bi-question-circle', 'Not read yet']
    };
    var key = c.pending ? 'pending' : (c.verdict || 'unknown');
    var icon = c.pending ? ['bi-arrow-repeat', 'Waiting for the router…'] : (verdicts[key] || verdicts.unknown);
    var label = icon[1];
    if (!c.pending && c.drift_seconds != null && c.verdict !== 'ok') label += '<small>' + esc(human(c.drift_seconds)) + '</small>';
    v.className = 'rclk-verdict ' + key;
    v.innerHTML = '<i class="bi ' + icon[0] + '"></i><span>' + label + '</span>';
    face.classList.toggle('wrong', c.verdict === 'wrong');
    if (!c.router_local) { $('rclkRouter').textContent = '--:--:--'; $('rclkRouterDate').textContent = c.pending ? 'Asked through ' + (j.via || 'TapTap Link') : 'Not read yet'; }
    $('rclkZoneLabel').textContent = c.tz ? '(' + c.tz + ')' : (c.offset ? '(GMT ' + c.offset + ')' : '');
    var meta = [];
    if (c.tz) meta.push('<span>Zone: ' + esc(c.tz) + (c.offset ? ' · GMT ' + esc(c.offset) : '') + '</span>');
    if (c.router_local) meta.push('<span class="' + (c.ntp_enabled ? 'good' : 'bad') + '">NTP ' + (c.ntp_enabled ? 'on' : 'off') + (c.ntp_status ? ' · ' + esc(c.ntp_status) : '') + '</span>');
    if (c.ntp_servers && c.ntp_servers.length) meta.push('<span>' + esc(c.ntp_servers.join(', ')) + '</span>');
    if (c.at) meta.push('<span>Read ' + esc(new Date(c.at).toLocaleTimeString()) + ' via ' + esc(c.via || j.via || '') + '</span>');
    if (c.corrected_at) meta.push('<span class="good">Corrected ' + esc(new Date(c.corrected_at).toLocaleTimeString()) + '</span>');
    $('rclkMeta').innerHTML = meta.join('');
    var note = $('rclkNote');
    if (c.error) { note.hidden = false; note.className = 'rclk-note danger'; note.textContent = c.error; }
    else if (c.router_local && !c.ntp_enabled) { note.hidden = false; note.className = 'rclk-note'; note.textContent = 'NTP is off: the clock will be wrong again after the next power cut. "Set correct time" turns it on.'; }
    else note.hidden = true;
    if (c.tz && !$('rclkTz').value) $('rclkTz').value = c.tz;
    if (j.manual_command) $('rclkCmd').textContent = j.manual_command;
    if (j.zones && !zonesLoaded) {
      zonesLoaded = true;
      $('rclkZones').innerHTML = j.zones.map(function (z) { return '<option value="' + esc(z) + '">'; }).join('');
    }
    if (c.pending) startPoll(); else stopPoll();
    tick();
  }

  function startPoll() {
    if (poller) return;
    pollUntil = Date.now() + 3 * 60 * 1000;
    poller = setInterval(function () {
      if (Date.now() > pollUntil) { stopPoll(); toast('The router has not answered yet. It applies Link commands at its next check-in.', 'warning'); return; }
      req(box.dataset.status).then(render).catch(function () {});
    }, 3000);
  }
  function stopPoll() { if (poller) { clearInterval(poller); poller = null; } }

  function busy(btn, on) { if (btn) btn.disabled = on; }

  function read() {
    var b = $('rclkRead'); busy(b, true);
    req(box.dataset.read, {}).then(render).catch(function (e) { toast(e.message, 'danger'); }).then(function () { busy(b, false); });
  }
  function correct(extra) {
    var data = Object.assign({ ntp: $('rclkNtp').checked }, extra || {});
    var b = $('rclkFix'), a = $('rclkApply'); busy(b, true); busy(a, true);
    req(box.dataset.set, data).then(function (j) {
      render(j);
      toast(j.clock && j.clock.pending ? 'Sent through TapTap Link — the router sets its clock at its next check-in.' : 'Router clock corrected.', 'success');
    }).catch(function (e) { toast(e.message, 'danger'); }).then(function () { busy(b, false); busy(a, false); });
  }

  $('rclkRead').addEventListener('click', read);
  $('rclkFix').addEventListener('click', function () { correct({}); });
  $('rclkApply').addEventListener('click', function () {
    var tz = $('rclkTz').value.trim(), manual = $('rclkManual').value;
    if (manual && !confirm('Set ' + manual.replace('T', ' ') + ' as the router\'s local time?' + ($('rclkNtp').checked ? '\nNTP is on and may change it back to the real time.' : ''))) return;
    correct({ tz: tz && tz !== (state && state.tz) ? tz : '', manual: manual });
  });
  setInterval(tick, 1000);
  req(box.dataset.status + '?zones=1').then(render).catch(function () {});
})();
