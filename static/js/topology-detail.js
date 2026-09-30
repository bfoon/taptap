/* TapTap Topology → Detail tab.
 *
 * A designed, tidy tree of how the routers are linked:
 *   Internet → MikroTik → port → TP-Link (or a shared cable when several share a port) → customers
 * Links animate with the live traffic of the MikroTik port they run through (the Map tab already
 * polls it: window.taptapNetMap.traffic), plus the list of routers with why TapTap found each one,
 * confirm / "not a router" / edit, and "Add routers by IP" (192.168.0.1, lists, ranges).
 * Data comes from core/site_routers.py via core/views_topology.py.
 */
(function () {
  'use strict';
  const cfgEl = document.getElementById('topology-detail');
  if (!cfgEl) return;
  let P = JSON.parse(cfgEl.textContent);
  const URLS = P.urls;
  const $ = s => document.querySelector(s);
  const esc = s => String(s == null ? '' : s).replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
  const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const svgNS = 'http://www.w3.org/2000/svg';
  const csrf = () => { const i = document.querySelector('[name=csrfmiddlewaretoken]'); const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return (i && i.value) || (m && decodeURIComponent(m[1])) || ''; };
  const fmtBps = v => { v = +v || 0; if (v >= 1e9) return (v / 1e9).toFixed(1) + ' Gb/s'; if (v >= 1e6) return (v / 1e6).toFixed(1) + ' Mb/s'; if (v >= 1e3) return Math.round(v / 1e3) + ' kb/s'; return Math.round(v) + ' b/s'; };
  const MODE = { ap: 'Access point', nat: 'Router (NAT)' };
  const ROLE_ICON = { router: 'bi-router', ap: 'bi-wifi', repeater: 'bi-broadcast', switch: 'bi-hdd-rack', modem: 'bi-modem' };

  // ------------------------------------------------------------------ tabs
  const tabs = document.querySelectorAll('.td-switch [role=tab]');
  function show(view, focus) {
    tabs.forEach(t => { const on = t.dataset.view === view; t.setAttribute('aria-selected', on); t.tabIndex = on ? 0 : -1; if (on && focus) t.focus(); });
    $('#paneMap').hidden = view !== 'map'; $('#paneDetail').hidden = view !== 'detail';
    if (view === 'map' && window.taptapNetMap) requestAnimationFrame(() => window.taptapNetMap.fit());
    if (view === 'detail') drawDiagram();
    history.replaceState(null, '', view === 'detail' ? '#detail' : location.pathname + location.search);
  }
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => show(t.dataset.view));
    t.addEventListener('keydown', e => { if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { e.preventDefault(); const n = tabs[(i + 1) % tabs.length]; show(n.dataset.view, true); } });
  });
  window.addEventListener('taptap:detail', e => { show('detail'); highlight(e.detail); });

  // ------------------------------------------------------------------ data helpers
  const entries = () => P.entries || [];
  const routerById = id => (P.routers || []).find(r => r.id === id);
  const byKey = k => entries().find(e => e.key === k);
  const confirmedOrLikely = e => e.status === 'confirmed' || e.confidence === 'likely';
  function updateCount() { const n = entries().filter(confirmedOrLikely).length; const c = $('#tdCount'); if (c) c.textContent = n; }

  // ------------------------------------------------------------------ designed diagram (tidy tree)
  const SIZE = { internet: [150, 52], mt: [236, 78], port: [96, 30], shared: [196, 44], site: [214, 74], clients: [136, 34], group: [170, 40] };
  const GAP = 20, ROW = 118;

  function buildTree() {
    const showSuggest = $('#tdSuggest').checked;
    const list = entries().filter(e => e.status === 'confirmed' || (showSuggest && e.confidence === 'likely'));
    const kids = k => list.filter(e => e.parent_key === k);
    const siteNode = e => {
      const n = { type: 'site', e, rid: e.router_id, port: e.port, children: [] };
      kids(e.key).forEach(c => n.children.push(siteNode(c)));
      if (e.clients) n.children.push({ type: 'clients', count: e.clients, rid: e.router_id, port: e.port, online: e.online, children: [] });
      return n;
    };
    const root = { type: 'internet', children: [] };
    (P.routers || []).forEach(r => {
      const mt = { type: 'mt', r, children: [] };
      const ports = {};
      list.filter(e => !e.parent_key && e.router_id === r.id).forEach(e => (ports[e.port || '?'] = ports[e.port || '?'] || []).push(e));
      Object.keys(ports).sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).forEach(port => {
        const p = { type: 'port', name: port, rid: r.id, children: [] };
        const group = ports[port];
        if (group.length > 1) {
          const sh = { type: 'shared', rid: r.id, port, children: group.map(siteNode) };
          p.children.push(sh);
        } else p.children.push(siteNode(group[0]));
        mt.children.push(p);
      });
      root.children.push(mt);
    });
    const loose = list.filter(e => !e.parent_key && (!e.router_id || !routerById(e.router_id)));
    if (loose.length) root.children.push({ type: 'group', label: 'Not linked yet', children: loose.map(siteNode) });
    return root;
  }

  function measure(n) {
    const [w, h] = SIZE[n.type]; n.w = w; n.h = h;
    n.children.forEach(measure);
    const kidsSpan = n.children.reduce((s, c) => s + c.span, 0) + GAP * Math.max(0, n.children.length - 1);
    n.kidsSpan = kidsSpan; n.span = Math.max(w, kidsSpan);
  }
  function place(n, x, depth, out) {
    n.x = x + (n.span - n.w) / 2; n.y = depth * ROW + (SIZE.mt[1] - n.h) / 2; out.push(n);
    let cx = x + (n.span - n.kidsSpan) / 2;
    n.children.forEach(c => { place(c, cx, depth + 1, out); cx += c.span + GAP; });
  }

  function nodeHtml(n) {
    if (n.type === 'internet') return `<span class="td-ic"><i class="bi bi-globe2"></i></span><b>Internet</b>`;
    if (n.type === 'mt') {
      const r = n.r;
      return `<span class="td-ic"><i class="bi bi-router-fill"></i></span><div><b>${esc(r.name)}</b><small>${esc(r.ip || 'MikroTik')} · ${r.devices} online</small></div><span class="td-dot ${r.online ? 'on' : ''}" title="${r.online ? 'Online' : 'Unreachable'}"></span>`;
    }
    if (n.type === 'port') return `<i class="bi bi-ethernet"></i> ${esc(n.name === '?' ? 'port ?' : n.name)}`;
    if (n.type === 'shared') return `<span class="td-ic"><i class="bi bi-hdd-rack"></i></span><div><b>Switch / shared cable</b><small>order not known</small></div>`;
    if (n.type === 'clients') return `<i class="bi bi-phone"></i> ${n.count} customer${n.count === 1 ? '' : 's'}`;
    if (n.type === 'group') return `<i class="bi bi-question-diamond"></i> ${esc(n.label)}`;
    const e = n.e;
    const mode = MODE[e.mode || e.mode_guess] || '';
    return `${e.brand ? `<em class="td-brand">${esc(e.brand)}</em>` : ''}<span class="td-ic"><i class="bi ${ROLE_ICON[e.role] || 'bi-router'}"></i></span>
      <div><b>${esc(e.name)}</b><small>${esc([e.model, e.ip].filter(Boolean).join(' · ') || e.mac || 'not seen yet')}</small>${mode ? `<small class="td-mode">${esc(mode)}</small>` : ''}</div>
      <span class="td-dot ${e.online ? 'on' : ''}" title="${e.online ? 'Online' : e.seen ? 'Offline' : 'Not seen yet'}"></span>${e.status !== 'confirmed' ? '<span class="td-q" title="Found by TapTap — not confirmed">?</span>' : ''}`;
  }

  let links = [];

  // ------------------------------------------------------------------ view: fit, zoom, drag, Open big
  const V = { k: 1, x: 0, y: 0, moved: false };
  let D = null;
  const panel = $('#tdPanel'), box0 = $('#tdDiagram'), bigBtn = $('#tdBig');
  const isBig = () => panel.classList.contains('td-big');
  function fitK() {
    const bw = box0.clientWidth - 24, bh = box0.clientHeight - 24;
    if (isBig()) return Math.max(0.2, Math.min(2, bw / D.W, bh / D.H));   // big: the whole network on screen
    return Math.max(0.55, Math.min(1, bw / D.W));                           // in the page: fit the width
  }
  function sizeBox() {
    if (!D) return;
    if (isBig()) { box0.style.height = ''; return; }
    box0.style.height = Math.round(Math.min(Math.max(260, D.H * fitK() + 24), window.innerHeight * 0.8)) + 'px';
  }
  function apply(glide) {
    if (!D) return;
    D.stage.classList.toggle('glide', !!glide);
    D.stage.style.transform = `translate(${V.x}px,${V.y}px) scale(${V.k})`;
    $('#tdPct').textContent = Math.round(V.k * 100) + '%';
    if (glide) setTimeout(() => D && D.stage.classList.remove('glide'), 260);
  }
  function fit(glide, whole) {
    if (!D) return;
    let k = fitK();
    // A wide network on a phone: open at a readable size and drag around (the Fit button still shows it all).
    if (isBig() && !whole && k < 0.45) {
      k = Math.min(0.7, (box0.clientHeight - 24) / D.H);
      V.k = k; V.x = 12; V.y = Math.max(12, (box0.clientHeight - D.H * k) / 2); V.moved = false; apply(glide); return;
    }
    V.k = k; V.x = Math.max(12, (box0.clientWidth - D.W * k) / 2); V.y = Math.max(12, (box0.clientHeight - D.H * k) / 2); V.moved = false;
    if (!isBig() && D.W * k > box0.clientWidth - 24) V.x = 12;               // too wide even at 55 %: start left, drag to see more
    apply(glide);
  }
  function zoomAt(f, cx, cy, glide) {
    if (!D) return;
    cx = cx == null ? box0.clientWidth / 2 : cx; cy = cy == null ? box0.clientHeight / 2 : cy;
    const k = Math.max(0.2, Math.min(3, V.k * f)), r = k / V.k;
    V.x = cx - (cx - V.x) * r; V.y = cy - (cy - V.y) * r; V.k = k; V.moved = true; apply(glide);
  }
  function oneToOne() { if (!D) return; const k = 1 / V.k; zoomAt(k, null, null, true); }
  document.querySelectorAll('[data-zoom]').forEach(b => b.addEventListener('click', () => {
    const z = b.dataset.zoom;
    if (z === 'in') zoomAt(1.25, null, null, true); else if (z === 'out') zoomAt(0.8, null, null, true);
    else if (z === 'one') oneToOne(); else fit(true, true);
  }));

  // drag to move (a click on a router still opens it), two fingers to pinch, wheel to zoom when big
  const pts = new Map(); let drag = null, pinch = null, dragged = false;
  box0.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 1) { drag = { x: e.clientX, y: e.clientY, vx: V.x, vy: V.y, on: false }; dragged = false; }
    else if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, k: V.k }; drag = null; }
  });
  box0.addEventListener('pointermove', e => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && pts.size === 2) {
      const [a, b] = [...pts.values()], r = box0.getBoundingClientRect();
      zoomAt((pinch.k * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d) / V.k, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top); dragged = true; return;
    }
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.on && Math.hypot(dx, dy) > 5) { drag.on = true; dragged = true; box0.setPointerCapture(e.pointerId); box0.classList.add('grabbing'); }
    if (drag.on) { V.x = drag.vx + dx; V.y = drag.vy + dy; V.moved = true; apply(); }
  });
  const endPtr = e => { pts.delete(e.pointerId); if (pts.size < 2) pinch = null; if (!pts.size) { drag = null; box0.classList.remove('grabbing'); } };
  box0.addEventListener('pointerup', endPtr); box0.addEventListener('pointercancel', endPtr);
  box0.addEventListener('click', e => { if (dragged) { e.stopPropagation(); e.preventDefault(); dragged = false; } }, true);
  box0.addEventListener('wheel', e => {
    if (!isBig() && !e.ctrlKey && !e.metaKey) return;          // in the page, the wheel scrolls the page
    e.preventDefault(); const r = box0.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.12 : 0.89, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  box0.addEventListener('keydown', e => {
    const step = 60, keys = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (e.key === '+' || e.key === '=') zoomAt(1.2, null, null, true);
    else if (e.key === '-' || e.key === '_') zoomAt(0.83, null, null, true);
    else if (e.key === '0') fit(true, true);
    else if (keys[e.key]) { V.x += keys[e.key][0]; V.y += keys[e.key][1]; V.moved = true; apply(true); }
    else return;
    e.preventDefault();
  });

  const canFullscreen = !!(panel.requestFullscreen && document.fullscreenEnabled);
  function setBigButton(on) {
    bigBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    bigBtn.innerHTML = on ? '<i class="bi bi-fullscreen-exit"></i> <span>Close big view</span>' : '<i class="bi bi-arrows-fullscreen"></i> <span>Open big</span>';
  }
  async function openBig() {
    panel.classList.add('td-big'); document.body.classList.add('td-noscroll'); setBigButton(true);
    if (canFullscreen) { try { await panel.requestFullscreen({ navigationUI: 'hide' }); } catch (err) { /* the overlay still fills the window */ } }
    requestAnimationFrame(() => { sizeBox(); fit(); box0.focus({ preventScroll: true }); });
  }
  function closeBig() {
    if (!isBig()) return;
    panel.classList.remove('td-big'); document.body.classList.remove('td-noscroll'); setBigButton(false);
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    requestAnimationFrame(() => { sizeBox(); fit(); bigBtn.focus({ preventScroll: true }); });
  }
  bigBtn.addEventListener('click', () => (isBig() ? closeBig() : openBig()));
  document.addEventListener('fullscreenchange', () => {
    // The browser's own Esc leaves full screen: close the big view too, unless the edit dialog is open.
    if (!document.fullscreenElement && isBig() && !document.querySelector('.modal.show')) closeBig();
    else if (document.fullscreenElement === panel) requestAnimationFrame(() => fit());
  });
  // Capture phase: runs before Bootstrap closes the dialog, so Esc closes the dialog first, the big view next time.
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && isBig() && !document.querySelector('.modal.show')) closeBig();
  }, true);
  // In big view the dialog and messages must live inside the full-screen panel to be seen.
  const modalHome = document.getElementById('tdEdit').parentNode;
  document.getElementById('tdEdit').addEventListener('hidden.bs.modal', ev => { if (ev.target.parentNode !== modalHome) modalHome.appendChild(ev.target); });

  function drawDiagram() {
    const box = $('#tdDiagram'); if (!box || $('#paneDetail').hidden) return;
    const root = buildTree(); measure(root);
    const all = []; place(root, 0, 0, all);
    const W = Math.max(root.span, 400) + 40, depthMax = Math.max(...all.map(n => n.y + n.h));
    box.innerHTML = '';
    const stage = document.createElement('div'); stage.className = 'td-stage'; stage.style.width = W + 'px'; stage.style.height = (depthMax + 30) + 'px';
    const svg = document.createElementNS(svgNS, 'svg'); svg.setAttribute('width', W); svg.setAttribute('height', depthMax + 30); svg.setAttribute('aria-hidden', 'true');
    stage.appendChild(svg);
    box.appendChild(stage);
    D = { W, H: depthMax + 30, stage };
    sizeBox();
    if (V.moved) apply(); else fit();
    links = [];
    const off = 0;
    all.forEach(n => {
      const d = document.createElement(n.type === 'site' ? 'button' : 'div');
      d.className = `td-n td-${n.type}` + (n.e && n.e.status !== 'confirmed' ? ' sug' : '') + ((n.e && !n.e.online) || (n.r && !n.r.online) ? ' off' : '');
      d.style.cssText = `left:${n.x + off}px;top:${n.y + 10}px;width:${n.w}px;height:${n.h}px`;
      d.innerHTML = nodeHtml(n);
      if (n.type === 'site') { d.type = 'button'; d.dataset.key = n.e.key; d.setAttribute('aria-label', `Edit ${n.e.name}`); d.addEventListener('click', () => openEdit(n.e.key)); }
      stage.appendChild(d);
      n.children.forEach(c => {
        const x1 = n.x + off + n.w / 2, y1 = n.y + 10 + n.h, x2 = c.x + off + c.w / 2, y2 = c.y + 10, my = (y1 + y2) / 2;
        const path = document.createElementNS(svgNS, 'path');
        const dAttr = `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`;
        path.setAttribute('d', dAttr);
        const rid = c.rid || (c.type === 'mt' ? c.r.id : null), port = c.port || (c.type === 'port' ? c.name : '');
        const online = c.type === 'mt' ? c.r.online : c.e ? c.e.online : c.type === 'clients' ? c.online !== false : true;
        path.setAttribute('class', 'td-link' + (online ? ' on' : ' off') + (c.e && c.e.status !== 'confirmed' ? ' sug' : ''));
        svg.appendChild(path);
        links.push({ path, d: dAttr, rid, port, online, uplink: c.type === 'mt', dots: [] });
      });
    });
    paintTraffic(true);
  }

  // live traffic → animation speed; each link follows the MikroTik port it runs through
  function trafficFor(l) {
    const T = (window.taptapNetMap && window.taptapNetMap.traffic) || {};
    const ifs = T[String(l.rid)] || {};
    if (l.uplink) {   // Internet → MikroTik: sum of the WAN links drawn on the map
      let down = 0, up = 0;
      ((window.taptapNetMap && window.taptapNetMap.graph.edges) || []).forEach(e => {
        if (e.kind === 'wan' && e.target === `router:${l.rid}` && ifs[e.iface]) { down += ifs[e.iface].rx_bps || 0; up += ifs[e.iface].tx_bps || 0; }
      });
      return down || up ? { down, up } : null;
    }
    const t = ifs[l.port]; return t ? { down: t.tx_bps || 0, up: t.rx_bps || 0 } : null;
  }
  function dur(bps) { return bps > 0 ? Math.max(0.5, 4.2 - Math.log10(1 + bps / 1e3) * 0.9) : 3.6; }
  function paintTraffic(rebuild) {
    const anim = $('#tdAnim').checked && !reduceMotion;
    let total = 0, shown = 0;
    links.forEach(l => {
      const f = l.online ? trafficFor(l) : null;
      const bps = f ? f.down + f.up : 0;
      if (l.uplink && f) { total += f.down; shown += f.up; }
      l.path.style.setProperty('--dur', dur(bps).toFixed(2) + 's');
      l.path.classList.toggle('busy', bps > 5e5);
      l.path.classList.toggle('anim', anim && l.online);
      l.path.style.strokeWidth = bps ? Math.min(6, 2 + Math.log10(1 + bps / 1e4)).toFixed(1) : '';
      const want = anim && l.online ? (bps > 5e4 ? 2 : 1) : 0;
      if (rebuild || l.dots.length !== want * 2) {
        l.dots.forEach(c => c.remove()); l.dots = [];
        for (let i = 0; i < want; i++) ['down', 'up'].forEach(dir => {
          if (dir === 'up' && !(f && f.up > 2e3)) return;
          const c = document.createElementNS(svgNS, 'circle'); c.setAttribute('r', 3.2); c.setAttribute('class', 'td-pt ' + dir);
          const m = document.createElementNS(svgNS, 'animateMotion'); m.setAttribute('path', l.d); m.setAttribute('repeatCount', 'indefinite');
          if (dir === 'up') { m.setAttribute('keyPoints', '1;0'); m.setAttribute('keyTimes', '0;1'); m.setAttribute('calcMode', 'linear'); }
          m.setAttribute('begin', (i * 0.5) + 's'); c.appendChild(m); l.path.parentNode.appendChild(c); l.dots.push(c);
        });
      }
      l.dots.forEach(c => c.firstChild.setAttribute('dur', (dur(bps) * 1.4).toFixed(2) + 's'));
    });
    const live = $('#tdLive');
    if (live) live.textContent = total || shown ? `Internet now ↓ ${fmtBps(total)} ↑ ${fmtBps(shown)}` : (window.taptapNetMap && window.taptapNetMap.opts.live ? 'Waiting for live traffic…' : 'Live traffic is off on the Map');
  }
  setInterval(() => { if (!$('#paneDetail').hidden) paintTraffic(false); }, 2000);
  $('#tdSuggest').addEventListener('change', drawDiagram);
  let rz = null; window.addEventListener('resize', () => { clearTimeout(rz); rz = setTimeout(drawDiagram, 200); });
  $('#tdAnim').addEventListener('change', () => paintTraffic(true));

  // ------------------------------------------------------------------ router list
  function pathOf(e) {
    const parts = []; let cur = e, guard = 0;
    while (cur && cur.parent_key && guard++ < 10) { cur = byKey(cur.parent_key); if (cur) parts.unshift(cur.name); }
    const root = cur || e;
    const r = routerById(root.router_id);
    return [r ? r.name : 'Not linked', root.port || (r ? 'port ?' : '')].filter(Boolean).concat(parts).join(' › ');
  }
  function reasons(e) {
    if (!(e.reasons || []).length) return '';
    return `<details class="td-why"><summary>Why${e.score != null ? ` · ${e.score} pts` : ''}</summary>${e.reasons.map(r =>
      `<div class="${r.sign === '+' ? 'pro' : 'con'}"><i class="bi ${r.sign === '+' ? 'bi-plus-circle' : 'bi-dash-circle'}"></i> ${esc(r.text)}</div>`).join('')}</details>`;
  }
  function row(e) {
    const status = e.online ? '<span class="td-pill on">Online</span>' : e.seen ? '<span class="td-pill">Offline</span>' : '<span class="td-pill warn">Not seen yet</span>';
    const tag = e.status === 'confirmed' ? '' : `<span class="td-pill ${e.confidence === 'likely' ? 'sug' : 'maybe'}">${e.confidence === 'likely' ? 'Likely router' : 'Maybe'}</span>`;
    let actions;
    if (e.status === 'confirmed') actions = `<button type="button" class="btn btn-sm btn-outline-secondary" data-edit="${esc(e.key)}">Edit</button>`;
    else actions = `<button type="button" class="btn btn-sm btn-success" data-confirm="${esc(e.mac)}">Confirm</button>
      <button type="button" class="btn btn-sm btn-outline-secondary" data-edit="${esc(e.key)}">Edit…</button>
      <button type="button" class="btn btn-sm btn-link text-secondary" data-ignore="${esc(e.mac)}">Not a router</button>`;
    let pick = '';
    if ((e.candidates || []).length > 1) pick = `<div class="td-pick"><small>${e.candidates.length} devices use ${esc(e.ip)}. Which one is this router?</small>
      <div class="d-flex gap-1"><select class="form-select form-select-sm" data-bind-sel="${e.id}" aria-label="Device">${e.candidates.map(c =>
        `<option value="${esc(c.mac)}">${esc(c.mac)} · ${esc(c.brand || c.hostname || '')} · ${esc(c.router)} › ${esc(c.port || '?')}${c.online ? '' : ' (offline)'}</option>`).join('')}</select>
      <button type="button" class="btn btn-sm btn-primary" data-bind="${e.id}">Use</button></div></div>`;
    return `<tr data-key="${esc(e.key)}" class="${e.status !== 'confirmed' ? 'td-sugrow' : ''}">
      <td><div class="td-rname"><i class="bi ${ROLE_ICON[e.role] || 'bi-router'}"></i><div><b>${esc(e.name)}</b> ${tag}<small>${esc([e.brand, e.model].filter(Boolean).join(' ') || 'Maker unknown')}${(e.mode || e.mode_guess) ? ' · ' + esc(MODE[e.mode || e.mode_guess]) + (e.mode ? '' : '?') : ''}</small>${reasons(e)}</div></div>${pick}</td>
      <td><span class="font-monospace small">${esc(e.ip || '—')}</span><small class="d-block text-secondary font-monospace">${esc(e.mac || '')}</small></td>
      <td><small>${esc(pathOf(e))}</small></td>
      <td class="text-end">${e.clients || 0}</td>
      <td>${status}</td>
      <td class="text-end"><div class="td-acts">${actions}</div></td></tr>`;
  }
  function drawList() {
    const box = $('#tdList');
    const conf = entries().filter(e => e.status === 'confirmed'), likely = entries().filter(e => e.status !== 'confirmed' && e.confidence === 'likely'),
      maybe = entries().filter(e => e.status !== 'confirmed' && e.confidence === 'possible');
    const head = '<thead><tr><th>Router</th><th>Address</th><th>Linked via</th><th class="text-end">Customers</th><th>Status</th><th></th></tr></thead>';
    const group = (title, rows, note) => rows.length ? `<h4 class="td-gh">${title}</h4>${note ? `<p class="small text-secondary">${note}</p>` : ''}<div class="table-responsive"><table class="table align-middle td-table">${head}<tbody>${rows.map(row).join('')}</tbody></table></div>` : '';
    const mts = (P.routers || []).map(r => `<span class="td-mtchip ${r.online ? 'on' : ''}"><i class="bi bi-router-fill"></i> ${esc(r.name)} <small>${r.online ? 'online' : 'unreachable'} · ${r.ports.length} ports</small></span>`).join('');
    box.innerHTML = `<div class="td-mts">${mts || '<span class="text-secondary small">No MikroTik routers yet.</span>'}</div>` +
      group('Your routers', conf) +
      group('Found by TapTap — please check', likely, 'These look like routers. Confirm them, or mark “not a router” so TapTap stops suggesting them.') +
      group('Maybe routers', maybe, 'Weaker signs. Confirm only if you recognise them.') +
      (!conf.length && !likely.length && !maybe.length ? `<div class="td-empty"><i class="bi bi-router"></i><b>No other routers found yet</b><span>Press “Discover all now” on the Map so TapTap reads the MikroTik tables, or add routers by IP on the right.</span></div>` : '');
  }

  // ------------------------------------------------------------------ actions
  async function act(body) {
    const res = await fetch(URLS.action, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() }, body: JSON.stringify(body) });
    const d = await res.json().catch(() => ({ success: false, message: 'Could not reach TapTap.' }));
    if (!d.success) throw new Error(d.message || 'Could not save.');
    P = { ...P, ...d }; renderAll();
    if (window.taptapNetMap) window.taptapNetMap.reloadGraph();
    return d;
  }
  function toast(msg, bad) {
    const t = document.createElement('div'); t.className = 'td-toast' + (bad ? ' bad' : ''); t.setAttribute('role', 'status'); t.textContent = msg;
    (isBig() ? panel : document.body).appendChild(t); setTimeout(() => t.remove(), 3200);
  }
  $('#tdList').addEventListener('click', async e => {
    const b = e.target.closest('button'); if (!b) return;
    try {
      if (b.dataset.edit) openEdit(b.dataset.edit);
      else if (b.dataset.confirm) { const en = entries().find(x => x.mac === b.dataset.confirm); await act({ action: 'confirm', mac: b.dataset.confirm, ip: en && en.ip, name: en && en.name }); toast('Router confirmed.'); }
      else if (b.dataset.ignore) { await act({ action: 'ignore', mac: b.dataset.ignore }); toast('Marked as not a router — TapTap will not suggest it again.'); }
      else if (b.dataset.bind) { const sel = document.querySelector(`[data-bind-sel="${b.dataset.bind}"]`); await act({ action: 'bind', id: +b.dataset.bind, mac: sel.value }); toast('Linked to that device.'); }
    } catch (err) { toast(err.message, true); }
  });

  function highlight(key) {
    drawList();
    const tr = document.querySelector(`#tdList tr[data-key="${CSS.escape(key)}"]`);
    if (tr) { tr.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' }); tr.classList.add('td-flash'); setTimeout(() => tr.classList.remove('td-flash'), 2000); }
  }

  // ------------------------------------------------------------------ edit dialog
  const modalEl = $('#tdEdit'); let editing = null;
  const modal = () => window.bootstrap ? bootstrap.Modal.getOrCreateInstance(modalEl) : null;
  const opts = (list, val) => list.map(([v, l]) => `<option value="${esc(v)}"${String(v) === String(val) ? ' selected' : ''}>${esc(l)}</option>`).join('');
  function fillPorts(rid, val) {
    const r = routerById(+rid);
    $('#tePort').innerHTML = opts([['', 'Found automatically'], ...((r && r.ports) || []).map(p => [p, p])].concat(val && r && !r.ports.includes(val) ? [[val, val]] : []), val);
  }
  function openEdit(key) {
    const e = byKey(key); if (!e) return; editing = e;
    if (isBig() && modalEl.parentNode !== panel) panel.appendChild(modalEl);
    $('#tdEditTitle').textContent = e.status === 'confirmed' ? `Edit ${e.name}` : `Confirm ${e.name}`;
    $('#teName').value = e.name; $('#teModel').value = e.model || ''; $('#teIp').value = e.ip || ''; $('#teNotes').value = e.notes || '';
    const brands = P.brands.includes(e.brand) || !e.brand ? P.brands : [e.brand, ...P.brands];
    $('#teBrand').innerHTML = opts([['', 'Unknown'], ...brands.map(b => [b, b])], e.brand);
    $('#teRole').innerHTML = opts(P.roles, e.role); $('#teMode').innerHTML = opts(P.modes, e.mode || (e.status !== 'confirmed' ? e.mode_guess : '') || '');
    $('#teRouter').innerHTML = opts([['', 'Not linked'], ...(P.routers || []).map(r => [r.id, r.name])], e.router_id || '');
    fillPorts(e.router_id, e.id ? (e.port || '') : '');
    const parents = entries().filter(x => x.status === 'confirmed' && x.key !== e.key);
    $('#teParent').innerHTML = opts([['', 'Choose a router'], ...parents.map(x => [x.id, `${x.name}${x.ip ? ' · ' + x.ip : ''}`])], e.parent_key ? e.parent_key.slice(3) : '');
    const viaParent = !!e.parent_key;
    modalEl.querySelector(`[name=linkto][value=${viaParent ? 'parent' : 'port'}]`).checked = true; toggleLink();
    $('#teRemove').hidden = e.status !== 'confirmed'; $('#teErr').hidden = true;
    const m = modal(); if (m) m.show();
  }
  function toggleLink() {
    const v = modalEl.querySelector('[name=linkto]:checked').value;
    modalEl.querySelectorAll('[data-link]').forEach(x => x.hidden = x.dataset.link !== v);
  }
  modalEl.querySelectorAll('[name=linkto]').forEach(r => r.addEventListener('change', toggleLink));
  $('#teRouter').addEventListener('change', () => fillPorts($('#teRouter').value, ''));
  $('#tdEditForm').addEventListener('submit', async ev => {
    ev.preventDefault(); if (!editing) return;
    const f = new FormData(ev.target), viaParent = f.get('linkto') === 'parent';
    const body = { name: f.get('name'), brand: f.get('brand'), model: f.get('model'), role: f.get('role'), mode: f.get('mode'), ip: f.get('ip'), notes: f.get('notes'),
      parent_id: viaParent ? f.get('parent_id') : '', router_id: viaParent ? '' : f.get('router_id'), port: viaParent ? '' : f.get('port') };
    if (viaParent && !body.parent_id) { $('#teErr').textContent = 'Choose the router it is connected to.'; $('#teErr').hidden = false; return; }
    Object.assign(body, editing.status === 'confirmed' ? { action: 'update', id: editing.id } : { action: 'confirm', mac: editing.mac });
    try { await act(body); const m = modal(); if (m) m.hide(); toast('Saved.'); }
    catch (err) { $('#teErr').textContent = err.message; $('#teErr').hidden = false; }
  });
  $('#teRemove').addEventListener('click', async () => {
    if (!editing || !confirm(`Remove ${editing.name} from your list? If TapTap still sees it, it may suggest it again.`)) return;
    try { await act({ action: 'remove', id: editing.id }); const m = modal(); if (m) m.hide(); toast('Removed.'); } catch (err) { toast(err.message, true); }
  });

  // ------------------------------------------------------------------ add by IP
  $('#tdQuick').innerHTML = (P.quick_ips || []).map(ip => `<button type="button" class="btn btn-sm btn-light" data-ip="${esc(ip)}">${esc(ip)}</button>`).join('');
  $('#tdQuick').addEventListener('click', e => { const b = e.target.closest('[data-ip]'); if (b) { $('#tdIp').value = b.dataset.ip; find(); } });
  $('#tdFind').addEventListener('submit', e => { e.preventDefault(); find(); });
  let lastFind = null;
  async function find() {
    const ip = $('#tdIp').value.trim(), out = $('#tdFound');
    if (!ip) { out.innerHTML = '<p class="small text-danger">Type an IP address first.</p>'; return; }
    out.innerHTML = '<p class="small text-secondary">Looking…</p>';
    const res = await fetch(`${URLS.find}?ip=${encodeURIComponent(ip)}`, { credentials: 'same-origin', cache: 'no-store' });
    const d = await res.json().catch(() => ({})); lastFind = d;
    if (!d.success) { out.innerHTML = `<p class="small text-danger">${esc(d.message || 'Could not search.')}</p>`; return; }
    if (!d.rows.length) {
      const one = d.ips.length === 1 ? d.ips[0] : '';
      out.innerHTML = `<div class="td-nomatch"><b>No device with ${esc(ip)} was seen by your MikroTiks.</b><span>Run “Discover all now” on the Map, or check that the MikroTik has an address in that network so it can see it.</span>
        ${one ? `<div class="d-flex gap-2 mt-2"><select class="form-select form-select-sm" id="tdAnyRouter" aria-label="MikroTik it hangs from">${opts([['', 'Not linked yet'], ...(P.routers || []).map(r => [r.id, r.name])], '')}</select>
        <button type="button" class="btn btn-sm btn-outline-primary text-nowrap" id="tdAddAnyway">Add ${esc(one)} anyway</button></div>` : ''}</div>`;
      const b = $('#tdAddAnyway'); if (b) b.onclick = async () => { try { await act({ action: 'add', items: [{ ip: one }], router_id: $('#tdAnyRouter').value }); toast(`${one} added — it links itself when TapTap sees it.`); out.innerHTML = ''; } catch (err) { toast(err.message, true); } };
      return;
    }
    out.innerHTML = `<p class="small mb-1"><b>${d.rows.length}</b> device${d.rows.length === 1 ? '' : 's'} found. Tick the routers to add:</p>
      <div class="td-found">${d.rows.map((r, i) => `<label class="td-cand${r.state === 'confirmed' ? ' done' : ''}">
        <input type="checkbox" data-i="${i}" ${r.state === 'confirmed' ? 'disabled checked' : (r.score >= 35 || d.rows.length <= 5) ? 'checked' : ''}>
        <span><b>${esc(r.ip)}</b> <span class="font-monospace">${esc(r.mac)}</span>${r.brand ? ` <em class="td-brand in">${esc(r.brand)}</em>` : ''}
        <small>${esc(r.hostname || 'no name')} · ${esc(r.router)} › ${esc(r.port || '?')}${r.online ? '' : ' · offline'}${r.state === 'confirmed' ? ' · already added' : r.state === 'ignored' ? ' · marked not a router' : ''}</small></span></label>`).join('')}</div>
      <button type="button" class="btn btn-primary btn-sm mt-2" id="tdAddSel">Add selected</button>`;
    $('#tdAddSel').onclick = async () => {
      const items = [...out.querySelectorAll('input[data-i]:checked:not(:disabled)')].map(c => { const r = lastFind.rows[+c.dataset.i]; return { mac: r.mac, ip: r.ip }; });
      try { await act({ action: 'add', items }); toast(`${items.length} router${items.length === 1 ? '' : 's'} added.`); find(); } catch (err) { toast(err.message, true); }
    };
  }
  function drawIgnored() {
    const box = $('#tdIgnored'); const list = P.ignored || [];
    box.innerHTML = list.length ? `<details><summary class="small">Marked “not a router” (${list.length})</summary>${list.map(x =>
      `<div class="td-ign"><span class="small font-monospace">${esc(x.name)}</span><button type="button" class="btn btn-sm btn-link" data-undo="${x.id}">Undo</button></div>`).join('')}</details>` : '';
  }
  $('#tdIgnored').addEventListener('click', async e => { const b = e.target.closest('[data-undo]'); if (!b) return; try { await act({ action: 'remove', id: +b.dataset.undo }); toast('TapTap may suggest it again.'); } catch (err) { toast(err.message, true); } });

  // Discovery on the Map changes the inventory: refresh the list shortly after.
  let rt = null;
  window.addEventListener('taptap:graph', () => { clearTimeout(rt); rt = setTimeout(async () => {
    try { const r = await fetch(URLS.list, { credentials: 'same-origin', cache: 'no-store' }); const d = await r.json(); if (d.success) { P = { ...P, ...d }; renderAll(); } } catch (err) { /* keep */ }
  }, 400); });

  function renderAll() { updateCount(); drawList(); drawIgnored(); drawDiagram(); }
  renderAll();
  if (location.hash === '#detail') show('detail');
})();
