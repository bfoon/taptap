/* TapTap Portal renderer.
 * One renderer for three places: the Studio preview (iframe), the hosted page
 * (/p/<slug>/) and the self-contained login.html/alogin.html exported to a
 * MikroTik router. No dependencies, no strict mode (MikroTik CHAP values are
 * octal-escaped string literals).
 */
(function (global) {
  var FONT_STACKS = {
    system: 'system-ui,-apple-system,"Segoe UI",Roboto,sans-serif', outfit: '"Outfit",system-ui,sans-serif', sora: '"Sora",system-ui,sans-serif',
    grotesk: '"Space Grotesk",system-ui,sans-serif', bricolage: '"Bricolage Grotesque",system-ui,sans-serif', syne: '"Syne",system-ui,sans-serif',
    archivo: '"Archivo Black","Arial Black",sans-serif', fraunces: '"Fraunces",Georgia,serif', dmserif: '"DM Serif Display",Georgia,serif',
    nunito: '"Nunito",system-ui,sans-serif', rubik: '"Rubik",system-ui,sans-serif', mono: '"JetBrains Mono",ui-monospace,monospace',
    caveat: '"Caveat","Comic Sans MS",cursive', bebas: '"Bebas Neue",Impact,sans-serif'
  };
  var FONT_QUERY = {
    outfit: 'Outfit:wght@400;600;800', sora: 'Sora:wght@400;600;800', grotesk: 'Space+Grotesk:wght@400;600;700',
    bricolage: 'Bricolage+Grotesque:opsz,wght@12..96,400;12..96,700;12..96,800', syne: 'Syne:wght@500;700;800', archivo: 'Archivo+Black',
    fraunces: 'Fraunces:opsz,wght@9..144,400;9..144,700', dmserif: 'DM+Serif+Display', nunito: 'Nunito:wght@400;700;900',
    rubik: 'Rubik:wght@400;600;800', mono: 'JetBrains+Mono:wght@400;700;800', caveat: 'Caveat:wght@500;700', bebas: 'Bebas+Neue'
  };

  /* ---------- tiny helpers ---------- */
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function h(tag, attrs, html) { var e = document.createElement(tag); if (attrs) for (var k in attrs) { if (k === 'class') e.className = attrs[k]; else if (k === 'style') e.style.cssText = attrs[k]; else e.setAttribute(k, attrs[k]); } if (html != null) e.innerHTML = html; return e; }
  function hexA(hex, a) { if (!hex || hex.charAt(0) !== '#') return hex; var n = hex.slice(1); if (n.length === 3) n = n.replace(/./g, '$&$&'); var r = parseInt(n.slice(0, 2), 16), g = parseInt(n.slice(2, 4), 16), b = parseInt(n.slice(4, 6), 16); return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')'; }
  function isDark(hex) { if (!hex || hex.charAt(0) !== '#') return false; var n = hex.slice(1); if (n.length === 3) n = n.replace(/./g, '$&$&'); var r = parseInt(n.slice(0, 2), 16), g = parseInt(n.slice(2, 4), 16), b = parseInt(n.slice(4, 6), 16); return (r * 299 + g * 587 + b * 114) / 1000 < 140; }
  // Plans carry exact minutes; designs saved before minute plans only have hours.
  function durOf(p) { return p.minutes != null ? +p.minutes : (+p.hours || 0) * 60; }
  function dur(m) { m = +m || 0; if (m <= 0) return 'Unlimited'; if (m % 43200 === 0) return (m / 43200) + (m === 43200 ? ' month' : ' months'); if (m % 1440 === 0) { var d = m / 1440; if (d % 7 === 0 && d < 28) return (d / 7) + (d === 7 ? ' week' : ' weeks'); return d + (d === 1 ? ' day' : ' days'); } if (m % 60 === 0) return (m / 60) + ' h'; if (m < 60) return m + ' min'; return Math.floor(m / 60) + ' h ' + (m % 60) + ' min'; }
  function money(ctx, v, free) { var n = +v || 0; if (free && !n) return 'Free'; return (ctx.business.currency || 'D') + (n % 1 ? n.toFixed(2) : n.toLocaleString()); }

  /* ---------- MD5 for MikroTik CHAP (compact, public-domain style) ---------- */
  function md5(str) {
    function rh(n) { var s = '', j; for (j = 0; j <= 3; j++) s += ((n >> (j * 8 + 4)) & 15).toString(16) + ((n >> (j * 8)) & 15).toString(16); return s; }
    function ad(x, y) { var l = (x & 0xFFFF) + (y & 0xFFFF); return (((x >> 16) + (y >> 16) + (l >> 16)) << 16) | (l & 0xFFFF); }
    function rl(n, c) { return (n << c) | (n >>> (32 - c)); }
    function cm(q, a, b, x, s, t) { return ad(rl(ad(ad(a, q), ad(x, t)), s), b); }
    function ff(a, b, c, d, x, s, t) { return cm((b & c) | ((~b) & d), a, b, x, s, t); }
    function gg(a, b, c, d, x, s, t) { return cm((b & d) | (c & (~d)), a, b, x, s, t); }
    function hh(a, b, c, d, x, s, t) { return cm(b ^ c ^ d, a, b, x, s, t); }
    function ii(a, b, c, d, x, s, t) { return cm(c ^ (b | (~d)), a, b, x, s, t); }
    var nb = ((str.length + 8) >> 6) + 1, x = new Array(nb * 16), i;
    for (i = 0; i < nb * 16; i++) x[i] = 0;
    for (i = 0; i < str.length; i++) x[i >> 2] |= (str.charCodeAt(i) & 0xFF) << ((i % 4) * 8);
    x[i >> 2] |= 0x80 << ((i % 4) * 8); x[nb * 16 - 2] = str.length * 8;
    var a = 1732584193, b = -271733879, c = -1732584194, d = 271733878;
    for (i = 0; i < x.length; i += 16) {
      var oa = a, ob = b, oc = c, od = d;
      a = ff(a, b, c, d, x[i], 7, -680876936); d = ff(d, a, b, c, x[i + 1], 12, -389564586); c = ff(c, d, a, b, x[i + 2], 17, 606105819); b = ff(b, c, d, a, x[i + 3], 22, -1044525330);
      a = ff(a, b, c, d, x[i + 4], 7, -176418897); d = ff(d, a, b, c, x[i + 5], 12, 1200080426); c = ff(c, d, a, b, x[i + 6], 17, -1473231341); b = ff(b, c, d, a, x[i + 7], 22, -45705983);
      a = ff(a, b, c, d, x[i + 8], 7, 1770035416); d = ff(d, a, b, c, x[i + 9], 12, -1958414417); c = ff(c, d, a, b, x[i + 10], 17, -42063); b = ff(b, c, d, a, x[i + 11], 22, -1990404162);
      a = ff(a, b, c, d, x[i + 12], 7, 1804603682); d = ff(d, a, b, c, x[i + 13], 12, -40341101); c = ff(c, d, a, b, x[i + 14], 17, -1502002290); b = ff(b, c, d, a, x[i + 15], 22, 1236535329);
      a = gg(a, b, c, d, x[i + 1], 5, -165796510); d = gg(d, a, b, c, x[i + 6], 9, -1069501632); c = gg(c, d, a, b, x[i + 11], 14, 643717713); b = gg(b, c, d, a, x[i], 20, -373897302);
      a = gg(a, b, c, d, x[i + 5], 5, -701558691); d = gg(d, a, b, c, x[i + 10], 9, 38016083); c = gg(c, d, a, b, x[i + 15], 14, -660478335); b = gg(b, c, d, a, x[i + 4], 20, -405537848);
      a = gg(a, b, c, d, x[i + 9], 5, 568446438); d = gg(d, a, b, c, x[i + 14], 9, -1019803690); c = gg(c, d, a, b, x[i + 3], 14, -187363961); b = gg(b, c, d, a, x[i + 8], 20, 1163531501);
      a = gg(a, b, c, d, x[i + 13], 5, -1444681467); d = gg(d, a, b, c, x[i + 2], 9, -51403784); c = gg(c, d, a, b, x[i + 7], 14, 1735328473); b = gg(b, c, d, a, x[i + 12], 20, -1926607734);
      a = hh(a, b, c, d, x[i + 5], 4, -378558); d = hh(d, a, b, c, x[i + 8], 11, -2022574463); c = hh(c, d, a, b, x[i + 11], 16, 1839030562); b = hh(b, c, d, a, x[i + 14], 23, -35309556);
      a = hh(a, b, c, d, x[i + 1], 4, -1530992060); d = hh(d, a, b, c, x[i + 4], 11, 1272893353); c = hh(c, d, a, b, x[i + 7], 16, -155497632); b = hh(b, c, d, a, x[i + 10], 23, -1094730640);
      a = hh(a, b, c, d, x[i + 13], 4, 681279174); d = hh(d, a, b, c, x[i], 11, -358537222); c = hh(c, d, a, b, x[i + 3], 16, -722521979); b = hh(b, c, d, a, x[i + 6], 23, 76029189);
      a = hh(a, b, c, d, x[i + 9], 4, -640364487); d = hh(d, a, b, c, x[i + 12], 11, -421815835); c = hh(c, d, a, b, x[i + 15], 16, 530742520); b = hh(b, c, d, a, x[i + 2], 23, -995338651);
      a = ii(a, b, c, d, x[i], 6, -198630844); d = ii(d, a, b, c, x[i + 7], 10, 1126891415); c = ii(c, d, a, b, x[i + 14], 15, -1416354905); b = ii(b, c, d, a, x[i + 5], 21, -57434055);
      a = ii(a, b, c, d, x[i + 12], 6, 1700485571); d = ii(d, a, b, c, x[i + 3], 10, -1894986606); c = ii(c, d, a, b, x[i + 10], 15, -1051523); b = ii(b, c, d, a, x[i + 1], 21, -2054922799);
      a = ii(a, b, c, d, x[i + 8], 6, 1873313359); d = ii(d, a, b, c, x[i + 15], 10, -30611744); c = ii(c, d, a, b, x[i + 6], 15, -1560198380); b = ii(b, c, d, a, x[i + 13], 21, 1309151649);
      a = ii(a, b, c, d, x[i + 4], 6, -145523070); d = ii(d, a, b, c, x[i + 11], 10, -1120210379); c = ii(c, d, a, b, x[i + 2], 15, 718787259); b = ii(b, c, d, a, x[i + 9], 21, -343485551);
      a = ad(a, oa); b = ad(b, ob); c = ad(c, oc); d = ad(d, od);
    }
    return rh(a) + rh(b) + rh(c) + rh(d);
  }

  /* ---------- icons (24×24 stroke) ---------- */
  var ICONS = {
    wifi: '<path d="M2 8.5a15 15 0 0 1 20 0"/><path d="M5 12a10.5 10.5 0 0 1 14 0"/><path d="M8.5 15.5a5.5 5.5 0 0 1 7 0"/><circle cx="12" cy="19" r="1.2" fill="currentColor"/>',
    phone: '<path d="M5 3h4l2 5-2.5 1.5a11 11 0 0 0 6 6L16 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 5a2 2 0 0 1 2-2z"/>',
    whatsapp: '<path d="M3.5 20.5l1.3-4.2A8.5 8.5 0 1 1 8 19.3z"/><path d="M9 8.5c0 3 2.5 6 6 6.5l1-1.6-2-1-1 .8a4 4 0 0 1-2.2-2.2l.8-1-1-2z"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    facebook: '<path d="M14 8h3V4h-3a4 4 0 0 0-4 4v3H7v4h3v6h4v-6h3l1-4h-4V8z"/>',
    instagram: '<rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="1" fill="currentColor"/>',
    tiktok: '<path d="M14 3v11.5a3.5 3.5 0 1 1-3.5-3.5"/><path d="M14 3c.5 3 2.5 5 5.5 5"/>',
    gift: '<rect x="3" y="8" width="18" height="4"/><path d="M5 12v9h14v-9M12 8v13M12 8c-1-4-6-4-6-1s6 1 6 1zm0 0c1-4 6-4 6-1s-6 1-6 1z"/>',
    bell: '<path d="M6 16V11a6 6 0 0 1 12 0v5l2 2H4z"/><path d="M10 20a2 2 0 0 0 4 0"/>',
    trophy: '<path d="M7 4h10v5a5 5 0 0 1-10 0z"/><path d="M7 6H4a3 3 0 0 0 3 4M17 6h3a3 3 0 0 1-3 4M12 14v4M8 21h8"/>',
    'cup-hot': '<path d="M4 10h13v5a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5z"/><path d="M17 12h1.5a2.5 2.5 0 0 1 0 5H17M8 3c0 1.5 1 1.5 1 3M12 3c0 1.5 1 1.5 1 3"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    qr: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zM20 14v1M14 20h1M17 17h4v4h-4"/>',
    warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
    check: '<path d="M4 12.5l5 5L20 6.5"/>', lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>', devices: '<rect x="2" y="5" width="14" height="10" rx="1.5"/><path d="M6 19h6"/><rect x="17" y="9" width="5" height="11" rx="1"/>'
  };
  function icon(name, size) { return '<svg class="tp-ico" width="' + (size || 18) + '" height="' + (size || 18) + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || ICONS.info) + '</svg>'; }

  /* ---------- background patterns ---------- */
  function pattern(name, color, op) {
    var c = color, s;
    switch (name) {
      case 'waves': s = '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40"><path d="M0 20 Q15 5 30 20 T60 20 T90 20 T120 20" fill="none" stroke="' + c + '" stroke-width="2"/></svg>'; break;
      case 'dots': s = '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22"><circle cx="3" cy="3" r="1.6" fill="' + c + '"/></svg>'; break;
      case 'grid': s = '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28"><path d="M28 0H0V28" fill="none" stroke="' + c + '" stroke-width="1"/></svg>'; break;
      case 'stripes': s = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><path d="M-4 4l8-8M0 16L16 0M12 20l8-8" stroke="' + c + '" stroke-width="3"/></svg>'; break;
      case 'kente': s = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="10" fill="#d7263d"/><rect y="10" width="64" height="4" fill="#111"/><rect y="14" width="64" height="6" fill="#1b998b"/><rect x="0" y="20" width="16" height="24" fill="#111"/><rect x="32" y="20" width="16" height="24" fill="#d7263d"/><rect y="44" width="64" height="4" fill="#111"/><rect y="48" width="64" height="6" fill="#1b998b"/><rect y="54" width="64" height="10" fill="#d7263d"/></svg>'; break;
      case 'leaves': s = '<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60"><path d="M10 50 Q10 20 40 10 Q40 40 10 50z M10 50 L34 18" fill="none" stroke="' + c + '" stroke-width="1.6"/></svg>'; break;
      case 'palms': s = '<svg xmlns="http://www.w3.org/2000/svg" width="140" height="140"><g fill="none" stroke="' + c + '" stroke-width="2" stroke-linecap="round"><path d="M70 130 Q74 90 70 60"/><path d="M70 60 Q50 45 25 52M70 60 Q60 38 40 30M70 60 Q78 36 98 30M70 60 Q92 46 118 54M70 60 Q70 40 70 25"/></g></svg>'; break;
      case 'pitch': s = '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><g fill="none" stroke="' + c + '" stroke-width="2"><rect x="10" y="10" width="180" height="180"/><path d="M10 100H190"/><circle cx="100" cy="100" r="28"/><rect x="60" y="10" width="80" height="34"/><rect x="60" y="156" width="80" height="34"/></g></svg>'; break;
      default: return '';
    }
    return 'url("data:image/svg+xml;utf8,' + encodeURIComponent(s) + '")';
  }

  /* ---------- CSS ---------- */

  var EXTRA_CSS = '.tp-ad{position:relative}.tp-ad-in{display:block;text-decoration:none;color:inherit;border-radius:var(--tp-r,16px);overflow:hidden;border:1px solid rgba(127,127,127,.18);background:rgba(127,127,127,.06)}' +
    '.tp-ad-img{display:block;width:100%;height:auto;max-height:260px;object-fit:cover}.tp-ad-banner .tp-ad-img{max-height:120px}' +
    '.tp-ad-txt{display:flex;flex-wrap:wrap;align-items:center;gap:.25rem .6rem;padding:.7rem .85rem}.tp-ad-txt b{flex-basis:100%;font-size:1rem}.tp-ad-txt span{flex-basis:100%;font-size:.86rem;opacity:.85}' +
    '.tp-ad-cta{font-style:normal;font-weight:800;font-size:.8rem;margin-top:.2rem;padding:.35rem .75rem;border-radius:999px;background:rgba(127,127,127,.18)}' +
    '.tp-ad-lab{display:block;font-size:.66rem;letter-spacing:.06em;text-transform:uppercase;opacity:.6;margin:0 0 .3rem}' +
    '.tp-ad-empty{display:flex;gap:.6rem;align-items:center;border:1.5px dashed rgba(127,127,127,.4);border-radius:14px;padding:.8rem;font-size:.82rem;opacity:.8}' +
    '.tp-ad-car{position:relative}.tp-ad-car .tp-ad{display:none}.tp-ad-car .tp-ad.on{display:block;animation:tpFade .5s}.tp-ad-dots{display:flex;justify-content:center;gap:6px;margin-top:.5rem}' +
    '.tp-ad-dots i{width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.25;cursor:pointer}.tp-ad-dots i.on{opacity:.8}@keyframes tpFade{from{opacity:0}to{opacity:1}}' +
    '.tp-ad-over{position:fixed;inset:0;z-index:50;background:rgba(5,10,20,.78);display:grid;place-items:center;padding:1rem}.tp-ad-box{width:min(420px,100%);background:#fff;color:#102033;border-radius:18px;padding:.8rem;box-shadow:0 20px 60px rgba(0,0,0,.4)}' +
    '.tp-ad-skip{display:block;margin:.7rem auto 0;border:0;border-radius:999px;padding:.5rem 1.1rem;font-weight:800;background:#102033;color:#fff}.tp-ad-skip:disabled{opacity:.55}' +
    '.tp-trial{text-align:center}.tp-faq details{border-bottom:1px solid rgba(127,127,127,.2);padding:.55rem 0}.tp-faq summary{cursor:pointer;font-weight:700}.tp-faq p{margin:.4rem 0 0;opacity:.8;font-size:.9rem}' +
    '.tp-ticker{display:flex;align-items:center;gap:.5rem;border-radius:12px;padding:.45rem .7rem;background:rgba(127,127,127,.1);overflow:hidden}.tp-tk-w{overflow:hidden;flex:1}' +
    '.tp-tk-r{display:inline-flex;white-space:nowrap;animation:tpTick var(--tk,20s) linear infinite}.tp-tk-r span{padding-right:2.5rem}.tp-tk-r i{font-style:normal;opacity:.5;margin:0 .7rem}' +
    '@keyframes tpTick{from{transform:translateX(0)}to{transform:translateX(-50%)}}@media(prefers-reduced-motion:reduce){.tp-tk-r{animation:none;white-space:normal}.tp-tk-r span+span{display:none}}' +
    '.tp-privacy{font-size:.72rem;opacity:.6;margin:.5rem 0 0;text-align:center}';

  function css(t) {
    var bg = t.bg || {}, F = FONT_STACKS[t.font] || FONT_STACKS.system, HF = FONT_STACKS[t.heading_font] || F;
    var patColor = isDark(bg.color1) ? '#ffffff' : '#000000';
    var layers = [];
    var pat = bg.pattern && bg.pattern !== 'none' ? pattern(bg.pattern, patColor) : '';
    var base = bg.type === 'gradient' ? 'linear-gradient(' + (bg.angle || 150) + 'deg,' + bg.color1 + ',' + (bg.color2 || bg.color1) + ')' : (bg.type === 'image' && bg.image ? 'url("' + bg.image + '") center/cover no-repeat' : bg.color1);
    var overlay = bg.type === 'image' && bg.image ? 'linear-gradient(rgba(0,0,0,' + (bg.overlay || 0) + '),rgba(0,0,0,' + (bg.overlay || 0) + ')),' : '';
    var surf = t.surface_opacity < 1 ? hexA(t.surface, t.surface_opacity) : t.surface;
    var sh = { none: 'none', soft: '0 24px 60px -18px rgba(9,20,40,.35)', strong: '0 30px 80px -10px rgba(0,0,0,.55)', hard: '8px 8px 0 ' + t.text, glow: '0 0 0 1px ' + hexA(t.accent, .35) + ',0 0 60px -6px ' + hexA(t.accent, .55) }[t.shadow || 'soft'];
    var r = +t.radius || 0, al = t.align === 'left' ? 'left' : 'center';
    return [
      '*,*::before,*::after{box-sizing:border-box}html,body{margin:0;min-height:100%}',
      '.tp{min-height:100vh;min-height:100dvh;font-family:' + F + ';color:' + t.text + ';background:' + overlay + base + ';position:relative;display:flex;align-items:center;justify-content:center;padding:28px 16px;-webkit-font-smoothing:antialiased;overflow-x:hidden}',
      pat ? '.tp::before{content:"";position:absolute;inset:0;background-image:' + pat + ';opacity:' + (bg.pattern_opacity == null ? .12 : bg.pattern_opacity) + ';pointer-events:none}' : '',
      '.tp-card{position:relative;width:100%;max-width:' + (t.width || 420) + 'px;background:' + surf + ';border-radius:' + r + 'px;padding:30px 26px;box-shadow:' + sh + ';text-align:' + al + (t.surface_opacity < 1 ? ';-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px)' : '') + '}',
      '.tp-card>*+*{margin-top:18px}',
      '.tp h1,.tp h2{font-family:' + HF + ';margin:0;line-height:1.08;letter-spacing:-.01em}',
      '.tp-h-sm h1{font-size:20px}.tp-h-md h1{font-size:26px}.tp-h-lg h1{font-size:32px}.tp-h-xl h1{font-size:42px}',
      '.tp-sub{margin:8px 0 0;color:' + t.muted + ';font-size:15px;line-height:1.45}',
      '.tp-logo{display:inline-grid;place-items:center;background:' + t.accent + ';color:' + t.accent_text + ';font-weight:800;overflow:hidden;font-family:' + HF + '}',
      '.tp-logo img{width:100%;height:100%;object-fit:contain}.tp-logo.img{display:inline-flex;box-sizing:border-box;background:#fff;box-shadow:0 4px 14px rgba(0,0,0,.12)}.tp-logo.img img{display:block;flex:1;min-width:0}',
      '.tp-label{display:block;font-size:13px;font-weight:600;color:' + t.muted + ';margin-bottom:8px;text-align:' + al + '}',
      '.tp-input{width:100%;font:inherit;font-size:20px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;padding:14px 16px;border-radius:' + Math.max(4, r * .55) + 'px;border:1.5px solid ' + t.border + ';background:' + t.input_bg + ';color:' + t.text + ';outline:none;text-align:center;transition:border-color .15s,box-shadow .15s}',
      '.tp-input::placeholder{color:' + hexA(isDark(t.surface) ? '#ffffff' : '#000000', .3) + ';letter-spacing:.06em;text-transform:none}',
      '.tp-input:focus{border-color:' + t.accent + ';box-shadow:0 0 0 4px ' + hexA(t.accent, .18) + '}',
      '.tp-boxes{position:relative;display:flex;gap:6px;justify-content:' + (al === 'left' ? 'flex-start' : 'center') + '}',
      '.tp-boxes span{flex:1 1 0;max-width:44px;height:54px;border-radius:' + Math.max(4, r * .4) + 'px;border:1.5px solid ' + t.border + ';background:' + t.input_bg + ';display:grid;place-items:center;font-size:22px;font-weight:800;text-transform:uppercase;transition:border-color .12s,transform .12s}',
      '.tp-boxes span.on{border-color:' + t.accent + ';transform:translateY(-2px)}',
      '.tp-boxes input{position:absolute;inset:0;opacity:0;width:100%;font-size:16px;text-transform:uppercase;caret-color:transparent}',
      '.tp-btn{display:flex;gap:8px;align-items:center;justify-content:center;width:100%;margin-top:12px;border:0;cursor:pointer;font:inherit;font-weight:700;font-size:16px;padding:15px 18px;border-radius:' + Math.max(4, r * .55) + 'px;background:' + t.accent + ';color:' + t.accent_text + ';text-decoration:none;transition:transform .12s,filter .12s}',
      '.tp-btn:hover{filter:brightness(1.06)}.tp-btn:active{transform:scale(.98)}.tp-btn:focus-visible{outline:3px solid ' + hexA(t.accent, .45) + ';outline-offset:2px}',
      '.tp-btn.outline{background:transparent;color:' + t.text + ';border:1.5px solid ' + t.border + '}',
      '.tp-btn[disabled]{opacity:.6;cursor:wait}',
      '.tp-hint{font-size:12.5px;color:' + t.muted + ';margin:10px 0 0}',
      /* Scan the QR printed on the voucher (camera photo → decoded on the phone) */
      '.tp-scan{display:flex;width:100%;align-items:center;justify-content:center;gap:8px;margin-top:10px;padding:11px 14px;font:inherit;font-weight:700;font-size:14.5px;cursor:pointer;border-radius:' + Math.max(4, r * .55) + 'px;border:1.5px dashed ' + hexA(t.accent, .6) + ';background:' + hexA(t.accent, .07) + ';color:' + t.accent + '}',
      '.tp-scan:hover{background:' + hexA(t.accent, .13) + '}.tp-scan:focus-visible{outline:3px solid ' + hexA(t.accent, .45) + ';outline-offset:2px}.tp-scan[disabled]{opacity:.6;cursor:progress}',
      /* Voucher / Member login switch */
      '.tp-tabs{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:4px;margin-bottom:14px;border-radius:' + Math.max(4, r * .55) + 'px;background:' + t.input_bg + ';border:1.5px solid ' + t.border + '}',
      '.tp-tabs button{border:0;background:transparent;font:inherit;font-weight:700;font-size:14px;padding:9px 8px;border-radius:' + Math.max(3, r * .45) + 'px;color:' + t.muted + ';cursor:pointer;transition:background .15s,color .15s}',
      '.tp-tabs button[aria-selected="true"]{background:' + (t.accent || '#1769e0') + ';color:' + (t.accent_text || '#fff') + '}',
      /* member fields: typed as is (the voucher code box is upper-case and centred) */
      '.tp-pane[data-pane="member"] .tp-input{font-size:17px;font-weight:600;letter-spacing:normal;text-transform:none;text-align:left}',
      '.tp-tabs button:focus-visible{outline:3px solid ' + hexA(t.accent, .45) + ';outline-offset:2px}',
      '.tp-pw{position:relative}.tp-pw .tp-input{padding-right:64px}',
      '.tp-pw button{position:absolute;right:6px;top:50%;transform:translateY(-50%);border:0;background:none;color:' + t.muted + ';font:inherit;font-size:13px;font-weight:700;padding:6px 8px;cursor:pointer}',
      '.tp-gap{margin-top:12px}',
      '.tp-msg{font-size:14px;padding:11px 14px;border-radius:' + Math.max(4, r * .45) + 'px;margin-top:12px;display:flex;gap:9px;align-items:flex-start;text-align:left;line-height:1.4}',
      '.tp-msg.err{background:#fde8e8;color:#9b1c1c}.tp-msg.ok{background:#e3f7ec;color:#11683f}',
      '.tp-bz{display:flex;align-items:center;gap:12px;text-decoration:none;color:' + t.accent_text + ';background:linear-gradient(135deg,' + t.accent + ',#f97316);border-radius:' + Math.max(8, r * .7) + 'px;padding:12px 14px;text-align:left;box-shadow:0 8px 20px rgba(0,0,0,.18)}',
      '.tp-bz b{display:block;font-size:17px}.tp-bz small{display:block;opacity:.85;font-size:12.5px}',
      '.tp-bz-w{flex:none;width:40px;height:40px;border-radius:50%;background:conic-gradient(#f59e0b 0 25%,#1769e0 0 50%,#18a66a 0 75%,#e5484d 0);border:3px solid #fff;animation:tpbz 6s linear infinite}',
      '@keyframes tpbz{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.tp-bz-w{animation:none}}',
      /* warning / paused page (frozen voucher) */
      '.tp-block{position:fixed;inset:0;z-index:9999;background:rgba(10,18,30,.72);display:flex;align-items:center;justify-content:center;padding:18px;font-family:inherit}',
      '.tp-block-card{background:#fff;color:#1d2735;max-width:420px;width:100%;border-radius:18px;padding:26px 22px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.35)}',
      '.tp-block-ic{width:64px;height:64px;border-radius:50%;margin:0 auto 12px;display:flex;align-items:center;justify-content:center;background:#fdecc8;color:#a15c00}',
      '.tp-block-ic.freeze{background:#e0efff;color:#1a5fb4}.tp-block-ic svg{width:34px;height:34px}',
      '.tp-block h2{margin:0 0 10px;font-size:22px}.tp-block p{margin:0 0 10px;font-size:15px;line-height:1.5}.tp-block small{display:block;color:#5c6b7d;font-size:13px;margin-top:6px}',
      '.tp-block-code{font-family:monospace;font-weight:700;letter-spacing:.12em;background:#f1f4f8;border-radius:8px;padding:4px 10px;display:inline-block;margin-bottom:12px}',
      '.tp-block .tp-btn{margin-top:14px;width:100%}',
      '.tp-note{display:flex;gap:10px;align-items:flex-start;text-align:left;padding:13px 14px;border-radius:' + Math.max(4, r * .5) + 'px;font-size:14px;line-height:1.45;background:' + hexA(t.accent, .1) + ';color:' + t.text + '}',
      '.tp-note.promo{background:' + t.accent + ';color:' + t.accent_text + '}.tp-note.warning{background:#fff4d6;color:#7a5200}',
      '.tp-note .tp-ico{flex:none;margin-top:1px}',
      '.tp-section-t{font-size:13px;font-weight:700;color:' + t.muted + ';margin:0 0 10px}',
      '.tp-plans-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}',
      '.tp-plan{position:relative;border:1.5px solid ' + t.border + ';border-radius:' + Math.max(4, r * .5) + 'px;padding:14px 12px;text-align:left;background:' + hexA(t.input_bg, 1) + '}',
      '.tp-plan b{display:block;font-size:14px}.tp-plan strong{display:block;font-family:' + HF + ';font-size:22px;margin:4px 0 2px;color:' + t.accent + '}.tp-plan small{color:' + t.muted + ';font-size:12px}',
      '.tp-plan.hot{border-color:' + t.accent + '}.tp-plan .tag{position:absolute;top:-9px;right:10px;background:' + t.accent + ';color:' + t.accent_text + ';font-size:10.5px;font-weight:800;padding:2px 8px;border-radius:99px}',
      '.tp-plans-list{border-top:1.5px solid ' + t.border + '}.tp-plans-list div{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:11px 2px;border-bottom:1.5px solid ' + t.border + ';font-size:15px;text-align:left}',
      '.tp-plans-list span{color:' + t.muted + ';font-size:13px;margin-left:6px}.tp-plans-list strong{font-family:' + HF + ';font-size:18px;white-space:nowrap}',
      '.tp-chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:' + (al === 'left' ? 'flex-start' : 'center') + '}',
      '.tp-chips span{border:1.5px solid ' + t.border + ';border-radius:99px;padding:7px 12px;font-size:13.5px;font-weight:600}.tp-chips b{color:' + t.accent + '}',
      '.tp-steps{list-style:none;padding:0;margin:0;counter-reset:s;text-align:left}.tp-steps li{counter-increment:s;display:flex;gap:12px;align-items:center;padding:7px 0;font-size:14.5px}',
      '.tp-steps li::before{content:counter(s);flex:none;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:13px;background:' + hexA(t.accent, .14) + ';color:' + t.accent + '}',
      '.tp-pay{display:grid;gap:8px;text-align:left}.tp-pay div{display:flex;justify-content:space-between;gap:10px;padding:11px 13px;border-radius:' + Math.max(4, r * .45) + 'px;background:' + t.input_bg + ';font-size:14px}.tp-pay b{font-weight:800}.tp-pay span{color:' + t.muted + '}',
      '.tp-row{display:flex;flex-wrap:wrap;gap:10px 16px;justify-content:' + (al === 'left' ? 'flex-start' : 'center') + ';font-size:14px}.tp-row a,.tp-row span{display:inline-flex;gap:6px;align-items:center;color:inherit;text-decoration:none}',
      '.tp-social{display:flex;gap:10px;justify-content:' + (al === 'left' ? 'flex-start' : 'center') + '}.tp-social a{width:44px;height:44px;border-radius:50%;display:grid;place-items:center;border:1.5px solid ' + t.border + ';color:inherit}',
      '.tp-img{width:100%;display:block;object-fit:cover;max-height:220px}',
      '.tp-ph{border:2px dashed ' + t.border + ';border-radius:12px;padding:26px;font-size:13px;color:' + t.muted + ';text-align:center}',
      '.tp-terms{display:flex;gap:10px;align-items:flex-start;text-align:left;font-size:13px;color:' + t.muted + '}.tp-terms input{margin-top:2px;width:18px;height:18px;accent-color:' + t.accent + '}',
      '.tp-foot{font-size:12px;color:' + t.muted + ';opacity:.85;text-align:center}',
      '.tp-text{font-size:15px;line-height:1.55;white-space:pre-line}',
      '.tp-sess{display:grid;grid-template-columns:1fr 1fr;gap:8px;text-align:left}.tp-sess div{padding:12px;border-radius:' + Math.max(4, r * .45) + 'px;background:' + t.input_bg + '}.tp-sess small{display:block;font-size:11.5px;color:' + t.muted + '}.tp-sess b{font-size:17px;font-family:' + HF + '}',
      '.tp-cd{display:grid;justify-items:center;gap:12px}.tp-cd p{margin:0;color:' + t.muted + ';font-size:14px}',
      '.tp-ring{position:relative;width:132px;height:132px}.tp-ring svg{transform:rotate(-90deg)}.tp-ring b{position:absolute;inset:0;display:grid;place-items:center;font-size:44px;font-family:' + HF + '}',
      '.tp-bar{width:100%;height:6px;border-radius:9px;background:' + hexA(t.accent, .15) + ';overflow:hidden}.tp-bar i{display:block;height:100%;background:' + t.accent + ';width:0}',
      '.tp-num{font-size:56px;font-family:' + HF + ';line-height:1}',
      '.tp-spin{width:46px;height:46px;border-radius:50%;border:4px solid ' + hexA(t.accent, .18) + ';border-top-color:' + t.accent + ';animation:tpspin .8s linear infinite}@keyframes tpspin{to{transform:rotate(360deg)}}',
      /* layouts */
      '.tp.l-full .tp-card{box-shadow:none}',
      '.tp.l-poster{align-items:flex-start;padding-top:8vh}.tp.l-poster .tp-card{border:2.5px solid ' + t.text + '}',
      '.tp.l-ticket .tp-card{padding-top:26px}.tp.l-ticket .tp-card::before,.tp.l-ticket .tp-card::after{content:"";position:absolute;top:118px;width:26px;height:26px;border-radius:50%;background:' + (bg.color1 || '#000') + '}.tp.l-ticket .tp-card::before{left:-13px}.tp.l-ticket .tp-card::after{right:-13px}',
      '.tp.l-ticket .tp-card>.tp-b-heading{padding-bottom:22px;border-bottom:2px dashed ' + t.border + '}',
      '.tp.l-sheet{align-items:stretch;flex-direction:column;justify-content:flex-end;padding:0}.tp.l-sheet .tp-hero{padding:48px 24px 28px;color:' + (isDark(bg.color1) ? '#fff' : t.text) + ';text-align:center;position:relative}',
      '.tp.l-sheet .tp-hero .tp-sub{color:inherit;opacity:.85}.tp.l-sheet .tp-card{max-width:560px;margin:0 auto;border-radius:' + r + 'px ' + r + 'px 0 0;flex:1;box-shadow:0 -12px 40px rgba(0,0,0,.15)}',
      '.tp.l-split{padding:0;align-items:stretch}.tp.l-split .tp-wrap{display:grid;grid-template-columns:1fr 1fr;width:100%;min-height:100vh}',
      '.tp.l-split .tp-hero{position:relative;padding:8vh 6vw;display:flex;flex-direction:column;justify-content:flex-end;gap:18px;color:' + (isDark(bg.color1) ? '#fff' : t.text) + '}.tp.l-split .tp-hero .tp-sub{color:inherit;opacity:.8}.tp.l-split .tp-hero h1{font-size:clamp(34px,4.6vw,60px)}',
      '.tp.l-split .tp-side{background:' + t.surface + ';display:flex;align-items:center;justify-content:center;padding:6vh 5vw}.tp.l-split .tp-card{box-shadow:none;background:transparent;padding:0}',
      '.tp.l-bands{flex-direction:column;justify-content:flex-start;padding-top:0}.tp-bands{width:calc(100% + 32px);margin:0 -16px 34px;height:30px;background:linear-gradient(#ce1126 0 33%,#fff 33% 38%,#0c1c8c 38% 62%,#fff 62% 67%,#3a7728 67%)}',
      '@media(max-width:760px){.tp.l-split .tp-wrap{grid-template-columns:1fr}.tp.l-split .tp-hero{padding:44px 24px 26px;justify-content:flex-start}.tp.l-split .tp-side{padding:28px 20px;align-items:flex-start}}',
      '@media(max-width:420px){.tp-card{padding:24px 18px}.tp-h-xl h1{font-size:34px}.tp-h-lg h1{font-size:28px}.tp-boxes span{height:46px;font-size:18px}}',
      t.animation === 'rise' ? '.tp-card{animation:tprise .55s cubic-bezier(.2,.8,.2,1) both}@keyframes tprise{from{opacity:0;transform:translateY(18px)}}' : '',
      t.animation === 'fade' ? '.tp-card{animation:tpfade .5s ease both}@keyframes tpfade{from{opacity:0}}' : '',
      '@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}',
      /* preview helpers */
      '.tp-pv [data-bid]{position:relative;cursor:pointer;border-radius:6px;outline:2px solid transparent;outline-offset:5px;transition:outline-color .12s}.tp-pv [data-bid]:hover{outline-color:' + hexA(t.accent, .45) + '}.tp-pv [data-bid].sel{outline-color:' + t.accent + '}'
    ].join('\n');
  }

  /* ---------- blocks ---------- */
  function initials(name) { return String(name || 'W').split(/\s+/).filter(Boolean).slice(0, 2).map(function (w) { return w.charAt(0); }).join('').toUpperCase(); }

  var B = {};
  B.logo = function (b, ctx) {
    var s = +b.size || 60, rad = b.shape === 'circle' ? '50%' : (b.shape === 'rounded' ? Math.round(s * .28) + 'px' : '0');
    var inner = (b.mode !== 'initials' && ctx.business.logo) ? '<img src="' + esc(ctx.business.logo) + '" alt="' + esc(ctx.business.name) + '">' : esc(initials(ctx.business.name));
    var img = !!(ctx.business.logo && b.mode !== 'initials');
    if (img) inner = '<img src="' + esc(ctx.business.logo) + '" alt="' + esc(ctx.business.name) + '">';
    return '<div class="tp-logo' + (img ? ' img' : '') + '" style="' + (img ? 'padding:' + Math.round(s * .08) + 'px;' : '') + 'width:' + s + 'px;height:' + s + 'px;border-radius:' + rad + ';font-size:' + Math.round(s * .38) + 'px">' + inner + '</div>';
  };
  /* Bonanza: a button to the live spin-the-wheel page (hidden when no Bonanza is live). */
  B.bonanza = function (b, ctx) {
    var bz = ctx.bonanza;
    if (!bz || !bz.url) return ctx.mode === 'preview' ? '<div class="tp-ph">Bonanza button — shows when a Bonanza is live (Vouchers → Bonanza)</div>' : '';
    return '<a class="tp-bz" href="' + esc(bz.url) + '" target="_blank" rel="noopener"><span class="tp-bz-w" aria-hidden="true"></span><span><b>' + esc(b.text || bz.headline || 'Spin & win') + '</b>' +
      (b.note ? '<small>' + esc(tok(b.note, ctx)) + '</small>' : '') + '</span></a>';
  };
  B.heading = function (b, ctx) { return '<div class="tp-h-' + (b.size || 'md') + '"><h1>' + esc(tok(b.title, ctx)) + '</h1>' + (b.subtitle ? '<p class="tp-sub">' + esc(tok(b.subtitle, ctx)) + '</p>' : '') + '</div>'; };
  B.text = function (b, ctx) { return '<p class="tp-text" style="text-align:' + (b.align || 'inherit') + ';margin:0">' + esc(tok(b.text, ctx)) + '</p>'; };

  /* ---------- adverts ---------- */
  function pickAds(ctx, max) {
    var list = ((ctx.ads || {})[ctx.kind] || []).slice(), out = [];
    while (list.length && out.length < (max || 6)) {   // weighted shuffle
      var total = list.reduce(function (a, x) { return a + (x.weight || 1); }, 0), r = Math.random() * total, i = 0;
      for (; i < list.length; i++) { r -= (list[i].weight || 1); if (r <= 0) break; }
      out.push(list.splice(Math.min(i, list.length - 1), 1)[0]);
    }
    return out;
  }
  function adInner(a, style) {
    var th = a.theme || {}, href = a.click || a.link || '';
    var img = a.image ? '<img class="tp-ad-img" src="' + esc(a.image) + '" alt="' + esc(a.headline || a.advertiser || 'Advert') + '" loading="lazy">' : '';
    var text = (a.headline ? '<b>' + esc(a.headline) + '</b>' : '') + (a.body && style !== 'banner' ? '<span>' + esc(a.body) + '</span>' : '');
    var cta = href && a.cta ? '<em class="tp-ad-cta">' + esc(a.cta) + '</em>' : '';
    var body = img + (text || cta ? '<div class="tp-ad-txt"' + (!a.image ? ' style="background:' + esc(th.bg || '#102033') + ';color:' + esc(th.fg || '#fff') + '"' : '') + '>' + text + cta + '</div>' : '');
    return (href ? '<a class="tp-ad-in" href="' + esc(href) + '" target="_blank" rel="noopener sponsored" data-ad="' + a.id + '">' : '<div class="tp-ad-in" data-ad="' + a.id + '">') + body + (href ? '</a>' : '</div>');
  }
  B.ads = function (b, ctx) {
    var style = b.style || 'card', ads = pickAds(ctx, style === 'carousel' ? 8 : 1);
    if (!ads.length) return ctx.mode === 'preview' ? '<div class="tp-ad-empty">' + icon('gift') + '<span>Your live adverts appear here. Create one under <b>Adverts</b> and tick “' + esc(ctx.kind === 'login' ? 'Login page' : ctx.kind === 'status' ? 'Status page' : 'After login') + '”.</span></div>' : '';
    var label = b.label ? '<small class="tp-ad-lab">' + esc(b.label) + '</small>' : '';
    if (style === 'interstitial') {
      return '<div class="tp-ad-over" data-skip="' + (+b.skip || 5) + '" role="dialog" aria-label="Advert"><div class="tp-ad-box">' + label + adInner(ads[0], 'card') +
        '<button type="button" class="tp-ad-skip" disabled>Skip in <span>' + (+b.skip || 5) + '</span></button></div></div>';
    }
    if (style === 'carousel' && ads.length > 1) {
      return label + '<div class="tp-ad-car" data-rotate="' + (+b.rotate || 6) + '">' + ads.map(function (a, i) { return '<div class="tp-ad tp-ad-card' + (i ? '' : ' on') + '">' + adInner(a, 'card') + '</div>'; }).join('') +
        '<div class="tp-ad-dots">' + ads.map(function (a, i) { return '<i' + (i ? '' : ' class="on"') + '></i>'; }).join('') + '</div></div>';
    }
    return label + '<div class="tp-ad tp-ad-' + (style === 'banner' ? 'banner' : 'card') + '">' + adInner(ads[0], style) + '</div>';
  };
  B.trial = function (b, ctx) {
    return '<div class="tp-trial"><button type="button" class="tp-btn outline tp-trial-btn">' + icon('clock') + '<span>' + esc(b.text || 'Try free for a few minutes') + '</span></button>' +
      (b.note ? '<p class="tp-hint">' + esc(tok(b.note, ctx)) + '</p>' : '') + '<div class="tp-trial-out" aria-live="polite"></div></div>';
  };
  B.faq = function (b) {
    return (b.title ? '<p class="tp-section-t">' + esc(b.title) + '</p>' : '') + '<div class="tp-faq">' + (b.items || []).map(function (x) {
      return '<details><summary>' + esc(x.name) + '</summary><p>' + esc(x.detail) + '</p></details>'; }).join('') + '</div>';
  };
  B.ticker = function (b, ctx) {
    var items = (b.items || []).filter(Boolean).map(function (x) { return esc(tok(x, ctx)); });
    if (!items.length) return '';
    var run = items.join('<i>•</i>');
    return '<div class="tp-ticker" style="--tk:' + Math.max(8, items.join(' ').length / 5) + 's">' + icon(b.icon || 'bell') + '<div class="tp-tk-w"><div class="tp-tk-r"><span>' + run + '</span><span aria-hidden="true">' + run + '</span></div></div></div>';
  };

  B.voucher = function (b, ctx) {
    var id = 'v' + b.id, n = +b.length || 8, field;
    if (b.style === 'boxes') {
      var cells = ''; for (var i = 0; i < n; i++) cells += '<span></span>';
      field = '<div class="tp-boxes" data-n="' + n + '">' + cells + '<input id="' + id + '" name="code" autocomplete="one-time-code" autocapitalize="characters" spellcheck="false" inputmode="text" maxlength="32" aria-label="' + esc(b.label || 'Voucher code') + '"></div>';
    } else {
      field = '<input class="tp-input" id="' + id + '" name="code" autocomplete="one-time-code" autocapitalize="characters" spellcheck="false" placeholder="' + esc(b.placeholder || '') + '" maxlength="32">';
    }
    var err = ctx.mt && ctx.mt.error ? '<div class="tp-msg err" role="alert">' + icon('warn') + '<span>' + esc(ctx.mt.error) + '</span></div>' : '';
    var hint = b.show_hint ? '<p class="tp-hint">Codes are not case-sensitive — spaces are ignored.</p>' : '';
    var scan = b.scan === false ? '' : '<button type="button" class="tp-scan">' + icon('qr', 20) + '<span>' + esc(b.scan_label || 'Scan the QR on your voucher') + '</span></button>' +
      '<input type="file" accept="image/*" capture="environment" class="tp-scan-in" hidden aria-hidden="true" tabindex="-1">';
    var voucherPane = (b.label ? '<label class="tp-label" for="' + id + '">' + esc(b.label) + '</label>' : '') + field + hint + scan;
    var btn = '<button class="tp-btn" type="submit">' + icon('wifi') + '<span>' + esc(b.button || 'Connect') + '</span></button>' +
      '<div class="tp-out" aria-live="polite">' + err + '</div>';
    // Members log in with a username and a password. On by default; Voucher is always the first tab.
    if (b.members === false) return '<form class="tp-vform" data-mode="voucher" novalidate>' + voucherPane + btn + '</form>';
    return '<form class="tp-vform" data-mode="voucher" novalidate>' +
      '<div class="tp-tabs" role="tablist" aria-label="How do you log in?">' +
        '<button type="button" role="tab" id="' + id + 't1" aria-controls="' + id + 'p1" aria-selected="true" data-tab="voucher">' + esc(b.voucher_tab || 'Voucher') + '</button>' +
        '<button type="button" role="tab" id="' + id + 't2" aria-controls="' + id + 'p2" aria-selected="false" data-tab="member" tabindex="-1">' + esc(b.member_tab || 'Member') + '</button></div>' +
      '<div class="tp-pane" data-pane="voucher" role="tabpanel" id="' + id + 'p1" aria-labelledby="' + id + 't1">' + voucherPane + '</div>' +
      '<div class="tp-pane" data-pane="member" role="tabpanel" id="' + id + 'p2" aria-labelledby="' + id + 't2" hidden>' +
        '<label class="tp-label" for="' + id + 'u">Username</label>' +
        '<input class="tp-input" id="' + id + 'u" name="m_user" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="64">' +
        '<label class="tp-label tp-gap" for="' + id + 'pw">Password</label>' +
        '<div class="tp-pw"><input class="tp-input" id="' + id + 'pw" name="m_pass" type="password" autocomplete="current-password" maxlength="64">' +
        '<button type="button" class="tp-show" aria-controls="' + id + 'pw">Show</button></div></div>' +
      btn + '</form>';
  };
  B.plans = function (b, ctx) {
    var ps = ctx.plans || [];
    if (Array.isArray(b.visible_plans)) {
      var visible = {};
      b.visible_plans.forEach(function (name) { visible[String(name)] = true; });
      ps = ps.filter(function (p) { return visible[String(p.name)]; });
    }
    if (!ps.length) {
      if (ctx.mode !== 'preview') return '';
      return '<div class="tp-ph">' + (Array.isArray(b.visible_plans) ? 'No plans selected for this block' : 'Your active plans appear here') + '</div>';
    }
    var t = b.title ? '<p class="tp-section-t">' + esc(b.title) + '</p>' : '', inner = '';
    ps.forEach(function (p) {
      var hot = b.highlight && p.name.toLowerCase() === String(b.highlight).toLowerCase();
      var dev = b.show_devices ? ' · ' + p.devices + (p.devices > 1 ? ' devices' : ' device') : '';
      if (b.style === 'list') inner += '<div><span style="margin:0;color:inherit;font-size:15px">' + esc(p.name) + '<span>' + dur(durOf(p)) + dev + '</span></span><strong>' + money(ctx, p.price, p.free) + '</strong></div>';
      else if (b.style === 'chips') inner += '<span>' + esc(p.name) + ' <b>' + money(ctx, p.price, p.free) + '</b></span>';
      else inner += '<div class="tp-plan' + (hot ? ' hot' : '') + '">' + (hot ? '<span class="tag">Popular</span>' : '') + '<b>' + esc(p.name) + '</b><strong>' + money(ctx, p.price, p.free) + '</strong><small>' + dur(durOf(p)) + dev + '</small></div>';
    });
    var cls = b.style === 'list' ? 'tp-plans-list' : (b.style === 'chips' ? 'tp-chips' : 'tp-plans-cards');
    return t + '<div class="' + cls + '">' + inner + '</div>';
  };
  B.notice = function (b, ctx) { return '<div class="tp-note ' + (b.tone || 'info') + '">' + icon(b.icon || 'info') + '<span>' + esc(tok(b.text, ctx)) + '</span></div>'; };
  B.image = function (b, ctx) {
    if (!b.src) return ctx.mode === 'preview' ? '<div class="tp-ph">Add a promo image in the block settings</div>' : '';
    var img = '<img class="tp-img" src="' + esc(b.src) + '" alt="' + esc(b.alt || '') + '" style="border-radius:' + (+b.radius || 0) + 'px">';
    return b.link ? '<a href="' + esc(b.link) + '">' + img + '</a>' : img;
  };
  B.steps = function (b) { return (b.title ? '<p class="tp-section-t">' + esc(b.title) + '</p>' : '') + '<ol class="tp-steps">' + (b.items || []).map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ol>'; };
  B.payment = function (b) { return (b.title ? '<p class="tp-section-t">' + esc(b.title) + '</p>' : '') + '<div class="tp-pay">' + (b.methods || []).map(function (m) { return '<div><b>' + esc(m.name) + '</b><span>' + esc(m.detail) + '</span></div>'; }).join('') + '</div>' + (b.note ? '<p class="tp-hint">' + esc(b.note) + '</p>' : ''); };
  B.contact = function (b, ctx) {
    var ph = b.phone || ctx.business.phone, out = [];
    if (ph) out.push('<a href="tel:' + esc(ph) + '">' + icon('phone', 16) + esc(ph) + '</a>');
    if (b.whatsapp) out.push('<a href="https://wa.me/' + esc(String(b.whatsapp).replace(/\D/g, '')) + '">' + icon('whatsapp', 16) + 'WhatsApp</a>');
    if (b.email) out.push('<a href="mailto:' + esc(b.email) + '">' + icon('mail', 16) + esc(b.email) + '</a>');
    if (b.hours) out.push('<span>' + icon('clock', 16) + esc(b.hours) + '</span>');
    return out.length ? '<div class="tp-row">' + out.join('') + '</div>' : (ctx.mode === 'preview' ? '<div class="tp-ph">Add a phone, WhatsApp or opening hours</div>' : '');
  };
  B.social = function (b, ctx) {
    var map = [['facebook', 'facebook', 'https://facebook.com/'], ['instagram', 'instagram', 'https://instagram.com/'], ['tiktok', 'tiktok', 'https://tiktok.com/@'], ['whatsapp', 'whatsapp', 'https://wa.me/']], out = '';
    map.forEach(function (m) { var v = b[m[0]]; if (!v) return; var url = /^https?:/.test(v) ? v : m[2] + String(v).replace(/^@/, '').replace(m[0] === 'whatsapp' ? /\D/g : /$^/, ''); out += '<a href="' + esc(url) + '" aria-label="' + m[0] + '">' + icon(m[1], 19) + '</a>'; });
    return out ? '<div class="tp-social">' + out + '</div>' : (ctx.mode === 'preview' ? '<div class="tp-ph">Add your social handles</div>' : '');
  };
  B.button = function (b, ctx) { var url = tok(b.url || '', ctx); if (!url && ctx.mode !== 'preview') url = ctx.dest || '#'; return '<a class="tp-btn ' + (b.style === 'outline' ? 'outline' : '') + '" href="' + esc(url || '#') + '">' + esc(b.text || 'Continue') + '</a>'; };
  B.terms = function (b) { return '<label class="tp-terms"><input type="checkbox" class="tp-terms-cb"' + (b.required ? ' data-req="1"' : '') + '><span>' + esc(b.text || 'I accept the terms of use.') + '</span></label>'; };
  B.spacer = function (b) { return '<div style="height:' + (+b.size || 12) + 'px"></div>'; };
  B.footer = function (b, ctx) { return '<div class="tp-foot">' + esc(tok(b.text, ctx)) + '</div>'; };
  B.session = function (b, ctx) {
    var m = ctx.mt || {}, S = ctx.mode === 'preview' || !m.uptime ? { plan: '24 Hours', time_left: '23h 41m', uptime: '19m 02s', ip: '10.5.50.23', mac: 'A4:83:E7:1C:0F:2B', data: '38.4 MiB' } : { plan: m.username || '', time_left: m.timeLeft || '—', uptime: m.uptime, ip: m.ip, mac: m.mac, data: (m.bytesIn || '0') + ' ↓ / ' + (m.bytesOut || '0') + ' ↑' };
    var L = { plan: 'Voucher', time_left: 'Time left', uptime: 'Online for', ip: 'IP address', mac: 'Device', data: 'Data used' };
    return '<div class="tp-sess">' + (b.show || []).map(function (k) { return '<div><small>' + L[k] + '</small><b>' + esc(S[k]) + '</b></div>'; }).join('') + '</div>';
  };
  B.countdown = function (b, ctx) {
    var s = +((ctx.settings || {}).redirect_delay) || 5, st = b.style || 'ring', v;
    if (st === 'ring') v = '<div class="tp-ring"><svg width="132" height="132"><circle cx="66" cy="66" r="58" fill="none" stroke="currentColor" stroke-opacity=".12" stroke-width="8"/><circle class="tp-ring-p" cx="66" cy="66" r="58" fill="none" stroke="' + ctx.theme.accent + '" stroke-width="8" stroke-linecap="round" stroke-dasharray="364.4" stroke-dashoffset="0"/></svg><b class="tp-cd-n">' + s + '</b></div>';
    else if (st === 'bar') v = '<div class="tp-bar"><i class="tp-bar-i"></i></div>';
    else if (st === 'number') v = '<div class="tp-num tp-cd-n">' + s + '</div>';
    else v = '<div class="tp-spin"></div>';
    return '<div class="tp-cd" data-secs="' + s + '"><p>' + esc(b.text || '') + (st === 'bar' ? ' <b class="tp-cd-n">' + s + '</b>s' : '') + '</p>' + v + '</div>';
  };

  function tok(s, ctx) { return String(s || '').replace(/\{business\}/g, ctx.business.name || '').replace(/\{ssid\}/g, ctx.business.ssid || 'our Wi-Fi').replace(/\{phone\}/g, ctx.business.phone || '').replace(/\{logout\}/g, (ctx.mt && ctx.mt.linkLogout) || '#'); }

  /* ---------- behaviour ---------- */
  function normalize(v) { return String(v || '').replace(/\s+/g, '').toUpperCase(); }

  function loadQrLib(ctx, cb) {
    if (window.jsQR || window.BarcodeDetector) return cb();
    var inline = document.getElementById('tp-jsqr'), s = document.createElement('script');
    if (inline) { s.text = inline.textContent; document.head.appendChild(s); return cb(); }   // router pages carry it, switched off until needed
    if (!ctx.qrLib) return cb();
    s.src = ctx.qrLib; s.onload = s.onerror = function () { cb(); }; document.head.appendChild(s);
  }
  function readQr(file, ctx, done) {
    loadQrLib(ctx, function () {
      var url = URL.createObjectURL(file), img = new Image(), sizes = [1000, 640, 1500], i = 0;
      var finish = function (t) { URL.revokeObjectURL(url); done(t || ''); };
      img.onerror = function () { finish(''); };
      img.onload = function () {
        (function next() {
          if (i >= sizes.length) return finish('');
          var k = Math.min(1, sizes[i++] / Math.max(img.width, img.height)), c = document.createElement('canvas');
          c.width = Math.max(1, Math.round(img.width * k)); c.height = Math.max(1, Math.round(img.height * k));
          var g = c.getContext('2d'); g.drawImage(img, 0, 0, c.width, c.height);
          var viaJs = function () {
            if (window.jsQR) { var d = g.getImageData(0, 0, c.width, c.height), q = window.jsQR(d.data, c.width, c.height, { inversionAttempts: 'attemptBoth' }); if (q && q.data) return finish(q.data); }
            next();
          };
          if (window.BarcodeDetector) {
            try { new window.BarcodeDetector({ formats: ['qr_code'] }).detect(c).then(function (r) { if (r && r[0] && r[0].rawValue) finish(r[0].rawValue); else viaJs(); }, viaJs); return; } catch (e) { /* not supported here */ }
          }
          viaJs();
        })();
      };
      img.src = url;
    });
  }
  // What a voucher QR can hold: the TapTap login link (…/login?username=CODE&password=CODE), a
  // member login link (different password), the bare code, or a "join Wi-Fi" QR (not a login).
  function parseVoucherQr(text) {
    var t = String(text || '').trim(); if (!t) return null;
    if (/^WIFI:/i.test(t)) return { wifi: true };
    var q = t.indexOf('?') >= 0 ? t.slice(t.indexOf('?') + 1).split('#')[0] : '', params = {};
    q.split('&').forEach(function (kv) { var p = kv.split('='); if (p[0]) { try { params[decodeURIComponent(p[0])] = decodeURIComponent((p[1] || '').replace(/\+/g, ' ')); } catch (e) { /* bad escape */ } } });
    var user = params.username || params.code || params.voucher || '';
    if (user) { var pass = params.password || user; return { user: user, pass: pass, member: pass !== user }; }
    if (/^[A-Za-z0-9][A-Za-z0-9 ._@-]{2,40}$/.test(t) && !/^https?:/i.test(t)) { var c = t.replace(/\s+/g, ''); return { user: c, pass: c, member: false }; }
    return null;
  }

  function wireBoxes(root) {
    [].forEach.call(root.querySelectorAll('.tp-boxes'), function (w) {
      var input = w.querySelector('input'), cells = w.querySelectorAll('span');
      function paint() {
        var v = normalize(input.value);
        [].forEach.call(cells, function (c, i) { c.textContent = v.charAt(i) || ''; c.classList.toggle('on', i === Math.min(v.length, cells.length - 1) && document.activeElement === input); });
      }
      input.addEventListener('input', paint); input.addEventListener('focus', paint); input.addEventListener('blur', paint); paint();
    });
  }

  function postForm(action, fields) {
    var f = document.createElement('form'); f.method = 'post'; f.action = action; f.style.display = 'none';
    for (var k in fields) { var i = document.createElement('input'); i.type = 'hidden'; i.name = k; i.value = fields[k]; f.appendChild(i); }
    document.body.appendChild(f); f.submit();
  }

  /* ---------- frozen / warned vouchers ---------- */
  function postJSON(url, data, done, fail, timeout) {
    try {
      var x = new XMLHttpRequest(); x.open('POST', url, true);
      x.setRequestHeader('Content-Type', 'text/plain');   // no CORS pre-flight from router-served pages
      if (timeout) x.timeout = timeout;
      x.onload = function () { var d = {}; try { d = JSON.parse(x.responseText); } catch (e) {} done(d, x.status); };
      x.onerror = x.ontimeout = function () { fail && fail(); };
      x.send(JSON.stringify(data));
    } catch (e) { fail && fail(); }
  }

  function showBlock(ctx, d, go) {
    var old = document.querySelector('.tp-block'); if (old) old.parentNode.removeChild(old);
    var w = document.createElement('div'); w.className = 'tp-block'; w.setAttribute('role', 'alertdialog'); w.setAttribute('aria-modal', 'true');
    var warn = d.kind === 'warning';
    w.innerHTML = '<div class="tp-block-card"><div class="tp-block-ic' + (warn ? '' : ' freeze') + '">' + icon(warn ? 'warn' : 'lock') + '</div>' +
      '<h2>' + esc(d.title || (warn ? 'Warning' : 'Voucher paused')) + '</h2>' +
      (d.code ? '<div class="tp-block-code">' + esc(d.code) + '</div>' : '') +
      '<p>' + esc(d.message || '') + '</p>' + (d.keep ? '<small>' + esc(d.keep) + '</small>' : '') +
      (d.contact ? '<small>Help: ' + esc(d.contact) + '</small>' : '') +
      (warn && d.can_accept ? '<button class="tp-btn" type="button">' + icon('check') + '<span>' + esc(d.button || 'I agree') + '</span></button>' : '<button class="tp-btn" type="button" data-close="1"><span>Close</span></button>') +
      '<div class="tp-block-out"></div></div>';
    document.body.appendChild(w);
    var btn = w.querySelector('button'), out = w.querySelector('.tp-block-out');
    btn.focus();
    btn.addEventListener('click', function () {
      if (btn.getAttribute('data-close')) { w.parentNode.removeChild(w); return; }
      if (!ctx.acceptUrl) { out.innerHTML = '<small>Ask staff for help.</small>'; return; }
      btn.disabled = true; out.innerHTML = '<small>Please wait…</small>';
      var dv = deviceSignature();
      postJSON(ctx.acceptUrl, { code: d.code, fp: dv.fp || '' }, function (r) {
        if (!r.success) { btn.disabled = false; out.innerHTML = '<small>' + esc(r.message || 'Could not continue. Try again.') + '</small>'; return; }
        var secs = +r.wait || 0;
        out.innerHTML = '<small>' + esc(r.message || 'Thank you.') + (secs ? ' Connecting in <b class="tp-wait">' + secs + '</b> s…' : ' Connecting…') + '</small>';
        var t = setInterval(function () { secs--; var n = w.querySelector('.tp-wait'); if (n) n.textContent = Math.max(0, secs); if (secs <= 0) { clearInterval(t); go(r.code || d.code); } }, 1000);
        if (!secs) { clearInterval(t); go(r.code || d.code); }
      }, function () { btn.disabled = false; out.innerHTML = '<small>Could not reach the server. Try again.</small>'; });
    });
  }

  function wireVoucher(root, cfg, ctx) {
    [].forEach.call(root.querySelectorAll('.tp-vform'), function (form) {
      var out = form.querySelector('.tp-out'), btn = form.querySelector('button[type=submit]'), input = form.querySelector('input[name=code]');
      var mUser = form.querySelector('input[name=m_user]'), mPass = form.querySelector('input[name=m_pass]');
      var tabs = form.querySelectorAll('.tp-tabs button');
      function say(kind, msg) { out.innerHTML = '<div class="tp-msg ' + kind + '" role="' + (kind === 'err' ? 'alert' : 'status') + '">' + icon(kind === 'err' ? 'warn' : 'check') + '<span>' + msg + '</span></div>'; }
      // Voucher entry protection: wrong code (with tries left), strong warning, or a timed lock with a live countdown.
      function tries(n) { return (n === 0 || n) ? ' <b class="tp-tries">' + n + ' ' + (n === 1 ? 'try' : 'tries') + ' left</b>' : ''; }
      function security(st) {
        if (st.security_block) {
          var left = Math.max(1, +st.block_remaining_seconds || 60), fields = form.querySelectorAll('input,button');
          fields.forEach(function (f) { f.disabled = true; });
          var fmt = function (n) { var h = Math.floor(n / 3600), m = Math.floor(n % 3600 / 60), x = n % 60; return (h ? h + ':' + (m < 10 ? '0' : '') : '') + m + ':' + (x < 10 ? '0' : '') + x; };
          say('err', '<b>⛔ Locked</b> ' + esc(st.message || 'Too many wrong codes.') + '<br><b class="tp-left" style="font-size:1.4em">' + fmt(left) + '</b>' +
            '<br><small>Stay connected to this Wi-Fi — this page opens again when the time is up.</small>' +
            (st.support_phone ? '<br><a href="tel:' + esc(st.support_phone) + '" style="font-weight:700">📞 ' + esc(st.support_phone) + '</a>' : ''));
          var tm = setInterval(function () { left--; var n = out.querySelector('.tp-left'); if (n) n.textContent = fmt(Math.max(0, left));
            if (left <= 0) { clearInterval(tm); fields.forEach(function (f) { f.disabled = false; }); say('ok', 'You can try again now. Type the code carefully.'); } }, 1000);
          return true;
        }
        if (st.invalid || st.security_warning) {
          btn.disabled = false;
          say('err', (st.security_warning ? '<b>⚠️</b> ' : '') + esc(st.message || 'That code was not recognised.') + (st.security_warning ? '' : tries(st.attempts_remaining)));
          if (input) { input.focus(); if (input.select) input.select(); }
          return true;
        }
        return false;
      }
      function setMode(m, focus) {
        form.setAttribute('data-mode', m);
        [].forEach.call(tabs, function (t) { var on = t.getAttribute('data-tab') === m; t.setAttribute('aria-selected', on ? 'true' : 'false'); t.tabIndex = on ? 0 : -1; });
        [].forEach.call(form.querySelectorAll('.tp-pane'), function (p) { p.hidden = p.getAttribute('data-pane') !== m; });
        if (focus) (m === 'member' ? mUser : input).focus();
      }
      [].forEach.call(tabs, function (t, i) {
        t.addEventListener('click', function () { out.innerHTML = ''; setMode(t.getAttribute('data-tab'), true); });
        t.addEventListener('keydown', function (e) {   // arrow keys move between the two tabs
          if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
          var next = tabs[(i + 1) % tabs.length]; next.focus(); next.click(); e.preventDefault();
        });
      });
      // ---- Scan the QR on the voucher: the phone camera takes a photo, the page reads the code and logs in.
      // A photo (not live video) works on http:// router pages too, where browsers block live camera access.
      var scanBtn = form.querySelector('.tp-scan'), scanIn = form.querySelector('.tp-scan-in');
      if (scanBtn && scanIn) {
        scanBtn.addEventListener('click', function () { out.innerHTML = ''; scanIn.value = ''; scanIn.click(); });
        scanIn.addEventListener('change', function () {
          var file = scanIn.files && scanIn.files[0]; if (!file) return;
          scanBtn.disabled = true; say('ok', 'Reading the QR code…');
          readQr(file, ctx, function (text) {
            scanBtn.disabled = false;
            var got = parseVoucherQr(text);
            if (!got) { say('err', text ? 'That QR code is not a voucher. Scan the QR printed on your voucher, or type the code.' : 'No QR code found in the photo. Hold the voucher flat, fill the screen with the QR and try again.'); return; }
            if (got.wifi) { say('err', 'That QR joins the Wi-Fi — you are already connected. Scan the other QR on the voucher, or type the code.'); return; }
            if (got.member && mUser) { setMode('member', false); mUser.value = got.user; mPass.value = got.pass; }
            else { if (mUser) setMode('voucher', false); input.value = got.user; input.dispatchEvent(new Event('input')); }
            if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event('submit', { cancelable: true }));
          });
        });
      }
      var show = form.querySelector('.tp-show');
      if (show) show.addEventListener('click', function () { var h = mPass.type === 'password'; mPass.type = h ? 'text' : 'password'; show.textContent = h ? 'Hide' : 'Show'; });
      // After a failed login the router shows this page again with its error: reopen the tab the customer used.
      var remembered = ''; try { remembered = sessionStorage.getItem('tp-mode') || ''; } catch (e) {}
      if (mUser && ctx.mt && ctx.mt.error && remembered === 'member') setMode('member', false);
      form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        var member = form.getAttribute('data-mode') === 'member', code, mpw = '';
        if (member) {
          code = String(mUser.value || '').replace(/\s+/g, '').toLowerCase(); mpw = String(mPass.value || '');
          if (!code) { say('err', 'Type your username.'); mUser.focus(); return; }
          if (!mpw) { say('err', 'Type your password.'); mPass.focus(); return; }
        } else {
          var raw = String(input.value || '').replace(/\s+/g, '');
          code = (cfg.settings && cfg.settings.case_sensitive) ? raw : raw.toUpperCase();
          if (!code) { say('err', 'Type the code printed on your voucher.'); input.focus(); return; }
        }
        var req = root.querySelector('.tp-terms-cb[data-req]');
        if (req && !req.checked) { say('err', 'Tick the box to accept the terms first.'); return; }
        var dst = (cfg.settings && cfg.settings.redirect_url) || (ctx.mt && ctx.mt.linkOrig) || '';
        if (ctx.mode === 'preview' || ctx.mode === 'thumb') { say('ok', member ? 'Preview: member <b>' + esc(code) + '</b> would be logged in now.' : 'Preview: a customer typing <b>' + esc(code) + '</b> would be logged in now.'); return; }
        try { sessionStorage.setItem('tp-mode', member ? 'member' : 'voucher'); } catch (e) {}
        // Vouchers: the code is username AND password. Members: username + their own password.
        var passFor = function (c) { return member ? mpw : c; };
        if (ctx.mode === 'mikrotik') {
          var login = function (c) {
            var pw = passFor(c);
            if (ctx.mt.chapId) pw = md5(ctx.mt.chapId + pw + ctx.mt.chapChallenge);
            btn.disabled = true; postForm(ctx.mt.linkLoginOnly, { username: c, password: pw, dst: dst, popup: 'true' });
          };
          if (!ctx.stateUrl) { sendDevice(ctx, code); login(code); return; }
          // Ask TapTap first: a frozen or warned voucher shows its page instead of logging in.
          btn.disabled = true; say('ok', member ? 'Checking your account…' : 'Checking your voucher…');
          var dv0 = (ctx.settings && ctx.settings.collect_device === false) ? {} : deviceSignature();
          postJSON(ctx.stateUrl, { code: code, fp: dv0.fp || '', c: dv0.c || {}, mac: ctx.mt.mac || '', ip: ctx.mt.ip || '' }, function (st) {
            if (security(st)) return;      // wrong code / warning / locked: answered here, nothing sent to the router
            if (st.blocked) { btn.disabled = false; out.innerHTML = ''; showBlock(ctx, st, login); return; }
            if (st.wait) {   // TapTap is clearing this voucher's old session on the router first
              var left = +st.wait; say('ok', esc(st.message || 'Getting your connection ready…') + ' <b class="tp-left">' + left + '</b> s');
              var tm = setInterval(function () { left--; var n = out.querySelector('.tp-left'); if (n) n.textContent = Math.max(0, left); if (left <= 0) { clearInterval(tm); login(code); } }, 1000);
              return;
            }
            login(st.code || code);
          }, function () { sendDevice(ctx, code); login(code); }, 4000);
          return;
        }
        // hosted
        btn.disabled = true; say('ok', member ? 'Checking your account…' : 'Checking your voucher…');
        var xhr = new XMLHttpRequest(); xhr.open('POST', ctx.checkUrl, true); xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.onload = function () {
          var d = {}; try { d = JSON.parse(xhr.responseText); } catch (e) {}
          if (d.blocked) {
            btn.disabled = false; out.innerHTML = '';
            showBlock(ctx, d, function (c) {
              if (ctx.mt && ctx.mt.linkLoginOnly) postForm(ctx.mt.linkLoginOnly, { username: c, password: passFor(c), dst: dst, popup: 'true' });
              else { var b = document.querySelector('.tp-block'); if (b) b.parentNode.removeChild(b); say('ok', 'Thank you — your voucher works again. Enter it on the Wi-Fi login page to go online.'); }
            });
            return;
          }
          if (!d.success && security(d)) return;
          if (!d.success) { btn.disabled = false; say('err', esc(d.message || (member ? 'Username or password is wrong.' : 'That code was not recognised. Check it and try again.'))); return; }
          var real = d.code || code;
          if (ctx.mt && ctx.mt.linkLoginOnly) {
            var go = function () { postForm(ctx.mt.linkLoginOnly, { username: real, password: passFor(real), dst: dst, popup: 'true' }); };
            say('ok', (member ? 'Welcome back, ' + esc(real) : 'Valid ' + esc(d.plan) + ' voucher') + ' — connecting…');
            if (d.wait) setTimeout(go, d.wait * 1000); else go();      // waits while TapTap clears an old session
          }
          else if (member) { btn.disabled = false; say('ok', 'Your account is ready. Join <b>' + esc(ctx.business.ssid || 'our Wi-Fi') + '</b> and log in on the Wi-Fi page with your username and password.'); }
          else { btn.disabled = false; say('ok', '<b>' + esc(d.plan) + '</b> voucher is valid' + (d.duration ? ' (' + esc(d.duration) + ')' : '') + '. Join <b>' + esc(ctx.business.ssid || 'our Wi-Fi') + '</b> and enter it on the login page to go online.'); }
        };
        xhr.onerror = function () { btn.disabled = false; say('err', 'Could not reach the server. Check you are connected to ' + esc(ctx.business.ssid || 'the Wi-Fi') + '.'); };
        var dv = (ctx.settings && ctx.settings.collect_device === false) ? {} : deviceSignature();
        var body = { code: code, fp: dv.fp || '', c: dv.c || {}, mac: (ctx.mt && ctx.mt.mac) || '', ip: (ctx.mt && ctx.mt.ip) || '' };
        if (member) { body.member = true; body.password = mpw; }
        xhr.send(JSON.stringify(body));
      });
    });
  }


  /* ---------- device signature (identifies a phone even when it randomises its MAC) ---------- */
  var DEV = null;
  function deviceSignature() {
    if (DEV) return DEV;
    var n = navigator || {}, sc = screen || {}, c = {};
    try { c.platform = n.platform || ''; c.lang = (n.languages || [n.language]).slice(0, 3).join(','); } catch (e) {}
    try { c.tz = Intl.DateTimeFormat().resolvedOptions().timeZone || String(new Date().getTimezoneOffset()); } catch (e) { c.tz = String(new Date().getTimezoneOffset()); }
    c.screen = [sc.width, sc.height].sort(function (a, b) { return b - a; }).join('x'); c.dpr = String(window.devicePixelRatio || 1); c.depth = String(sc.colorDepth || '');
    c.cores = String(n.hardwareConcurrency || ''); c.mem = String(n.deviceMemory || ''); c.touch = String(n.maxTouchPoints || 0);
    try { var cv = document.createElement('canvas'); cv.width = 220; cv.height = 40; var g = cv.getContext('2d'); g.textBaseline = 'top'; g.font = '14px Arial'; g.fillStyle = '#f60'; g.fillRect(100, 1, 62, 20);
      g.fillStyle = '#069'; g.fillText('TapTap ✓ Wi-Fi 🇬🇲', 2, 15); g.fillStyle = 'rgba(102,204,0,.7)'; g.fillText('TapTap ✓ Wi-Fi 🇬🇲', 4, 17); c.canvas = md5(cv.toDataURL()); } catch (e) { c.canvas = ''; }
    try { var gl = document.createElement('canvas').getContext('webgl'), ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
      if (ext) { c.vendor = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL); c.gpu = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL); } } catch (e) {}
    var fp = md5([c.platform, c.tz, c.screen, c.dpr, c.depth, c.cores, c.mem, c.touch, c.canvas, c.gpu, c.vendor, (n.userAgent || '').replace(/[\d.]+/g, '')].join('|'));
    DEV = { fp: fp, c: c }; return DEV;
  }
  function sendDevice(ctx, code) {
    if (!ctx.deviceUrl || ctx.mode === 'preview' || ctx.mode === 'thumb' || (ctx.settings && ctx.settings.collect_device === false)) return;
    try {
      var d = deviceSignature(), body = JSON.stringify({ fp: d.fp, c: d.c, mac: (ctx.mt && ctx.mt.mac) || '', ip: (ctx.mt && ctx.mt.ip) || '', code: code || '' });
      // text/plain avoids a CORS pre-flight, so it also works from pages served by the router
      if (navigator.sendBeacon && navigator.sendBeacon(ctx.deviceUrl, new Blob([body], { type: 'text/plain' }))) return;
      var x = new XMLHttpRequest(); x.open('POST', ctx.deviceUrl, true); x.setRequestHeader('Content-Type', 'text/plain'); x.send(body);
    } catch (e) {}
  }

  function wireAds(root, ctx) {
    if (ctx.mode !== 'preview' && ctx.mode !== 'thumb') {
      var seen = {};
      [].forEach.call(root.querySelectorAll('[data-ad]'), function (el) {
        var id = el.getAttribute('data-ad'), ad = ((ctx.ads || {})[ctx.kind] || []).filter(function (a) { return String(a.id) === id; })[0];
        if (ad && ad.beacon && !seen[id]) { seen[id] = 1; try { new Image().src = ad.beacon + '?t=' + Date.now(); } catch (e) {} }
      });
    }
    [].forEach.call(root.querySelectorAll('.tp-ad-car'), function (car) {
      var slides = car.querySelectorAll('.tp-ad'), dots = car.querySelectorAll('.tp-ad-dots i'), i = 0, secs = +car.getAttribute('data-rotate') || 6;
      function show(n) { i = (n + slides.length) % slides.length; [].forEach.call(slides, function (s, k) { s.classList.toggle('on', k === i); }); [].forEach.call(dots, function (s, k) { s.classList.toggle('on', k === i); }); }
      [].forEach.call(dots, function (d, k) { d.addEventListener('click', function () { show(k); }); });
      if (!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches)) setInterval(function () { show(i + 1); }, secs * 1000);
    });
    var over = root.querySelector('.tp-ad-over');
    if (over && ctx.mode !== 'preview' && ctx.mode !== 'thumb') {
      // A transformed ancestor (entrance animation) would trap position:fixed, so lift it to <body>.
      var old = document.querySelectorAll('body > .tp-ad-over'); [].forEach.call(old, function (o) { o.remove(); });
      over.style.fontFamily = getComputedStyle(root).fontFamily;
      document.body.appendChild(over);
    }
    if (over) {
      var left = +over.getAttribute('data-skip') || 5, btn = over.querySelector('.tp-ad-skip'), span = btn.querySelector('span');
      var t = setInterval(function () { left--; if (left <= 0) { clearInterval(t); btn.disabled = false; btn.textContent = 'Close ✕'; } else span.textContent = left; }, 1000);
      btn.addEventListener('click', function () { over.remove(); });
    }
  }
  function wireTrial(root, ctx) {
    [].forEach.call(root.querySelectorAll('.tp-trial-btn'), function (btn) {
      var out = btn.parentNode.querySelector('.tp-trial-out');
      btn.addEventListener('click', function () {
        var mac = ctx.mt && ctx.mt.mac, login = ctx.mt && ctx.mt.linkLoginOnly;
        if (ctx.mode === 'preview' || ctx.mode === 'thumb') { out.innerHTML = '<p class="tp-hint">Preview: the router would start a free trial for this phone.</p>'; return; }
        if (!login || !mac) { out.innerHTML = '<p class="tp-hint">Free trial is only available on the hotspot login page.</p>'; return; }
        sendDevice(ctx, 'TRIAL');
        btn.disabled = true;
        // RouterOS HotSpot trial: username "T-<mac>" with an empty password (enable trial on the server profile).
        postForm(login, { username: 'T-' + mac, password: '', dst: (ctx.settings && ctx.settings.redirect_url) || ctx.mt.linkOrig || '', popup: 'true' });
      });
    });
  }

  function wireCountdown(root, cfg, ctx) {
    var box = root.querySelector('.tp-cd'); if (!box) return;
    var total = +box.getAttribute('data-secs') || 5, left = total, t0 = Date.now();
    var nums = box.querySelectorAll('.tp-cd-n'), ring = box.querySelector('.tp-ring-p'), bar = box.querySelector('.tp-bar-i');
    var dest = ctx.dest;
    function frame() {
      var el = (Date.now() - t0) / 1000, p = Math.min(1, el / total); left = Math.max(0, Math.ceil(total - el));
      [].forEach.call(nums, function (n) { n.textContent = left; });
      if (ring) ring.setAttribute('stroke-dashoffset', String(364.4 * p)); if (bar) bar.style.width = (p * 100) + '%';
      if (p < 1) { requestAnimationFrame(frame); return; }
      if (ctx.mode === 'preview' || ctx.mode === 'thumb') { setTimeout(function () { t0 = Date.now(); requestAnimationFrame(frame); }, 1200); return; }
      if (dest) location.href = dest;
    }
    requestAnimationFrame(frame);
  }

  /* ---------- main ---------- */
  function render(root, cfg, ctx) {
    var t = cfg.theme || {}; ctx.theme = t; ctx.settings = cfg.settings || {}; ctx.mt = ctx.mt || {};
    if (!ctx.dest) ctx.dest = ctx.settings.redirect_url || ctx.mt.linkRedirect || ctx.mt.linkOrig || '';
    var q = [];
    [t.font, t.heading_font].forEach(function (f) { if (FONT_QUERY[f] && q.indexOf(FONT_QUERY[f]) < 0) q.push(FONT_QUERY[f]); });
    var fl = document.getElementById('tp-fonts');
    if (q.length) { if (!fl) { fl = h('link', { id: 'tp-fonts', rel: 'stylesheet' }); document.head.appendChild(fl); } fl.href = 'https://fonts.googleapis.com/css2?family=' + q.join('&family=') + '&display=swap'; }
    var st = document.getElementById('tp-style'); if (!st) { st = h('style', { id: 'tp-style' }); document.head.appendChild(st); } st.textContent = css(t) + EXTRA_CSS;

    var blocks = (cfg.blocks || []).filter(function (b) { return !b.hidden && B[b.type]; });
    // Your uploaded logo shows on every page: pages without a Logo block get a small one on top.
    if (ctx.business && ctx.business.logo && ctx.settings.auto_logo !== false && !blocks.some(function (b) { return b.type === 'logo'; }))
      blocks.unshift({ id: 'auto-logo', type: 'logo', mode: 'image', size: 56, shape: 'rounded' });
    function html(b) { var inner = B[b.type](b, ctx); if (!inner) return ''; return '<div class="tp-b tp-b-' + b.type + '"' + (ctx.mode === 'preview' ? ' data-bid="' + esc(b.id) + '"' : '') + '>' + inner + '</div>'; }
    var layout = t.layout || 'card', hero = [], rest = blocks;
    if (layout === 'split' || layout === 'sheet') { var i = 0; while (i < blocks.length && (blocks[i].type === 'logo' || blocks[i].type === 'heading')) i++; hero = blocks.slice(0, i); rest = blocks.slice(i); }
    var body;
    if (layout === 'split') body = '<div class="tp-wrap"><div class="tp-hero">' + hero.map(html).join('') + '</div><div class="tp-side"><div class="tp-card">' + rest.map(html).join('') + '</div></div></div>';
    else if (layout === 'sheet') body = '<div class="tp-hero">' + hero.map(html).join('') + '</div><div class="tp-card">' + rest.map(html).join('') + '</div>';
    else body = (layout === 'bands' ? '<div class="tp-bands"></div>' : '') + '<main class="tp-card">' + rest.map(html).join('') + '</main>';
    root.className = 'tp l-' + layout + (ctx.mode === 'preview' ? ' tp-pv' : '');
    root.innerHTML = body;

    if (ctx.kind === 'login' && ctx.settings.collect_device !== false && ctx.settings.device_notice !== '' && root.querySelector('.tp-vform')) {
      root.querySelector('.tp-vform').insertAdjacentHTML('beforeend', '<p class="tp-privacy">' + esc(ctx.settings.device_notice || 'We note basic details of your device to keep your voucher safe from misuse.') + '</p>');
    }
    wireBoxes(root); wireVoucher(root, cfg, ctx); wireCountdown(root, cfg, ctx); wireAds(root, ctx); wireTrial(root, ctx);
    if (ctx.kind === 'login') sendDevice(ctx, '');
    if (ctx.mode === 'preview') {
      root.addEventListener('click', function (e) {
        var el = e.target.closest('[data-bid]'); if (!el) return;
        if (e.target.closest('input,button,label')) return;
        e.preventDefault(); parent.postMessage({ tp: 'select', id: el.getAttribute('data-bid') }, '*');
      });
    }
  }

  function select(root, id) { [].forEach.call(root.querySelectorAll('[data-bid]'), function (e) { e.classList.toggle('sel', e.getAttribute('data-bid') === id); }); }

  global.TapPortal = { render: render, select: select, md5: md5, dur: dur, parseQr: parseVoucherQr };
})(window);
