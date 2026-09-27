/* TapTap voucher card renderer — millimetre-accurate so screen and paper match.
 * Needs qrcode-generator (global `qrcode`) for QR elements.
 */
(function (global) {
  var FONTS = {
    system: 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif', outfit: '"Outfit",system-ui,sans-serif', sora: '"Sora",system-ui,sans-serif',
    grotesk: '"Space Grotesk",system-ui,sans-serif', bricolage: '"Bricolage Grotesque",system-ui,sans-serif', syne: '"Syne",system-ui,sans-serif',
    archivo: '"Archivo Black","Arial Black",sans-serif', fraunces: '"Fraunces",Georgia,serif', dmserif: '"DM Serif Display",Georgia,serif',
    nunito: '"Nunito",system-ui,sans-serif', rubik: '"Rubik",system-ui,sans-serif', mono: '"JetBrains Mono",ui-monospace,monospace',
    caveat: '"Caveat","Comic Sans MS",cursive', bebas: '"Bebas Neue",Impact,sans-serif'
  };
  var ICONS = {
    wifi: '<path d="M2 8.5a15 15 0 0 1 20 0"/><path d="M5 12a10.5 10.5 0 0 1 14 0"/><path d="M8.5 15.5a5.5 5.5 0 0 1 7 0"/><circle cx="12" cy="19" r="1.2" fill="currentColor"/>',
    phone: '<path d="M5 3h4l2 5-2.5 1.5a11 11 0 0 0 6 6L16 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 5a2 2 0 0 1 2-2z"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', devices: '<rect x="2" y="5" width="14" height="10" rx="1.5"/><path d="M6 19h6"/><rect x="17" y="9" width="5" height="11" rx="1"/>',
    star: '<path d="M12 3l2.8 5.8 6.2.9-4.5 4.4 1 6.3L12 17.5 6.5 20.4l1-6.3L3 9.7l6.2-.9z"/>', bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M8.5 7.5L20 18M8.5 16.5L20 6"/>', lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    cup: '<path d="M4 10h13v5a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z"/><path d="M17 12h1.5a2.5 2.5 0 0 1 0 5H17"/>', ball: '<circle cx="12" cy="12" r="9"/><path d="M12 7l4 3-1.5 5h-5L8 10z"/>'
  };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function isDark(hex) { if (!hex || hex.charAt(0) !== '#') return false; var n = hex.slice(1); if (n.length === 3) n = n.replace(/./g, '$&$&'); var r = parseInt(n.slice(0, 2), 16), g = parseInt(n.slice(2, 4), 16), b = parseInt(n.slice(4, 6), 16); return (r * 299 + g * 587 + b * 114) / 1000 < 140; }
  function pattern(name, c) {
    var s = {
      waves: '<svg xmlns="http://www.w3.org/2000/svg" width="60" height="20"><path d="M0 10 Q7.5 2 15 10 T30 10 T45 10 T60 10" fill="none" stroke="' + c + '" stroke-width="1.2"/></svg>',
      dots: '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12"><circle cx="2" cy="2" r="1" fill="' + c + '"/></svg>',
      stripes: '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path d="M-2 2l4-4M0 10L10 0M8 12l4-4" stroke="' + c + '" stroke-width="1.5"/></svg>',
      grid: '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14"><path d="M14 0H0V14" fill="none" stroke="' + c + '" stroke-width=".6"/></svg>',
      palms: '<svg xmlns="http://www.w3.org/2000/svg" width="70" height="70"><g fill="none" stroke="' + c + '" stroke-width="1.2" stroke-linecap="round"><path d="M35 65 Q37 45 35 30"/><path d="M35 30 Q25 22 12 26M35 30 Q30 19 20 15M35 30 Q39 18 49 15M35 30 Q46 23 59 27"/></g></svg>',
      pitch: '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><g fill="none" stroke="' + c + '" stroke-width="1.2"><rect x="5" y="5" width="90" height="90"/><path d="M5 50H95"/><circle cx="50" cy="50" r="14"/></g></svg>',
      kente: '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="5" fill="#d7263d"/><rect y="5" width="32" height="2" fill="#111"/><rect y="7" width="32" height="3" fill="#1b998b"/><rect y="22" width="32" height="3" fill="#1b998b"/><rect y="25" width="32" height="2" fill="#111"/><rect y="27" width="32" height="5" fill="#d7263d"/></svg>',
      circuit: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><g fill="none" stroke="' + c + '" stroke-width="1"><path d="M0 10h14l6 6v24M40 26H26l-4-4V0"/><circle cx="20" cy="16" r="2"/><circle cx="22" cy="22" r="2"/></g></svg>'
    }[name];
    return s ? 'url("data:image/svg+xml;utf8,' + encodeURIComponent(s) + '")' : '';
  }

  function fill(text, d) { return String(text || '').replace(/\{(\w+)\}/g, function (m, k) { return d[k] != null ? d[k] : m; }); }
  function groupCode(code, g) { g = +g || 0; if (!g || g >= code.length || code.length % g !== 0 || code.length < 8) return code; var out = []; for (var i = 0; i < code.length; i += g) out.push(code.slice(i, i + g)); return out.join(' '); }
  function loginLink(d) {
    var u = String(d.login_url || '').trim(); if (!u) return '';
    if (!/^https?:\/\//i.test(u)) u = 'http://' + u;
    if (!/\/login\b/.test(u)) u = u.replace(/\/$/, '') + '/login';
    return u + (u.indexOf('?') < 0 ? '?' : '&') + 'username=' + encodeURIComponent(d.code) + '&password=' + encodeURIComponent(d.code);
  }
  function qrSvg(text, color, bg, margin) {
    if (!global.qrcode) return '<svg viewBox="0 0 10 10"><rect width="10" height="10" fill="#ddd"/></svg>';
    var q = global.qrcode(0, 'M'); q.addData(text || ' '); q.make();
    var n = q.getModuleCount(), m = +margin || 0, size = n + m * 2, p = '';
    for (var r = 0; r < n; r++) for (var c = 0; c < n; c++) if (q.isDark(r, c)) p += 'M' + (c + m) + ' ' + (r + m) + 'h1v1h-1z';
    return '<svg viewBox="0 0 ' + size + ' ' + size + '" width="100%" height="100%" shape-rendering="crispEdges" preserveAspectRatio="xMidYMid meet"><rect width="' + size + '" height="' + size + '" fill="' + (bg || '#fff') + '"/><path d="' + p + '" fill="' + (color || '#000') + '"/></svg>';
  }
  function initials(name) { return String(name || 'W').split(/\s+/).filter(Boolean).slice(0, 2).map(function (w) { return w.charAt(0); }).join('').toUpperCase(); }

  function element(e, cfg, d, editable) {
    var el = document.createElement('div');
    el.className = 'tv-el tv-' + e.type; if (editable) el.setAttribute('data-eid', e.id);
    var st = 'position:absolute;left:' + e.x + 'mm;top:' + e.y + 'mm;width:' + e.w + 'mm;height:' + e.h + 'mm;opacity:' + (e.opacity == null ? 1 : e.opacity) + ';';
    var font = FONTS[e.font] || FONTS[cfg.font] || FONTS.system;
    if (e.type === 'text' || e.type === 'code') {
      var txt = fill(e.text, d); if (e.type === 'code') txt = groupCode(txt, e.group);
      st += 'display:flex;align-items:center;justify-content:' + ({ left: 'flex-start', center: 'center', right: 'flex-end' }[e.align] || 'flex-start') + ';text-align:' + (e.align || 'left') + ';';
      st += 'font-family:' + font + ';font-size:' + e.size + 'pt;font-weight:' + e.weight + ';color:' + e.color + ';letter-spacing:' + (e.spacing || 0) + 'pt;line-height:1.15;white-space:' + (e.type === 'code' ? 'nowrap' : 'normal') + ';overflow:hidden;';
      if (e.upper) st += 'text-transform:uppercase;'; if (e.italic) st += 'font-style:italic;';
      if (e.bg) st += 'background:' + e.bg + ';'; if (e.radius) st += 'border-radius:' + e.radius + 'mm;';
      if (e.type === 'code') st += 'font-variant-numeric:tabular-nums;' + (e.border_width ? 'border:' + e.border_width + 'mm solid ' + e.border + ';' : '');
      el.innerHTML = '<span style="display:block;width:100%">' + esc(txt) + '</span>';
    } else if (e.type === 'qr') {
      var mode = e.content || cfg.qr_mode || 'login', data = mode === 'code' ? d.code : (mode === 'wifi' ? 'WIFI:S:' + (d.ssid || '') + ';T:nopass;;' : (loginLink(d) || d.code));
      el.innerHTML = qrSvg(data, e.color, e.bg, e.margin);
    } else if (e.type === 'rect' || e.type === 'ellipse') {
      st += 'background:' + (e.fill || 'transparent') + ';border-radius:' + (e.type === 'ellipse' ? '50%' : (e.radius || 0) + 'mm') + ';' + (e.stroke_width ? 'border:' + e.stroke_width + 'mm solid ' + e.stroke + ';' : '');
    } else if (e.type === 'line') {
      var vert = e.h > e.w; st += (vert ? 'border-left:' : 'border-top:') + e.stroke_width + 'mm ' + (e.dash || 'solid') + ' ' + e.stroke + ';' + (vert ? 'width:0;' : 'height:0;');
    } else if (e.type === 'logo') {
      var rad = e.shape === 'circle' ? '50%' : (e.shape === 'rounded' ? '22%' : '0');
      st += 'border-radius:' + rad + ';overflow:hidden;display:grid;place-items:center;background:' + (d.logo ? 'transparent' : e.bg) + ';color:' + e.color + ';font-family:' + font + ';font-weight:800;font-size:' + (Math.min(e.w, e.h) * 1.15) + 'pt;';
      el.innerHTML = d.logo ? '<img src="' + esc(d.logo) + '" style="width:100%;height:100%;object-fit:cover" alt="">' : esc(initials(d.business));
    } else if (e.type === 'icon') {
      st += 'color:' + e.color + ';';
      el.innerHTML = '<svg viewBox="0 0 24 24" width="100%" height="100%" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' + (ICONS[e.icon] || ICONS.wifi) + '</svg>';
    }
    el.style.cssText = st;
    return el;
  }

  function renderCard(cfg, d, opts) {
    opts = opts || {};
    var W = cfg.size.w, H = cfg.size.h, bg = cfg.bg || {}, bd = cfg.border || {};
    var card = document.createElement('div'); card.className = 'tv-card';
    var back = bg.type === 'gradient' ? 'linear-gradient(' + (bg.angle || 135) + 'deg,' + bg.color1 + ',' + (bg.color2 || bg.color1) + ')' : (bg.type === 'image' && bg.image ? 'url("' + bg.image + '") center/cover' : bg.color1);
    card.style.cssText = 'position:relative;overflow:hidden;width:' + W + 'mm;height:' + H + 'mm;background:' + back + ';border-radius:' + (bd.radius || 0) + 'mm;' +
      (bd.width ? 'box-shadow:inset 0 0 0 ' + bd.width + 'mm ' + bd.color + ';' : '') + 'font-family:' + (FONTS[cfg.font] || FONTS.system) + ';-webkit-print-color-adjust:exact;print-color-adjust:exact;';
    if (bd.width && bd.style && bd.style !== 'solid') card.style.cssText += 'box-shadow:none;border:' + bd.width + 'mm ' + bd.style + ' ' + bd.color + ';box-sizing:border-box;';
    if (bg.pattern && bg.pattern !== 'none') {
      var p = document.createElement('div'); p.style.cssText = 'position:absolute;inset:0;pointer-events:none;background-image:' + pattern(bg.pattern, isDark(bg.color1) ? '#ffffff' : '#000000') + ';background-size:' + (bg.pattern === 'kente' ? '10mm' : 'auto') + ';opacity:' + (bg.pattern_opacity == null ? .12 : bg.pattern_opacity);
      card.appendChild(p);
    }
    (cfg.elements || []).forEach(function (e) { if (!e.hidden) card.appendChild(element(e, cfg, d, opts.editable)); });
    return card;
  }

  function sample(extra) {
    var d = { business: 'Kairaba Wi-Fi', plan: '24 Hours', price: '40', currency: 'D', duration: '1 day', devices: '1 device', speed: '5 Mbps', data: 'Unlimited',
      code: 'K7Q2M9XP', serial: '000128', ssid: 'Kairaba-WiFi', login_url: 'wifi.local', phone: '+220 700 0000', batch: 'Batch 12', created: new Date().toLocaleDateString(), logo: '' };
    for (var k in (extra || {})) if (extra[k] != null && extra[k] !== '') d[k] = extra[k];
    return d;
  }

  global.TapVoucher = { renderCard: renderCard, sample: sample, fill: fill, FONTS: FONTS, ICONS: Object.keys(ICONS) };
})(window);
