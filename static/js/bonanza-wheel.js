/* Bonanza wheel — a small SVG wheel shared by the customer page and the admin preview.
   BonanzaWheel.mount(el, items)  items: [{id, label, color, out}]
   wheel.spinTo(prizeId, done)    turns 6+ times and stops with that prize under the pointer.
   The prize is always picked by the server; the wheel only shows it. */
(function (w) {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function textColor(hex) {
    var h = String(hex || '#888').replace('#', ''); if (h.length === 3) h = h.replace(/(.)/g, '$1$1');
    var r = parseInt(h.substr(0, 2), 16), g = parseInt(h.substr(2, 2), 16), b = parseInt(h.substr(4, 2), 16);
    return (r * 299 + g * 587 + b * 114) / 1000 > 150 ? '#1b1b1b' : '#ffffff';
  }
  function arc(cx, cy, r, a0, a1) {
    var x0 = cx + r * Math.cos(a0), y0 = cy + r * Math.sin(a0), x1 = cx + r * Math.cos(a1), y1 = cy + r * Math.sin(a1);
    return 'M' + cx + ',' + cy + ' L' + x0.toFixed(2) + ',' + y0.toFixed(2) + ' A' + r + ',' + r + ' 0 ' + (a1 - a0 > Math.PI ? 1 : 0) + ' 1 ' + x1.toFixed(2) + ',' + y1.toFixed(2) + ' Z';
  }
  function Wheel(el, items) { this.el = el; this.rot = 0; this.busy = false; this.set(items); }
  Wheel.prototype.set = function (items) {
    this.items = (items || []).slice();
    var n = Math.max(1, this.items.length), C = 200, R = 190, seg = 2 * Math.PI / n, h = [];
    h.push('<svg viewBox="0 0 400 400" class="bw-svg" role="img" aria-label="Prize wheel"><g class="bw-rot" style="transform-origin:200px 200px;transform:rotate(' + this.rot + 'deg)">');
    if (!this.items.length) h.push('<circle cx="200" cy="200" r="190" fill="#e5e7eb"/>');
    for (var i = 0; i < this.items.length; i++) {
      var it = this.items[i], a0 = -Math.PI / 2 + i * seg - seg / 2, a1 = a0 + seg, mid = (a0 + a1) / 2, col = it.color || '#f59e0b';
      h.push('<path d="' + (n === 1 ? 'M200,10 A190,190 0 1 1 199.9,10 Z' : arc(C, C, R, a0, a1)) + '" fill="' + esc(col) + '"' + (it.out ? ' opacity=".35"' : '') + ' stroke="#fff" stroke-width="3"/>');
      var tx = C + R * .62 * Math.cos(mid), ty = C + R * .62 * Math.sin(mid), deg = mid * 180 / Math.PI;
      var nd = ((deg % 360) + 360) % 360; if (nd > 90 && nd < 270) deg += 180;   // keep labels on the left half readable
      var label = String(it.label || ''); if (label.length > 16) label = label.slice(0, 15) + '…';
      h.push('<text x="' + tx.toFixed(1) + '" y="' + ty.toFixed(1) + '" transform="rotate(' + deg.toFixed(1) + ' ' + tx.toFixed(1) + ' ' + ty.toFixed(1) + ')" text-anchor="middle" dominant-baseline="middle" font-size="' + (n > 10 ? 13 : 16) + '" font-weight="800" fill="' + textColor(col) + '" font-family="system-ui,sans-serif">' + esc(label) + (it.out ? ' ✕' : '') + '</text>');
    }
    h.push('</g><circle cx="200" cy="200" r="190" fill="none" stroke="rgba(0,0,0,.12)" stroke-width="6"/>');
    h.push('<circle cx="200" cy="200" r="34" fill="#fff" stroke="rgba(0,0,0,.15)" stroke-width="3"/><text x="200" y="201" text-anchor="middle" dominant-baseline="middle" font-size="13" font-weight="900" fill="#333" font-family="system-ui,sans-serif">SPIN</text>');
    h.push('<path d="M200,2 L186,30 L214,30 Z" fill="#111" stroke="#fff" stroke-width="3" class="bw-pointer"/></svg>');
    this.el.innerHTML = h.join('');
    this.g = this.el.querySelector('.bw-rot');
  };
  Wheel.prototype.indexOf = function (id) { for (var i = 0; i < this.items.length; i++) if (String(this.items[i].id) === String(id)) return i; return -1; };
  Wheel.prototype.spinTo = function (id, done) {
    var i = this.indexOf(id), n = this.items.length || 1, seg = 360 / n, self = this;
    if (i < 0) { done && done(); return; }
    this.busy = true;
    var reduce = w.matchMedia && w.matchMedia('(prefers-reduced-motion: reduce)').matches;
    // Slice i is centred at i*seg (0° = top). To bring it under the pointer, rotate to -i*seg, plus a little wobble inside the slice.
    var wobble = (Math.random() - .5) * seg * .6, target = -i * seg + wobble;
    var base = Math.ceil(this.rot / 360) * 360 + (reduce ? 360 : 360 * 6);
    this.rot = base + ((target % 360) + 360) % 360;
    this.g.style.transition = 'transform ' + (reduce ? .6 : 5.2) + 's cubic-bezier(.12,.64,.12,1)';
    this.g.style.transform = 'rotate(' + this.rot + 'deg)';
    setTimeout(function () { self.busy = false; done && done(); }, reduce ? 700 : 5300);
  };
  w.BonanzaWheel = { mount: function (el, items) { return new Wheel(el, items); } };
})(window);
