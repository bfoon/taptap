/* Control Center → Drag & Drop Configuration Studio: visual effects (static/css/control-studio-skin.css).
   Only dresses and animates the designer's own markup; all behaviour stays in the designer script
   (router_control.html / control_designer.py). Safe to remove: the studio keeps working without it. */
(function () {
  'use strict';
  const shell = document.querySelector('.mikrotik-shell');
  if (!shell) return;
  const panel = shell.closest('section'); if (panel) panel.classList.add('cs-panel');
  const hist = document.getElementById('visualHistoryPanel'); if (hist) hist.classList.add('cs-panel-history');
  const reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── realistic ports: socket by kind, link + activity lights, count of configurations ──
  const kindOf = name => /^sfp/i.test(name) ? 'sfp' : /^(wlan|wifi)/i.test(name) ? 'wlan' : 'eth';
  document.querySelectorAll('.designer-port').forEach(port => {
    if (port.querySelector('.cs-sock')) return;
    const lights = document.createElement('div'); lights.className = 'cs-lights'; lights.setAttribute('aria-hidden', 'true');
    lights.innerHTML = '<i class="cs-light link"></i><i class="cs-light act"></i>';
    const sock = document.createElement('div'); sock.className = 'cs-sock ' + kindOf(port.dataset.target || ''); sock.setAttribute('aria-hidden', 'true');
    const cap = port.querySelector('.port-caption');
    port.insertBefore(lights, port.firstChild); port.insertBefore(sock, cap || null);
  });
  function counts() {
    document.querySelectorAll('[data-port-badges]').forEach(box => {
      const port = box.closest('.designer-port'); if (!port) return;
      const n = box.children.length; let c = port.querySelector('.cs-count');
      if (!n) { if (c) c.remove(); return; }
      if (!c) { c = document.createElement('span'); c.className = 'cs-count'; port.appendChild(c); }
      if (c.textContent !== String(n)) { c.textContent = n; c.title = n + ' configuration' + (n > 1 ? 's' : '') + ' on this port'; }
    });
  }
  const mo = new MutationObserver(counts);
  document.querySelectorAll('[data-port-badges], #routerConfigBadges').forEach(b => mo.observe(b, { childList: true }));
  counts();

  // ── remember what was dragged where (or clicked, when the inspector is used) ──
  let fromEl = null, toEl = null;
  document.addEventListener('dragstart', e => { const t = e.target.closest && e.target.closest('.recipe-tile'); if (t) { fromEl = t; t.classList.add('cs-picked'); } }, true);
  document.addEventListener('dragend', () => { document.querySelectorAll('.cs-picked').forEach(t => t.classList.remove('cs-picked')); }, true);
  document.addEventListener('drop', e => { const z = e.target.closest && e.target.closest('.designer-port, .router-dropzone'); if (z) toEl = z.classList.contains('router-dropzone') ? (z.querySelector('.router-cpu') || z) : z; }, true);
  document.addEventListener('click', e => { const z = e.target.closest && e.target.closest('.designer-port, .router-inspect-trigger'); if (z) { toEl = z.classList.contains('designer-port') ? z : (shell.querySelector('.router-cpu') || shell); fromEl = null; } }, true);

  // ── when the designer reports a successful apply: a packet flies onto the target and it flashes ──
  function fly() {
    const to = toEl && document.body.contains(toEl) ? toEl : shell;
    const from = fromEl && document.body.contains(fromEl) ? fromEl : (document.querySelector('.recipe-palette') || to);
    const flash = () => { to.classList.remove('cs-flash'); void to.offsetWidth; to.classList.add('cs-flash'); setTimeout(() => to.classList.remove('cs-flash'), 1300); };
    if (reduce) { flash(); return; }
    const a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
    const dx = b.left + b.width / 2 - (a.left + a.width / 2), dy = b.top + b.height / 2 - (a.top + a.height / 2);
    ['', 'trail', 'trail'].forEach((cls, i) => {
      const p = document.createElement('div'); p.className = 'cs-packet ' + cls;
      p.style.left = (a.left + a.width / 2) + 'px'; p.style.top = (a.top + a.height / 2) + 'px';
      document.body.appendChild(p);
      setTimeout(() => requestAnimationFrame(() => { p.style.transform = `translate(${dx}px, ${dy}px) scale(.6)`; p.style.opacity = '.15'; }), i * 70);
      setTimeout(() => p.remove(), 900 + i * 70);
    });
    setTimeout(flash, 640);
  }
  const realFetch = window.fetch.bind(window);
  window.fetch = function (url, opts) {
    const p = realFetch(url, opts);
    try {
      if (String(url).includes('/control/designer/apply/') && opts && String(opts.method || '').toUpperCase() === 'POST') {
        p.then(r => r.clone().json()).then(d => { if (d && d.success) fly(); }).catch(() => {});
      }
    } catch (e) { /* never get in the designer's way */ }
    return p;
  };
})();
