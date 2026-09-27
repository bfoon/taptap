/* TapTap Portal Studio editor */
(function () {
  var J = function (id) { return JSON.parse(document.getElementById(id).textContent); };
  var $ = function (s, r) { return (r || document).querySelector(s); }, $$ = function (s, r) { return [].slice.call((r || document).querySelectorAll(s)); };
  var ADS = document.getElementById('adsData') ? J('adsData') : {}; var PAGE = J('pageData'), BIZ = J('bizData'), PLANS = J('plansData'), FONTS = J('fontsData'), GALLERY = J('galleryData');
  var cfg = J('cfgData'); cfg.theme = cfg.theme || {}; cfg.blocks = cfg.blocks || []; cfg.settings = cfg.settings || {};
  var sel = null, rtab = 'style', dirty = false, published = PAGE.published, isDefault = PAGE.is_default, device = 'phone', frameReady = false;
  var hist = [], hi = -1;
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var uid = function (t) { return t + Math.random().toString(36).slice(2, 8); };
  var clone = function (o) { return JSON.parse(JSON.stringify(o)); };

  /* ---------- block catalogue ---------- */
  var ICONS = { info: 'Info', gift: 'Gift', bell: 'Bell', trophy: 'Trophy', 'cup-hot': 'Cup', warn: 'Warning', wifi: 'Wi-Fi', clock: 'Clock', lock: 'Lock', devices: 'Devices' };
  var BLOCKS = {
    logo: { n: 'Logo', i: 'bi-person-badge', def: { mode: 'image', size: 64, shape: 'circle' },
      f: [['mode', 'seg', 'Show', { image: 'Your logo', initials: 'Initials' }], ['size', 'range', 'Size', [32, 140, 2, 'px']], ['shape', 'seg', 'Shape', { circle: 'Circle', rounded: 'Rounded', square: 'Square' }]],
      note: 'Upload your logo once in Settings — every page uses it.' },
    heading: { n: 'Heading', i: 'bi-type-h1', def: { title: 'Welcome to {business}', subtitle: '', size: 'lg' },
      f: [['title', 'text', 'Title'], ['subtitle', 'textarea', 'Subtitle'], ['size', 'seg', 'Size', { sm: 'S', md: 'M', lg: 'L', xl: 'XL' }]], tokens: true },
    text: { n: 'Text', i: 'bi-text-paragraph', def: { text: 'Write a short message for your customers.', align: 'center' },
      f: [['text', 'textarea', 'Text'], ['align', 'seg', 'Align', { left: 'Left', center: 'Centre', right: 'Right' }]], tokens: true },
    voucher: { n: 'Voucher box', i: 'bi-ticket-perforated', kinds: ['login'], def: { label: 'Voucher code', placeholder: 'Enter code', button: 'Connect', style: 'single', length: 8, show_hint: true },
      f: [['label', 'text', 'Label'], ['placeholder', 'text', 'Placeholder'], ['button', 'text', 'Button text'], ['style', 'seg', 'Input', { single: 'One field', boxes: 'Letter boxes' }],
          ['length', 'range', 'Number of boxes', [4, 12, 1, ''], function (b) { return b.style === 'boxes'; }], ['show_hint', 'check', 'Show the “not case-sensitive” hint']] },
    plans: { n: 'Plans & prices', i: 'bi-tags', def: { title: 'Prices', style: 'cards', show_devices: true, highlight: '' },
      f: [['title', 'text', 'Title'], ['style', 'seg', 'Style', { cards: 'Cards', list: 'List', chips: 'Chips' }], ['show_devices', 'check', 'Show number of devices'],
          ['highlight', 'select', 'Mark as popular', function () { var o = { '': 'None' }; PLANS.forEach(function (p) { o[p.name] = p.name; }); return o; }]],
      note: 'Plans and prices come from your active voucher plans.' },
    notice: { n: 'Notice', i: 'bi-megaphone', def: { text: 'Happy hour: double time on every voucher 5–7pm.', tone: 'promo', icon: 'gift' },
      f: [['text', 'textarea', 'Message'], ['tone', 'seg', 'Look', { info: 'Info', promo: 'Promo', warning: 'Warning' }], ['icon', 'select', 'Icon', ICONS]], tokens: true },
    image: { n: 'Image', i: 'bi-image', def: { src: '', alt: '', radius: 14, link: '' },
      f: [['src', 'image', 'Image'], ['alt', 'text', 'Describe the image'], ['radius', 'range', 'Corner radius', [0, 40, 1, 'px']], ['link', 'url', 'Link when tapped (optional)']] },
    steps: { n: 'How-to steps', i: 'bi-list-ol', def: { title: 'How to connect', items: ['Buy a voucher', 'Type the code', 'Start browsing'] },
      f: [['title', 'text', 'Title'], ['items', 'lines', 'Steps, one per line']] },
    payment: { n: 'Mobile money', i: 'bi-phone-vibrate', def: { title: 'Pay with mobile money', methods: [{ name: 'Wave', detail: 'Send to 000 0000' }], note: '' },
      f: [['title', 'text', 'Title'], ['methods', 'pairs', 'Ways to pay'], ['note', 'text', 'Note']] },
    contact: { n: 'Contact', i: 'bi-telephone', def: { phone: '', whatsapp: '', email: '', hours: '' },
      f: [['phone', 'text', 'Phone (blank = business phone)'], ['whatsapp', 'text', 'WhatsApp number'], ['email', 'text', 'Email'], ['hours', 'text', 'Opening hours']] },
    social: { n: 'Social links', i: 'bi-share', def: { facebook: '', instagram: '', tiktok: '', whatsapp: '' },
      f: [['facebook', 'text', 'Facebook page name or link'], ['instagram', 'text', 'Instagram @handle'], ['tiktok', 'text', 'TikTok @handle'], ['whatsapp', 'text', 'WhatsApp number']] },
    button: { n: 'Button', i: 'bi-hand-index', def: { text: 'Continue', url: '', style: 'solid' },
      f: [['text', 'text', 'Label'], ['url', 'url', 'Link (blank = where the customer was going)'], ['style', 'seg', 'Style', { solid: 'Solid', outline: 'Outline' }]] },
    terms: { n: 'Terms checkbox', i: 'bi-check2-square', kinds: ['login'], def: { text: 'I agree to use this network fairly and legally.', required: true },
      f: [['text', 'textarea', 'Text'], ['required', 'check', 'Must be ticked to connect']] },
    ads: { n: 'Advert', i: 'bi-badge-ad', def: { style: 'card', label: 'Sponsored', rotate: 6, skip: 5 },
      f: [['style', 'seg', 'Show as', { card: 'Card', banner: 'Banner', carousel: 'Carousel', interstitial: 'Full screen' }], ['label', 'text', 'Small label above (blank = none)'],
          ['rotate', 'range', 'Change slide every', [3, 20, 1, 's'], function (b) { return b.style === 'carousel'; }],
          ['skip', 'range', 'Skip button appears after', [2, 15, 1, 's'], function (b) { return b.style === 'interstitial'; }]],
      note: 'Shows your live campaigns from Adverts for this kind of page, rotating by weight. Views and taps are counted.' },
    trial: { n: 'Free trial', i: 'bi-hourglass-top', kinds: ['login'], def: { text: 'Try free for 5 minutes', note: 'One free trial per phone per day.' },
      f: [['text', 'text', 'Button text'], ['note', 'text', 'Small print']],
      note: 'Uses the RouterOS HotSpot trial. Turn on “Trial” in IP › HotSpot › Server Profiles › Login and set the trial time there.' },
    faq: { n: 'Questions & answers', i: 'bi-question-circle', def: { title: 'Questions', items: [{ name: 'Where do I buy a voucher?', detail: 'At the counter or from our agents.' }, { name: 'Can I use it on two phones?', detail: 'Only if your plan allows more than one device.' }] },
      f: [['title', 'text', 'Title'], ['items', 'pairs', 'Question and answer']] },
    ticker: { n: 'News ticker', i: 'bi-broadcast', def: { items: ['Happy hour 5–7pm: double time on every voucher', 'New: weekly passes now available'], icon: 'bell' },
      f: [['items', 'lines', 'Messages, one per line'], ['icon', 'select', 'Icon', ICONS]], tokens: true },
    session: { n: 'Session info', i: 'bi-hourglass-split', kinds: ['redirect', 'status'], def: { show: ['time_left', 'uptime', 'data'] },
      f: [['show', 'multi', 'Show', { plan: 'Voucher', time_left: 'Time left', uptime: 'Online for', ip: 'IP address', mac: 'Device', data: 'Data used' }]],
      note: 'Filled in by the router. The preview shows example values.' },
    countdown: { n: 'Countdown', i: 'bi-stopwatch', kinds: ['redirect'], def: { text: 'Taking you on in', style: 'ring' },
      f: [['text', 'text', 'Text'], ['style', 'seg', 'Style', { ring: 'Ring', bar: 'Bar', number: 'Number', spinner: 'Spinner' }]],
      note: 'Set the delay and destination under Page.' },
    spacer: { n: 'Space', i: 'bi-arrows-expand', def: { size: 16 }, f: [['size', 'range', 'Height', [4, 80, 2, 'px']]] },
    footer: { n: 'Footer', i: 'bi-layout-text-window-reverse', def: { text: '© {business}' }, f: [['text', 'text', 'Text']], tokens: true }
  };
  function allowed(t) { var k = BLOCKS[t].kinds; return !k || k.indexOf(PAGE.kind) >= 0; }
  function summary(b) { var t = b.title || b.text || b.label || b.button || (b.items && b.items.join(', ')) || (b.methods && b.methods.map(function (m) { return m.name; }).join(', ')) || (b.src ? 'Image added' : '') || ''; return String(t).slice(0, 48); }

  /* ---------- theme presets ---------- */
  var PALETTES = [
    ['Ocean', { accent: '#1769e0', accent_text: '#ffffff', text: '#102033', muted: '#65758a', surface: '#ffffff', input_bg: '#f3f6fa', border: '#e3e9f1', bg: { type: 'gradient', color1: '#1769e0', color2: '#0b2237' } }],
    ['Sunset', { accent: '#ff6a3d', accent_text: '#ffffff', text: '#1a1f3d', muted: '#6b6f8c', surface: '#ffffff', input_bg: '#fff4ef', border: '#f3dcd2', bg: { type: 'gradient', color1: '#ff8a5b', color2: '#1b2a6b' } }],
    ['Mint tea', { accent: '#2f9e6a', accent_text: '#ffffff', text: '#15261d', muted: '#5b7063', surface: '#f7fbf6', input_bg: '#e9f3ec', border: '#d6e6da', bg: { type: 'gradient', color1: '#1f3b2d', color2: '#0f1f18' } }],
    ['Neon', { accent: '#ff2fb3', accent_text: '#ffffff', text: '#f3e9ff', muted: '#a992c9', surface: '#140a24', input_bg: '#221338', border: '#3a2360', bg: { type: 'gradient', color1: '#1a0633', color2: '#05010d' } }],
    ['Market', { accent: '#d7263d', accent_text: '#ffffff', text: '#111111', muted: '#4a4a4a', surface: '#ffffff', input_bg: '#fff7da', border: '#111111', bg: { type: 'solid', color1: '#ffd23f', color2: '#ffd23f' } }],
    ['Paper', { accent: '#000000', accent_text: '#ffffff', text: '#000000', muted: '#666666', surface: '#ffffff', input_bg: '#ffffff', border: '#000000', bg: { type: 'solid', color1: '#f4f4f0', color2: '#f4f4f0' } }],
    ['Pitch', { accent: '#ffe600', accent_text: '#0b1d12', text: '#ffffff', muted: '#a7c7b1', surface: '#0b1d12', input_bg: '#13301f', border: '#245a3a', bg: { type: 'gradient', color1: '#0d5c2e', color2: '#083a1d' } }],
    ['Lagoon', { accent: '#f2c14e', accent_text: '#1b1b1b', text: '#ffffff', muted: '#d8eef0', surface: '#ffffff', input_bg: 'rgba(255,255,255,.14)', border: 'rgba(255,255,255,.35)', bg: { type: 'gradient', color1: '#0e4d64', color2: '#137177' } }]
  ];
  var LAYOUTS = { card: 'Centred card', split: 'Split screen', full: 'Full bleed', poster: 'Poster', ticket: 'Ticket', sheet: 'Bottom sheet', bands: 'Flag bands' };
  var PATTERNS = { none: 'None', waves: 'Waves', dots: 'Dots', grid: 'Grid', stripes: 'Stripes', kente: 'Kente', leaves: 'Leaves', palms: 'Palms', pitch: 'Football pitch' };

  /* ---------- preview frame ---------- */
  var frame = $('#pv');
  frame.srcdoc = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><script src="' + PAGE.renderer + '"><\/script></head><body><div id="tp"></div><script>' +
    'var root=document.getElementById("tp");addEventListener("message",function(e){var d=e.data||{};if(d.tp==="render"){var y=scrollY;TapPortal.render(root,d.cfg,d.ctx);if(d.sel)TapPortal.select(root,d.sel);scrollTo(0,y);}if(d.tp==="sel")TapPortal.select(root,d.id);});parent.postMessage({tp:"ready"},"*");<\/script></body></html>';
  window.addEventListener('message', function (e) {
    var d = e.data || {};
    if (e.source !== frame.contentWindow) return;
    if (d.tp === 'ready') { frameReady = true; paint(); }
    if (d.tp === 'select') { select(d.id); }
  });
  var paintT;
  function paint() { clearTimeout(paintT); paintT = setTimeout(function () { if (!frameReady) return; frame.contentWindow.postMessage({ tp: 'render', cfg: cfg, ctx: { mode: 'preview', kind: PAGE.kind, business: BIZ, plans: PLANS, mt: {}, ads: ADS }, sel: sel }, '*'); }, 40); }

  var SIZES = { phone: [390, 780], tablet: [768, 1024], desktop: [1280, 800] };
  function fit() {
    var st = $('#stage'), sz = SIZES[device], dev = $('#device'), wrap = $('#devWrap');
    var aw = st.clientWidth - 40, ah = st.clientHeight - 56, sc = Math.min(1, aw / sz[0], device === 'phone' ? Math.max(.55, ah / sz[1]) : aw / sz[0]);
    dev.style.width = sz[0] + 'px'; dev.style.height = sz[1] + 'px'; dev.style.transform = 'scale(' + sc + ')'; dev.className = 'device ' + device;
    wrap.style.width = sz[0] * sc + 'px'; wrap.style.height = sz[1] * sc + 'px';
  }
  window.addEventListener('resize', fit);
  $$('.dev-toggle button').forEach(function (b) { b.onclick = function () { device = b.dataset.dev; $$('.dev-toggle button').forEach(function (x) { x.classList.toggle('on', x === b); }); fit(); }; });

  /* ---------- history + save ---------- */
  var histT;
  function commit(immediate) {
    dirty = true; status('dirty'); paint(); renderBlocks();
    clearTimeout(histT); histT = setTimeout(pushHist, immediate ? 0 : 350);
    clearTimeout(saveT); saveT = setTimeout(save, 2500);
  }
  function pushHist() { var snap = JSON.stringify(cfg); if (hist[hi] === snap) return; hist = hist.slice(0, hi + 1); hist.push(snap); if (hist.length > 80) hist.shift(); hi = hist.length - 1; undoState(); }
  function undoState() { $('#undo').disabled = hi <= 0; $('#redo').disabled = hi >= hist.length - 1; }
  function restore(i) { hi = i; cfg = JSON.parse(hist[hi]); if (sel && !cfg.blocks.some(function (b) { return b.id === sel; })) sel = null; dirty = true; status('dirty'); paint(); renderBlocks(); renderProps(); undoState(); clearTimeout(saveT); saveT = setTimeout(save, 2500); }
  $('#undo').onclick = function () { if (hi > 0) restore(hi - 1); };
  $('#redo').onclick = function () { if (hi < hist.length - 1) restore(hi + 1); };

  var saveT, saving = false;
  function status(k, msg) { var s = $('#status'); s.className = 'ed-status ' + (k || ''); s.textContent = msg || { dirty: 'Unsaved changes', saving: 'Saving…', err: 'Could not save' }[k] || 'All changes saved'; }
  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ''; }
  function save(extra) {
    if (saving) { clearTimeout(saveT); saveT = setTimeout(save, 800); return Promise.resolve(); }
    saving = true; status('saving');
    var body = Object.assign({ config: cfg, name: $('#pgName').value.trim() }, extra || {});
    return fetch(location.pathname, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok || !d.success) throw new Error(d.message || 'Save failed'); return d; }); })
      .then(function (d) { dirty = false; published = d.is_published; isDefault = d.is_default; pubBtn(); status('', 'Saved at ' + d.updated_at); })
      .catch(function (e) { status('err', e.message); })
      .then(function () { saving = false; });
  }
  $('#saveBtn').onclick = function () { clearTimeout(saveT); save(); };
  $('#pgName').addEventListener('input', function () { dirty = true; status('dirty'); clearTimeout(saveT); saveT = setTimeout(save, 1500); });
  function pubBtn() { var b = $('#publishBtn'); b.innerHTML = published ? '<i class="bi bi-broadcast"></i> Live' + (isDefault ? ' · default' : '') : '<i class="bi bi-broadcast"></i> Publish'; b.className = 'btn btn-sm ' + (published ? 'btn-success' : 'btn-outline-primary'); b.title = published ? 'Customers can open this page. Click to unpublish.' : 'Make this page reachable by customers'; }
  $('#publishBtn').onclick = function () { clearTimeout(saveT); save({ is_published: !published }); };
  window.addEventListener('beforeunload', function (e) { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
  document.addEventListener('keydown', function (e) {
    var inField = /INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName);
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); clearTimeout(saveT); save(); }
    if (inField) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); (e.shiftKey ? $('#redo') : $('#undo')).click(); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); $('#redo').click(); }
    if ((e.key === 'Delete' || e.key === 'Backspace') && sel) { e.preventDefault(); removeBlock(sel); }
  });

  /* ---------- blocks list ---------- */
  function renderBlocks() {
    var box = $('#blocks');
    box.innerHTML = cfg.blocks.map(function (b) {
      var d = BLOCKS[b.type] || { n: b.type, i: 'bi-square' };
      return '<div class="blk' + (b.id === sel ? ' sel' : '') + (b.hidden ? ' off' : '') + '" draggable="true" data-id="' + esc(b.id) + '" tabindex="0" role="button" aria-label="' + esc(d.n) + '">' +
        '<i class="bi bi-grip-vertical grip" aria-hidden="true"></i><span class="ico"><i class="bi ' + d.i + '"></i></span><span class="nm"><b>' + esc(d.n) + '</b><small>' + esc(summary(b)) + '</small></span>' +
        '<button class="act" data-a="hide" title="' + (b.hidden ? 'Show' : 'Hide') + '" aria-label="' + (b.hidden ? 'Show' : 'Hide') + ' block"><i class="bi bi-eye' + (b.hidden ? '-slash' : '') + '"></i></button>' +
        '<button class="act" data-a="dup" title="Duplicate" aria-label="Duplicate block"><i class="bi bi-copy"></i></button>' +
        '<button class="act" data-a="del" title="Remove" aria-label="Remove block"><i class="bi bi-x-lg"></i></button></div>';
    }).join('') || '<div class="empty-props">No blocks yet. Add one below.</div>';
    $$('.blk', box).forEach(function (el) {
      var id = el.dataset.id;
      el.onclick = function (e) { var a = e.target.closest('[data-a]'); if (!a) return select(id);
        e.stopPropagation(); var i = idx(id);
        if (a.dataset.a === 'hide') { cfg.blocks[i].hidden = !cfg.blocks[i].hidden; commit(true); }
        if (a.dataset.a === 'dup') { var c = clone(cfg.blocks[i]); c.id = uid(c.type); cfg.blocks.splice(i + 1, 0, c); sel = c.id; commit(true); renderProps(); }
        if (a.dataset.a === 'del') removeBlock(id); };
      el.onkeydown = function (e) { if (e.key === 'Enter') select(id); if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) { e.preventDefault(); move(id, e.key === 'ArrowUp' ? -1 : 1); } };
      el.ondragstart = function (e) { e.dataTransfer.setData('text/plain', id); el.style.opacity = .4; };
      el.ondragend = function () { el.style.opacity = ''; };
      el.ondragover = function (e) { e.preventDefault(); el.classList.add('drag-over'); };
      el.ondragleave = function () { el.classList.remove('drag-over'); };
      el.ondrop = function (e) { e.preventDefault(); el.classList.remove('drag-over'); var from = idx(e.dataTransfer.getData('text/plain')), to = idx(id); if (from < 0 || from === to) return; var b = cfg.blocks.splice(from, 1)[0]; cfg.blocks.splice(from < to ? to - 1 : to, 0, b); commit(true); };
    });
  }
  function idx(id) { for (var i = 0; i < cfg.blocks.length; i++) if (cfg.blocks[i].id === id) return i; return -1; }
  function move(id, d) { var i = idx(id), j = i + d; if (j < 0 || j >= cfg.blocks.length) return; var t = cfg.blocks[i]; cfg.blocks[i] = cfg.blocks[j]; cfg.blocks[j] = t; commit(true); var el = $('.blk[data-id="' + id + '"]'); if (el) el.focus(); }
  function removeBlock(id) { var i = idx(id); if (i < 0) return; cfg.blocks.splice(i, 1); if (sel === id) { sel = null; rtab = 'style'; renderProps(); } commit(true); }
  function select(id) { sel = id; rtab = 'block'; renderBlocks(); renderProps(); if (frameReady) frame.contentWindow.postMessage({ tp: 'sel', id: id }, '*'); }

  $('#addGrid').innerHTML = Object.keys(BLOCKS).filter(allowed).map(function (t) { return '<button data-t="' + t + '"><i class="bi ' + BLOCKS[t].i + '"></i>' + BLOCKS[t].n + '</button>'; }).join('');
  $$('#addGrid button').forEach(function (b) { b.onclick = function () {
    var t = b.dataset.t, nb = Object.assign({ id: uid(t), type: t, hidden: false }, clone(BLOCKS[t].def));
    var i = sel ? idx(sel) + 1 : cfg.blocks.length; var foot = cfg.blocks.length && cfg.blocks[cfg.blocks.length - 1].type === 'footer';
    if (!sel && foot && t !== 'footer') i = cfg.blocks.length - 1;
    cfg.blocks.splice(i, 0, nb); sel = nb.id; rtab = 'block'; commit(true); renderProps();
  }; });

  /* left tabs */
  $$('[data-lt]').forEach(function (b) { b.onclick = function () { $$('[data-lt]').forEach(function (x) { x.classList.toggle('on', x === b); }); $$('[data-lp]').forEach(function (p) { p.hidden = p.dataset.lp !== b.dataset.lt; }); if (b.dataset.lt === 'templates') thumbs(); }; });
  function thumbs() {
    var box = $('#tplList'); if (box.dataset.done) return; box.dataset.done = 1;
    box.innerHTML = GALLERY.map(function (g, i) { return '<button data-i="' + i + '"><div class="t-thumb"><iframe tabindex="-1" title=""></iframe></div><b>' + esc(g.label) + '</b></button>'; }).join('');
    $$('button', box).forEach(function (b) {
      var g = GALLERY[+b.dataset.i], f = $('iframe', b), c = JSON.stringify(g.config).replace(/</g, '\\u003c'), x = JSON.stringify({ mode: 'thumb', business: BIZ, plans: PLANS }).replace(/</g, '\\u003c');
      f.style.setProperty('--s', ((f.parentElement.clientWidth || 120) / 390).toFixed(4));
      f.srcdoc = '<!doctype html><html><head><meta charset="utf-8"><script src="' + PAGE.renderer + '"><\/script></head><body><div id="tp"></div><script>TapPortal.render(document.getElementById("tp"),' + c + ',' + x + ')<\/script></body></html>';
      b.onclick = function () { if (!confirm('Switch to “' + g.label + '”? Your current blocks and style will be replaced. You can undo this.')) return; var keep = cfg.settings; cfg = clone(g.config); cfg.settings = Object.assign({}, cfg.settings, keep); sel = null; rtab = 'style'; commit(true); renderProps(); };
    });
  }

  /* ---------- property panel ---------- */
  $$('[data-rt]').forEach(function (b) { b.onclick = function () { rtab = b.dataset.rt; renderProps(); }; });
  function field(label, html, id) { return '<div class="fld">' + (label ? '<label' + (id ? ' for="' + id + '"' : '') + '>' + label + '</label>' : '') + html + '</div>'; }
  function colorField(label, key, val) { var hex = /^#[0-9a-f]{6}$/i.test(val || '') ? val : '#000000'; return field(label, '<div class="color-in"><input type="color" data-k="' + key + '" value="' + hex + '" aria-label="' + label + ' picker"><input type="text" data-k="' + key + '" value="' + esc(val) + '" aria-label="' + label + '"></div>'); }
  function rangeField(label, key, val, min, max, step, unit) { return field(label, '<div class="rng"><input type="range" data-k="' + key + '" data-num="1" min="' + min + '" max="' + max + '" step="' + step + '" value="' + val + '" aria-label="' + label + '"><output>' + val + (unit || '') + '</output></div>'); }
  function selectField(label, key, val, opts) { return field(label, '<select data-k="' + key + '" aria-label="' + label + '">' + Object.keys(opts).map(function (k) { return '<option value="' + esc(k) + '"' + (String(val) === k ? ' selected' : '') + '>' + esc(opts[k]) + '</option>'; }).join('') + '</select>'); }
  function segField(label, key, val, opts) { return field(label, '<div class="seg-in" data-seg="' + key + '">' + Object.keys(opts).map(function (k) { return '<button type="button" data-v="' + esc(k) + '" class="' + (String(val) === k ? 'on' : '') + '">' + esc(opts[k]) + '</button>'; }).join('') + '</div>'); }

  function renderProps() {
    $$('[data-rt]').forEach(function (x) { x.classList.toggle('on', x.dataset.rt === rtab); });
    var p = $('#props'), html = '';
    if (rtab === 'block') html = blockProps();
    else if (rtab === 'style') html = styleProps();
    else html = pageProps();
    p.innerHTML = html; bind(p);
  }

  function blockProps() {
    var b = cfg.blocks[idx(sel)]; if (!b) return '<div class="empty-props"><i class="bi bi-hand-index fs-3 d-block mb-2"></i>Click a block in the preview or in the list to edit it.</div>';
    var d = BLOCKS[b.type], h = '<div class="side-h">' + d.n + '</div>';
    d.f.forEach(function (f) {
      var k = f[0], t = f[1], lab = f[2], v = b[k];
      if (f[4] && !f[4](b)) return;
      if (t === 'text' || t === 'url') h += field(lab, '<input type="' + (t === 'url' ? 'url' : 'text') + '" data-bk="' + k + '" value="' + esc(v) + '">');
      else if (t === 'textarea') h += field(lab, '<textarea data-bk="' + k + '">' + esc(v) + '</textarea>');
      else if (t === 'seg') h += field(lab, '<div class="seg-in" data-bseg="' + k + '">' + Object.keys(f[3]).map(function (o) { return '<button type="button" data-v="' + o + '" class="' + (String(v) === o ? 'on' : '') + '">' + f[3][o] + '</button>'; }).join('') + '</div>');
      else if (t === 'select') { var o = typeof f[3] === 'function' ? f[3]() : f[3]; h += field(lab, '<select data-bk="' + k + '">' + Object.keys(o).map(function (x) { return '<option value="' + esc(x) + '"' + (String(v) === x ? ' selected' : '') + '>' + esc(o[x]) + '</option>'; }).join('') + '</select>'); }
      else if (t === 'range') h += field(lab, '<div class="rng"><input type="range" data-bk="' + k + '" data-num="1" min="' + f[3][0] + '" max="' + f[3][1] + '" step="' + f[3][2] + '" value="' + (v || f[3][0]) + '"><output>' + v + f[3][3] + '</output></div>');
      else if (t === 'check') h += '<label class="chk"><input type="checkbox" data-bk="' + k + '"' + (v ? ' checked' : '') + '> ' + lab + '</label>';
      else if (t === 'lines') h += field(lab, '<textarea data-bk="' + k + '" data-lines="1" rows="4">' + esc((v || []).join('\n')) + '</textarea>');
      else if (t === 'multi') h += field(lab, Object.keys(f[3]).map(function (o) { return '<label class="chk"><input type="checkbox" data-multi="' + k + '" value="' + o + '"' + ((v || []).indexOf(o) >= 0 ? ' checked' : '') + '> ' + f[3][o] + '</label>'; }).join(''));
      else if (t === 'pairs') h += field(lab, '<div class="list-ed">' + (v || []).map(function (m, i) { return '<div class="li"><input type="text" data-pair="' + i + '" data-pk="name" value="' + esc(m.name) + '" placeholder="Name" aria-label="Method name"><input type="text" data-pair="' + i + '" data-pk="detail" value="' + esc(m.detail) + '" placeholder="Details" aria-label="Method details"><button class="rm" data-rm="' + i + '" aria-label="Remove"><i class="bi bi-x-lg"></i></button></div>'; }).join('') + '<button class="btn btn-sm btn-light" data-addpair="1"><i class="bi bi-plus"></i> Add</button></div>');
      else if (t === 'image') h += field(lab, (v ? '<img class="upload-prev" src="' + esc(v) + '" alt="">' : '') + '<label class="upload-drop"><input type="file" accept="image/*" data-img="' + k + '" hidden><i class="bi bi-upload"></i> ' + (v ? 'Replace image' : 'Upload an image') + '</label>' + (v ? '<button class="btn btn-link btn-sm text-danger p-0 mt-1" data-clearimg="' + k + '">Remove image</button>' : '') + '<small class="text-secondary d-block mt-1">Resized automatically so the page stays fast.</small>');
    });
    if (d.tokens) h += '<div class="small text-secondary">Use <code>{business}</code>, <code>{ssid}</code> or <code>{phone}</code> to insert your details.</div>';
    if (d.note) h += '<div class="fit-note mt-2">' + d.note + '</div>';
    h += '<div class="d-flex gap-2 mt-3"><button class="btn btn-sm btn-light" data-mv="-1"><i class="bi bi-arrow-up"></i> Up</button><button class="btn btn-sm btn-light" data-mv="1"><i class="bi bi-arrow-down"></i> Down</button><button class="btn btn-sm btn-outline-danger ms-auto" data-delsel="1"><i class="bi bi-trash"></i> Remove</button></div>';
    return h;
  }

  function styleProps() {
    var t = cfg.theme, bg = t.bg || (t.bg = {}), h = '';
    h += '<div class="side-h">Quick palettes</div><div class="swatches">' + PALETTES.map(function (p, i) { return '<button data-pal="' + i + '" title="' + p[0] + '" aria-label="' + p[0] + ' palette" style="background:linear-gradient(135deg,' + p[1].bg.color1 + ' 50%,' + p[1].accent + ' 50%)"></button>'; }).join('') +
      '<button data-pal="brand" title="Use my brand colour" aria-label="Use my brand colour" style="background:' + esc(BIZ.brand) + '"><i class="bi bi-star-fill text-white" style="font-size:.7rem"></i></button></div>';
    h += '<div class="side-h">Layout</div>' + selectField('Arrangement', 'layout', t.layout, LAYOUTS) + segField('Text alignment', 'align', t.align, { center: 'Centre', left: 'Left' });
    h += rangeField('Content width', 'width', t.width || 420, 320, 620, 10, 'px') + rangeField('Corner roundness', 'radius', t.radius == null ? 20 : t.radius, 0, 40, 1, 'px');
    h += selectField('Shadow', 'shadow', t.shadow, { none: 'None', soft: 'Soft', strong: 'Strong', hard: 'Hard offset', glow: 'Glow' }) + segField('Entrance', 'animation', t.animation, { none: 'None', rise: 'Rise', fade: 'Fade' });
    h += '<div class="side-h">Type</div>' + selectField('Body font', 'font', t.font, FONTS) + selectField('Heading font', 'heading_font', t.heading_font, FONTS);
    h += '<div class="side-h">Colours</div>' + colorField('Button & accent', 'accent', t.accent) + colorField('Button text', 'accent_text', t.accent_text) + colorField('Text', 'text', t.text) + colorField('Secondary text', 'muted', t.muted) +
      colorField('Card', 'surface', t.surface) + rangeField('Card opacity', 'surface_opacity', t.surface_opacity == null ? 1 : t.surface_opacity, 0, 1, .02, '') + colorField('Input background', 'input_bg', t.input_bg) + colorField('Borders', 'border', t.border);
    h += '<div class="side-h">Background</div>' + segField('Type', 'bg.type', bg.type, { solid: 'Colour', gradient: 'Gradient', image: 'Photo' }) + colorField(bg.type === 'gradient' ? 'From' : 'Colour', 'bg.color1', bg.color1);
    if (bg.type === 'gradient') h += colorField('To', 'bg.color2', bg.color2) + rangeField('Angle', 'bg.angle', bg.angle || 150, 0, 360, 5, '°');
    if (bg.type === 'image') h += field('Photo', (bg.image ? '<img class="upload-prev" src="' + esc(bg.image) + '" alt="">' : '') + '<label class="upload-drop"><input type="file" accept="image/*" data-bgimg="1" hidden><i class="bi bi-upload"></i> ' + (bg.image ? 'Replace photo' : 'Upload a photo') + '</label>') + rangeField('Darken photo', 'bg.overlay', bg.overlay || 0, 0, .8, .05, '');
    h += selectField('Pattern', 'bg.pattern', bg.pattern || 'none', PATTERNS);
    if (bg.pattern && bg.pattern !== 'none') h += rangeField('Pattern strength', 'bg.pattern_opacity', bg.pattern_opacity == null ? .12 : bg.pattern_opacity, .03, 1, .01, '');
    return h;
  }

  function pageProps() {
    var s = cfg.settings, h = '<div class="side-h">Where customers find it</div>';
    h += field('Public link', '<div class="d-flex gap-1"><input type="text" readonly value="' + esc(PAGE.public_url) + '" id="pubUrl" aria-label="Public link"><button class="btn btn-sm btn-light" id="copyUrl" aria-label="Copy link"><i class="bi bi-clipboard"></i></button></div>');
    h += '<p class="small text-secondary">' + (published ? 'This page is live.' : 'Draft — only you can open this link until you publish.') + '</p>';
    if (!isDefault) h += '<button class="btn btn-sm btn-outline-primary w-100 mb-2" id="mkDefault"><i class="bi bi-star"></i> Make this my default ' + (PAGE.kind === 'login' ? 'login' : PAGE.kind) + ' page</button>';
    if (PAGE.kind === 'login') {
      h += '<div class="side-h">After login</div>' + field('Send customers to', '<input type="url" data-sk="redirect_url" value="' + esc(s.redirect_url || '') + '" placeholder="Blank = the site they were trying to open">');
      h += '<label class="chk"><input type="checkbox" data-sk="case_sensitive"' + (s.case_sensitive ? ' checked' : '') + '> Codes are case-sensitive</label><small class="text-secondary d-block">Leave off for TapTap codes. Turn on only if your router has lowercase codes.</small>';
      h += '<div class="side-h">Device identification</div><label class="chk"><input type="checkbox" data-sk="collect_device"' + (s.collect_device !== false ? ' checked' : '') + '> Recognise returning devices</label>';
      h += '<small class="text-secondary d-block mb-2">Records a device signature (model, screen, browser traits) with the MAC and voucher so you can spot shared vouchers and phones that change MAC. See <a href="/devices/">Devices</a>.</small>';
      h += field('Notice shown under the voucher box', '<input type="text" data-sk="device_notice" value="' + esc(s.device_notice == null ? 'We note basic details of your device to keep your voucher safe from misuse.' : s.device_notice) + '" placeholder="Leave empty to hide">');
    } else {
      h += '<div class="side-h">Redirect</div>' + field('Destination', '<input type="url" data-sk="redirect_url" value="' + esc(s.redirect_url || '') + '" placeholder="Blank = the site they were trying to open">');
      h += field('Wait before continuing', '<div class="rng"><input type="range" data-sk="redirect_delay" data-num="1" min="0" max="30" step="1" value="' + (s.redirect_delay == null ? 5 : s.redirect_delay) + '"><output>' + (s.redirect_delay == null ? 5 : s.redirect_delay) + 's</output></div>');
    }
    h += '<div class="side-h">Put it on your router</div><a class="btn btn-sm btn-light w-100 mb-2 text-start" href="' + location.pathname + 'export/?mode=offline"><i class="bi bi-router"></i> Download router files</a><a class="btn btn-sm btn-light w-100 text-start" href="' + location.pathname + 'export/?mode=hosted"><i class="bi bi-cloud"></i> Download hosted redirect</a>';
    h += '<p class="small text-secondary mt-2">Each download includes step-by-step install instructions for WinBox.</p>';
    return h;
  }

  function setPath(obj, path, val) { var p = path.split('.'); while (p.length > 1) { var k = p.shift(); obj = obj[k] = obj[k] || {}; } obj[p[0]] = val; }
  function readImage(file, maxW, cb) {
    var r = new FileReader(); r.onload = function () { var img = new Image(); img.onload = function () {
      var sc = Math.min(1, maxW / img.width), c = document.createElement('canvas'); c.width = Math.round(img.width * sc); c.height = Math.round(img.height * sc);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height); var png = /png|gif|svg/.test(file.type);
      var out = c.toDataURL(png ? 'image/png' : 'image/jpeg', .82); if (png && out.length > 350000) out = c.toDataURL('image/jpeg', .82); cb(out); }; img.src = r.result; }; r.readAsDataURL(file);
  }

  function bind(p) {
    var b = cfg.blocks[idx(sel)];
    // theme fields
    $$('[data-k]', p).forEach(function (el) {
      var ev = el.type === 'range' || el.type === 'color' || el.type === 'text' ? 'input' : 'change';
      el.addEventListener(ev, function () {
        var v = el.dataset.num ? +el.value : el.value; setPath(cfg.theme, el.dataset.k, v);
        var o = el.parentElement.querySelector('output'); if (o) o.textContent = el.value + (/(width|radius)/.test(el.dataset.k) ? 'px' : (el.dataset.k === 'bg.angle' ? '°' : ''));
        if (el.type === 'color') el.parentElement.querySelector('input[type=text]').value = el.value;
        if (el.type === 'text' && /^#[0-9a-f]{6}$/i.test(el.value)) el.parentElement.querySelector('input[type=color]').value = el.value;
        commit(); if (el.tagName === 'SELECT' && /bg\./.test(el.dataset.k)) renderProps();
      });
    });
    $$('[data-seg]', p).forEach(function (g) { $$('button', g).forEach(function (bt) { bt.onclick = function () { setPath(cfg.theme, g.dataset.seg, bt.dataset.v); commit(); renderProps(); }; }); });
    $$('[data-pal]', p).forEach(function (bt) { bt.onclick = function () {
      if (bt.dataset.pal === 'brand') { cfg.theme.accent = BIZ.brand; cfg.theme.bg = Object.assign({}, cfg.theme.bg, cfg.theme.bg.type === 'solid' ? {} : { color1: BIZ.brand }); }
      else { var pal = clone(PALETTES[+bt.dataset.pal][1]); var bgp = pal.bg; delete pal.bg; Object.assign(cfg.theme, pal); cfg.theme.bg = Object.assign({}, cfg.theme.bg, bgp, { type: cfg.theme.bg.type === 'image' ? 'image' : bgp.type }); cfg.theme.surface_opacity = PALETTES[+bt.dataset.pal][0] === 'Lagoon' ? .16 : 1; }
      commit(true); renderProps(); }; });
    var bgi = $('[data-bgimg]', p); if (bgi) bgi.onchange = function () { if (bgi.files[0]) readImage(bgi.files[0], 1600, function (d) { cfg.theme.bg.image = d; commit(true); renderProps(); }); };
    // block fields
    if (b) {
      $$('[data-bk]', p).forEach(function (el) {
        el.addEventListener(el.type === 'checkbox' || el.tagName === 'SELECT' ? 'change' : 'input', function () {
          var k = el.dataset.bk, v = el.type === 'checkbox' ? el.checked : (el.dataset.num ? +el.value : el.value);
          if (el.dataset.lines) v = el.value.split('\n').map(function (x) { return x.trim(); }).filter(Boolean);
          b[k] = v; var o = el.parentElement.querySelector('output'); if (o) o.textContent = el.value + (k === 'size' || k === 'radius' ? 'px' : '');
          commit(); if (k === 'style') renderProps();
        });
      });
      $$('[data-bseg]', p).forEach(function (g) { $$('button', g).forEach(function (bt) { bt.onclick = function () { b[g.dataset.bseg] = bt.dataset.v; commit(); renderProps(); }; }); });
      $$('[data-multi]', p).forEach(function (el) { el.onchange = function () { b[el.dataset.multi] = $$('[data-multi="' + el.dataset.multi + '"]:checked', p).map(function (x) { return x.value; }); commit(); }; });
      $$('[data-pair]', p).forEach(function (el) { el.oninput = function () { b.methods[+el.dataset.pair][el.dataset.pk] = el.value; commit(); }; });
      $$('[data-rm]', p).forEach(function (el) { el.onclick = function () { b.methods.splice(+el.dataset.rm, 1); commit(true); renderProps(); }; });
      var ap = $('[data-addpair]', p); if (ap) ap.onclick = function () { (b.methods = b.methods || []).push({ name: 'QMoney', detail: 'Send to 000 0000' }); commit(true); renderProps(); };
      $$('[data-img]', p).forEach(function (el) { el.onchange = function () { if (el.files[0]) readImage(el.files[0], 1000, function (d) { b[el.dataset.img] = d; commit(true); renderProps(); }); }; });
      $$('[data-clearimg]', p).forEach(function (el) { el.onclick = function () { b[el.dataset.clearimg] = ''; commit(true); renderProps(); }; });
      $$('[data-mv]', p).forEach(function (el) { el.onclick = function () { move(sel, +el.dataset.mv); }; });
      var ds = $('[data-delsel]', p); if (ds) ds.onclick = function () { removeBlock(sel); };
    }
    // page settings
    $$('[data-sk]', p).forEach(function (el) { el.addEventListener(el.type === 'checkbox' ? 'change' : 'input', function () { cfg.settings[el.dataset.sk] = el.type === 'checkbox' ? el.checked : (el.dataset.num ? +el.value : el.value.trim()); var o = el.parentElement.querySelector('output'); if (o) o.textContent = el.value + 's'; commit(); }); });
    var cp = $('#copyUrl', p); if (cp) cp.onclick = function () { navigator.clipboard && navigator.clipboard.writeText(PAGE.public_url); cp.innerHTML = '<i class="bi bi-check2"></i>'; setTimeout(function () { cp.innerHTML = '<i class="bi bi-clipboard"></i>'; }, 1500); };
    var md = $('#mkDefault', p); if (md) md.onclick = function () { clearTimeout(saveT); save({ is_default: true }).then(renderProps); };
  }

  /* ---------- boot ---------- */
  pushHist(); undoState(); pubBtn(); renderBlocks(); renderProps(); fit();
  setTimeout(fit, 50);
})();
