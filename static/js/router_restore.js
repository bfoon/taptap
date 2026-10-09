/* TapTap — Restore a router (core/router_restore.py): choose → backup → confirm → the journey and the restart. */
(function () {
  var el = document.getElementById('restoreModal'); if (!el || !window.bootstrap) return;
  var U = el.dataset, LINK = U.via === 'link', ROUTER = U.router;
  var csrf = (document.querySelector('#csrf-holder [name=csrfmiddlewaretoken]') || document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
  var modal = new bootstrap.Modal(el);
  var $ = function (id) { return document.getElementById(id); };
  var hero = $('rsHero'), body = $('rsBody'), cur = null, poller = null, busy = false;

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function size(b) { b = +b || 0; if (b < 1024) return b + ' B'; var u = ['KB', 'MB', 'GB'], i = -1; do { b /= 1024; i++; } while (b >= 1024 && i < 2); return (b >= 100 ? b.toFixed(0) : b.toFixed(1)) + ' ' + u[i]; }
  function when(iso) { try { return new Date(iso).toLocaleString(); } catch (e) { return iso; } }
  function post(url, data) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf }, body: JSON.stringify(data || {}) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok || !j.ok) throw new Error(j.message || 'TapTap could not do that.'); return j; }); });
  }
  function getJ(url) { return fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }); }
  function step(s) {
    var order = ['source', 'pick', 'review', 'run'], at = order.indexOf(s);
    hero.dataset.step = s;
    document.querySelectorAll('#rsSteps li').forEach(function (li) { var i = order.indexOf(li.dataset.s); li.className = i < at ? 'ok' : i === at ? 'on' : ''; });
  }
  function phase(p) { hero.dataset.phase = p || 'idle'; }
  function head(title, text) { $('rsTitle').textContent = title; if (text != null) $('rsText').textContent = text; }
  var FROM = { upload: ['bi-laptop', 'My computer'], server: ['bi-cloud-check', 'TapTap server'], router: ['bi-hdd', 'On the router'] };
  function from(src) { var f = FROM[src] || ['bi-archive', 'Backup']; $('rsFrom').querySelector('i').className = 'bi ' + f[0]; $('rsFromLbl').textContent = f[1]; hero.dataset.local = src === 'router' ? 'yes' : 'no'; }

  // ── 1. choose the source ──
  function sources() {
    stop(); cur = null; step('source'); phase('idle'); from(null);
    head('Restore ' + ROUTER, 'Bring back a saved configuration — from your computer, from the TapTap server, or from a backup already on the router.');
    body.innerHTML = '<div class="rs-sources">'
      + card('upload', 'bi-laptop', 'From my computer', 'A .backup or .rsc you saved (for example with “Save to my computer” in Clean router memory).', 0)
      + card('server', 'bi-cloud-check', 'From the TapTap server', 'Any backup TapTap stored safely for this business.', 1)
      + card('router', 'bi-hdd', 'Already on the router', 'A backup file in the router’s own storage — nothing to send.', 2) + '</div>'
      + (LINK ? '<div class="mt-3" id="rsHelperBox"></div>' : '');
    body.querySelectorAll('[data-src]').forEach(function (b) { b.addEventListener('click', function () { pick(b.dataset.src); }); });
    if (LINK) helperBox($('rsHelperBox'), false);
  }
  function card(src, icon, title, text, i) {
    return '<button type="button" class="rs-src ' + src + '" data-src="' + src + '" style="animation-delay:' + (i * 70) + 'ms"><i class="bi ' + icon + '"></i><b>' + title + '</b><small>' + text + '</small></button>';
  }
  function helperBox(box, open) {
    getJ(U.helper).then(function (j) {
      if (!j.ok) return;
      box.innerHTML = '<details class="rs-helper alert ' + (open ? 'alert-warning' : 'alert-light border') + ' small mb-0" ' + (open ? 'open' : '') + '>'
        + '<summary class="fw-bold"><i class="bi bi-key"></i> Allow restores on this router (TapTap Link, once)</summary>'
        + '<p class="mt-2 mb-2">Loading a backup needs more rights than TapTap Link has. Paste this once into <b>WinBox → New Terminal</b>: it adds one small script that only loads the backup you choose here.</p>'
        + '<pre>' + esc(j.command) + '</pre><button type="button" class="btn btn-sm btn-dark" id="rsCopyHelper"><i class="bi bi-clipboard"></i> Copy</button></details>';
      $('rsCopyHelper').onclick = function () { navigator.clipboard.writeText(j.command).then(function () { $('rsCopyHelper').innerHTML = '<i class="bi bi-check2"></i> Copied'; }); };
    });
  }

  // ── 2. pick the backup ──
  function pick(src, preselect) {
    step('pick'); from(src); phase('idle');
    var back = '<button type="button" class="rs-back" id="rsBack"><i class="bi bi-arrow-left"></i> Choose another source</button>';
    if (src === 'upload') {
      head('From your computer', 'Drop the file here. TapTap keeps it privately until the router has it.');
      body.innerHTML = back + '<label class="rs-drop" id="rsDrop"><input type="file" accept=".backup,.rsc" hidden id="rsFile"><i class="bi bi-cloud-arrow-up"></i>'
        + '<b>Drop a .backup or .rsc here, or click to choose</b><small>Up to 64 MB</small></label><div class="rs-up" id="rsUp" hidden><i></i></div><p class="small text-secondary mt-2" id="rsUpTxt"></p>';
      var drop = $('rsDrop'), input = $('rsFile');
      ['dragenter', 'dragover'].forEach(function (e) { drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.add('over'); }); });
      ['dragleave', 'drop'].forEach(function (e) { drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.remove('over'); }); });
      drop.addEventListener('drop', function (ev) { if (ev.dataTransfer.files[0]) upload(ev.dataTransfer.files[0]); });
      input.addEventListener('change', function () { if (input.files[0]) upload(input.files[0]); });
    } else if (src === 'server') {
      head('From the TapTap server', 'Backups stored safely on TapTap. A .backup restores everything; a .rsc only the settings.');
      body.innerHTML = back + '<div class="rs-skel"><div></div><div></div><div></div></div>';
      getJ(U.backups).then(function (j) {
        var items = (j.items || []).filter(function (b) { return b.saved_on_server; });
        if (!items.length) { body.innerHTML = back + '<div class="rs-err">No backup of this router is stored on the TapTap server yet. Press <b>Back up now</b> first, or restore from your computer.</div>'; return wire(); }
        body.innerHTML = back + '<div class="rs-list">' + items.map(function (b, i) {
          return '<div class="rs-row" style="animation-delay:' + (i * 40) + 'ms"><i class="bi bi-cloud-check"></i><div style="min-width:0"><b>' + esc(b.name) + '</b><small>' + when(b.at) + (b.version ? ' · RouterOS ' + esc(b.version) : '') + (b.automatic ? ' · nightly' : '') + '</small></div>'
            + '<div class="btns"><button type="button" class="btn btn-sm btn-dark" data-b="' + b.id + '" data-k="backup">.backup · ' + size(b.server_backup_size) + '</button>'
            + (b.server_export_size ? '<button type="button" class="btn btn-sm btn-outline-secondary" data-b="' + b.id + '" data-k="rsc">.rsc · ' + size(b.server_export_size) + '</button>' : '') + '</div></div>';
        }).join('') + '</div>';
        body.querySelectorAll('[data-b]').forEach(function (b) { b.addEventListener('click', function () { prepare({ source: 'server', backup: +b.dataset.b, kind: b.dataset.k }); }); });
        if (preselect) { var p = body.querySelector('[data-b="' + preselect + '"][data-k="backup"]'); if (p) p.click(); }
        wire();
      }).catch(function (e) { body.innerHTML = back + '<div class="rs-err">' + esc(e.message) + '</div>'; wire(); });
    } else {
      head('Already on the router', 'Looking for backups in ' + ROUTER + '’s storage…');
      body.innerHTML = back + '<div class="rs-skel"><div></div><div></div><div></div></div>';
      post(U.scan).then(function (j) { return waitJob(j.job); }).then(function (job) {
        var files = [];
        ((job.result || {}).plan || {}).groups.forEach(function (g) { g.files.forEach(function (f) { if (/\.(backup|rsc)$/i.test(f.name) && f.name.indexOf('/') < 0) files.push(f); }); });
        files.sort(function (a, b) { return (b.time || '').localeCompare(a.time || ''); });
        head('Already on the router', files.length ? 'Choose the backup to load. Nothing needs to be sent.' : 'No backup files were found on the router.');
        body.innerHTML = back + (files.length ? '<div class="rs-list">' + files.map(function (f, i) {
          return '<div class="rs-row" style="animation-delay:' + (i * 40) + 'ms"><i class="bi bi-hdd"></i><div style="min-width:0"><b>' + esc(f.name) + '</b><small>' + size(f.size) + (f.age != null ? ' · ' + (f.age < 1 ? 'today' : f.age + ' days old') : '') + '</small></div>'
            + '<div class="btns"><button type="button" class="btn btn-sm btn-dark" data-n="' + esc(f.name) + '" data-z="' + f.size + '">Use this</button></div></div>';
        }).join('') + '</div>' : '<div class="rs-err">No .backup or .rsc files are on the router.</div>');
        body.querySelectorAll('[data-n]').forEach(function (b) { b.addEventListener('click', function () { prepare({ source: 'router', name: b.dataset.n, size: +b.dataset.z }); }); });
        wire();
      }).catch(function (e) { body.innerHTML = back + '<div class="rs-err">' + esc(e.message) + '</div>'; wire(); });
    }
    wire();
    function wire() { var b = $('rsBack'); if (b) b.onclick = sources; }
  }
  function waitJob(job) {
    if (job.status !== 'waiting') return Promise.resolve(job);
    return new Promise(function (resolve, reject) {
      var t0 = Date.now();
      (function poll() { setTimeout(function () {
        getJ(U.job.replace(/0\/$/, job.id + '/')).then(function (j) {
          if (j.job.status === 'waiting' && Date.now() - t0 < 420000) { $('rsText').textContent = 'Waiting for the router (TapTap Link)… ' + Math.round((Date.now() - t0) / 1000) + ' s'; poll(); }
          else if (j.job.status === 'failed') reject(new Error((j.job.result || {}).error || 'The router did not answer.'));
          else resolve(j.job);
        }).catch(function () { poll(); });
      }, 2000); })();
    });
  }
  function upload(file) {
    if (!/\.(backup|rsc)$/i.test(file.name)) { $('rsUpTxt').textContent = 'Only .backup or .rsc files can be restored.'; return; }
    var bar = $('rsUp'), txt = $('rsUpTxt'); bar.hidden = false; phase('sending'); from('upload');
    var fd = new FormData(); fd.append('file', file);
    var xhr = new XMLHttpRequest(); xhr.open('POST', U.upload); xhr.setRequestHeader('X-CSRFToken', csrf);
    xhr.upload.onprogress = function (e) { if (e.lengthComputable) { bar.firstChild.style.width = (e.loaded * 100 / e.total) + '%'; txt.textContent = 'Uploading to TapTap… ' + Math.round(e.loaded * 100 / e.total) + '% (' + size(e.loaded) + ' of ' + size(e.total) + ')'; } };
    xhr.onload = function () {
      phase('idle'); var j = {}; try { j = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status !== 200 || !j.ok) { txt.textContent = j.message || 'The upload failed.'; return; }
      review(j.restore, j.warnings);
    };
    xhr.onerror = function () { phase('idle'); txt.textContent = 'The upload failed — check your connection.'; };
    xhr.send(fd);
  }
  function prepare(data) {
    body.querySelectorAll('button').forEach(function (b) { b.disabled = true; });
    post(U.prepare, data).then(function (j) { review(j.restore, j.warnings); }).catch(function (e) { alert(e.message); body.querySelectorAll('button').forEach(function (b) { b.disabled = false; }); });
  }

  // ── 3. review and confirm ──
  function review(r, warnings) {
    cur = r; step('review'); from(r.source); phase('idle');
    head('Restore ' + ROUTER + ' from ' + r.file_name, 'Check the details. ' + ROUTER + ' restarts with this configuration.');
    var bad = (warnings || []).some(function (w) { return w[0] === 'bad'; });
    body.innerHTML = '<button type="button" class="rs-back" id="rsBack"><i class="bi bi-arrow-left"></i> Choose another backup</button><div class="rs-review">'
      + '<div class="rs-card"><h4>The backup</h4><div class="rs-file-card"><i class="bi ' + (r.kind === 'rsc' ? 'bi-file-earmark-code' : 'bi-file-earmark-zip') + '"></i><div><b>' + esc(r.file_name) + '</b>'
      + '<small class="text-secondary">' + esc(FROM[r.source][1]) + (r.size ? ' · ' + size(r.size) : '') + '</small><br><span class="rs-kind ' + r.kind + '">' + (r.kind === 'rsc' ? 'Export · settings only' : 'Full backup · everything') + '</span></div></div>'
      + '<ul class="rs-warn">' + (warnings || []).map(function (w) { return '<li class="' + esc(w[0]) + '">' + esc(w[1]) + '</li>'; }).join('') + '</ul></div>'
      + '<div class="rs-card rs-confirm"><h4>Confirm</h4>'
      + '<div class="form-check form-switch mb-2"><input class="form-check-input" type="checkbox" id="rsSafe" checked><label class="form-check-label fw-normal" for="rsSafe">First save the current configuration on the router as <code>taptap-before-restore.backup</code></label></div>'
      + (r.kind === 'backup' ? '<details class="mb-2"><summary class="small text-secondary">The backup has a password?</summary><input class="form-control form-control-sm mt-1" id="rsPass" type="password" autocomplete="off" placeholder="Backup password"></details>' : '')
      + '<label for="rsConfirm">Type <b>' + esc(ROUTER) + '</b> to confirm</label><input class="form-control mb-2" id="rsConfirm" autocomplete="off" spellcheck="false">'
      + '<button type="button" class="btn btn-danger rs-go" id="rsGo" disabled><i class="bi bi-arrow-counterclockwise"></i> Restore ' + esc(ROUTER) + '</button>'
      + (bad ? '<p class="small text-danger mt-2 mb-0">Fix the problem above first.</p>' : '')
      + '<div id="rsHelperBox2" class="mt-2"></div></div></div>';
    $('rsBack').onclick = function () { pick(r.source); };
    var inp = $('rsConfirm'), go = $('rsGo');
    inp.addEventListener('input', function () { go.disabled = bad || inp.value.trim().toLowerCase() !== ROUTER.trim().toLowerCase(); });
    go.addEventListener('click', function () { run(r); });
    if (LINK) helperBox($('rsHelperBox2'), false);
    setTimeout(function () { inp.focus(); }, 200);
  }

  // ── 4. run: the journey, the restart, the return ──
  function run(r) {
    if (busy) return; busy = true;
    var go = $('rsGo'); go.disabled = true; go.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Starting…';
    post(U.start.replace(/0\/start\/$/, r.id + '/start/'), { confirm: $('rsConfirm').value, safety: $('rsSafe').checked, password: ($('rsPass') || {}).value || '' })
      .then(function (j) { busy = false; step('run'); show(j.restore); poll(j.restore.id); })
      .catch(function (e) { busy = false; go.disabled = false; go.innerHTML = '<i class="bi bi-arrow-counterclockwise"></i> Restore ' + esc(ROUTER); alert(e.message); });
  }
  function poll(id) {
    stop();
    poller = setInterval(function () {
      getJ(U.status.replace(/0\/$/, id + '/')).then(function (j) { if (j.ok) { show(j.restore); if (j.restore.status === 'done' || j.restore.status === 'failed') stop(); } }).catch(function () {});
    }, 2500);
  }
  function stop() { if (poller) { clearInterval(poller); poller = null; } }
  var TITLES = { sending: 'Sending the backup to the router…', checking: 'The router checks the file…', restoring: 'Restoring…', rebooting: ROUTER + ' is restarting…', done: ROUTER + ' is restored', failed: 'The restore did not finish' };
  function show(r) {
    cur = r; phase(r.status === 'draft' ? 'idle' : r.status); from(r.source);
    var text = { sending: LINK ? 'TapTap Link hands the job to the router at its next check-in, then the router downloads the file.' : 'The router downloads the file from TapTap.',
                 checking: 'The file is on the router — checking it is complete.', restoring: (r.safety_copy ? 'Safety copy saved. ' : '') + 'Loading the configuration; the router restarts now.',
                 rebooting: 'Usually 1–3 minutes. Customers reconnect by themselves when it is back.', done: 'Back online with the restored configuration.', failed: r.error }[r.status] || '';
    head(TITLES[r.status] || 'Restore', text);
    var ev = '<ul class="rs-events">' + (r.events || []).map(function (e) {
      var t = new Date(e.at); return '<li class="' + esc(e.level) + '"><i class="bi ' + (e.level === 'ok' ? 'bi-check-lg' : e.level === 'bad' ? 'bi-x-lg' : 'bi-arrow-right') + '"></i><span>' + esc(e.text) + '</span><small>' + t.toLocaleTimeString() + '</small></li>';
    }).join('') + '</ul>';
    var left;
    if (r.status === 'rebooting' || r.status === 'restoring') {
      var since = r.loading_at ? Math.max(0, Math.round((Date.now() - new Date(r.loading_at)) / 1000)) : 0;
      left = '<div class="rs-reboot"><div class="rs-big">' + Math.floor(since / 60) + ':' + ('0' + since % 60).slice(-2) + '</div><div class="rs-sub">since the restart began · TapTap is watching for ' + esc(ROUTER) + ' to come back</div></div>';
    } else if (r.status === 'done') {
      left = '<div class="rs-done" id="rsDone"><div class="rs-big"><i class="bi bi-check-circle-fill"></i> Restored</div><div class="rs-sub">' + esc(r.file_name) + '</div>'
        + '<button type="button" class="btn btn-success mt-3" onclick="location.reload()"><i class="bi bi-arrow-clockwise"></i> Reload the Control Center</button></div>';
    } else if (r.status === 'failed') {
      left = '<div class="rs-err"><b>' + esc(r.error) + '</b></div><button type="button" class="btn btn-outline-secondary mt-3" id="rsAgain"><i class="bi bi-arrow-counterclockwise"></i> Start again</button>'
        + (/Allow restores/.test(r.error) ? '<div class="mt-3" id="rsHelperBox3"></div>' : '');
    } else {
      left = '<div><div class="rs-big">' + (r.size ? size(r.size) : '') + '</div><div class="rs-sub">' + esc(r.file_name) + ' · ' + (r.elapsed || 0) + ' s</div></div>';
    }
    body.innerHTML = '<div class="rs-run"><div>' + left + '</div><div class="rs-card"><h4>What happened</h4>' + ev + '</div></div>';
    var again = $('rsAgain'); if (again) again.onclick = sources;
    if ($('rsHelperBox3')) helperBox($('rsHelperBox3'), true);
    if (r.status === 'done' && !el.dataset.sparked) { el.dataset.sparked = '1'; sparks($('rsDone')); }
    if (r.status === 'done') document.querySelectorAll('#rsSteps li').forEach(function (li) { li.className = 'ok'; });
  }
  function sparks(box) {
    if (!box) return; var colors = ['#22c55e', '#a78bfa', '#60a5fa', '#fbbf24', '#f472b6'];
    for (var i = 0; i < 40; i++) {
      var s = document.createElement('i'); s.className = 'rs-spark'; var a = Math.random() * Math.PI * 2, d = 110 + Math.random() * 230;
      s.style.cssText = 'background:' + colors[i % 5] + ';--x:' + Math.cos(a) * d + 'px;--y:' + Math.sin(a) * d * 0.6 + 'px;animation-delay:' + Math.random() * 0.3 + 's';
      box.appendChild(s);
    }
  }

  // open from the backups window
  document.addEventListener('click', function (e) {
    var t = e.target.closest('#restoreOpen,[data-restore-server]'); if (!t) return;
    var bm = bootstrap.Modal.getInstance(document.getElementById('backupModal')); if (bm) bm.hide();
    delete el.dataset.sparked; modal.show();
    if (t.dataset.restoreServer) pick('server', t.dataset.restoreServer); else sources();
  });
  el.addEventListener('hidden.bs.modal', function () { if (!cur || cur.status === 'done' || cur.status === 'failed' || cur.status === 'draft') stop(); });
})();
