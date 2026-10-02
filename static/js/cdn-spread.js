/* CDNs & "Other" traffic — <div class="tt-cdn" data-url="/traffic/cdn/?…"></div>
   Cards per CDN (what kind of traffic it carried), click one for a network-spread view:
   the CDN in the middle, the services it served around it, sized by MB/GB. */
(function () {
  'use strict';
  var KC = { 'Video': '#e5484d', 'Social': '#7c3aed', 'Calls & chat': '#18a66a', 'Audio': '#f59e0b', 'Apps & updates': '#0ea5a4',
             'Games': '#db2777', 'Websites': '#1769e0', 'System': '#64748b', 'Other': '#94a3b8' };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function hb(n) { n = +n || 0; var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i >= 2 ? n.toFixed(n >= 100 ? 0 : 1) : Math.round(n)) + ' ' + u[i]; }
  function col(k) { return KC[k] || '#94a3b8'; }

  function css() {
    if (document.getElementById('ttCdnCss')) return;
    var st = document.createElement('style'); st.id = 'ttCdnCss';
    st.textContent = '.cdn-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:.6rem}' +
      '.cdn-card{border:1px solid var(--border,#e3e9f1);border-radius:14px;padding:.7rem .8rem;background:var(--panel,#fff);cursor:pointer;text-align:left;width:100%;transition:transform .12s,box-shadow .12s}' +
      '.cdn-card:hover{transform:translateY(-2px);box-shadow:0 10px 24px rgba(10,20,35,.08)}.cdn-card b{font-size:.95rem}.cdn-card .tot{float:right;font-weight:800}' +
      '.cdn-bar{display:flex;height:8px;border-radius:6px;overflow:hidden;margin:.45rem 0;background:#eef2f7}.cdn-bar span{display:block;height:100%}' +
      '.cdn-top{font-size:.76rem;color:var(--muted,#6c7c90)}.cdn-legend{display:flex;flex-wrap:wrap;gap:.6rem;font-size:.74rem;color:var(--muted,#6c7c90);margin:.3rem 0 .6rem}' +
      '.cdn-legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:4px;vertical-align:middle}' +
      '.cdn-ov{position:fixed;inset:0;background:rgba(8,16,28,.55);z-index:2000;display:flex;align-items:center;justify-content:center;padding:1rem}' +
      '.cdn-box{background:var(--panel,#fff);color:var(--ink,#102033);border-radius:18px;max-width:960px;width:100%;max-height:calc(100vh - 2rem);overflow:auto;padding:1rem 1.1rem;box-shadow:0 30px 70px rgba(0,0,0,.35)}' +
      '.cdn-box h3{margin:0;font-size:1.15rem;font-weight:800}.cdn-x{float:right;border:0;background:none;font-size:1.4rem;line-height:1;cursor:pointer;color:inherit}' +
      '.cdn-split{display:grid;grid-template-columns:1.25fr 1fr;gap:1rem;align-items:start}@media(max-width:800px){.cdn-split{grid-template-columns:1fr}}' +
      '.cdn-svg text{font-size:11px;fill:currentColor}.cdn-svg .lbl{font-weight:700}.cdn-svg .sub{fill:#6c7c90;font-size:10px}' +
      '.cdn-list{width:100%;font-size:.84rem}.cdn-list td{padding:.3rem .2rem;border-bottom:1px solid var(--border,#eef1f5)}.cdn-list td:last-child{text-align:right;font-weight:700;white-space:nowrap}' +
      '.cdn-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}';
    document.head.appendChild(st);
  }

  function bar(kinds, total) {
    return '<div class="cdn-bar">' + Object.keys(kinds).map(function (k) { return '<span title="' + esc(k) + ': ' + hb(kinds[k]) + '" style="width:' + (kinds[k] * 100 / (total || 1)).toFixed(1) + '%;background:' + col(k) + '"></span>'; }).join('') + '</div>';
  }

  // the network spread: CDN in the middle, services around it
  function spread(c) {
    var W = 520, H = 520, cx = W / 2, cy = H / 2, R = 190, svcs = c.services, max = Math.max.apply(null, svcs.map(function (s) { return s.bytes; }).concat([1]));
    var h = '<svg class="cdn-svg" viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" aria-label="' + esc(c.name) + ' and the services it delivered">';
    svcs.forEach(function (s, i) {
      var a = -Math.PI / 2 + i * 2 * Math.PI / svcs.length, x = cx + R * Math.cos(a), y = cy + R * Math.sin(a);
      var w = 1.5 + 9 * Math.sqrt(s.bytes / max), r = 8 + 22 * Math.sqrt(s.bytes / max);
      h += '<line x1="' + cx + '" y1="' + cy + '" x2="' + x.toFixed(1) + '" y2="' + y.toFixed(1) + '" stroke="' + col(s.kind) + '" stroke-opacity=".35" stroke-width="' + w.toFixed(1) + '" stroke-linecap="round"/>';
      h += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + r.toFixed(1) + '" fill="' + col(s.kind) + '" fill-opacity=".9"><title>' + esc(s.name) + ' — ' + hb(s.bytes) + ' (' + esc(s.kind) + ')</title></circle>';
      var right = Math.cos(a) >= 0, lx = x + (right ? r + 5 : -r - 5);
      h += '<text class="lbl" x="' + lx.toFixed(1) + '" y="' + (y - 1).toFixed(1) + '" text-anchor="' + (right ? 'start' : 'end') + '">' + esc(s.name.length > 22 ? s.name.slice(0, 21) + '…' : s.name) + '</text>';
      h += '<text class="sub" x="' + lx.toFixed(1) + '" y="' + (y + 12).toFixed(1) + '" text-anchor="' + (right ? 'start' : 'end') + '">' + hb(s.bytes) + '</text>';
    });
    h += '<circle cx="' + cx + '" cy="' + cy + '" r="54" fill="#0f2a4d"/><text x="' + cx + '" y="' + (cy - 4) + '" text-anchor="middle" style="fill:#fff;font-weight:800;font-size:13px">' + esc(c.name.length > 16 ? c.name.slice(0, 15) + '…' : c.name) + '</text>' +
         '<text x="' + cx + '" y="' + (cy + 14) + '" text-anchor="middle" style="fill:#cfe0f5;font-size:12px">' + hb(c.bytes) + '</text></svg>';
    return h;
  }

  function open(c) {
    var ov = document.createElement('div'); ov.className = 'cdn-ov';
    var kinds = Object.keys(c.kinds).map(function (k) { return '<span><i style="background:' + col(k) + '"></i>' + esc(k) + ' ' + hb(c.kinds[k]) + ' (' + Math.round(c.kinds[k] * 100 / (c.bytes || 1)) + '%)</span>'; }).join('');
    ov.innerHTML = '<div class="cdn-box" role="dialog" aria-modal="true" aria-label="' + esc(c.name) + '"><button class="cdn-x" aria-label="Close">×</button>' +
      '<h3>' + esc(c.name) + ' <span style="font-weight:600;color:#6c7c90;font-size:.9rem">— ' + hb(c.bytes) + ' (' + hb(c.down) + ' down · ' + hb(c.up) + ' up)</span></h3>' +
      '<div class="cdn-legend">' + kinds + '</div>' + bar(c.kinds, c.bytes) +
      '<div class="cdn-split"><div>' + spread(c) + '</div><div><table class="cdn-list"><tbody>' + c.services.map(function (s) {
        return '<tr><td><span class="cdn-dot" style="background:' + col(s.kind) + '"></span><b>' + esc(s.name) + '</b><br><small style="color:#6c7c90;margin-left:16px">' + esc(s.kind) + (s.domain && s.domain !== s.name ? ' · ' + esc(s.domain) : '') + '</small></td><td>' + hb(s.bytes) + '</td></tr>';
      }).join('') + '</tbody></table>' +
      '<p style="font-size:.75rem;color:#6c7c90;margin-top:.6rem">Names come from what phones asked the router\'s DNS for. Services TapTap cannot name show as the site they used.</p></div></div></div>';
    function close() { ov.remove(); document.removeEventListener('keydown', key); }
    function key(e) { if (e.key === 'Escape') close(); }
    ov.addEventListener('click', function (e) { if (e.target === ov || e.target.closest('.cdn-x')) close(); });
    document.addEventListener('keydown', key);
    document.body.appendChild(ov); ov.querySelector('.cdn-x').focus();
  }

  function render(el, d) {
    var cd = d.cdns || [], o = d.others || {};
    var h = '<div class="d-flex justify-content-between align-items-baseline flex-wrap gap-2"><h3 class="h6 fw-bold m-0"><i class="bi bi-diagram-3"></i> Delivered by CDNs</h3>' +
      '<small class="text-secondary">Click a CDN to see what it served</small></div>';
    if (!cd.length) h += '<p class="small text-secondary mt-2 mb-0">No CDN traffic recognised in this period yet (it builds up from the router\'s connection and DNS samples).</p>';
    else {
      var kinds = {}; cd.forEach(function (c) { Object.keys(c.kinds).forEach(function (k) { kinds[k] = 1; }); });
      h += '<div class="cdn-legend mt-2">' + Object.keys(kinds).map(function (k) { return '<span><i style="background:' + col(k) + '"></i>' + esc(k) + '</span>'; }).join('') + '</div><div class="cdn-grid">' +
        cd.map(function (c, i) {
          return '<button type="button" class="cdn-card" data-i="' + i + '"><b>' + esc(c.name) + '</b><span class="tot">' + hb(c.bytes) + '</span>' + bar(c.kinds, c.bytes) +
            '<div class="cdn-top">' + c.services.slice(0, 3).map(function (s) { return esc(s.name) + ' ' + hb(s.bytes); }).join(' · ') + '</div></button>';
        }).join('') + '</div>';
    }
    if ((o.sites || []).length || o.unnamed) {
      h += '<h3 class="h6 fw-bold mt-4 mb-2"><i class="bi bi-question-circle"></i> What is in “Other”</h3><div class="table-responsive"><table class="cdn-list"><tbody>' +
        (o.sites || []).map(function (s) { return '<tr><td><span class="cdn-dot" style="background:' + col(s.kind) + '"></span><b>' + esc(s.domain) + '</b> <small style="color:#6c7c90">' + esc(s.kind) + (s.via ? ' · via ' + esc(s.via) : '') + '</small></td><td>' + hb(s.bytes) + '</td></tr>'; }).join('') +
        (o.more ? '<tr><td><small class="text-secondary">' + o.more + ' smaller sites</small></td><td></td></tr>' : '') +
        (o.unnamed ? '<tr><td><small class="text-secondary">Connections with no name (no DNS answer seen — apps using their own DNS, or direct IPs)</small></td><td>' + hb(o.unnamed) + '</td></tr>' : '') +
        '</tbody></table></div>';
    }
    el.innerHTML = h;
    el.querySelectorAll('.cdn-card').forEach(function (b) { b.onclick = function () { open(cd[+b.dataset.i]); }; });
  }

  function load(el) {
    css();
    el.innerHTML = '<p class="small text-secondary m-0">Loading CDN breakdown…</p>';
    fetch(el.dataset.url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); }).then(function (d) { render(el, d); })
      .catch(function () { el.innerHTML = '<p class="small text-secondary m-0">The CDN breakdown could not be loaded.</p>'; });
  }
  document.querySelectorAll('.tt-cdn').forEach(load);
  window.TapCdn = { reload: function () { document.querySelectorAll('.tt-cdn').forEach(load); } };
})();
