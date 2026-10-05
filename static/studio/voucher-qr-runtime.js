/* TapTap Voucher QR extensions.
 * Existing login/code/Wi-Fi QR modes remain unchanged.
 * Adds:
 *   portal  -> stable online TapTap customer portal (/c/?v=CODE)
 *   custom: -> arbitrary QR payload with voucher-token replacement
 */
(function (global) {
  if (!global.TapVoucher || global.TapVoucher.__qrExtrasInstalled) return;
  var original = global.TapVoucher.renderCard;

  function decodeCustom(mode) {
    var raw = String(mode || '').slice(7);
    try { return decodeURIComponent(raw); } catch (e) { return raw; }
  }
  function portalUrl(d) {
    var base = String((d && d.customer_portal_base) || '');
    if (!base) {
      try {
        if (global.location && global.location.origin && global.location.origin !== 'null') base = global.location.origin;
      } catch (e) {}
    }
    if (!base) return String((d && d.code) || '');
    return base.replace(/\/+$/, '') + '/c/?v=' + encodeURIComponent(String((d && d.code) || ''));
  }
  function payload(mode, d) {
    mode = String(mode || '');
    if (mode === 'portal') return portalUrl(d);
    if (mode.indexOf('custom:') === 0) return global.TapVoucher.fill(decodeCustom(mode), d || {});
    return '';
  }
  function qrSvg(text, color, bg, margin) {
    try {
      if (!global.qrcode) throw new Error('QR library unavailable');
      var q = global.qrcode(0, 'M'); q.addData(String(text == null ? '' : text) || ' '); q.make();
      var n = q.getModuleCount(), m = +margin || 0, size = n + m * 2, p = '';
      for (var r = 0; r < n; r++) for (var c = 0; c < n; c++) {
        if (q.isDark(r, c)) p += 'M' + (c + m) + ' ' + (r + m) + 'h1v1h-1z';
      }
      return '<svg viewBox="0 0 ' + size + ' ' + size + '" width="100%" height="100%" shape-rendering="crispEdges" preserveAspectRatio="xMidYMid meet"><rect width="' + size + '" height="' + size + '" fill="' + (bg || '#fff') + '"/><path d="' + p + '" fill="' + (color || '#000') + '"/></svg>';
    } catch (e) {
      return '<div style="width:100%;height:100%;display:grid;place-items:center;text-align:center;padding:1mm;background:#fff3cd;color:#7a5200;font:600 5pt system-ui;border:.2mm solid #e8cf8a">QR data is too long</div>';
    }
  }

  global.TapVoucher.renderCard = function (cfg, d, opts) {
    var card = original(cfg, d, opts);
    var elements = (cfg && cfg.elements || []).filter(function (e) {
      return e && e.type === 'qr' && !e.hidden;
    });
    var nodes = card.querySelectorAll('.tv-qr');
    elements.forEach(function (e, i) {
      var mode = e.content || (cfg && cfg.qr_mode) || 'login';
      if (mode !== 'portal' && String(mode).indexOf('custom:') !== 0) return;
      if (nodes[i]) nodes[i].innerHTML = qrSvg(payload(mode, d), e.color, e.bg, e.margin);
    });
    return card;
  };
  global.TapVoucher.customerPortalUrl = portalUrl;
  global.TapVoucher.__qrExtrasInstalled = true;
})(window);
