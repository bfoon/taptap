/* Map tiles for every TapTap map: window.ttTiles(map) adds the configured tile layer.
 * - Sends the site's origin to the tile server (referrerPolicy) — OpenStreetMap refuses tiles without it
 *   ("403 … osm.wiki/Blocked"), and TapTap's pages send no Referer to other sites by default.
 * - If the provider still refuses (several tiles fail before any loads), switches to the fallback provider. */
(function () {
  'use strict';
  function cfg() {
    var el = document.getElementById('ttMapTiles');
    try { return el ? JSON.parse(el.textContent) : {}; } catch (e) { return {}; }
  }
  window.ttTiles = function (map) {
    var c = cfg(), url = c.url || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    var opts = { maxZoom: 20, maxNativeZoom: c.maxZoom || 19, attribution: c.attribution || '&copy; OpenStreetMap contributors',
                 referrerPolicy: 'strict-origin-when-cross-origin', crossOrigin: false };
    var layer = L.tileLayer(url, opts).addTo(map), ok = 0, bad = 0, switched = false;
    layer.on('tileload', function () { ok++; });
    layer.on('tileerror', function () {
      bad++;
      if (!switched && !ok && bad >= 3 && c.fallbackUrl) {
        switched = true;
        map.removeLayer(layer);
        layer = L.tileLayer(c.fallbackUrl, { maxZoom: 20, maxNativeZoom: 19, subdomains: 'abcd', attribution: c.fallbackAttribution || '',
                                             referrerPolicy: 'strict-origin-when-cross-origin' }).addTo(map);
        if (window.console) console.info('TapTap map: main tiles refused, using the fallback provider.');
      }
    });
    return layer;
  };
})();
