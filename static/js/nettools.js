/* TapTap network tools: run tests on the router and draw the answers (templates/core/network_tools.html). */
(function () {
  var root = document.getElementById('nt'); if (!root) return;
  var RUN = root.dataset.run, TEST = root.dataset.test, ROUTER = root.dataset.router, VIA = root.dataset.via;
  var csrf = (root.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  var $ = function (id) { return document.getElementById(id); };
  var out = $('ntOut'), msg = $('ntMsg'), target = $('ntTarget'), runBtn = $('ntRun'), hist = $('ntHist');
  var tool = 'ping', busy = false, keep = null, keepTimes = [], keepStarted = 0;
  var remembered = { ping: '8.8.8.8', trace: '8.8.8.8', dns: 'google.com', web: 'google.com' };
  var LABEL = { ping: 'Address to ping', trace: 'Trace the path to', dns: 'Name to look up', web: 'Website' };
  var CHIPS = { ping: ['8.8.8.8', '1.1.1.1', 'google.com'], trace: ['8.8.8.8', '1.1.1.1', 'facebook.com'],
                dns: ['google.com', 'facebook.com', 'whatsapp.net', 'youtube.com'], web: ['google.com', 'facebook.com', 'youtube.com', 'web.whatsapp.com'] };
  var ICON = { mypath: 'bi-bezier', whoami: 'bi-crosshair', hops: 'bi-diagram-2', doctor: 'bi-heart-pulse', ping: 'bi-broadcast', trace: 'bi-bezier2', dns: 'bi-signpost-split', web: 'bi-window', speed: 'bi-speedometer2' };
  var devices = [].map.call(document.querySelectorAll('#ntSuggest option'), function (o) { return o.textContent && /^\d/.test(o.value) && !/^(8\.8\.8\.8|1\.1\.1\.1)$/.test(o.value) ? o.value : null; }).filter(Boolean).slice(0, 3);
  CHIPS.ping = CHIPS.ping.concat(devices);

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function ms(v) { return v == null ? '—' : (v < 10 ? v.toFixed(1) : Math.round(v)) + ' ms'; }

  // ── tools / tabs ──
  function show(t) {
    tool = t; stopKeep();
    document.querySelectorAll('.nt-tabs button').forEach(function (b) { b.classList.toggle('on', b.dataset.tool === t); b.setAttribute('aria-selected', b.dataset.tool === t); });
    document.querySelectorAll('#ntForm [data-for]').forEach(function (el) { el.hidden = el.dataset.for.split(' ').indexOf(t) < 0; });
    if (LABEL[t]) { $('ntTargetLabel').textContent = LABEL[t]; target.value = remembered[t] || ''; target.placeholder = t === 'web' ? 'google.com or https://example.com/page' : '8.8.8.8 or google.com'; }
    $('ntChips').innerHTML = (CHIPS[t] || []).map(function (c) { return '<button type="button" data-v="' + esc(c) + '">' + esc(c) + '</button>'; }).join('');
    runBtn.querySelector('span').textContent = t === 'speed' ? 'Start speed test' : 'Run';
    msg.textContent = '';
  }
  document.querySelectorAll('.nt-tabs button').forEach(function (b) { b.addEventListener('click', function () { show(b.dataset.tool); }); });
  $('ntChips').addEventListener('click', function (e) { var v = e.target.dataset && e.target.dataset.v; if (v) { target.value = v; remembered[tool] = v; target.focus(); } });
  target.addEventListener('input', function () { remembered[tool] = target.value; });

  // ── talking to TapTap ──
  function post(data) {
    return fetch(RUN, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: JSON.stringify(data) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || !j.ok) throw new Error(j.message || 'The test could not start.'); return j.test; }); });
  }
  function waitFor(t, onWait) {
    if (t.status !== 'waiting') return Promise.resolve(t);
    var started = Date.now();
    return new Promise(function (resolve, reject) {
      (function poll() {
        setTimeout(function () {
          fetch(TEST.replace(/0\/$/, t.id + '/'), { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (j) {
            if (j.test.status === 'waiting' && Date.now() - started < 400000) { onWait && onWait(Math.round((Date.now() - started) / 1000)); poll(); }
            else resolve(j.test);
          }).catch(function () { Date.now() - started < 400000 ? poll() : reject(new Error('Lost contact with TapTap.')); });
        }, 2000);
      })();
    });
  }
  function run(kind, data) {
    data.router = ROUTER; data.kind = kind;
    return post(data).then(function (t) { addHist(t); return t; });
  }

  // ── the Internet check (hero) ──
  var hero = $('ntHero'), doc = $('ntDoctor');
  doc.addEventListener('click', function () {
    if (doc.disabled) return;
    doc.disabled = true; hero.dataset.state = 'running';
    document.querySelectorAll('#ntPath li').forEach(function (li) { li.className = ''; });
    $('ntFixes').hidden = true;
    $('ntVerdict').textContent = 'Checking…';
    $('ntVerdictText').textContent = VIA === 'TapTap Link' ? 'Asked the router through TapTap Link — it runs the check at its next check-in (usually under 30 seconds).' : 'The router is pinging, looking up names and loading a test page. About 10 seconds.';
    run('doctor', {}).then(function (t) {
      return waitFor(t, function (s) { $('ntVerdictText').textContent = 'Waiting for the router… ' + s + ' s'; });
    }).then(function (t) { drawDoctor(t); updHist(t); }).catch(function (e) {
      hero.dataset.state = 'bad'; $('ntVerdict').textContent = 'The check could not run'; $('ntVerdictText').textContent = e.message;
    }).then(function () { doc.disabled = false; doc.querySelector('span').textContent = 'Check again'; });
  });
  function drawDoctor(t, quiet) {
    var r = t.result || {};
    if (t.status === 'failed') { hero.dataset.state = 'bad'; $('ntVerdict').textContent = 'The check could not run'; $('ntVerdictText').textContent = r.error || ''; return; }
    hero.dataset.state = r.level || 'idle';
    $('ntVerdict').textContent = r.title || '';
    $('ntVerdictText').textContent = r.detail || '';
    var broken = false;
    (r.chain || []).forEach(function (c) {
      var li = document.querySelector('#ntPath li[data-k="' + c.key + '"]'); if (!li) return;
      li.className = broken && c.state !== 'ok' ? 'skip' : (c.state === 'unknown' ? '' : c.state);
      if (c.state === 'bad') broken = true;
      if (c.detail && c.key !== 'router') li.querySelector('small').textContent = c.detail;
    });
    if (r.fixes && r.fixes.length) {
      $('ntFixes').innerHTML = '<b>' + (r.level === 'ok' ? 'Good to know' : 'What to do') + '</b><ul>' + r.fixes.map(function (f) { return '<li>' + esc(f) + '</li>'; }).join('') + '</ul>';
      $('ntFixes').hidden = false;
    }
    if (!quiet) hero.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  // ── tools ──
  $('ntForm').addEventListener('submit', function (e) {
    e.preventDefault(); if (busy) return;
    var data = {};
    if (tool !== 'speed') data.target = target.value.trim();
    if (tool === 'ping') { data.count = +$('ntCount').value; data.size = +$('ntSize').value; }
    var k = $('ntKeep');
    if (tool === 'ping' && k && k.checked) { startKeep(data); return; }
    go(tool, data);
  });
  function go(kind, data) {
    busy = true; runBtn.disabled = true; msg.textContent = '';
    out.innerHTML = '<div class="nt-wait"><span class="spinner-border spinner-border-sm"></span> ' + waitText(kind) + '</div>';
    return run(kind, data).then(function (t) {
      return waitFor(t, function (s) { out.querySelector('.nt-wait').lastChild.textContent = ' Waiting for the router (TapTap Link)… ' + s + ' s'; });
    }).then(function (t) { draw(t); updHist(t); return t; }).catch(function (e) {
      msg.textContent = e.message; out.innerHTML = '<div class="nt-hint">' + esc(e.message) + '</div>';
    }).then(function (t) { busy = false; runBtn.disabled = false; return t; });
  }
  function waitText(kind) {
    if (VIA === 'TapTap Link') return ' Sent to the router through TapTap Link — waiting for its answer…';
    return { ping: ' Pinging from the router…', trace: ' Tracing the path hop by hop (up to 20 seconds)…', dns: ' Looking up the name…',
             web: ' Loading the page from the router…', speed: ' Downloading through your Internet line — about 10–40 seconds…' }[kind] || ' Running…';
  }

  // Keep pinging: batches of 10 pings, one rolling chart (Direct API / TapTap Tunnel only), stops after 5 minutes.
  function startKeep(data) {
    stopKeep(); keepTimes = []; keepStarted = Date.now(); data.count = 10;
    runBtn.querySelector('span').textContent = 'Stop';
    keep = true;
    (function loop() {
      if (!keep) return;
      busy = true;
      run('ping', Object.assign({}, data)).then(function (t) {
        if (!keep) return;
        if (t.status === 'failed') throw new Error((t.result || {}).error || 'Ping failed');
        keepTimes = keepTimes.concat((t.result || {}).times || []).slice(-60);
        var st = stats(keepTimes); st.host = t.result.host;
        drawPing(st, t, 'Keep pinging · last ' + keepTimes.length + ' replies');
        if (Date.now() - keepStarted > 300000) { stopKeep(); msg.textContent = 'Stopped after 5 minutes.'; return; }
        setTimeout(loop, 600);
      }).catch(function (e) { msg.textContent = e.message; stopKeep(); });
    })();
    runBtn.onclick = function (e) { if (keep) { e.preventDefault(); stopKeep(); } };
  }
  function stopKeep() { keep = null; busy = false; runBtn.disabled = false; runBtn.onclick = null; if (runBtn.querySelector('span')) runBtn.querySelector('span').textContent = tool === 'speed' ? 'Start speed test' : 'Run'; }
  function stats(times) {
    var got = times.filter(function (x) { return x != null; }), s = { sent: times.length, received: got.length, times: times };
    s.loss = times.length ? Math.round((times.length - got.length) * 100 / times.length) : 100;
    s.min = got.length ? Math.min.apply(null, got) : null; s.max = got.length ? Math.max.apply(null, got) : null;
    s.avg = got.length ? got.reduce(function (a, b) { return a + b; }, 0) / got.length : null;
    s.jitter = got.length > 1 ? got.slice(1).reduce(function (a, b, i) { return a + Math.abs(b - got[i]); }, 0) / (got.length - 1) : null;
    s.quality = !got.length ? 'down' : (s.loss >= 20 || s.avg >= 400) ? 'poor' : (s.loss >= 5 || s.avg >= 150 || s.jitter >= 60) ? 'fair' : (s.avg >= 60 || s.jitter >= 20) ? 'good' : 'excellent';
    s.quality_note = { down: 'No replies at all.', poor: 'Customers will notice: pages hang, calls drop.', fair: 'Browsing works; video calls may stutter.', good: 'Fine for browsing, video and calls.', excellent: 'Fast and steady.' }[s.quality];
    return s;
  }

  // ── drawing results ──
  function head(t, badge, cls) {
    return '<div class="nt-head"><div><b>' + esc(t.kind_label) + (t.target && t.kind !== 'speed' && t.kind !== 'doctor' ? ' · <span style="font-family:var(--mono)">' + esc(t.target) + '</span>' : '') + '</b> '
      + (badge ? '<span class="nt-badge ' + esc(cls || badge) + '">' + esc(badge) + '</span>' : '') + '</div><small>' + esc(t.router) + ' · ' + esc(t.via) + ' · ' + esc(t.at) + (t.secs ? ' · ' + t.secs + ' s' : '') + '</small></div>';
  }
  function draw(t) {
    if (t.kind === 'doctor') { drawDoctor(t); out.innerHTML = '<div class="nt-hint">Internet check result is shown at the top of the page.</div>'; return; }
    if (t.status === 'failed') { out.innerHTML = head(t, 'failed', 'bad') + '<p class="nt-note">' + esc((t.result || {}).error || 'The test failed.') + '</p>'; return; }
    ({ ping: function () { drawPing(t.result, t); }, trace: drawTrace, dns: drawDns, web: drawWeb, speed: drawSpeed })[t.kind](t);
  }
  function drawPing(r, t, title) {
    var times = r.times || [], got = times.filter(function (x) { return typeof x === 'number'; }), peak = Math.max.apply(null, got.concat([20]));
    var bars = times.map(function (v, i) {
      if (v == null) return '<div class="lost" title="#' + (i + 1) + ' lost"></div>';
      if (v === 'ok') return '<div class="answered" title="#' + (i + 1) + ': answered (no time reported)"></div>';
      return '<div class="' + (v > 150 ? 'slow' : '') + '" style="height:' + Math.max(4, Math.round(v * 100 / peak)) + '%" title="#' + (i + 1) + ': ' + ms(v) + '">' + (times.length <= 20 ? '<span>' + Math.round(v) + '</span>' : '') + '</div>';
    }).join('');
    out.innerHTML = head(Object.assign({}, t, title ? { kind_label: title } : {}), r.quality, r.quality)
      + '<div class="nt-tiles">' + [['Replies', r.received + '/' + r.sent], ['Lost', r.loss + '%'], ['Fastest', ms(r.min)], ['Average', ms(r.avg)], ['Slowest', ms(r.max)], ['Jitter', ms(r.jitter)]]
        .map(function (x) { return '<div class="nt-tile"><b>' + esc(x[1]) + '</b><small>' + x[0] + '</small></div>'; }).join('') + '</div>'
      + '<div class="nt-bars" aria-label="Reply times">' + bars + '</div>'
      + '<p class="nt-note">' + esc(r.quality_note || '') + (r.host && r.host !== t.target ? ' ' + esc(t.target) + ' is ' + esc(r.host) + '.' : '') + '</p>';
  }
  function drawTrace(t) {
    var r = t.result, peak = Math.max.apply(null, (r.hops || []).map(function (h) { return h.ms || 0; }).concat([50]));
    var zone = { local: 'your network', isp: 'ISP network', internet: '', silent: 'no answer' };
    out.innerHTML = head(t, r.reached ? 'reached' : 'stopped', r.reached ? 'ok' : 'warn')
      + '<ol class="nt-hops">' + (r.hops || []).map(function (h) {
        return '<li class="' + h.zone + (h.ms > 150 ? ' slow' : '') + '"><span class="n">' + h.n + '</span><span class="a">' + (h.address ? esc(h.address) : '* * *') + (zone[h.zone] ? '<small>' + zone[h.zone] + '</small>' : '') + '</span>'
          + '<span class="ms">' + (h.ms == null ? '—' : ms(h.ms)) + '</span><span class="bar"><i style="width:' + (h.ms ? Math.max(2, Math.round(h.ms * 100 / peak)) : 0) + '%"></i></span></li>';
      }).join('') + '</ol><p class="nt-note">' + esc(r.note || '') + '</p>';
  }
  function drawDns(t) {
    var r = t.result;
    out.innerHTML = head(t, r.ok ? 'found' : 'not found', r.ok ? 'ok' : 'bad')
      + '<div class="nt-dns"><span>' + esc(r.name) + '</span><span class="arrow">→</span>' + (r.ok ? '<b>' + esc(r.ip) + '</b>' : '<span class="nt-badge bad">no answer</span>') + '</div>'
      + '<dl class="nt-kv"><dt>DNS servers set</dt><dd>' + esc((r.servers || []).join(', ') || 'none') + '</dd><dt>From the ISP</dt><dd>' + esc((r.dynamic || []).join(', ') || 'none') + '</dd>'
      + (r.ms != null ? '<dt>Time</dt><dd>' + ms(r.ms) + '</dd>' : '') + '</dl><p class="nt-note">' + esc(r.note || '') + '</p>';
  }
  function drawWeb(t) {
    var r = t.result;
    out.innerHTML = head(t, r.ok ? 'loads' : (r.http ? 'error ' + r.http : 'not reachable'), r.ok ? ((r.ms || 0) > 3000 ? 'warn' : 'ok') : 'bad')
      + '<dl class="nt-kv"><dt>Address</dt><dd>' + esc(r.url) + '</dd>' + (r.ms != null ? '<dt>Time</dt><dd>' + ms(r.ms) + '</dd>' : (r.time_text ? '<dt>Time</dt><dd>' + esc(r.time_text) + '</dd>' : ''))
      + (r.bytes ? '<dt>Size</dt><dd>' + (r.bytes / 1024).toFixed(1) + ' KB</dd>' : '') + (r.error ? '<dt>Router said</dt><dd>' + esc(r.error) + '</dd>' : '') + '</dl>'
      + '<p class="nt-note">' + esc(r.note || '') + '</p>';
  }
  function drawSpeed(t) {
    var r = t.result;
    if (!r.ok) { out.innerHTML = head(t, 'did not finish', 'bad') + '<p class="nt-note">' + esc(r.note || '') + (r.error ? ' (' + esc(r.error) + ')' : '') + '</p>'; return; }
    var f = Math.min(1, Math.log10(r.mbps + 1) / Math.log10(201)), ang = -90 + 180 * f;
    var ticks = [1, 5, 20, 50, 100, 200].map(function (v) { var a = (-90 + 180 * Math.log10(v + 1) / Math.log10(201)) * Math.PI / 180; return '<text x="' + (120 + 92 * Math.sin(a)).toFixed(1) + '" y="' + (128 - 92 * Math.cos(a)).toFixed(1) + '" font-size="9" text-anchor="middle" fill="currentColor" opacity=".55">' + v + '</text>'; }).join('');
    out.innerHTML = head(t, r.rating, r.rating === 'poor' ? 'bad' : r.rating === 'fair' ? 'warn' : 'ok')
      + '<div class="nt-gauge"><svg viewBox="0 0 240 140" role="img" aria-label="' + r.mbps + ' megabits per second">'
      + '<path d="M20 128 A100 100 0 0 1 220 128" fill="none" stroke="#e6edf5" stroke-width="16" stroke-linecap="round"/>'
      + '<path id="ntArc" d="M20 128 A100 100 0 0 1 220 128" fill="none" stroke="url(#ntG)" stroke-width="16" stroke-linecap="round" pathLength="100" stroke-dasharray="0 100"/>'
      + '<defs><linearGradient id="ntG"><stop offset="0" stop-color="#e04848"/><stop offset=".35" stop-color="#f59e0b"/><stop offset=".7" stop-color="#18a66a"/></linearGradient></defs>' + ticks
      + '<g id="ntNeedle" style="transform-origin:120px 128px;transform:rotate(-90deg);transition:transform 1.2s cubic-bezier(.2,.9,.3,1.2)"><line x1="120" y1="128" x2="120" y2="44" stroke="currentColor" stroke-width="3" stroke-linecap="round"/><circle cx="120" cy="128" r="7" fill="currentColor"/></g></svg>'
      + '<div><div class="big">' + r.mbps + '</div><div class="unit">Mbit/s download</div><dl class="nt-kv"><dt>Data used</dt><dd>' + r.mb_used + ' MB</dd><dt>Took</dt><dd>' + r.seconds + ' s</dd></dl></div></div>'
      + '<p class="nt-note">' + esc(r.note) + ' This measures the line from the router to the Internet; Wi-Fi to customers can be slower.</p>';
    requestAnimationFrame(function () { setTimeout(function () { var n = $('ntNeedle'), a = $('ntArc'); if (n) n.style.transform = 'rotate(' + ang + 'deg)'; if (a) { a.style.transition = 'stroke-dasharray 1.2s ease-out'; a.setAttribute('stroke-dasharray', (f * 100).toFixed(1) + ' 100'); } }, 30); });
  }

  // ── history ──
  var items = [];
  try { items = JSON.parse(document.getElementById('ntHistory').textContent) || []; } catch (e) {}
  function row(t) {
    return '<button type="button" class="nt-h ' + esc(t.status) + '" data-id="' + t.id + '"><span class="ic"><i class="bi ' + (ICON[t.kind] || 'bi-activity') + '"></i></span>'
      + '<span style="min-width:0"><b>' + esc(t.kind_label) + (t.kind !== 'doctor' && t.kind !== 'speed' ? ' · ' + esc(t.target) : '') + '</b><small>' + esc(t.summary) + ' · ' + esc(t.router) + '</small></span><em>' + esc(t.at) + '</em></button>';
  }
  function paint() { hist.innerHTML = items.length ? items.map(row).join('') : '<p class="text-secondary small m-0">No tests yet. Results are kept here so you can compare later.</p>'; }
  function addHist(t) { items = [t].concat(items.filter(function (x) { return x.id !== t.id; })).slice(0, 40); paint(); }
  function updHist(t) { if (!t) return; items = items.map(function (x) { return x.id === t.id ? t : x; }); paint(); }
  hist.addEventListener('click', function (e) {
    var b = e.target.closest('.nt-h'); if (!b) return;
    var t = items.find(function (x) { return String(x.id) === b.dataset.id; }); if (!t) return;
    stopKeep();
    if (t.kind === 'mypath' || t.kind === 'whoami' || t.kind === 'hops') { mode('mine'); document.dispatchEvent(new CustomEvent('nt:show', { detail: t })); return; }
    if (t.kind === 'doctor') { mode('router'); drawDoctor(t); return; }
    if (t.kind !== tool) show(t.kind);
    if (t.target && LABEL[t.kind]) { target.value = t.target; remembered[t.kind] = t.target; }
    if (t.status === 'waiting') { out.innerHTML = head(t, 'waiting', 'warn') + '<p class="nt-note">Still waiting for the router.</p>'; return; }
    draw(t);
  });

  // Modes: "From this device" (mypath.js) and "From the router" (the Internet check above).
  function mode(m) {
    document.querySelectorAll('.nt-modes button').forEach(function (b) { b.classList.toggle('on', b.dataset.mode === m); b.setAttribute('aria-selected', b.dataset.mode === m); });
    var mp = $('mp'); if (mp) mp.hidden = m !== 'mine';
    hero.hidden = m !== 'router';
    try { sessionStorage.setItem('ntMode', m); } catch (e) {}
  }
  document.querySelectorAll('.nt-modes button').forEach(function (b) { b.addEventListener('click', function () { mode(b.dataset.mode); }); });
  var startMode = 'mine'; try { startMode = sessionStorage.getItem('ntMode') || 'mine'; } catch (e) {}
  mode(startMode);
  document.addEventListener('nt:test', function (e) { var t = e.detail; if (items.some(function (x) { return x.id === t.id; })) updHist(t); else addHist(t); });
  window.ntItems = function () { return items; };

  show('ping'); paint();
  var lastDoc = items.find(function (x) { return x.kind === 'doctor' && x.status === 'done' && String(x.router_id) === String(ROUTER); });
  if (lastDoc) { drawDoctor(lastDoc, true); $('ntVerdictText').textContent = (lastDoc.result.detail || '') + ' (Last checked ' + lastDoc.at + '.)'; doc.querySelector('span').textContent = 'Check again'; }
})();
