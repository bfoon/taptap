/* Topology → Geo map: every mapped router on a street map, with its connections (core/geomap.py). */
(function () {
  'use strict';
  const pane = document.getElementById('paneGeo');
  if (!pane) return;
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let map = null, layer = null, cover = null, data = null;

  function icon(t) {
    const cls = ['geo-pin', t.kind === 'mikrotik' ? 'mt' : 'ap', t.online ? 'on' : 'off', t.suggested ? 'sug' : ''].join(' ');
    return L.divIcon({ className: '', html: `<div class="${cls}"><i class="bi ${t.kind === 'mikrotik' ? 'bi-router' : 'bi-wifi'}"></i></div>`,
      iconSize: [30, 30], iconAnchor: [15, 15], popupAnchor: [0, -14] });
  }

  function popup(t) {
    const g = t.geo, when = g.at ? new Date(g.at).toLocaleString() : '';
    return `<div class="geo-pop"><b>${esc(t.name)}</b><small>${esc(t.detail)}${t.ip ? ' · ' + esc(t.ip) : ''}</small>
      ${t.site && t.kind !== 'mikrotik' ? `<small>behind ${esc(t.site)}${t.port ? ' › ' + esc(t.port) : ''}</small>` : ''}
      <span class="geo-st ${t.online ? 'on' : 'off'}">${t.online ? 'Online' : 'Offline'}</span>
      ${g.note ? `<p class="m-0 mt-1">${esc(g.note)}</p>` : ''}
      <small class="d-block mt-1 text-secondary">Mapped ${esc(when)}${g.by ? ' by ' + esc(g.by) : ''}${g.accuracy ? ' · ±' + g.accuracy + ' m' : ''}</small>
      <div class="geo-pop-acts">
        <a class="btn btn-sm btn-primary" target="_blank" rel="noopener" href="https://www.google.com/maps/dir/?api=1&destination=${g.lat},${g.lng}"><i class="bi bi-sign-turn-right"></i> Directions</a>
        <a class="btn btn-sm btn-outline-secondary" href="${esc(pane.dataset.fieldUrl)}?r=${encodeURIComponent(t.key)}"><i class="bi bi-geo"></i> Re-map</a>
      </div></div>`;
  }

  function draw() {
    if (!map) {
      map = L.map('geoMap', { zoomControl: true, attributionControl: true });
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 20, maxNativeZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' }).addTo(map);
      layer = L.layerGroup().addTo(map); cover = L.layerGroup();
      document.getElementById('geoCoverage').addEventListener('change', e => { e.target.checked ? cover.addTo(map) : map.removeLayer(cover); });
    }
    layer.clearLayers(); cover.clearLayers();
    const pts = data.points || [];
    (data.lines || []).forEach(l => {
      const pl = L.polyline(l.path, { color: l.online ? '#16a34a' : '#dc2626', weight: 3, opacity: .85, dashArray: l.online ? null : '6 6' }).addTo(layer);
      pl.bindTooltip(`${esc(l.distance)} · ${esc(l.how)}`, { permanent: true, direction: 'center', className: 'geo-dist' });
    });
    pts.forEach(t => {
      L.marker([t.geo.lat, t.geo.lng], { icon: icon(t), title: t.name, keyboard: true }).addTo(layer).bindPopup(popup(t));
      if (t.kind !== 'mikrotik') L.circle([t.geo.lat, t.geo.lng], { radius: 30, color: '#0ea5e9', weight: 1, fillOpacity: .08 }).addTo(cover);
      if (t.geo.accuracy && t.geo.accuracy > 15) L.circle([t.geo.lat, t.geo.lng], { radius: t.geo.accuracy, color: '#94a3b8', weight: 1, dashArray: '3 4', fill: false }).addTo(layer);
    });
    const c = data.counts || { mapped: 0, total: 0 };
    document.getElementById('geoCount').textContent = `${c.mapped} of ${c.total} router${c.total === 1 ? '' : 's'} on the map` +
      ((data.lines || []).length ? ` · ${(data.lines || []).length} connection${data.lines.length === 1 ? '' : 's'}` : '');
    const empty = document.getElementById('geoEmpty');
    empty.hidden = pts.length > 0;
    if (!pts.length) {
      map.setView([13.4549, -16.579], 11);                 // a calm default view until the first router is mapped
      const q = document.getElementById('geoQr');
      if (q && !q.dataset.done && window.qrcode) { const qr = qrcode(0, 'M'); qr.addData(pane.dataset.fieldUrl); qr.make(); q.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 0, scalable: true }); q.dataset.done = 1; }
    } else if (pts.length === 1) map.setView([pts[0].geo.lat, pts[0].geo.lng], 18);
    else map.fitBounds(L.latLngBounds(pts.map(t => [t.geo.lat, t.geo.lng])), { padding: [40, 40], maxZoom: 19 });
    const un = data.unmapped || [];
    document.getElementById('geoUnmapped').innerHTML = un.length ? `<h4>Not on the map yet <span class="badge text-bg-light border">${un.length}</span></h4>
      <div class="geo-chips">${un.map(t => `<a class="geo-chip" href="${esc(pane.dataset.fieldUrl)}?r=${encodeURIComponent(t.key)}" title="Map it with your phone">
        <i class="bi ${t.kind === 'mikrotik' ? 'bi-router' : 'bi-wifi'}"></i> ${esc(t.name)}${t.site && t.kind !== 'mikrotik' ? ` <small>· ${esc(t.site)}</small>` : ''}</a>`).join('')}</div>` : '';
    setTimeout(() => map.invalidateSize(), 50);
  }

  async function load() {
    if (!window.L) { setTimeout(load, 200); return; }       // Leaflet still loading
    try {
      const r = await fetch(pane.dataset.geoUrl, { credentials: 'same-origin' });
      data = await r.json();
      draw();
    } catch (e) { document.getElementById('geoCount').textContent = 'Could not load the map.'; }
  }
  window.addEventListener('taptap:geo', load);
  if (!pane.hidden) load();
})();
