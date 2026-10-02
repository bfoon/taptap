/* TapTap NetMap — live network topology for Django.
 *
 * Django (WSGI) answers short requests, it does not hold sockets open like the
 * Node.js build did. So the map is fed by three small endpoints:
 *   graph   – the whole map from the database (instant, no router calls)
 *   refresh – live discovery of ONE router (run in parallel, bounded time)
 *   live    – bits/second for the drawn interfaces (cached server-side ~2s)
 * A dead or slow router therefore never blocks the page or the others.
 */
(function () {
  'use strict';

  const NODE_W = 184, NODE_H = 64, CLUSTER_W = 150, LAYER_GAP = 150, COL_GAP = 44;
  const ICONS = {
    internet: 'bi-globe2', isp: 'bi-broadcast-pin', wan: 'bi-hdd-network-fill', router: 'bi-router-fill', siterouter: 'bi-router',
    switch: 'bi-hdd-rack', wifi: 'bi-wifi', network: 'bi-diagram-3', clients: 'bi-phone'
  };
  const TYPE_LABEL = {
    internet: 'Internet', isp: 'ISP modem', wan: 'WAN link', router: 'MikroTik (managed)', siterouter: 'Router (not managed)', switch: 'Switch',
    wifi: 'Access point', network: 'Network device', clients: 'Connected devices'
  };
  const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function svgEl(tag, attrs) { const e = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const k in attrs || {}) e.setAttribute(k, attrs[k]); return e; }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c])); }
  function fmtBps(v) {
    v = Number(v) || 0;
    if (v >= 1e9) return (v / 1e9).toFixed(v >= 1e10 ? 0 : 1) + ' Gb/s';
    if (v >= 1e6) return (v / 1e6).toFixed(v >= 1e8 ? 0 : 1) + ' Mb/s';
    if (v >= 1e3) return Math.round(v / 1e3) + ' kb/s';
    return v ? Math.round(v) + ' b/s' : '0';
  }
  function csrf() { const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); const i = document.querySelector('[name=csrfmiddlewaretoken]'); return (i && i.value) || (m && decodeURIComponent(m[1])) || ''; }

  // ------------------------------------------------------------------ layout
  function layout(graph, opts) {
    const nodes = graph.nodes.filter(n => (opts.clients || n.type !== 'clients') && (opts.offline || n.status !== 'offline' || n.type === 'router' || n.type === 'wan'));
    const ids = new Set(nodes.map(n => n.id));
    const edges = graph.edges.filter(e => ids.has(e.source) && ids.has(e.target));
    const adj = {}; nodes.forEach(n => adj[n.id] = []);
    edges.forEach(e => { adj[e.source].push(e.target); adj[e.target].push(e.source); });

    // Rank = longest path from the Internet along edge direction (internet → modem → WAN → router → …),
    // so a router always sits below its own WAN chain. Islands (unreachable routers) get BFS ranks.
    const depth = {};
    if (ids.has('internet')) depth.internet = 0;
    const cap = Math.min(14, nodes.length);
    for (let it = 0; it < cap; it++) {
      let moved = false;
      edges.forEach(e => {
        if (depth[e.source] == null) return;
        const want = Math.min(cap, depth[e.source] + 1);
        if (depth[e.target] == null || depth[e.target] < want) { depth[e.target] = want; moved = true; }
      });
      if (!moved) break;
    }
    const queue = [];
    const bfs = () => { while (queue.length) { const id = queue.shift(); for (const nb of adj[id]) if (depth[nb] == null) { depth[nb] = depth[id] + 1; queue.push(nb); } } };
    nodes.filter(n => depth[n.id] == null && n.type === 'router').forEach(n => { depth[n.id] = 2; queue.push(n.id); bfs(); });
    nodes.forEach(n => { if (depth[n.id] == null) depth[n.id] = 3; });
    // Compact: pull WAN/ISP nodes down next to their router so long chains do not leave gaps.
    edges.forEach(e => { const s = nodes.find(n => n.id === e.source); if (s && (s.type === 'wan' || s.type === 'isp') && depth[e.target] - depth[s.id] > 1 && e.target.startsWith('router:')) depth[s.id] = depth[e.target] - 1; });
    const tiers = {};
    nodes.forEach(n => (tiers[depth[n.id]] = tiers[depth[n.id]] || []).push(n));
    const tierKeys = Object.keys(tiers).map(Number).sort((a, b) => a - b);
    const order = {};
    tierKeys.forEach(t => tiers[t].forEach((n, i) => order[n.id] = i));
    // Barycenter ordering: a few sweeps give far fewer crossings.
    for (let pass = 0; pass < 6; pass++) {
      const seq = pass % 2 === 0 ? tierKeys.slice(1) : tierKeys.slice(0, -1).reverse();
      seq.forEach(t => {
        const ref = pass % 2 === 0 ? t - 1 : t + 1;
        tiers[t].forEach(n => {
          const ns = adj[n.id].filter(x => depth[x] === ref);
          n._bc = ns.length ? ns.reduce((s, x) => s + order[x], 0) / ns.length : order[n.id];
        });
        tiers[t].sort((a, b) => a._bc - b._bc || String(a.label).localeCompare(String(b.label)));
        tiers[t].forEach((n, i) => order[n.id] = i);
      });
    }
    const pos = {}; let maxW = 0;
    tierKeys.forEach(t => {
      const list = tiers[t];
      const width = list.reduce((s, n) => s + (n.type === 'clients' ? CLUSTER_W : NODE_W) + COL_GAP, -COL_GAP);
      maxW = Math.max(maxW, width);
    });
    tierKeys.forEach((t, row) => {
      const list = tiers[t];
      const width = list.reduce((s, n) => s + (n.type === 'clients' ? CLUSTER_W : NODE_W) + COL_GAP, -COL_GAP);
      let x = (maxW - width) / 2;
      list.forEach(n => {
        const w = n.type === 'clients' ? CLUSTER_W : NODE_W;
        pos[n.id] = { x, y: row * (NODE_H + LAYER_GAP), w, h: n.type === 'clients' ? 52 : NODE_H };
        x += w + COL_GAP;
      });
    });
    const height = tierKeys.length * (NODE_H + LAYER_GAP) - LAYER_GAP;
    return { nodes, edges, pos, width: Math.max(maxW, 400), height: Math.max(height, 200) };
  }

  // ------------------------------------------------------------------ map
  class NetMap {
    constructor(root, cfg) {
      this.root = root; this.cfg = cfg; this.graph = cfg.graph;
      this.opts = { clients: true, offline: true, labels: true, live: true };
      this.view = { x: 0, y: 0, k: 1 }; this.traffic = {}; this.particles = []; this.edgePaths = {};
      this.liveFail = 0; this.search = '';
      this.build(); this.render(true); this.bindPan();
      if (cfg.autoRefreshIds && cfg.autoRefreshIds.length) this.discover(cfg.autoRefreshIds, true);
      this.startLive();
      if (!reduceMotion) requestAnimationFrame(t => this.tick(t));
    }

    build() {
      const r = this.root; r.classList.add('nm');
      r.innerHTML = `
        <div class="nm-toolbar">
          <div class="nm-search"><i class="bi bi-search"></i><input type="search" placeholder="Find a device, MAC or IP" aria-label="Find a device, MAC or IP"></div>
          <div class="nm-toggles" role="group" aria-label="Map options">
            <button type="button" class="nm-chip on" data-opt="clients" aria-pressed="true"><i class="bi bi-phone"></i> Devices</button>
            <button type="button" class="nm-chip on" data-opt="offline" aria-pressed="true"><i class="bi bi-clock-history"></i> Offline</button>
            <button type="button" class="nm-chip on" data-opt="labels" aria-pressed="true"><i class="bi bi-tag"></i> Port labels</button>
            <button type="button" class="nm-chip on nm-live" data-opt="live" aria-pressed="true"><span class="nm-pulse"></span> Live traffic</button>
          </div>
          <div class="nm-actions">
            <button type="button" class="nm-icon" data-act="zoom-in" title="Zoom in" aria-label="Zoom in"><i class="bi bi-plus-lg"></i></button>
            <button type="button" class="nm-icon" data-act="zoom-out" title="Zoom out" aria-label="Zoom out"><i class="bi bi-dash-lg"></i></button>
            <button type="button" class="nm-icon" data-act="fit" title="Fit to screen" aria-label="Fit to screen"><i class="bi bi-arrows-angle-contract"></i></button>
            <button type="button" class="nm-icon" data-act="full" title="Full screen" aria-label="Full screen"><i class="bi bi-fullscreen"></i></button>
          </div>
        </div>
        <div class="nm-discovery" aria-live="polite"></div>
        <div class="nm-viewport" tabindex="-1">
          <div class="nm-stage"><svg class="nm-edges"></svg><div class="nm-nodes"></div></div>
          <div class="nm-empty" hidden></div>
        </div>
        <div class="nm-legend">
          <span><i class="lg lg-down"></i> Download</span><span><i class="lg lg-up"></i> Upload</span>
          <span><i class="lg lg-wifi"></i> Wireless</span><span><i class="lg lg-off"></i> Offline / standby</span>
          <span class="nm-hint">Drag to move · scroll to zoom · click a device for details</span>
        </div>
        <aside class="nm-drawer" aria-hidden="true"><button type="button" class="nm-drawer-close" aria-label="Close details"><i class="bi bi-x-lg"></i></button><div class="nm-drawer-body"></div></aside>`;
      this.viewport = r.querySelector('.nm-viewport'); this.stage = r.querySelector('.nm-stage');
      this.svg = r.querySelector('.nm-edges'); this.nodeLayer = r.querySelector('.nm-nodes');
      this.drawer = r.querySelector('.nm-drawer'); this.discoveryBar = r.querySelector('.nm-discovery');
      r.querySelectorAll('[data-opt]').forEach(b => b.addEventListener('click', () => {
        const k = b.dataset.opt; this.opts[k] = !this.opts[k]; b.classList.toggle('on', this.opts[k]); b.setAttribute('aria-pressed', this.opts[k]);
        if (k === 'labels') this.root.classList.toggle('nm-no-labels', !this.opts.labels);
        else if (k === 'live') { this.opts.live ? this.startLive() : this.stopLive(); }
        else this.render(false);
      }));
      r.querySelector('[data-act=zoom-in]').onclick = () => this.zoomBy(1.25);
      r.querySelector('[data-act=zoom-out]').onclick = () => this.zoomBy(0.8);
      r.querySelector('[data-act=fit]').onclick = () => this.fit();
      r.querySelector('[data-act=full]').onclick = () => this.fullscreen();
      r.querySelector('.nm-drawer-close').onclick = () => this.closeDrawer();
      r.addEventListener('keydown', e => { if (e.key === 'Escape') this.closeDrawer(); });
      const input = r.querySelector('.nm-search input');
      input.addEventListener('input', () => { this.search = input.value.trim().toLowerCase(); this.applySearch(); });
      input.addEventListener('keydown', e => { if (e.key === 'Enter') { const hit = this.nodeLayer.querySelector('.nm-node.match'); if (hit) { this.focusNode(hit.dataset.id); this.openDrawer(hit.dataset.id); } } });
      document.addEventListener('visibilitychange', () => { if (!document.hidden && this.opts.live) this.pollLive(); });
      window.addEventListener('resize', () => { clearTimeout(this._rs); this._rs = setTimeout(() => this.fit(), 200); });
    }

    render(fit) {
      const L = this.L = layout(this.graph, this.opts);
      this.svg.setAttribute('width', L.width + 40); this.svg.setAttribute('height', L.height + 40);
      this.svg.setAttribute('viewBox', `-20 -20 ${L.width + 40} ${L.height + 40}`);
      this.stage.style.width = (L.width + 40) + 'px'; this.stage.style.height = (L.height + 40) + 'px';
      const empty = this.root.querySelector('.nm-empty');
      const hasRouters = L.nodes.some(n => n.type === 'router');
      empty.hidden = hasRouters;
      if (!hasRouters) empty.innerHTML = '<i class="bi bi-router"></i><b>No routers yet</b><span>Add a MikroTik on the Routers page and it will appear here.</span>';

      // Edges
      this.svg.innerHTML = ''; this.edgePaths = {};
      const gEdges = svgEl('g', { class: 'nm-edge-layer' }), gLabels = svgEl('g', { class: 'nm-label-layer' });
      this.gParticles = svgEl('g', { class: 'nm-particle-layer' });
      L.edges.forEach(e => {
        const a = L.pos[e.source], b = L.pos[e.target]; if (!a || !b) return;
        const x1 = a.x + a.w / 2, y1 = a.y + a.h, x2 = b.x + b.w / 2, y2 = b.y, my = (y1 + y2) / 2;
        const d = `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`;
        const path = svgEl('path', { d, class: `nm-edge k-${e.kind} ${e.online ? 'on' : 'off'}`, 'data-edge': e.id });
        gEdges.appendChild(path);
        const glow = svgEl('path', { d, class: 'nm-edge-glow', 'data-glow': e.id }); gEdges.insertBefore(glow, path);
        this.edgePaths[e.id] = { path, glow, edge: e, len: 0 };
        if (e.label) {
          const t = svgEl('g', { class: 'nm-elabel', 'data-label': e.id });
          const txt = svgEl('text', { 'text-anchor': 'middle', dy: '0.35em' }); txt.textContent = e.label;
          const rate = svgEl('text', { 'text-anchor': 'middle', dy: '1.75em', class: 'nm-rate' });
          t.appendChild(svgEl('rect', { x: -30, y: -9, height: 18, rx: 9 })); t.appendChild(txt); t.appendChild(rate);
          gLabels.appendChild(t);
          this.edgePaths[e.id + ':label'] = { g: t, path, pending: true };
        }
      });
      this.svg.appendChild(gEdges); this.svg.appendChild(this.gParticles); this.svg.appendChild(gLabels);
      // Place labels 72% along each curve (where fanned-out links are furthest apart), sized to the text.
      const incoming = {}; L.edges.forEach(e => incoming[e.target] = (incoming[e.target] || 0) + 1);
      Object.keys(this.edgePaths).filter(k => k.endsWith(':label')).forEach(k => {
        const l = this.edgePaths[k]; delete this.edgePaths[k];
        const edge = L.edges.find(x => x.id === k.slice(0, -6));
        const frac = edge && incoming[edge.target] > 1 ? 0.38 : 0.72;  // links merging into one node: label early
        try {
          const len = l.path.getTotalLength(), at = l.path.getPointAtLength(len * frac);
          l.g.setAttribute('transform', `translate(${at.x.toFixed(1)},${at.y.toFixed(1)})`);
          const w = Math.max(34, l.g.querySelector('text').getComputedTextLength() + 16);
          const rect = l.g.querySelector('rect'); rect.setAttribute('x', -w / 2); rect.setAttribute('width', w);
        } catch (err) { /* hidden container: keep default */ }
      });
      Object.values(this.edgePaths).forEach(p => { try { p.len = p.path.getTotalLength(); } catch (err) { p.len = 0; } });
      this.particles = [];

      // Nodes (HTML for crisp text and icons; transitions animate re-layouts)
      const existing = {}; this.nodeLayer.querySelectorAll('.nm-node').forEach(n => existing[n.dataset.id] = n);
      const seen = new Set();
      L.nodes.forEach(n => {
        const p = L.pos[n.id]; seen.add(n.id);
        let div = existing[n.id];
        if (!div) {
          div = el('div'); div.tabIndex = 0; div.setAttribute('role', 'button');
          div.addEventListener('click', ev => { ev.stopPropagation(); this.openDrawer(n.id); });
          div.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); this.openDrawer(n.id); } });
          this.nodeLayer.appendChild(div);
        }
        div.dataset.id = n.id;
        div.className = `nm-node t-${n.type} s-${n.status || 'online'}${n.state ? ' st-' + n.state : ''}${n.suggested ? ' suggested' : ''}`;
        div.style.width = p.w + 'px'; div.style.height = p.h + 'px';
        div.style.transform = `translate(${p.x}px,${p.y}px)`;
        div.setAttribute('aria-label', `${TYPE_LABEL[n.type] || n.type}: ${n.label}`);
        const badge = n.type === 'wan' && n.share != null && n.state === 'active' ? `<em class="nm-share">${n.share}%</em>` :
          n.type === 'wan' ? `<em class="nm-state">${esc(n.state || '')}</em>` :
          n.type === 'router' && n.lb_method && n.lb_method !== 'Single WAN' ? `<em class="nm-state">${esc(n.lb_method)}</em>` :
          n.type === 'siterouter' && n.suggested ? `<em class="nm-state" title="Found by TapTap — confirm it in the Detail tab">found?</em>` :
          n.type === 'siterouter' && n.brand ? `<em class="nm-brand">${esc(n.brand)}</em>` : '';
        div.innerHTML = `<span class="nm-ic"><i class="bi ${ICONS[n.type] || 'bi-circle'}"></i></span>
          <span class="nm-tx"><b>${esc(n.label)}</b><small>${esc(n.sub || TYPE_LABEL[n.type] || '')}</small></span>${badge}
          <span class="nm-dot" aria-hidden="true"></span>`;
      });
      Object.keys(existing).forEach(id => { if (!seen.has(id)) existing[id].remove(); });
      this.applySearch(); this.paintTraffic();
      if (fit) requestAnimationFrame(() => this.fit());
    }

    // ------------------------------------------------ pan / zoom
    applyView() { this.stage.style.transform = `translate(${this.view.x}px,${this.view.y}px) scale(${this.view.k})`; }
    fit() {
      if (!this.L) return;
      const vw = this.viewport.clientWidth || 800, vh = this.viewport.clientHeight || 500;
      const k = Math.min(1.15, Math.max(0.25, Math.min((vw - 30) / (this.L.width + 40), (vh - 30) / (this.L.height + 40))));
      this.view = { k, x: (vw - (this.L.width + 40) * k) / 2, y: Math.max(10, (vh - (this.L.height + 40) * k) / 2) };
      this.applyView();
    }
    zoomBy(f, cx, cy) {
      const vw = this.viewport.clientWidth, vh = this.viewport.clientHeight;
      cx = cx == null ? vw / 2 : cx; cy = cy == null ? vh / 2 : cy;
      const k = Math.min(2.5, Math.max(0.2, this.view.k * f)); const r = k / this.view.k;
      this.view.x = cx - (cx - this.view.x) * r; this.view.y = cy - (cy - this.view.y) * r; this.view.k = k; this.applyView();
    }
    focusNode(id) {
      const p = this.L.pos[id]; if (!p) return;
      const vw = this.viewport.clientWidth, vh = this.viewport.clientHeight, k = Math.max(this.view.k, 0.9);
      this.view = { k, x: vw / 2 - (p.x + 20 + p.w / 2) * k, y: vh / 2 - (p.y + 20 + p.h / 2) * k }; this.stage.classList.add('nm-glide'); this.applyView();
      setTimeout(() => this.stage.classList.remove('nm-glide'), 450);
    }
    bindPan() {
      const vp = this.viewport; let drag = null;
      vp.addEventListener('pointerdown', e => { if (e.target.closest('.nm-node')) return; drag = { x: e.clientX, y: e.clientY, vx: this.view.x, vy: this.view.y }; vp.setPointerCapture(e.pointerId); vp.classList.add('grabbing'); });
      vp.addEventListener('pointermove', e => { if (!drag) return; this.view.x = drag.vx + e.clientX - drag.x; this.view.y = drag.vy + e.clientY - drag.y; this.applyView(); });
      const end = () => { drag = null; vp.classList.remove('grabbing'); };
      vp.addEventListener('pointerup', end); vp.addEventListener('pointercancel', end);
      vp.addEventListener('wheel', e => { e.preventDefault(); const r = vp.getBoundingClientRect(); this.zoomBy(e.deltaY < 0 ? 1.12 : 0.89, e.clientX - r.left, e.clientY - r.top); }, { passive: false });
      vp.addEventListener('click', e => { if (!e.target.closest('.nm-node')) this.closeDrawer(); });
    }
    fullscreen() {
      if (document.fullscreenElement) document.exitFullscreen();
      else if (this.root.requestFullscreen) this.root.requestFullscreen().then(() => setTimeout(() => this.fit(), 150)).catch(() => {});
    }

    // ------------------------------------------------ search
    applySearch() {
      const q = this.search;
      this.root.classList.toggle('nm-searching', !!q);
      this.nodeLayer.querySelectorAll('.nm-node').forEach(div => {
        if (!q) { div.classList.remove('match'); return; }
        const n = this.graph.nodes.find(x => x.id === div.dataset.id); if (!n) return;
        let hay = [n.label, n.sub, n.mac, n.ip, n.board, n.port, n.iface].join(' ').toLowerCase();
        if (n.devices) hay += ' ' + n.devices.map(d => [d.name, d.mac, d.ip].join(' ')).join(' ').toLowerCase();
        div.classList.toggle('match', hay.includes(q));
      });
    }

    // ------------------------------------------------ drawer
    openDrawer(id) {
      const n = this.graph.nodes.find(x => x.id === id); if (!n) return;
      this.nodeLayer.querySelectorAll('.nm-node.sel').forEach(x => x.classList.remove('sel'));
      const div = this.nodeLayer.querySelector(`[data-id="${CSS.escape(id)}"]`); if (div) div.classList.add('sel');
      const rows = [];
      const add = (k, v) => { if (v !== undefined && v !== null && v !== '') rows.push(`<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`); };
      let extra = '';
      if (n.type === 'router') {
        add('Address', n.sub); add('Model', n.model); add('Status', n.status === 'online' ? 'Online' : 'Unreachable'); add('Ports', n.ports);
        add('Internet method', n.lb_method); add('Last contact', n.last_seen ? new Date(n.last_seen).toLocaleString() : 'never');
        if (n.error) extra += `<div class="nm-alert"><i class="bi bi-exclamation-triangle"></i> ${esc(n.error)}</div>`;
        extra += `<div class="nm-drawer-actions"><button type="button" class="btn btn-sm btn-primary" data-discover="${n.router_id}"><i class="bi bi-radar"></i> Discover now</button>
          <a class="btn btn-sm btn-outline-secondary" href="${this.cfg.controlUrl.replace('/0/', '/' + n.router_id + '/')}"><i class="bi bi-sliders"></i> Control Center</a></div>`;
      } else if (n.type === 'wan') {
        add('Interface', n.iface); add('Gateway', n.sub); add('State', n.state); add('Role', n.role);
        if (n.share != null) add('Planned share', n.share + '%'); add('Routing tables', (n.tables || []).join(', ')); add('Distance', n.distance); add('Type', n.source);
        extra += `<div class="nm-live-box" data-live-for="${esc(n.router_id)}:${esc(n.iface)}"></div>`;
      } else if (n.type === 'siterouter') {
        add('Maker', n.brand); add('Model', n.model); add('IP address', n.ip); add('MAC address', n.mac);
        add('Plugged into', n.port ? `${(this.cfg.routers.find(r => r.id === n.router_id) || {}).name || 'MikroTik'} › ${n.port}` : '');
        add('Works as', n.mode === 'ap' ? 'Access point — customers pass through' : n.mode === 'nat' ? 'Router (NAT) — customers hidden behind it' : '');
        add('Customers on its port', n.clients); add('Status', n.status === 'online' ? 'Online' : 'Not seen in the last discovery');
        if (n.suggested) extra += `<div class="nm-alert"><i class="bi bi-question-circle"></i> TapTap found this and thinks it is a router. Confirm it or mark it “not a router” in the Detail tab.</div>`;
        if ((n.reasons || []).length) extra += `<div class="nm-why"><b>Why TapTap thinks so</b>${n.reasons.map(r => `<div class="${r.sign === '+' ? 'pro' : 'con'}"><i class="bi ${r.sign === '+' ? 'bi-plus-circle' : 'bi-dash-circle'}"></i> ${esc(r.text)}</div>`).join('')}</div>`;
        extra += `<div class="nm-drawer-actions"><button type="button" class="btn btn-sm btn-primary" data-detail-key="${esc(n.key)}"><i class="bi bi-list-columns"></i> Open in Detail</button></div>`;
      } else if (n.type === 'clients') {
        add('Port', n.port); add('Devices', n.count); add('Wireless', n.wifi); add('Logged in to hotspot', n.hotspot);
        extra += `<input class="form-control form-control-sm nm-dev-filter" placeholder="Filter devices"><div class="nm-devlist">${(n.devices || []).map(d => `
          <div class="nm-dev"><i class="bi ${d.kind === 'wifi' ? 'bi-wifi' : 'bi-pc-display'}"></i><div><b>${esc(d.name || d.ip || d.mac || 'Device')}</b>
          <small>${esc(d.ip || 'no IP')} · ${d.mac ? `<a class="tt-dlink" href="/go/device/${encodeURIComponent(d.mac)}/">${esc(d.mac)}</a>` : 'no MAC'}${d.via ? ' · via ' + esc(d.via) : ''}</small></div></div>`).join('')}</div>`;
        if (n.count > (n.devices || []).length) extra += `<p class="nm-more">Showing ${(n.devices || []).length} of ${n.count}. The full list is in Router Inventory.</p>`;
      } else if (n.type !== 'internet') {
        add('IP address', n.ip); add('MAC address', n.mac); add('Model', n.board); add('Platform', n.platform); add('Version', n.version);
        add('Seen on port', n.port); add('Found by', n.discovered_by); add('Status', n.status === 'online' ? 'Online' : 'Not seen in the last discovery');
        add('Last seen', n.last_seen ? new Date(n.last_seen).toLocaleString() : '');
      } else {
        rows.push('<p class="nm-more">Everything above your WAN links. Traffic here is the sum of all Internet paths.</p>');
      }
      this.drawer.querySelector('.nm-drawer-body').innerHTML = `
        <div class="nm-drawer-head t-${n.type}"><span class="nm-ic"><i class="bi ${ICONS[n.type]}"></i></span><div><small>${esc(TYPE_LABEL[n.type] || n.type)}</small><h4>${esc(n.label)}</h4></div></div>
        <dl class="nm-facts">${rows.join('')}</dl>${extra}`;
      const f = this.drawer.querySelector('.nm-dev-filter');
      if (f) f.addEventListener('input', () => { const q = f.value.toLowerCase(); this.drawer.querySelectorAll('.nm-dev').forEach(d => d.hidden = !d.textContent.toLowerCase().includes(q)); });
      const btn = this.drawer.querySelector('[data-discover]'); if (btn) btn.onclick = () => this.discover([Number(btn.dataset.discover)]);
      const det = this.drawer.querySelector('[data-detail-key]');
      if (det) det.onclick = () => { this.closeDrawer(); window.dispatchEvent(new CustomEvent('taptap:detail', { detail: det.dataset.detailKey })); };
      this.drawer.classList.add('open'); this.drawer.setAttribute('aria-hidden', 'false'); this.paintDrawerLive();
      if (n.type !== 'internet' && n.type !== 'wan' && n.type !== 'siterouter') this.loadNodeDevices(n);
    }

    // Everything behind this node — online and offline — with a per-device alert bell.
    loadNodeDevices(n) {
      const body = this.drawer.querySelector('.nm-drawer-body');
      let box = body.querySelector('.nm-nd');
      if (!box) { box = el('div', 'nm-nd'); body.appendChild(box); }
      box.innerHTML = '<div class="nm-nd-head"><b>Devices</b><span class="nm-more">Loading…</span></div>';
      const token = csrf(), self = this, node = n;
      fetch('/topology/node-devices/?node=' + encodeURIComponent(n.id), { credentials: 'same-origin', cache: 'no-store' }).then(r => r.json()).then(d => {
        if (!d.ok) { box.innerHTML = '<p class="nm-more">' + esc(d.message || 'Could not load devices.') + '</p>'; return; }
        let filter = 'all', q = '';
        const ago = iso => { const s = (Date.now() - new Date(iso)) / 1000; return s < 90 ? 'just now' : s < 3600 ? Math.round(s / 60) + ' min ago' : s < 86400 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago'; };
        const bell = x => x.alert === 'alerting' ? ['bi-bell-fill', 'on', 'Alerts on' + (x.rule ? ' (' + x.rule + ')' : '') + ' — click to mute'] :
          x.alert === 'muted' ? ['bi-bell-slash', 'mute', 'Muted — click to follow your general rules'] : ['bi-bell', '', 'No alerts — click to alert me when it goes offline'];
        const draw = () => {
          const rows = d.devices.filter(x => (filter === 'all' || (filter === 'on') === x.online) && (!q || [x.name, x.ip, x.mac, x.detail].join(' ').toLowerCase().includes(q)));
          box.innerHTML = '<div class="nm-nd-head"><b>Devices</b><span class="nm-more">' + d.counts.online + ' online · ' + d.counts.offline + ' offline</span></div>' +
            '<div class="nm-nd-tabs" role="group" aria-label="Filter devices">' + [['all', 'All', d.devices.length], ['on', 'Online', d.counts.online], ['off', 'Offline', d.counts.offline]].map(t =>
              '<button type="button" data-f="' + t[0] + '" class="' + (filter === t[0] ? 'on' : '') + '">' + t[1] + ' <b>' + t[2] + '</b></button>').join('') + '</div>' +
            '<input class="form-control form-control-sm nm-dev-filter" placeholder="Search name, IP or MAC" value="' + esc(q) + '" aria-label="Search devices">' +
            '<div class="nm-devlist">' + (rows.map(x => { const bl = bell(x); return '<div class="nm-dev nm-nd-row' + (x.online ? '' : ' off') + '">' +
              '<span class="nm-nd-dot' + (x.online ? ' on' : '') + '" title="' + (x.online ? 'Online' : 'Offline') + '"></span><div class="flex-grow-1" style="min-width:0"><b>' + (x.network ? '<i class="bi bi-hdd-network"></i> ' : '') + esc(x.name) + '</b>' +
              '<small>' + esc([x.ip, x.mac].filter(Boolean).join(' · ')) + '</small><small>' + (x.online ? 'Online' : 'Offline · last seen ' + ago(x.last_seen)) + (x.port ? ' · ' + esc(x.port) : '') + (x.detail ? ' · ' + esc(x.detail) : '') + '</small>' +
              (x.types.length ? '<span class="nm-nd-types">' + x.types.filter(t => t !== 'private').map(t => '<i>' + esc(t) + '</i>').join('') + '</span>' : '') + '</div>' +
              '<button type="button" class="nm-nd-bell ' + bl[1] + '" data-k="' + esc(x.key) + '" title="' + esc(bl[2]) + '" aria-label="' + esc(bl[2]) + '"><i class="bi ' + bl[0] + '"></i></button></div>'; }).join('') ||
              '<p class="nm-more">No ' + (filter === 'off' ? 'offline ' : filter === 'on' ? 'online ' : '') + 'devices here.</p>') + '</div>';
          box.querySelectorAll('[data-f]').forEach(b => b.onclick = () => { filter = b.dataset.f; draw(); });
          const inp = box.querySelector('.nm-dev-filter'); inp.oninput = () => { q = inp.value.toLowerCase(); const pos = inp.selectionStart; draw(); const i2 = box.querySelector('.nm-dev-filter'); i2.focus(); i2.setSelectionRange(pos, pos); };
          box.querySelectorAll('.nm-nd-bell').forEach(b => b.onclick = () => {
            const x = d.devices.find(y => y.key === b.dataset.k), want = x.alert === 'alerting' ? 'mute' : x.alert === 'muted' ? 'default' : 'alert';
            const fd = new FormData(); fd.append('mac', x.mac || ''); fd.append('ip', x.ip || ''); fd.append('name', x.name); fd.append('network', x.network ? '1' : '0'); fd.append('want', want);
            b.disabled = true;
            fetch('/alerts/device/', { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'X-CSRFToken': token } }).then(r => r.json()).then(res => {
              if (!res.ok) { alert(res.message); b.disabled = false; return; }
              self.loadNodeDevices(node);
            });
          });
        };
        draw();
      }).catch(e => { box.innerHTML = '<p class="nm-more">Could not load devices: ' + esc(e.message) + '</p>'; });
    }
    closeDrawer() { this.drawer.classList.remove('open'); this.drawer.setAttribute('aria-hidden', 'true'); this.nodeLayer.querySelectorAll('.nm-node.sel').forEach(x => x.classList.remove('sel')); }

    // ------------------------------------------------ discovery
    async discover(ids, auto) {
      const routers = this.cfg.routers.filter(r => ids.includes(r.id)); if (!routers.length) return;
      const bar = this.discoveryBar; bar.classList.add('show');
      routers.forEach(r => {
        let chip = bar.querySelector(`[data-r="${r.id}"]`);
        if (!chip) { chip = el('span', 'nm-disc'); chip.dataset.r = r.id; bar.appendChild(chip); }
        chip.className = 'nm-disc run'; chip.innerHTML = `<i class="bi bi-arrow-repeat"></i> ${esc(r.name)} <small>${auto ? 'refreshing' : 'discovering'}…</small>`;
      });
      const limit = 4; let i = 0; let changed = false;
      const worker = async () => {
        while (i < routers.length) {
          const r = routers[i++]; const chip = bar.querySelector(`[data-r="${r.id}"]`);
          try {
            const res = await fetch(this.cfg.refreshUrl.replace('/0/', `/${r.id}/`), { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': csrf() } });
            const d = await res.json();
            if (d.success) { chip.className = 'nm-disc ok'; chip.innerHTML = `<i class="bi bi-check-circle-fill"></i> ${esc(r.name)} <small>${d.neighbors} network · ${d.devices} devices · ${d.seconds}s</small>`; changed = true; }
            else { chip.className = 'nm-disc bad'; chip.title = d.message || ''; const why = (d.message || 'no answer').split('. ')[0].slice(0, 70);
              chip.innerHTML = `<i class="bi bi-x-circle-fill"></i> ${esc(r.name)} <small>${esc(why)} — showing last saved map</small>`; changed = true; }
          } catch (err) { chip.className = 'nm-disc bad'; chip.innerHTML = `<i class="bi bi-x-circle-fill"></i> ${esc(r.name)} <small>${esc(err.message)}</small>`; }
          if (changed) this.reloadGraph();
        }
      };
      await Promise.all(Array.from({ length: Math.min(limit, routers.length) }, worker));
      setTimeout(() => { bar.querySelectorAll('.nm-disc.ok').forEach(c => c.remove()); if (!bar.children.length) bar.classList.remove('show'); }, 6000);
    }
    reloadGraph() {
      clearTimeout(this._rg);
      this._rg = setTimeout(async () => {
        try {
          const res = await fetch(this.cfg.graphUrl, { credentials: 'same-origin', cache: 'no-store' });
          if (!res.ok) return; this.graph = await res.json(); this.render(false); this.updateStats();
          if (this.root.dataset.onGraph) window.dispatchEvent(new CustomEvent('taptap:graph', { detail: this.graph }));
        } catch (err) { /* keep current map */ }
      }, 250);
    }
    updateStats() {
      const s = this.graph.stats || {};
      document.querySelectorAll('[data-stat]').forEach(e => { if (s[e.dataset.stat] != null) e.textContent = s[e.dataset.stat]; });
    }

    // ------------------------------------------------ live traffic
    startLive() { this.stopLive(); if (!this.opts.live) return; this.pollLive(); }
    stopLive() { clearTimeout(this._lt); this.traffic = {}; this.paintTraffic(); }
    async pollLive() {
      clearTimeout(this._lt);
      if (!this.opts.live) return;
      if (document.hidden) { this._lt = setTimeout(() => this.pollLive(), 5000); return; }
      const params = new URLSearchParams();
      const online = new Set(this.graph.nodes.filter(n => n.type === 'router' && n.status === 'online').map(n => String(n.router_id)));
      Object.entries(this.graph.live_interfaces || {}).forEach(([rid, names]) => { if (online.has(rid) && names.length) params.append('r', `${rid}:${names.join(',')}`); });
      let delay = 3000;
      if ([...params.keys()].length) {
        try {
          const res = await fetch(`${this.cfg.liveUrl}?${params}`, { credentials: 'same-origin', cache: 'no-store' });
          const d = await res.json(); this.traffic = {};
          Object.entries(d.routers || {}).forEach(([rid, info]) => { this.traffic[rid] = info.interfaces || {}; });
          this.liveFail = 0; this.paintTraffic();
        } catch (err) { this.liveFail++; delay = Math.min(30000, 3000 * 2 ** this.liveFail); }
      } else delay = 8000;
      this._lt = setTimeout(() => this.pollLive(), delay);
    }
    edgeFlow(e) {
      const t = (this.traffic[String(e.router_id)] || {})[e.iface]; if (!t) return null;
      const routerNode = `router:${e.router_id}`;
      const towardTargetIsRx = e.target === routerNode || e.direction === 'up';
      return { fwd: towardTargetIsRx ? t.rx_bps : t.tx_bps, back: towardTargetIsRx ? t.tx_bps : t.rx_bps,
        down: e.direction === 'up' ? t.rx_bps : t.tx_bps, up: e.direction === 'up' ? t.tx_bps : t.rx_bps };
    }
    paintTraffic() {
      let wanDown = 0, wanUp = 0; const counted = new Set();
      Object.values(this.edgePaths).forEach(p => {
        const f = this.opts.live && p.edge.online ? this.edgeFlow(p.edge) : null;
        const total = f ? f.fwd + f.back : 0;
        const w = total ? Math.min(9, 1.5 + Math.log10(1 + total / 1e4) * 1.6) : 0;
        p.glow.style.strokeWidth = w ? (w * 2.4) + 'px' : '0'; p.glow.style.opacity = w ? 0.5 : 0;
        p.path.classList.toggle('busy', total > 5e4);
        p.flow = f;
        const label = this.svg.querySelector(`[data-label="${p.edge.id}"] .nm-rate`);
        if (label) label.textContent = f && total > 0 ? `↓${fmtBps(f.down)} ↑${fmtBps(f.up)}` : '';
        if (f && p.edge.kind === 'wan' && p.edge.target === `router:${p.edge.router_id}` && !counted.has(p.edge.router_id + p.edge.iface)) {
          counted.add(p.edge.router_id + p.edge.iface); wanDown += f.down; wanUp += f.up;
        }
      });
      const tp = document.querySelector('[data-stat-live]');
      if (tp) tp.innerHTML = this.opts.live && (wanDown || wanUp) ? `↓ ${fmtBps(wanDown)} <small>↑ ${fmtBps(wanUp)}</small>` : '—';
      if (reduceMotion) return;
      this.paintDrawerLive();
    }
    paintDrawerLive() {
      const box = this.drawer.querySelector('[data-live-for]'); if (!box) return;
      const [rid, iface] = box.dataset.liveFor.split(':'); const t = (this.traffic[rid] || {})[iface];
      box.innerHTML = t ? `<div><span>Download now</span><b>${fmtBps(t.rx_bps)}</b></div><div><span>Upload now</span><b>${fmtBps(t.tx_bps)}</b></div>` : '<p class="nm-more">Live counters appear here while Live traffic is on.</p>';
    }
    // Particles travel along the real edge curves; rate and speed follow throughput (log scale).
    tick(now) {
      const dt = Math.min(0.05, ((now - (this._last || now)) / 1000)); this._last = now;
      if (this.opts.live && !document.hidden) {
        Object.values(this.edgePaths).forEach(p => {
          if (!p.flow || !p.len) return;
          [['fwd', 1], ['back', -1]].forEach(([k, dir]) => {
            const bps = p.flow[k]; if (!bps || bps < 2000) return;
            const rate = Math.min(9, 0.35 + Math.log10(bps / 1e3) * 1.35);
            p['acc' + k] = (p['acc' + k] || 0) + rate * dt;
            while (p['acc' + k] >= 1 && this.particles.length < 700) {
              p['acc' + k] -= 1;
              const downstream = (dir === 1) === (p.edge.direction !== 'up');
              const c = svgEl('circle', { r: Math.min(4.2, 1.6 + Math.log10(bps / 1e4 + 1)), class: downstream ? 'pt-down' : 'pt-up' });
              this.gParticles.appendChild(c);
              this.particles.push({ c, p, t: 0, dir, speed: 70 + Math.min(230, Math.log10(bps / 1e3 + 1) * 45) });
            }
          });
        });
      }
      for (let i = this.particles.length - 1; i >= 0; i--) {
        const q = this.particles[i]; q.t += q.speed * dt / q.p.len;
        if (q.t >= 1 || !q.p.path.isConnected) { q.c.remove(); this.particles.splice(i, 1); continue; }
        const at = q.p.path.getPointAtLength((q.dir === 1 ? q.t : 1 - q.t) * q.p.len);
        q.c.setAttribute('cx', at.x); q.c.setAttribute('cy', at.y);
        q.c.setAttribute('opacity', Math.min(1, Math.min(q.t, 1 - q.t) * 8));
      }
      requestAnimationFrame(t => this.tick(t));
    }
  }

  window.TapTapNetMap = NetMap;
  document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('netmap'); const cfgEl = document.getElementById('netmap-config');
    if (!root || !cfgEl) return;
    const cfg = JSON.parse(cfgEl.textContent);
    window.taptapNetMap = new NetMap(root, cfg);
  });
})();
