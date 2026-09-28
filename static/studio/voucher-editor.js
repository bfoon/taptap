/* TapTap Voucher Designer editor */
(function () {
  var J = function (id) { return JSON.parse(document.getElementById(id).textContent); };
  var $ = function (s, r) { return (r || document).querySelector(s); }, $$ = function (s, r) { return [].slice.call((r || document).querySelectorAll(s)); };
  var cfg = J('cfgData'), GAL = J('galleryData'), BIZ = J('bizData'), PLANS = J('plansData'), FONTS = J('fontsData'), SIZES = J('sizesData'), PAPERS = J('papersData'), TOKENS = J('tokensData'), D = J('designData');
  var MM = 3.7795, zoom = 2, sel = null, rtab = 'card', dirty = false, isDefault = D.is_default, hist = [], hi = -1, saveT, histT, saving = false;
  var rev = 0, userZoom = false;   // rev counts edits (so a save never hides newer changes); userZoom: you zoomed by hand
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var clone = function (o) { return JSON.parse(JSON.stringify(o)); };
  var uid = function () { return 'e' + Math.random().toString(36).slice(2, 8); };
  var r1 = function (v) { return Math.round(v * 10) / 10; };
  cfg.elements = cfg.elements || []; cfg.page = cfg.page || { paper: 'A4', margin: 8, gap: 3, cut_marks: true }; cfg.border = cfg.border || {}; cfg.bg = cfg.bg || { type: 'solid', color1: '#ffffff' };

  var FONT_Q = { outfit: 'Outfit:wght@400;600;800', sora: 'Sora:wght@400;600;800', grotesk: 'Space+Grotesk:wght@400;600;700', bricolage: 'Bricolage+Grotesque:opsz,wght@12..96,400;12..96,700;12..96,800', syne: 'Syne:wght@500;700;800', archivo: 'Archivo+Black', fraunces: 'Fraunces:opsz,wght@9..144,400;9..144,700', dmserif: 'DM+Serif+Display', nunito: 'Nunito:wght@400;700;900', rubik: 'Rubik:wght@400;600;800', mono: 'JetBrains+Mono:wght@400;700;800', caveat: 'Caveat:wght@500;700', bebas: 'Bebas+Neue' };
  function loadFonts() {
    var used = [cfg.font].concat(cfg.elements.map(function (e) { return e.font; })).filter(function (f, i, a) { return FONT_Q[f] && a.indexOf(f) === i; });
    var l = document.getElementById('vfonts'); if (!l) { l = document.createElement('link'); l.id = 'vfonts'; l.rel = 'stylesheet'; document.head.appendChild(l); }
    var href = used.length ? 'https://fonts.googleapis.com/css2?family=' + used.map(function (f) { return FONT_Q[f]; }).join('&family=') + '&display=swap' : '';
    if (href && l.getAttribute('href') !== href) l.setAttribute('href', href);   // re-setting the same link made text flicker on every edit
  }

  /* sample data */
  var planSel = $('#samplePlan');
  planSel.innerHTML = PLANS.map(function (p, i) { return '<option value="' + i + '">' + esc(p.name) + '</option>'; }).join('') || '<option value="">Example plan</option>';
  var VADS = document.getElementById('adsData') ? JSON.parse(document.getElementById('adsData').textContent) : [];
  function data() {
    var p = PLANS[+planSel.value] || {};
    return TapVoucher.sample({ business: BIZ.name, ssid: BIZ.ssid, phone: BIZ.phone, currency: BIZ.currency, login_url: BIZ.login_url, logo: BIZ.logo,
      plan: p.name, price: p.price, duration: p.duration, devices: p.devices, speed: p.speed, data: p.data,
      ad: VADS[0] || { headline: 'Your advert here', body: 'Sell this space to a local business', image: '' } });
  }
  planSel.onchange = draw;

  /* ---------- drawing ---------- */
  var canvas = $('#canvas'), wrap = $('#wrap');
  function el(id) { for (var i = 0; i < cfg.elements.length; i++) if (cfg.elements[i].id === id) return cfg.elements[i]; return null; }
  function ix(id) { for (var i = 0; i < cfg.elements.length; i++) if (cfg.elements[i].id === id) return i; return -1; }
  function draw() {
    loadFonts();
    var card = TapVoucher.renderCard(cfg, data(), { editable: true });
    canvas.innerHTML = ''; canvas.appendChild(card);
    canvas.style.transform = 'scale(' + zoom + ')'; canvas.style.width = cfg.size.w + 'mm'; canvas.style.height = cfg.size.h + 'mm';
    wrap.style.width = cfg.size.w * MM * zoom + 'px'; wrap.style.height = cfg.size.h * MM * zoom + 'px';
    cfg.elements.forEach(function (e) { var d = $('[data-eid="' + e.id + '"]', canvas); if (d && e.locked) d.classList.add('locked'); });
    $('#zVal').textContent = Math.round(zoom * 100) + '%';
    $('#sizeTag').textContent = cfg.size.w + ' × ' + cfg.size.h + ' mm';
    drawSel(); renderLayers();
  }
  function box(e) { return { l: e.x * MM * zoom, t: e.y * MM * zoom, w: Math.max(e.w * MM * zoom, 4), h: Math.max(e.h * MM * zoom, 4) }; }
  function drawSel() {
    $$('.vsel,.vguide', wrap).forEach(function (n) { n.remove(); });
    var e = el(sel); if (!e) return;
    var b = box(e), s = document.createElement('div'); s.className = 'vsel';
    s.style.cssText = 'left:' + b.l + 'px;top:' + b.t + 'px;width:' + b.w + 'px;height:' + b.h + 'px';
    s.innerHTML = '<span class="dims">' + r1(e.w) + ' × ' + r1(e.h) + ' mm</span>' + (e.locked ? '' : '<i data-h="nw"></i><i data-h="e"></i><i data-h="s"></i><i data-h="se"></i>');
    wrap.appendChild(s);
  }
  function guide(vert, pos) { var g = document.createElement('div'); g.className = 'vguide'; g.style.cssText = vert ? 'left:' + pos * MM * zoom + 'px;top:0;bottom:0;width:1px' : 'top:' + pos * MM * zoom + 'px;left:0;right:0;height:1px'; wrap.appendChild(g); }

  /* ---------- zoom ---------- */
  function autoZoom() { var st = $('#stage'); zoom = Math.max(.5, Math.min(4, Math.floor(Math.min((st.clientWidth - 80) / (cfg.size.w * MM), (st.clientHeight - 90) / (cfg.size.h * MM)) * 4) / 4)); }
  $('#zIn').onclick = function () { zoom = Math.min(6, zoom + .25); userZoom = true; draw(); };
  $('#zOut').onclick = function () { zoom = Math.max(.5, zoom - .25); userZoom = true; draw(); };
  $('#zVal').onclick = function () { userZoom = false; autoZoom(); draw(); };
  $('#zVal').title = 'Fit to screen';

  /* ---------- drag / resize ---------- */
  var drag = null;
  wrap.addEventListener('pointerdown', function (ev) {
    var h = ev.target.closest('.vsel i'), t = ev.target.closest('[data-eid]');
    if (!h && !t) { if (ev.target.closest('.tv-card')) { sel = null; rtab = 'card'; drawSel(); renderProps(); renderLayers(); } return; }
    ev.preventDefault();
    // Take focus off the side panel so arrow keys / Delete act on the card, not on a slider or button there.
    if (document.activeElement && document.activeElement !== document.body && document.activeElement.blur) document.activeElement.blur();
    if (t && !h) { var id = t.getAttribute('data-eid'); if (sel !== id) { sel = id; rtab = 'el'; renderProps(); renderLayers(); drawSel(); } }
    var e = el(sel); if (!e || e.locked) return;
    drag = { mode: h ? h.dataset.h : 'move', x0: ev.clientX, y0: ev.clientY, e0: clone(e), moved: false, pid: ev.pointerId };
    try { wrap.setPointerCapture(ev.pointerId); } catch (err) {}
  });
  wrap.addEventListener('pointermove', function (ev) {
    if (!drag || (drag.pid != null && ev.pointerId !== drag.pid)) return;
    var e = el(sel); if (!e) { drag = null; return; }
    var dx = (ev.clientX - drag.x0) / (MM * zoom), dy = (ev.clientY - drag.y0) / (MM * zoom), o = drag.e0, W = cfg.size.w, H = cfg.size.h;
    if (Math.abs(dx) + Math.abs(dy) > .05) drag.moved = true;
    var snap = ev.altKey ? function (v) { return r1(v); } : function (v) { return Math.round(v * 2) / 2; };
    $$('.vguide', wrap).forEach(function (n) { n.remove(); });
    if (drag.mode === 'move') {
      var nx = snap(o.x + dx), ny = snap(o.y + dy), th = 1.2;
      if (Math.abs(nx + o.w / 2 - W / 2) < th) { nx = r1(W / 2 - o.w / 2); guide(true, W / 2); }
      if (Math.abs(ny + o.h / 2 - H / 2) < th) { ny = r1(H / 2 - o.h / 2); guide(false, H / 2); }
      cfg.elements.forEach(function (x) { if (x.id === e.id) return; if (Math.abs(nx - x.x) < th) { nx = x.x; guide(true, x.x); } if (Math.abs(ny - x.y) < th) { ny = x.y; guide(false, x.y); } });
      e.x = nx; e.y = ny;
    } else {
      if (drag.mode === 'se' || drag.mode === 'e') e.w = Math.max(e.type === 'line' ? .1 : 1, snap(o.w + dx));
      if (drag.mode === 'se' || drag.mode === 's') e.h = Math.max(e.type === 'line' ? .1 : 1, snap(o.h + dy));
      if (drag.mode === 'nw') { var nw = Math.max(1, snap(o.w - dx)), nh = Math.max(1, snap(o.h - dy)); e.x = r1(o.x + o.w - nw); e.y = r1(o.y + o.h - nh); e.w = nw; e.h = nh; }
      if (e.type === 'qr' || (e.type === 'logo' && drag.mode === 'se')) { var m = Math.max(e.w, e.h); e.w = e.h = m; }
    }
    var d = $('[data-eid="' + e.id + '"]', canvas);
    if (d) { d.style.left = e.x + 'mm'; d.style.top = e.y + 'mm'; d.style.width = e.w + 'mm'; d.style.height = e.h + 'mm'; }
    var s = $('.vsel', wrap), b = box(e); if (s) { s.style.left = b.l + 'px'; s.style.top = b.t + 'px'; s.style.width = b.w + 'px'; s.style.height = b.h + 'px'; $('.dims', s).textContent = r1(e.w) + ' × ' + r1(e.h) + ' mm'; }
  });
  wrap.addEventListener('pointerup', function () { if (!drag) return; var m = drag.moved; drag = null; $$('.vguide', wrap).forEach(function (n) { n.remove(); }); if (m) { commit(true); } else draw(); });
  // A touch that turns into a scroll, or a lost capture, used to leave the drag "stuck" so the element
  // followed the next mouse move. Cancel puts the element back where it was.
  function cancelDrag() { if (!drag) return; var e = el(sel), o = drag.e0; drag = null; if (e && o) { e.x = o.x; e.y = o.y; e.w = o.w; e.h = o.h; } draw(); }
  wrap.addEventListener('pointercancel', cancelDrag);
  wrap.addEventListener('lostpointercapture', function () { if (drag) { var m = drag.moved; drag = null; if (m) commit(true); else draw(); } });
  window.addEventListener('blur', cancelDrag);
  wrap.addEventListener('dblclick', function (ev) { var t = ev.target.closest('[data-eid]'); if (!t) return; var f = $('#props textarea[data-p=text]'); if (f) { f.focus(); f.select(); } });

  /* ---------- history + save ---------- */
  function commit(now) { rev++; dirty = true; status('dirty'); draw(); if (rtab !== 'el' || !now) {} renderPropsSoft(); clearTimeout(histT); histT = setTimeout(pushHist, now ? 0 : 350); clearTimeout(saveT); saveT = setTimeout(save, 2500); }
  function pushHist() { histT = null; var s = JSON.stringify(cfg); if (hist[hi] === s) return; hist = hist.slice(0, hi + 1); hist.push(s); if (hist.length > 100) hist.shift(); hi = hist.length - 1; undoState(); }
  function undoState() { $('#undo').disabled = hi <= 0; $('#redo').disabled = hi >= hist.length - 1; }
  function restore(i) { hi = i; rev++; cfg = JSON.parse(hist[hi]); if (sel && !el(sel)) sel = null; dirty = true; status('dirty'); draw(); renderProps(); undoState(); clearTimeout(saveT); saveT = setTimeout(save, 2500); }
  // An edit made less than a moment ago may still be waiting to enter the history: add it first, so undo steps back exactly one change.
  function flushHist() { if (histT) { clearTimeout(histT); histT = null; pushHist(); } }
  $('#undo').onclick = function () { flushHist(); if (hi > 0) restore(hi - 1); };
  $('#redo').onclick = function () { flushHist(); if (hi < hist.length - 1) restore(hi + 1); };
  function status(k, msg) { var s = $('#status'); s.className = 'ed-status ' + (k || ''); s.textContent = msg || { dirty: 'Unsaved changes', saving: 'Saving…', err: 'Could not save' }[k] || 'All changes saved'; }
  function csrf() { var t = document.querySelector('meta[name=csrf-token]'); if (t && t.content) return t.content; var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ''; }
  function save(extra) {
    if (saving) { clearTimeout(saveT); saveT = setTimeout(save, 800); return Promise.resolve(); }
    saving = true; status('saving');
    var sent = rev;
    return fetch(location.pathname, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(Object.assign({ config: cfg, name: $('#dName').value.trim() }, extra || {})) })
      .then(function (r) {
        var ct = r.headers.get('content-type') || '';
        if (ct.indexOf('json') < 0) throw new Error(r.status === 403 ? 'Not saved — your session expired. Reload the page (your changes stay on screen until you do).' : r.status >= 500 ? 'Not saved — server error. Try again.' : 'Not saved (' + r.status + ').');
        return r.json().then(function (d) { if (!r.ok || !d.success) throw new Error(d.message || 'Save failed'); return d; });
      })
      .then(function (d) {
        isDefault = d.is_default; defBtn();
        if (rev === sent) { dirty = false; status('', 'Saved at ' + d.updated_at); }
        else { status('dirty'); clearTimeout(saveT); saveT = setTimeout(save, 1200); }   // you kept editing while it saved
      })
      .catch(function (e) { status('err', e.message === 'Failed to fetch' ? 'Not saved — no connection. Retrying…' : e.message); if (e.message === 'Failed to fetch') { clearTimeout(saveT); saveT = setTimeout(save, 5000); } })
      .then(function () { saving = false; });
  }
  $('#saveBtn').onclick = function () { clearTimeout(saveT); save(); };
  $('#dName').addEventListener('input', function () { rev++; dirty = true; status('dirty'); clearTimeout(saveT); saveT = setTimeout(save, 1500); });
  function defBtn() { var b = $('#defBtn'); b.innerHTML = isDefault ? '<i class="bi bi-star-fill"></i> Default' : '<i class="bi bi-star"></i> Make default'; b.disabled = isDefault; b.title = 'The default design is used when printing from Batches and Vouchers'; }
  $('#defBtn').onclick = function () { clearTimeout(saveT); save({ is_default: true }); };
  $('#testPrint').addEventListener('click', function () { if (dirty) { clearTimeout(saveT); save(); } });
  window.addEventListener('beforeunload', function (e) { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

  document.addEventListener('keydown', function (ev) {
    var k = ev.key.toLowerCase(), mod = ev.ctrlKey || ev.metaKey, inField = /INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName);
    if (mod && k === 's') { ev.preventDefault(); clearTimeout(saveT); save(); return; }
    if (inField || (document.activeElement && document.activeElement.isContentEditable)) return;
    if (mod && k === 'z') { ev.preventDefault(); (ev.shiftKey ? $('#redo') : $('#undo')).click(); return; }
    if (mod && k === 'y') { ev.preventDefault(); $('#redo').click(); return; }
    var ae = document.activeElement, onSide = ae && ae !== document.body && !$('#stage').contains(ae) && ae.tagName === 'BUTTON';
    var e = el(sel); if (!e) return;
    if (k === 'escape') { sel = null; rtab = 'card'; draw(); renderProps(); return; }
    if ((k === 'delete' || k === 'backspace') && !onSide) { ev.preventDefault(); removeEl(sel); return; }
    if (mod && k === 'd') { ev.preventDefault(); dup(sel); return; }
    var st = ev.shiftKey ? 2 : .5, mv = { arrowleft: [-st, 0], arrowright: [st, 0], arrowup: [0, -st], arrowdown: [0, st] }[k];
    if (mv && !e.locked) { ev.preventDefault(); e.x = r1(e.x + mv[0]); e.y = r1(e.y + mv[1]); commit(); }
  });

  /* ---------- add / layers ---------- */
  var ADD = [
    ['text', 'Text', 'bi-fonts', { w: 40, h: 6, text: 'Your text', size: 9, weight: 600 }],
    ['code', 'Voucher code', 'bi-upc', { w: 50, h: 11 }],
    ['qr', 'QR code', 'bi-qr-code', { w: 18, h: 18 }],
    ['logo', 'Logo', 'bi-person-badge', { w: 10, h: 10 }],
    ['rect', 'Box', 'bi-square', { w: 30, h: 12 }],
    ['ellipse', 'Circle', 'bi-circle', { w: 14, h: 14 }],
    ['line', 'Line', 'bi-dash-lg', { w: 40, h: .1 }],
    ['icon', 'Icon', 'bi-wifi', { w: 7, h: 7 }],
    ['image', 'Picture', 'bi-image', { w: 20, h: 14 }],
    ['barcode', 'Barcode', 'bi-upc-scan', { w: 40, h: 10 }],
    ['advert', 'Advert slot', 'bi-badge-ad', { w: 60, h: 9 }]
  ];
  var DEFAULTS = {
    text: { text: 'Your text', size: 9, weight: 600, color: '#102033', align: 'left', font: '', spacing: 0, upper: false, italic: false, bg: '', radius: 0 },
    code: { text: '{code}', size: 16, weight: 800, color: '#102033', align: 'center', font: '', spacing: 2, group: 4, upper: false, italic: false, bg: '', border: '#102033', border_width: .4, radius: 2 },
    qr: { content: 'login', color: '#102033', bg: '#ffffff', margin: 1 }, rect: { fill: '#1769e0', stroke: '', stroke_width: 0, radius: 1 }, ellipse: { fill: '#1769e0', stroke: '', stroke_width: 0 },
    line: { stroke: '#102033', stroke_width: .3, dash: 'solid' }, logo: { shape: 'rounded', color: '#ffffff', bg: '#1769e0' }, icon: { icon: 'wifi', color: '#1769e0' },
    image: { src: '', fit: 'cover', radius: 1 }, barcode: { text: '{code}', color: '#000000', bg: '#ffffff', show_text: true },
    advert: { show: 'both', size: 6, color: '#102033', bg: '#f3f6fa', radius: 1, label: 'Ad' }
  };
  var QUICK = [['Business name', '{business}', 11, 800], ['Price', '{currency}{price}', 14, 800], ['Plan & duration', '{plan} · {duration}', 7.5, 600], ['Wi-Fi name', 'Wi-Fi: {ssid}', 7, 600],
    ['How to connect', 'Join {ssid}, open any web page and type the code.', 6, 500], ['Serial number', '#{serial}', 6, 600], ['Help line', 'Help: {phone}', 6, 600], ['Devices & speed', '{devices} · {speed}', 6.5, 600]];
  function addEl(type, extra) {
    var a = ADD.filter(function (x) { return x[0] === type; })[0], d = Object.assign({ id: uid(), type: type, opacity: 1, locked: false }, clone(DEFAULTS[type]), clone(a[3]), extra || {});
    d.w = Math.min(d.w, cfg.size.w - 2); d.x = r1((cfg.size.w - d.w) / 2); d.y = r1(Math.max(1, (cfg.size.h - d.h) / 2));
    cfg.elements.push(d); sel = d.id; rtab = 'el'; commit(true); renderProps();
  }
  $('#addGrid').innerHTML = ADD.map(function (a) { return '<button data-t="' + a[0] + '"><i class="bi ' + a[2] + '"></i>' + a[1] + '</button>'; }).join('');
  $$('#addGrid button').forEach(function (b) { b.onclick = function () { addEl(b.dataset.t); }; });
  $('#quickText').innerHTML = QUICK.map(function (q, i) { return '<button class="btn btn-sm btn-light text-start" data-q="' + i + '"><b>' + q[0] + '</b> <small class="text-secondary">' + esc(q[1]) + '</small></button>'; }).join('');
  $$('#quickText button').forEach(function (b) { b.onclick = function () { var q = QUICK[+b.dataset.q]; addEl('text', { text: q[1], size: q[2], weight: q[3], w: Math.min(60, Math.max(20, q[1].length * q[2] * .22)), h: q[2] * .6 + 1 }); }; });

  function dup(id) { var e = el(id); if (!e) return; var c = clone(e); c.id = uid(); c.x = r1(c.x + 2); c.y = r1(c.y + 2); cfg.elements.splice(ix(id) + 1, 0, c); sel = c.id; commit(true); renderProps(); }
  function removeEl(id) { var i = ix(id); if (i < 0) return; cfg.elements.splice(i, 1); sel = null; rtab = 'card'; commit(true); renderProps(); }
  function order(id, d) { var i = ix(id), j = d === 'top' ? cfg.elements.length - 1 : d === 'bottom' ? 0 : i + d; if (j < 0 || j >= cfg.elements.length || i === j) return; var e = cfg.elements.splice(i, 1)[0]; cfg.elements.splice(j, 0, e); commit(true); }
  var TYPE_ICON = { text: 'bi-fonts', code: 'bi-upc', qr: 'bi-qr-code', logo: 'bi-person-badge', rect: 'bi-square', ellipse: 'bi-circle', line: 'bi-dash-lg', icon: 'bi-wifi', image: 'bi-image', barcode: 'bi-upc-scan', advert: 'bi-badge-ad' };
  function label(e) { return e.type === 'text' ? (e.text || 'Text') : ({ code: 'Voucher code', qr: 'QR code', logo: 'Logo', rect: 'Box', ellipse: 'Circle', line: 'Line', icon: 'Icon · ' + e.icon, image: 'Picture', barcode: 'Barcode', advert: 'Advert slot' }[e.type]); }
  function renderLayers() {
    var L = $('#layers'); if (!L) return;
    L.innerHTML = cfg.elements.slice().reverse().map(function (e) {
      return '<div class="layer' + (e.id === sel ? ' sel' : '') + (e.hidden ? ' hid' : '') + '" data-id="' + e.id + '"><i class="t bi ' + TYPE_ICON[e.type] + '"></i><span class="nm">' + esc(label(e)) + '</span>' +
        '<button data-a="hide" title="' + (e.hidden ? 'Show' : 'Hide') + '" aria-label="Toggle visibility"><i class="bi bi-eye' + (e.hidden ? '-slash' : '') + '"></i></button>' +
        '<button data-a="lock" title="' + (e.locked ? 'Unlock' : 'Lock') + '" aria-label="Toggle lock"><i class="bi bi-' + (e.locked ? 'lock-fill' : 'unlock') + '"></i></button></div>';
    }).join('') || '<div class="empty-props">The card is empty. Add elements from the Add tab.</div>';
    $$('.layer', L).forEach(function (r) { r.onclick = function (ev) { var e = el(r.dataset.id), a = ev.target.closest('[data-a]');
      if (a && a.dataset.a === 'hide') { e.hidden = !e.hidden; commit(true); return; }
      if (a && a.dataset.a === 'lock') { e.locked = !e.locked; commit(true); return; }
      sel = e.id; rtab = 'el'; draw(); renderProps(); }; });
  }

  $$('[data-lt]').forEach(function (b) { b.onclick = function () { $$('[data-lt]').forEach(function (x) { x.classList.toggle('on', x === b); }); $$('[data-lp]').forEach(function (p) { p.hidden = p.dataset.lp !== b.dataset.lt; }); if (b.dataset.lt === 'templates') tpls(); }; });
  function tpls() {
    var L = $('#tplList'); if (L.dataset.done) return; L.dataset.done = 1;
    var gf = []; GAL.forEach(function (g) { [g.config.font].concat((g.config.elements || []).map(function (x) { return x.font; })).forEach(function (f) { if (FONT_Q[f] && gf.indexOf(FONT_Q[f]) < 0) gf.push(FONT_Q[f]); }); });
    if (gf.length && !document.getElementById('vfonts-gal')) { var gl = document.createElement('link'); gl.id = 'vfonts-gal'; gl.rel = 'stylesheet'; gl.href = 'https://fonts.googleapis.com/css2?family=' + gf.join('&family=') + '&display=swap'; document.head.appendChild(gl); }
    L.innerHTML = GAL.map(function (g, i) { return '<button class="btn btn-light text-start p-2" data-i="' + i + '"><div class="vg-thumb mb-1" style="height:110px"></div><b class="small">' + esc(g.label) + '</b><small class="d-block text-secondary">' + esc(g.note) + '</small></button>'; }).join('');
    $$('button', L).forEach(function (b) {
      var g = GAL[+b.dataset.i], t = $('.vg-thumb', b), c = TapVoucher.renderCard(g.config, data()), w = g.config.size.w * MM, h = g.config.size.h * MM, s = Math.min((t.clientWidth - 16) / w, 94 / h);
      var wr = document.createElement('div'); wr.style.cssText = 'width:' + w * s + 'px;height:' + h * s + 'px'; c.style.transform = 'scale(' + s + ')'; c.style.transformOrigin = '0 0'; wr.appendChild(c); t.appendChild(wr);
      b.onclick = function () { if (!confirm('Replace this design with “' + g.label + '”? You can undo this.')) return; flushHist(); cfg = clone(g.config); sel = null; rtab = 'card'; userZoom = false; autoZoom(); commit(true); renderProps(); };
    });
  }

  /* ---------- properties ---------- */
  $$('[data-rt]').forEach(function (b) { b.onclick = function () { rtab = b.dataset.rt; renderProps(); }; });
  function f(label, html) { return '<div class="fld"><label>' + label + '</label>' + html + '</div>'; }
  function num(k, v, step, lab) { return '<div><label>' + lab + '</label><input type="number" step="' + (step || .5) + '" data-p="' + k + '" data-num="1" value="' + (v == null ? '' : r1(v)) + '" aria-label="' + lab + '"></div>'; }
  function color(label, k, v, clearable) { var hex = /^#[0-9a-f]{6}$/i.test(v || '') ? v : '#ffffff'; return f(label, '<div class="color-in"><input type="color" data-p="' + k + '" value="' + hex + '" aria-label="' + label + ' picker"><input type="text" data-p="' + k + '" value="' + esc(v) + '" placeholder="' + (clearable ? 'none' : '') + '" aria-label="' + label + '">' + (clearable ? '<button class="btn btn-sm btn-link p-0 text-secondary" data-clear="' + k + '" title="No colour" aria-label="Clear colour"><i class="bi bi-slash-circle"></i></button>' : '') + '</div>'); }
  function sel_(label, k, v, opts) { return f(label, '<select data-p="' + k + '" aria-label="' + label + '">' + Object.keys(opts).map(function (o) { return '<option value="' + esc(o) + '"' + (String(v) === o ? ' selected' : '') + '>' + esc(opts[o]) + '</option>'; }).join('') + '</select>'); }
  function seg(label, k, v, opts) { return f(label, '<div class="seg-in" data-seg="' + k + '">' + Object.keys(opts).map(function (o) { return '<button type="button" data-v="' + o + '" class="' + (String(v) === o ? 'on' : '') + '">' + opts[o] + '</button>'; }).join('') + '</div>'); }
  function rng(label, k, v, a, b, s, u) { return f(label, '<div class="rng"><input type="range" data-p="' + k + '" data-num="1" min="' + a + '" max="' + b + '" step="' + s + '" value="' + v + '" aria-label="' + label + '"><output>' + v + (u || '') + '</output></div>'); }
  var fontOpts = function (inherit) { var o = inherit ? { '': 'Same as card' } : {}; Object.keys(FONTS).forEach(function (k) { o[k] = FONTS[k]; }); return o; };

  function renderProps() {
    $$('[data-rt]').forEach(function (x) { x.classList.toggle('on', x.dataset.rt === rtab); });
    var p = $('#props'), target, h;
    if (rtab === 'el') { target = el(sel); h = target ? elProps(target) : '<div class="empty-props"><i class="bi bi-cursor fs-3 d-block mb-2"></i>Click something on the card to edit it.</div>'; }
    else if (rtab === 'card') { target = cfg; h = cardProps(); }
    else { target = cfg; h = printProps(); }
    p.innerHTML = h; bind(p, target);
  }
  var softT; function renderPropsSoft() { clearTimeout(softT); softT = setTimeout(function () { var a = document.activeElement; if (a && $('#props').contains(a) && /INPUT|TEXTAREA|SELECT/.test(a.tagName)) { syncGeom(); return; } renderProps(); }, 60); }
  function syncGeom() { var e = el(sel); if (!e || rtab !== 'el') return; ['x', 'y', 'w', 'h'].forEach(function (k) { var i = $('#props [data-p="' + k + '"]'); if (i && i !== document.activeElement) i.value = r1(e[k]); }); }

  function elProps(e) {
    var h = '<div class="side-h">' + esc(label(e)) + '<span><button class="btn btn-sm btn-light" data-act="dup" title="Duplicate (Ctrl+D)" aria-label="Duplicate"><i class="bi bi-copy"></i></button> <button class="btn btn-sm btn-light text-danger" data-act="del" title="Delete" aria-label="Delete"><i class="bi bi-trash"></i></button></span></div>';
    h += '<div class="fld"><div class="row4">' + num('x', e.x, .5, 'X mm') + num('y', e.y, .5, 'Y mm') + num('w', e.w, .5, 'Width') + num('h', e.h, .5, 'Height') + '</div></div>';
    h += f('Align on card', '<div class="align-row"><button data-al="l" title="Left" aria-label="Align left"><i class="bi bi-align-start"></i></button><button data-al="c" title="Centre" aria-label="Centre horizontally"><i class="bi bi-align-center"></i></button><button data-al="r" title="Right" aria-label="Align right"><i class="bi bi-align-end"></i></button><button data-al="t" title="Top" aria-label="Align top"><i class="bi bi-align-top"></i></button><button data-al="m" title="Middle" aria-label="Centre vertically"><i class="bi bi-align-middle"></i></button><button data-al="b" title="Bottom" aria-label="Align bottom"><i class="bi bi-align-bottom"></i></button></div>');
    if (e.type === 'text' || e.type === 'code') {
      h += f(e.type === 'code' ? 'Content' : 'Text', '<textarea data-p="text" rows="' + (e.type === 'code' ? 1 : 3) + '">' + esc(e.text) + '</textarea><div class="tok-chips">' + TOKENS.map(function (t) { return '<button type="button" data-tok="' + t[0] + '" title="Insert ' + t[1] + '">' + t[1] + '</button>'; }).join('') + '</div>');
      h += sel_('Font', 'font', e.font, fontOpts(true));
      h += '<div class="fld"><div class="row2"><div><label>Size (pt)</label><input type="number" step=".5" min="3" max="72" data-p="size" data-num="1" value="' + e.size + '"></div><div><label>Weight</label><select data-p="weight" data-num="1">' + [400, 500, 600, 700, 800].map(function (w) { return '<option' + (+e.weight === w ? ' selected' : '') + '>' + w + '</option>'; }).join('') + '</select></div></div></div>';
      h += color('Colour', 'color', e.color) + seg('Align text', 'align', e.align, { left: 'Left', center: 'Centre', right: 'Right' }) + rng('Letter spacing', 'spacing', e.spacing || 0, 0, 8, .5, 'pt');
      if (e.type === 'code') h += seg('Group characters', 'group', String(e.group || 0), { '0': 'No', '3': '3', '4': '4' }) + color('Frame colour', 'border', e.border) + rng('Frame thickness', 'border_width', e.border_width || 0, 0, 1.5, .1, 'mm');
      h += '<label class="chk"><input type="checkbox" data-p="upper"' + (e.upper ? ' checked' : '') + '> CAPITALS</label><label class="chk"><input type="checkbox" data-p="italic"' + (e.italic ? ' checked' : '') + '> Italic</label>';
      h += color('Highlight behind text', 'bg', e.bg, true) + rng('Highlight corners', 'radius', e.radius || 0, 0, 6, .5, 'mm');
    } else if (e.type === 'qr') {
      h += seg('When scanned', 'content', e.content || 'login', { login: 'Log in', code: 'Show code', wifi: 'Join Wi-Fi' });
      if ((e.content || 'login') === 'login' && !BIZ.login_url) h += '<div class="fit-note">Add your hotspot login address (e.g. <b>wifi.local</b>) in <a href="/settings/">Settings</a> so a scan logs the customer straight in. Until then the QR holds the code.</div>';
      h += color('Dots', 'color', e.color) + color('Background', 'bg', e.bg) + rng('Quiet border', 'margin', e.margin || 0, 0, 4, 1, '');
      h += '<small class="text-secondary d-block">Keep QR codes at least 15 mm wide so phone cameras read them.</small>';
    } else if (e.type === 'rect' || e.type === 'ellipse') {
      h += color('Fill', 'fill', e.fill, true) + color('Outline', 'stroke', e.stroke, true) + rng('Outline width', 'stroke_width', e.stroke_width || 0, 0, 2, .1, 'mm');
      if (e.type === 'rect') h += rng('Corners', 'radius', e.radius || 0, 0, 10, .5, 'mm');
    } else if (e.type === 'line') {
      h += color('Colour', 'stroke', e.stroke) + rng('Thickness', 'stroke_width', e.stroke_width || .3, .1, 2, .05, 'mm') + seg('Style', 'dash', e.dash || 'solid', { solid: 'Solid', dashed: 'Dashed', dotted: 'Dotted' });
    } else if (e.type === 'logo') {
      h += seg('Shape', 'shape', e.shape, { circle: 'Circle', rounded: 'Rounded', square: 'Square' }) + color('Background (no logo yet)', 'bg', e.bg) + color('Initials colour', 'color', e.color);
      h += '<div class="fit-note">' + (BIZ.logo ? 'Showing your uploaded logo.' : 'Showing your initials. <a href="/settings/">Upload a logo</a> to use it on every card.') + '</div>';
    } else if (e.type === 'image') {
      h += f('Picture', '<input type="file" accept="image/*" data-img="1" aria-label="Upload picture"><small class="text-secondary d-block">Resized in your browser so sheets print fast.</small>' + (e.src ? '<button class="btn btn-sm btn-link text-danger p-0" data-imgclear="1">Remove picture</button>' : ''));
      h += seg('Fit', 'fit', e.fit || 'cover', { cover: 'Fill', contain: 'Fit inside' }) + rng('Corners', 'radius', e.radius || 0, 0, 10, .5, 'mm');
    } else if (e.type === 'barcode') {
      h += f('Content', '<input type="text" data-p="text" value="' + esc(e.text || '{code}') + '">') + '<small class="text-secondary d-block mb-2">Code 128 — most USB and phone scanners read it. Keep it at least 35 mm wide.</small>';
      h += color('Bars', 'color', e.color) + color('Background', 'bg', e.bg) + '<label class="chk"><input type="checkbox" data-p="show_text"' + (e.show_text !== false ? ' checked' : '') + '> Print the code under the bars</label>';
    } else if (e.type === 'advert') {
      h += '<div class="fit-note">Each printed card shows one of your live adverts marked <b>Printed vouchers</b>, rotating through them. <a href="/ads/">Manage adverts</a>.</div>';
      h += seg('Show', 'show', e.show || 'both', { both: 'Picture + text', image: 'Picture', text: 'Text' }) + '<div class="fld"><label>Headline size (pt)</label><input type="number" step=".5" min="3" max="14" data-p="size" data-num="1" value="' + (e.size || 6) + '"></div>';
      h += color('Text', 'color', e.color) + color('Background', 'bg', e.bg, true) + rng('Corners', 'radius', e.radius || 0, 0, 6, .5, 'mm') + f('Corner label', '<input type="text" data-p="label" value="' + esc(e.label || '') + '" placeholder="e.g. Ad or Sponsored">');
    } else if (e.type === 'icon') {
      var io = {}; TapVoucher.ICONS.forEach(function (n) { io[n] = n.charAt(0).toUpperCase() + n.slice(1); });
      h += sel_('Icon', 'icon', e.icon, io) + color('Colour', 'color', e.color);
    }
    h += rng('Opacity', 'opacity', e.opacity == null ? 1 : e.opacity, .1, 1, .05, '');
    h += '<div class="side-h">Arrange</div><div class="d-flex gap-1 flex-wrap"><button class="btn btn-sm btn-light" data-ord="top">Bring to front</button><button class="btn btn-sm btn-light" data-ord="1">Forward</button><button class="btn btn-sm btn-light" data-ord="-1">Backward</button><button class="btn btn-sm btn-light" data-ord="bottom">Send to back</button></div>';
    h += '<label class="chk mt-2"><input type="checkbox" data-p="locked"' + (e.locked ? ' checked' : '') + '> Lock position</label>';
    return h;
  }

  function cardProps() {
    var sz = cfg.size, bg = cfg.bg, bd = cfg.border, preset = sz.preset && SIZES[sz.preset] ? sz.preset : 'custom', so = {};
    Object.keys(SIZES).forEach(function (k) { so[k] = SIZES[k][0]; }); so.custom = 'Custom size';
    var h = '<div class="side-h">Size</div>' + f('Card size', '<select data-size="1">' + Object.keys(so).map(function (k) { return '<option value="' + k + '"' + (preset === k ? ' selected' : '') + '>' + so[k] + '</option>'; }).join('') + '</select>');
    h += '<div class="fld"><div class="row2"><div><label>Width mm</label><input type="number" min="20" max="200" step="1" data-sz="w" value="' + sz.w + '"></div><div><label>Height mm</label><input type="number" min="15" max="200" step="1" data-sz="h" value="' + sz.h + '"></div></div><small class="text-secondary">Elements outside the card are cut off.</small></div>';
    h += '<div class="side-h">Look</div>' + sel_('Card font', 'font', cfg.font, fontOpts(false));
    h += seg('Background', 'bg.type', bg.type, { solid: 'Colour', gradient: 'Gradient', image: 'Picture' }) + color(bg.type === 'gradient' ? 'From' : 'Colour', 'bg.color1', bg.color1);
    if (bg.type === 'gradient') h += color('To', 'bg.color2', bg.color2) + rng('Angle', 'bg.angle', bg.angle || 135, 0, 360, 5, '°');
    if (bg.type === 'image') h += f('Picture', (bg.image ? '<img class="upload-prev" src="' + esc(bg.image) + '" alt="">' : '') + '<label class="upload-drop"><input type="file" accept="image/*" data-bgimg="1" hidden><i class="bi bi-upload"></i> ' + (bg.image ? 'Replace picture' : 'Upload a picture') + '</label>');
    h += sel_('Pattern', 'bg.pattern', bg.pattern || 'none', { none: 'None', dots: 'Dots', stripes: 'Stripes', grid: 'Grid', waves: 'Waves', palms: 'Palms', pitch: 'Pitch', kente: 'Kente', circuit: 'Circuit' });
    if (bg.pattern && bg.pattern !== 'none') h += rng('Pattern strength', 'bg.pattern_opacity', bg.pattern_opacity == null ? .12 : bg.pattern_opacity, .03, 1, .01, '');
    h += '<div class="side-h">Edge</div>' + rng('Border', 'border.width', bd.width || 0, 0, 1.5, .05, 'mm') + color('Border colour', 'border.color', bd.color || '#d6dee8') + seg('Border style', 'border.style', bd.style || 'solid', { solid: 'Solid', dashed: 'Dashed', dotted: 'Dotted' }) + rng('Corners', 'border.radius', bd.radius || 0, 0, 8, .5, 'mm');
    h += '<small class="text-secondary d-block">Dashed borders double as cutting guides.</small>';
    return h;
  }

  function fits() {
    var p = cfg.page, pp = PAPERS[p.paper] || PAPERS.A4, W = pp[0], H = pp[1], m = +p.margin || 0, g = +p.gap || 0;
    var cols = Math.max(1, Math.floor((W - 2 * m + g) / (cfg.size.w + g)));
    if (!H) return { cols: cols, rows: '∞', per: 'continuous roll' };
    var rows = Math.max(0, Math.floor((H - 2 * m + g) / (cfg.size.h + g)));
    return { cols: cols, rows: rows, per: cols * rows };
  }
  function printProps() {
    var p = cfg.page, fi = fits(), po = { A4: 'A4 (210 × 297 mm)', Letter: 'US Letter', A5: 'A5 (148 × 210 mm)', thermal58: '58 mm receipt roll', thermal80: '80 mm receipt roll' };
    var h = '<div class="side-h">Paper</div>' + sel_('Paper', 'page.paper', p.paper, po);
    h += rng('Page margin', 'page.margin', p.margin == null ? 8 : p.margin, 0, 20, .5, 'mm') + rng('Gap between cards', 'page.gap', p.gap == null ? 3 : p.gap, 0, 10, .5, 'mm');
    h += '<label class="chk"><input type="checkbox" data-p="page.cut_marks"' + (p.cut_marks ? ' checked' : '') + '> Print cutting lines</label>';
    h += '<div class="fit-note"><b>' + (typeof fi.per === 'number' ? fi.per + ' vouchers per sheet' : 'One after another on the roll') + '</b>' + (typeof fi.per === 'number' ? ' · ' + fi.cols + ' across × ' + fi.rows + ' down' : '') + (fi.per === 0 ? '<br>The card is too big for this paper — pick a larger paper or a smaller card.' : '') + '</div>';
    h += '<div class="side-h">Test it</div><p class="small text-secondary">Print one sheet of example vouchers, hold it next to a real card, and check the size and colours before printing a big batch.</p><a class="btn btn-sm btn-light w-100" target="_blank" href="' + $('#testPrint').getAttribute('href') + '"><i class="bi bi-printer"></i> Open test sheet</a>';
    return h;
  }

  function setPath(o, path, v) { var p = path.split('.'); while (p.length > 1) { var k = p.shift(); o = o[k] = o[k] || {}; } o[p[0]] = v; }
  function readImage(file, cb) { var r = new FileReader(); r.onload = function () { var img = new Image(); img.onload = function () { var s = Math.min(1, 1100 / img.width), c = document.createElement('canvas'); c.width = img.width * s; c.height = img.height * s; c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); cb(c.toDataURL('image/jpeg', .85)); }; img.src = r.result; }; r.readAsDataURL(file); }

  function shrinkImage(file, maxPx, cb) {
    var r = new FileReader();
    r.onload = function () { var im = new Image(); im.onload = function () {
      var k = Math.min(1, maxPx / Math.max(im.width, im.height)), c = document.createElement('canvas'); c.width = Math.round(im.width * k); c.height = Math.round(im.height * k);
      var g = c.getContext('2d'); g.drawImage(im, 0, 0, c.width, c.height);
      var png = /png|gif|svg/.test(file.type), out = c.toDataURL(png ? 'image/png' : 'image/jpeg', .82);
      if (png && out.length > 350000) out = c.toDataURL('image/jpeg', .8);
      cb(out); }; im.src = r.result; };
    r.readAsDataURL(file);
  }
  window.TapShrinkImage = shrinkImage;
  function bind(p, target) {
    $$('[data-img]', p).forEach(function (inp) { inp.addEventListener('change', function () { var file = inp.files && inp.files[0]; if (!file) return; shrinkImage(file, 700, function (url) { target.src = url; commit(true); renderProps(); }); }); });
    $$('[data-imgclear]', p).forEach(function (b) { b.onclick = function () { target.src = ''; commit(true); renderProps(); }; });
    $$('[data-p]', p).forEach(function (inp) {
      var ev = inp.type === 'checkbox' || inp.tagName === 'SELECT' ? 'change' : 'input';
      inp.addEventListener(ev, function () {
        var k = inp.dataset.p, v = inp.type === 'checkbox' ? inp.checked : (inp.dataset.num ? parseFloat(inp.value) : inp.value);
        if (inp.dataset.num && isNaN(v)) return;
        setPath(target, k, v);
        var o = inp.parentElement.querySelector('output'); if (o) o.textContent = inp.value + (/radius|width|margin|gap|border_width/.test(k) ? 'mm' : k === 'spacing' ? 'pt' : k === 'bg.angle' ? '°' : '');
        if (inp.type === 'color') { var t = inp.parentElement.querySelector('input[type=text]'); if (t) t.value = inp.value; }
        if (inp.type === 'text' && /^#[0-9a-f]{6}$/i.test(inp.value)) { var c = inp.parentElement.querySelector('input[type=color]'); if (c) c.value = inp.value; }
        commit();
        if (inp.tagName === 'SELECT' && /^(bg\.|page\.|content)/.test(k)) renderProps();
        if (/^page\./.test(k) && rtab === 'print') { var n = $('.fit-note', p), fi = fits(); if (n && inp.type === 'range') n.innerHTML = '<b>' + (typeof fi.per === 'number' ? fi.per + ' vouchers per sheet' : 'One after another on the roll') + '</b>' + (typeof fi.per === 'number' ? ' · ' + fi.cols + ' across × ' + fi.rows + ' down' : ''); }
      });
    });
    $$('[data-seg]', p).forEach(function (g) { $$('button', g).forEach(function (b) { b.onclick = function () { var v = b.dataset.v; setPath(target, g.dataset.seg, g.dataset.seg === 'group' ? +v : v); commit(true); renderProps(); }; }); });
    $$('[data-clear]', p).forEach(function (b) { b.onclick = function () { setPath(target, b.dataset.clear, ''); commit(true); renderProps(); }; });
    $$('[data-tok]', p).forEach(function (b) { b.onclick = function () { var ta = $('textarea[data-p=text]', p), s = ta.selectionStart || ta.value.length; ta.value = ta.value.slice(0, s) + '{' + b.dataset.tok + '}' + ta.value.slice(ta.selectionEnd || s); ta.dispatchEvent(new Event('input')); ta.focus(); }; });
    $$('[data-al]', p).forEach(function (b) { b.onclick = function () { var e = el(sel), W = cfg.size.w, H = cfg.size.h, m = 3;
      ({ l: function () { e.x = m; }, c: function () { e.x = r1((W - e.w) / 2); }, r: function () { e.x = r1(W - e.w - m); }, t: function () { e.y = m; }, m: function () { e.y = r1((H - e.h) / 2); }, b: function () { e.y = r1(H - e.h - m); } })[b.dataset.al]();
      commit(true); renderProps(); }; });
    $$('[data-ord]', p).forEach(function (b) { b.onclick = function () { var o = b.dataset.ord; order(sel, o === 'top' || o === 'bottom' ? o : +o); }; });
    $$('[data-act]', p).forEach(function (b) { b.onclick = function () { if (b.dataset.act === 'dup') dup(sel); else removeEl(sel); }; });
    var sz = $('[data-size]', p); if (sz) sz.onchange = function () { if (sz.value !== 'custom') { var s = SIZES[sz.value]; cfg.size = { w: s[1], h: s[2], preset: sz.value }; if (/thermal/.test(sz.value)) cfg.page.paper = sz.value; } else cfg.size.preset = 'custom'; autoZoom(); commit(true); renderProps(); };
    $$('[data-sz]', p).forEach(function (i) { i.onchange = function () { var v = Math.max(15, Math.min(200, +i.value || 0)); cfg.size[i.dataset.sz] = v; cfg.size.preset = 'custom'; autoZoom(); commit(true); renderProps(); }; });
    var bi = $('[data-bgimg]', p); if (bi) bi.onchange = function () { if (bi.files[0]) readImage(bi.files[0], function (d) { cfg.bg.image = d; commit(true); renderProps(); }); };
  }

  /* ---------- boot ---------- */
  pushHist(); undoState(); defBtn(); autoZoom(); draw(); renderProps();
  var lastStage = '', rzT;
  window.addEventListener('resize', function () { clearTimeout(rzT); rzT = setTimeout(function () {
    var st = $('#stage'), key = st.clientWidth + 'x' + st.clientHeight; if (key === lastStage) return; lastStage = key;
    if (!userZoom && !drag) { var z = zoom; autoZoom(); if (z !== zoom) draw(); }
  }, 150); });
  if (document.fonts) document.fonts.ready.then(draw);
})();
