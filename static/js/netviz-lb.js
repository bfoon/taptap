/* TapTap Load Balancing Engine
 *
 * Draws every WAN path between the router and the Internet and animates the
 * real traffic on each one. Data comes from /routers/<id>/telemetry/, which
 * reads RouterOS monitor-traffic (true bits/second) and re-evaluates route
 * state on each poll, so failover shows up live.
 *
 * Mount: <div data-lb-engine data-config-id="json-script-id"></div>
 */
(function () {
  'use strict';
  const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const NS = 'http://www.w3.org/2000/svg';
  const HOW = {
    'PCC': 'New connections are split between links by a hash of the addresses. One download or video call stays on one link, so byte totals can drift from the plan even when PCC is working.',
    'ECMP': 'RouterOS sends new connections round-robin across equal-cost routes. Weights come from how many times each gateway is listed.',
    'Failover': 'All traffic uses the primary link. When its gateway stops answering (check-gateway), RouterOS switches to the next distance automatically.',
    'Policy routing': 'Mangle marks or routing rules push chosen traffic (for example one subnet) into dedicated tables with their own Internet link.',
    'Bonding': 'Several physical links act as one interface; the bond mode decides how packets are spread.',
    'Single WAN': 'All customers share one Internet link. A second WAN adds backup or more capacity.',
    'No default route': 'Without a 0.0.0.0/0 route customers cannot reach the Internet through this router.'
  };
  const STATE_TEXT = { active: 'Carrying traffic', standby: 'Standby', down: 'Down', 'no-route': 'No route yet' };

  function s(tag, attrs) { const e = document.createElementNS(NS, tag); for (const k in attrs || {}) e.setAttribute(k, attrs[k]); return e; }
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c])); }
  function bps(v) {
    v = Number(v) || 0;
    if (v >= 1e9) return (v / 1e9).toFixed(1) + ' Gb/s';
    if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e8 ? 0 : 1) + ' Mb/s';
    if (v >= 1e3) return Math.round(v / 1e3) + ' kb/s';
    return Math.round(v) + ' b/s';
  }

  // Provider of an Internet line: an icon by kind of line, and a steady colour per name.
  function ispIcon(name) {
    const n = String(name || '').toLowerCase();
    if (/starlink|satellite|vsat|sat\b/.test(n)) return 'bi-broadcast';
    if (/4g|5g|lte|qcell|africell|comium|mobile|sim/.test(n)) return 'bi-reception-4';
    if (/fib|gamtel|netpage|unique|cable|adsl/.test(n)) return 'bi-ethernet';
    return 'bi-globe2';
  }
  function ispHue(name) { let h = 0; for (const c of String(name || '')) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
  function csrfToken() {
    const i = document.querySelector('[name=csrfmiddlewaretoken]'); const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return (i && i.value) || (m && decodeURIComponent(m[1])) || '';
  }

  class LBEngine {
    constructor(root, cfg) {
      this.root = root; this.cfg = cfg; this.lb = cfg.load_balancing || {};
      this.traffic = {}; this.history = {}; this.totals = {}; this.events = []; this.particles = [];
      this.fail = 0; this.compact = root.hasAttribute('data-compact');
      this.build(); this.draw();
      this.poll();
      if (!reduceMotion) requestAnimationFrame(t => this.tick(t));
      window.addEventListener('resize', () => { clearTimeout(this._r); this._r = setTimeout(() => this.draw(), 150); });
    }

    build() {
      this.root.classList.add('lbx'); if (this.compact) this.root.classList.add('lbx-compact');
      this.root.innerHTML = `
        <div class="lbx-head">
          <div class="lbx-method"><span class="lbx-pill"></span><p class="lbx-desc"></p></div>
          <div class="lbx-meters">
            <div class="lbx-meter"><span>Internet now</span><b class="lbx-total">—</b></div>
            <div class="lbx-meter"><span>Balance</span><b class="lbx-balance">—</b></div>
          </div>
        </div>
        <div class="lbx-stage"><svg class="lbx-svg" role="img" aria-label="Live Internet paths"></svg></div>
        <div class="lbx-lanes"></div>
        <div class="lbx-status" aria-live="polite"></div>
        <details class="lbx-how"><summary>How this router uses its Internet links</summary><p></p></details>
        <div class="lbx-foot"><div class="lbx-facts"></div><div class="lbx-events"></div></div>`;
      this.svg = this.root.querySelector('.lbx-svg');
    }

    links() { return (this.lb.wan_links || []).filter(l => l.interface || l.gateway); }

    draw() {
      const lb = this.lb, links = this.links();
      const pill = this.root.querySelector('.lbx-pill');
      pill.textContent = lb.method || 'Unknown'; pill.className = 'lbx-pill m-' + String(lb.method || 'x').toLowerCase().replace(/\W+/g, '-');
      this.root.querySelector('.lbx-desc').textContent = lb.description || 'Waiting for the first reading from the router…';
      this.root.querySelector('.lbx-how p').textContent = HOW[lb.method] || '';

      // ---------------- SVG stage ----------------
      const W = 1000, laneH = this.compact ? 64 : 80, H = Math.max(170, 60 + links.length * laneH);
      this.svg.setAttribute('viewBox', `0 0 ${W} ${H}`); this.svg.innerHTML = '';
      const rx = 110, ry = H / 2, cx = 890, cy = H / 2, ix = 560;
      const defs = s('defs'); defs.innerHTML = `
        <radialGradient id="lbxCloud-${this.cfg.router_id}" cx="50%" cy="40%" r="60%"><stop offset="0" stop-color="#3a8dff" stop-opacity=".35"/><stop offset="1" stop-color="#3a8dff" stop-opacity="0"/></radialGradient>`;
      this.svg.appendChild(defs);
      const gPaths = s('g'), gPart = s('g', { class: 'lbx-particles' }), gNodes = s('g');
      this.paths = {};
      links.forEach((l, i) => {
        const y = links.length === 1 ? H / 2 : 40 + i * ((H - 80) / Math.max(1, links.length - 1));
        const d = `M${rx + 58},${ry} C${rx + 230},${ry} ${ix - 190},${y} ${ix - 70},${y} L${ix + 70},${y} C${ix + 190},${y} ${cx - 190},${cy} ${cx - 70},${cy}`;
        const base = s('path', { d, class: `lbx-wire st-${l.state}` });
        const glow = s('path', { d, class: `lbx-wire-glow st-${l.state}` });
        gPaths.appendChild(glow); gPaths.appendChild(base);
        const g = s('g', { class: `lbx-isp st-${l.state}`, transform: `translate(${ix},${y})` });
        g.appendChild(s('rect', { x: -70, y: -22, width: 140, height: 44, rx: 12 }));
        const t1 = s('text', { x: 0, y: -3, 'text-anchor': 'middle', class: 'lbx-isp-name' }); t1.textContent = l.label || l.interface || 'WAN';
        if (l.isp) g.style.setProperty('--isp', `hsl(${ispHue(l.isp)} 70% 62%)`), g.classList.add('named');
        const t2 = s('text', { x: 0, y: 13, 'text-anchor': 'middle', class: 'lbx-isp-sub' }); t2.textContent = STATE_TEXT[l.state] || l.state || '';
        g.appendChild(t1); g.appendChild(t2);
        if (l.state === 'down') { const x = s('text', { x: 58, y: -12, class: 'lbx-x', 'text-anchor': 'middle' }); x.textContent = '✕'; g.appendChild(x); }
        gNodes.appendChild(g);
        this.paths[l.id] = { el: base, glow, link: l, len: 0, acc: { d: 0, u: 0 }, isp: g };
      });
      // Router
      const rg = s('g', { class: 'lbx-router', transform: `translate(${rx},${ry})` });
      rg.appendChild(s('rect', { x: -58, y: -34, width: 116, height: 68, rx: 16 }));
      const rt = s('text', { x: 0, y: -2, 'text-anchor': 'middle', class: 'lbx-node-name' }); rt.textContent = this.cfg.router_name;
      const rs = s('text', { x: 0, y: 16, 'text-anchor': 'middle', class: 'lbx-node-sub' }); rs.textContent = 'your customers';
      rg.appendChild(rt); rg.appendChild(rs);
      // Internet cloud
      const cg = s('g', { class: 'lbx-cloud', transform: `translate(${cx},${cy})` });
      cg.appendChild(s('circle', { r: 70, fill: `url(#lbxCloud-${this.cfg.router_id})` }));
      cg.appendChild(s('path', { d: 'M-44,14 a22,22 0 0 1 6,-43 a30,30 0 0 1 56,-4 a22,22 0 0 1 26,30 a17,17 0 0 1 -6,33 z', class: 'lbx-cloud-shape' }));
      const ct = s('text', { x: 4, y: 4, 'text-anchor': 'middle', class: 'lbx-node-name' }); ct.textContent = 'Internet';
      cg.appendChild(ct);
      this.svg.appendChild(gPaths); this.svg.appendChild(gPart); this.svg.appendChild(gNodes); this.svg.appendChild(rg); this.svg.appendChild(cg);
      this.gPart = gPart; this.particles = [];
      Object.values(this.paths).forEach(p => { try { p.len = p.el.getTotalLength(); } catch (e) { p.len = 0; } });
      if (!links.length) {
        const t = s('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle', class: 'lbx-empty' });
        t.textContent = 'No Internet path found yet — refresh the configuration or add a default route.'; this.svg.appendChild(t);
      }

      // ---------------- lane cards ----------------
      const lanes = this.root.querySelector('.lbx-lanes');
      lanes.innerHTML = links.map(l => `
        <article class="lbx-lane st-${l.state}${l.isp ? ' named' : ''}" data-id="${esc(l.id)}"${l.isp ? ` style="--isp:hsl(${ispHue(l.isp)} 70% 45%)"` : ''}>
          <header><span class="lbx-dot"></span>${this.nameHtml(l)}<em>${esc(STATE_TEXT[l.state] || l.state)}</em></header>
          <div class="lbx-namer" hidden></div>
          <div class="lbx-rates"><div><span>Down</span><strong data-k="down">—</strong></div><div><span>Up</span><strong data-k="up">—</strong></div></div>
          <div class="lbx-share" title="Bar: share of traffic now. Tick: planned share.">
            <div class="lbx-share-bar"><i data-k="bar"></i>${l.expected_share != null ? `<u style="left:${l.expected_share}%"></u>` : ''}</div>
            <small><span data-k="pct">0%</span> of traffic${l.expected_share != null ? ` · plan ${l.expected_share}%` : ''}</small>
          </div>
          <svg class="lbx-spark" viewBox="0 0 120 28" preserveAspectRatio="none"><polyline data-k="spark" points=""/></svg>
          <footer>${esc(l.interface || 'interface ?')}${l.gateway ? ' → ' + esc(l.gateway) : ''} · ${esc(l.source || '')}${l.distance ? ' · distance ' + esc(l.distance) : ''}${l.tables && l.tables.length ? ' · ' + esc(l.tables.join(', ')) : ''}</footer>
        </article>`).join('');

      lanes.querySelectorAll('[data-rename]').forEach(b => b.addEventListener('click', () => this.openNamer(b.closest('.lbx-lane'))));

      const facts = this.root.querySelector('.lbx-facts');
      facts.innerHTML = [
        ['PCC rules', lb.pcc_rule_count], ['Marking rules', lb.marked_rule_count], ['Routing tables', lb.routing_table_count],
        ['Routing rules', lb.routing_rule_count], ['Bonds', lb.bond_count]
      ].filter(([, v]) => v).map(([k, v]) => `<span>${k} <b>${v}</b></span>`).join('') + (lb.warnings || []).map(w => `<p class="lbx-warn"><i class="bi bi-exclamation-triangle"></i> ${esc(w)}</p>`).join('');
      this.paintTraffic();
    }

    // ---------------- provider names (Gamtel, QCell, Starlink…) ----------------
    nameHtml(l) {
      const can = !!this.cfg.name_url && !!l.interface;
      if (l.isp) {
        return `<span class="lbx-isp-ic"><i class="bi ${ispIcon(l.isp)}"></i></span><b>${esc(l.isp)}</b><small class="lbx-port">${esc(l.interface)}</small>` +
          (can ? `<button type="button" class="lbx-rename" data-rename aria-label="Rename ${esc(l.isp)}" title="Change the provider name"><i class="bi bi-pencil"></i></button>` : '');
      }
      return `<b>${esc(l.label || l.interface)}</b>` +
        (can ? `<button type="button" class="lbx-name-chip" data-rename title="Say which provider this line is"><i class="bi bi-tag"></i> Name this line</button>` : '');
    }
    openNamer(card) {
      if (!card) return;
      const l = this.links().find(x => x.id === card.dataset.id); if (!l) return;
      const box = card.querySelector('.lbx-namer'), head = card.querySelector('header');
      const list = `lbx-isps-${this.cfg.router_id}`;
      const picks = this.cfg.isp_quick || ['Gamtel', 'QCell', 'Africell', 'Starlink'];
      box.innerHTML = `
        <label class="lbx-namer-q" for="${list}-in-${esc(l.id)}">Which provider is on <b>${esc(l.interface)}</b>?</label>
        <form class="lbx-namer-form">
          <input id="${list}-in-${esc(l.id)}" class="form-control form-control-sm" maxlength="60" list="${list}" value="${esc(l.isp || '')}" placeholder="e.g. Gamtel, QCell, Starlink" autocomplete="off">
          <datalist id="${list}">${(this.cfg.isp_suggestions || []).map(n => `<option value="${esc(n)}">`).join('')}</datalist>
          <button class="btn btn-sm btn-primary">Save</button><button type="button" class="btn btn-sm btn-light" data-cancel>Cancel</button>
        </form>
        <div class="lbx-picks" aria-label="Quick picks">${picks.map(n => `<button type="button" data-pick="${esc(n)}"><i class="bi ${ispIcon(n)}"></i> ${esc(n)}</button>`).join('')}
          ${l.isp ? `<button type="button" class="clear" data-pick="">Use ${esc(l.interface)} again</button>` : ''}</div>
        <small class="lbx-namer-msg" aria-live="polite"></small>`;
      box.hidden = false; head.hidden = true; this.editing = true;
      const input = box.querySelector('input'); input.focus(); input.select();
      const close = () => { box.hidden = true; box.innerHTML = ''; head.hidden = false; this.editing = false; };
      box.querySelector('[data-cancel]').onclick = close;
      box.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); close(); } });
      box.querySelectorAll('[data-pick]').forEach(b => b.onclick = () => this.saveName(l, b.dataset.pick, box, close));
      box.querySelector('form').onsubmit = e => { e.preventDefault(); this.saveName(l, input.value, box, close); };
    }
    async saveName(l, name, box, close) {
      const msg = box.querySelector('.lbx-namer-msg'); box.querySelectorAll('button,input').forEach(x => x.disabled = true);
      try {
        const res = await fetch(this.cfg.name_url, { method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() }, body: JSON.stringify({ interface: l.interface, name: name.trim() }) });
        const d = await res.json().catch(() => ({ success: false, message: 'TapTap did not answer.' }));
        if (!d.success) throw new Error(d.message || 'Could not save the name.');
        // Every link on this port takes the name (a port can carry more than one route).
        (this.lb.wan_links || []).forEach(x => { if (x.interface === l.interface) { x.isp = d.name; x.label = d.name || x.interface; } });
        close(); this.draw();
        window.dispatchEvent(new CustomEvent('taptap:wan-named', { detail: { router_id: this.cfg.router_id, interface: l.interface, name: d.name } }));
      } catch (err) {
        box.querySelectorAll('button,input').forEach(x => x.disabled = false); msg.textContent = err.message; msg.classList.add('bad');
      }
    }

    async poll() {
      clearTimeout(this._t);
      if (document.hidden) { this._t = setTimeout(() => this.poll(), 5000); return; }
      let delay = 3000;
      try {
        const res = await fetch(this.cfg.telemetry_url, { credentials: 'same-origin', cache: 'no-store' });
        const data = await res.json();
        if (!data.success) throw new Error(data.message || 'Router did not answer');
        const prev = this.lb; this.lb = data.load_balancing || this.lb; this.traffic = data.traffic || {};
        this.detectEvents(prev, this.lb);
        if (this.signature(prev) !== this.signature(this.lb) && !this.editing) this.draw(); else this.paintTraffic();
        this.fail = 0; this.status('');
      } catch (err) {
        this.fail++; delay = Math.min(30000, 3000 * 2 ** this.fail);
        this.traffic = {}; this.paintTraffic();
        this.status(`Live data paused: ${err.message}. Showing the last saved analysis; retrying in ${Math.round(delay / 1000)}s.`);
      }
      this._t = setTimeout(() => this.poll(), delay);
    }
    signature(lb) { return JSON.stringify([lb.method, (lb.wan_links || []).map(l => [l.id, l.state, l.expected_share, l.label])]); }
    status(msg) { const e = this.root.querySelector('.lbx-status'); e.textContent = msg; e.classList.toggle('show', !!msg); }

    detectEvents(prev, next) {
      if (!prev || !prev.wan_links) return;
      const before = {}; prev.wan_links.forEach(l => before[l.id] = l.state);
      (next.wan_links || []).forEach(l => {
        const was = before[l.id];
        if (was && was !== l.state) {
          const moved = (next.wan_links || []).filter(x => x.state === 'active' && x.id !== l.id).map(x => x.label).join(', ');
          const text = l.state === 'down' ? `${l.label} went down${moved ? ' — traffic moved to ' + moved : ''}` :
            l.state === 'active' ? `${l.label} is carrying traffic again` : `${l.label}: ${was} → ${l.state}`;
          this.events.unshift({ t: new Date(), text, bad: l.state === 'down' });
        }
      });
      this.events = this.events.slice(0, 6);
      this.root.querySelector('.lbx-events').innerHTML = this.events.map(e => `<div class="${e.bad ? 'bad' : ''}"><time>${e.t.toLocaleTimeString()}</time> ${esc(e.text)}</div>`).join('');
    }

    paintTraffic() {
      const links = this.links(); let total = 0; const per = {};
      const zero = { rx_bps: 0, tx_bps: 0 };
      // A down link carries nothing, whatever stale counters say.
      links.forEach(l => { const t = l.state === 'down' ? zero : (this.traffic[l.interface] || zero); per[l.id] = t; total += (t.rx_bps || 0) + (t.tx_bps || 0); });
      let sumDown = 0, sumUp = 0;
      links.forEach(l => {
        const t = per[l.id], card = this.root.querySelector(`.lbx-lane[data-id="${CSS.escape(l.id)}"]`); if (!card) return;
        const both = (t.rx_bps || 0) + (t.tx_bps || 0); sumDown += t.rx_bps || 0; sumUp += t.tx_bps || 0;
        const pct = total ? Math.round(both * 100 / total) : 0;
        card.querySelector('[data-k=down]').textContent = this.hasData() ? bps(t.rx_bps) : '—';
        card.querySelector('[data-k=up]').textContent = this.hasData() ? bps(t.tx_bps) : '—';
        card.querySelector('[data-k=bar]').style.width = pct + '%';
        card.querySelector('[data-k=pct]').textContent = pct + '%';
        const h = this.history[l.id] = (this.history[l.id] || []).concat([both]).slice(-40);
        const max = Math.max(1, ...h);
        card.querySelector("[data-k=spark]").setAttribute("points", h.map((v, i) => `${(h.length > 1 ? i * 120 / (h.length - 1) : 0).toFixed(1)},${(27 - v / max * 25).toFixed(1)}`).join(' '));
        this.totals[l.id] = (this.totals[l.id] || 0) * 0.9 + both * 0.1;  // ~30s exponential average
        const p = this.paths && this.paths[l.id];
        if (p) {
          p.down = t.rx_bps || 0; p.up = t.tx_bps || 0;
          const w = both ? Math.min(14, 2 + Math.log10(1 + both / 1e4) * 2.2) : 0;
          p.glow.style.strokeWidth = w + 'px'; p.glow.style.opacity = w ? .55 : 0;
          const sub = p.isp.querySelector('.lbx-isp-sub');
          if (sub) sub.textContent = (l.isp ? l.interface + ' · ' : '') + (both ? `↓ ${bps(p.down)}` : (STATE_TEXT[l.state] || l.state));
        }
      });
      this.root.querySelector('.lbx-total').innerHTML = this.hasData() ? `↓ ${bps(sumDown)} <small>↑ ${bps(sumUp)}</small>` : '—';
      this.root.querySelector('.lbx-balance').textContent = this.balanceText(links);
    }
    hasData() { return Object.keys(this.traffic || {}).length > 0; }
    balanceText(links) {
      const planned = links.filter(l => l.expected_share != null && l.state === 'active');
      if (!this.hasData()) return '—';
      if (planned.length < 2) return this.lb.method === 'Failover' ? 'Primary only' : 'Single path';
      const sum = planned.reduce((a, l) => a + (this.totals[l.id] || 0), 0); if (!sum) return 'Idle';
      const dev = Math.max(...planned.map(l => Math.abs((this.totals[l.id] || 0) * 100 / sum - l.expected_share)));
      return dev < 12 ? 'On plan' : dev < 25 ? 'Slightly uneven' : 'Uneven';
    }

    tick(now) {
      const dt = Math.min(0.05, (now - (this._last || now)) / 1000); this._last = now;
      if (this.paths && !document.hidden) {
        Object.values(this.paths).forEach(p => {
          if (!p.len || p.link.state === 'down') return;
          [['d', p.down, -1], ['u', p.up, 1]].forEach(([k, v, dir]) => {
            if (!v || v < 1000) return;
            const rate = Math.min(14, 0.6 + Math.log10(v / 1e3) * 2.1);
            p.acc[k] += rate * dt;
            while (p.acc[k] >= 1 && this.particles.length < 420) {
              p.acc[k] -= 1;
              const c = s('circle', { r: Math.min(5, 1.8 + Math.log10(v / 1e4 + 1) * 1.1), class: k === 'd' ? 'pt-down' : 'pt-up' });
              this.gPart.appendChild(c);
              this.particles.push({ c, p, t: 0, dir, speed: 140 + Math.min(360, Math.log10(v / 1e3 + 1) * 70), jitter: (Math.random() - 0.5) * 5 });
            }
          });
        });
      }
      for (let i = this.particles.length - 1; i >= 0; i--) {
        const q = this.particles[i]; q.t += q.speed * dt / q.p.len;
        if (q.t >= 1 || !q.c.isConnected) { q.c.remove(); this.particles.splice(i, 1); continue; }
        const at = q.p.el.getPointAtLength((q.dir === 1 ? q.t : 1 - q.t) * q.p.len);
        q.c.setAttribute('cx', at.x); q.c.setAttribute('cy', at.y + q.jitter);
        q.c.setAttribute('opacity', Math.min(1, Math.min(q.t, 1 - q.t) * 10));
      }
      requestAnimationFrame(t => this.tick(t));
    }
  }

  window.TapTapLBEngine = LBEngine;
  function mountAll() {
    document.querySelectorAll('[data-lb-engine]:not([data-mounted])').forEach(root => {
      const cfgEl = document.getElementById(root.dataset.configId); if (!cfgEl) return;
      root.setAttribute('data-mounted', '1');
      try { new LBEngine(root, JSON.parse(cfgEl.textContent)); } catch (e) { root.textContent = 'Load balancing view failed to start: ' + e.message; }
    });
  }
  document.addEventListener('DOMContentLoaded', mountAll);
  window.addEventListener('taptap:mount-lb', mountAll);
})();
