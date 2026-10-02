/* Live MikroTik system health on the Topology front panel.
   Uses endpoints that already exist in TapTap:
   - Direct API / Tunnel: router_resource_api
   - TapTap Link: router_link_status
*/
(function () {
  'use strict';

  const script = document.currentScript;
  const resourceRoute = script?.dataset.resourceUrl || '';
  const linkRoute = script?.dataset.linkUrl || '';
  const rebootRoute = script?.dataset.rebootUrl || '';
  const REFRESH_MS = 20000;

  if (!resourceRoute || !linkRoute || !rebootRoute) return;

  const panels = Array.from(
    document.querySelectorAll('.topology-router-panel[id^="router-"]')
  );

  if (!panels.length) return;

  function routeFor(pattern, id) {
    // Django reverse URLs contain /0/ in the middle, for example:
    // /routers/0/control/resource/ and /routers/0/link/status/.
    // Replace that path segment, not only an ID at the end of the URL.
    return String(pattern || '').replace('/0/', '/' + id + '/');
  }

  function bytes(n) {
    n = Number(n || 0);
    if (!n) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) {
      n /= 1024;
      i++;
    }
    return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + u[i];
  }

  function num(v) {
    if (v == null || v === '') return 0;
    const m = String(v).match(/-?\d+(?:\.\d+)?/);
    return m ? Number(m[0]) : 0;
  }

  function pct(used, total) {
    if (!total) return 0;
    return Math.max(0, Math.min(100, (used * 100) / total));
  }

  function firstRow(data) {
    if (!data) return {};
    if (Array.isArray(data.rows)) return data.rows[0] || {};
    if (Array.isArray(data)) return data[0] || {};
    if (data.rows && typeof data.rows === 'object') return data.rows;
    return {};
  }

  function val(row, ...keys) {
    for (const k of keys) {
      if (row[k] != null && row[k] !== '') return row[k];
      const alt = k.replaceAll('-', '_');
      if (row[alt] != null && row[alt] !== '') return row[alt];
    }
    return '';
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  }


  async function responseJson(response) {
    const text = await response.text();

    if (!text) {
      return {};
    }

    try {
      return JSON.parse(text);
    } catch (_) {
      const type = response.headers.get('content-type') || '';
      const preview = text.replace(/\s+/g, ' ').slice(0, 180);

      if (response.redirected || /text\/html/i.test(type) || preview.startsWith('<!DOCTYPE') || preview.startsWith('<html')) {
        throw new Error(
          'TapTap returned an HTML page instead of JSON. ' +
          'Check that the router URL is correct and that your login is still active.'
        );
      }

      throw new Error('TapTap returned an invalid response: ' + preview);
    }
  }

  function csrfToken() {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  async function rebootRouter(panel, button) {
    const id = panel.id.replace('router-', '');
    const routerName = panel.dataset.routerName || 'this MikroTik';

    const typed = window.prompt(
      'Restart ' + routerName + '?\\n\\n' +
      'All connected users will disconnect while the MikroTik reboots.\\n\\n' +
      'Type the router name exactly to confirm:',
      ''
    );

    if (typed == null) return;

    const old = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Restarting…';

    try {
      const body = new FormData();
      body.append('confirm', typed);

      const response = await fetch(routeFor(rebootRoute, id), {
        method: 'POST',
        body: body,
        credentials: 'same-origin',
        headers: {
          'X-CSRFToken': csrfToken(),
          'X-Requested-With': 'XMLHttpRequest'
        },
        cache: 'no-store'
      });

      const data = await responseJson(response);

      if (!response.ok || !data.ok) {
        throw new Error(data.message || 'The MikroTik restart was not accepted.');
      }

      button.innerHTML = '<i class="bi bi-check-circle"></i> Restart queued';
      window.setTimeout(function () {
        button.disabled = false;
        button.innerHTML = old;
      }, 7000);

    } catch (err) {
      window.alert(err.message || 'Could not restart the MikroTik.');
      button.disabled = false;
      button.innerHTML = old;
    }
  }

  function stat(icon, title, value, detail, percent, id) {
    const p = percent == null ? null : Math.max(0, Math.min(100, Number(percent)));
    return `
      <div class="ch-health-item" data-health-stat="${id}">
        <span><i class="bi ${icon}"></i>${title}</span>
        <b>${value}</b>
        <small>${detail || '&nbsp;'}</small>
        ${p == null ? '' : `<div class="ch-health-bar"><i style="width:${p}%"></i></div>`}
      </div>`;
  }

  function classify(el, value) {
    if (!el) return;
    el.classList.remove('warn', 'hot');
    if (value >= 90) el.classList.add('hot');
    else if (value >= 75) el.classList.add('warn');
  }

  function buildFromResource(resource, health, panel) {
    const totalMem = num(val(resource, 'total-memory'));
    const freeMem = num(val(resource, 'free-memory'));
    const usedMem = Math.max(0, totalMem - freeMem);

    const totalDisk = num(val(resource, 'total-hdd-space'));
    const freeDisk = num(val(resource, 'free-hdd-space'));
    const usedDisk = Math.max(0, totalDisk - freeDisk);

    let temp = null;
    for (const key of [
      'cpu-temperature',
      'temperature',
      'board-temperature1',
      'board-temperature2',
      'board-temperature'
    ]) {
      const raw = val(health, key);
      if (raw !== '') {
        temp = num(raw);
        break;
      }
    }

    let voltage = null;
    for (const key of ['voltage', 'board-voltage']) {
      const raw = val(health, key);
      if (raw !== '') {
        voltage = num(raw);
        break;
      }
    }

    return {
      source: panel.dataset.mode === 'agent' ? 'TapTap Tunnel' : 'Direct API',
      cpu_load: num(val(resource, 'cpu-load')),
      cpu_count: num(val(resource, 'cpu-count')),
      cpu_frequency_mhz: num(val(resource, 'cpu-frequency')),
      cpu_name: val(resource, 'cpu'),
      memory_total: totalMem,
      memory_free: freeMem,
      memory_used: usedMem,
      memory_percent: pct(usedMem, totalMem),
      disk_total: totalDisk,
      disk_free: freeDisk,
      disk_used: usedDisk,
      disk_percent: pct(usedDisk, totalDisk),
      devices: Number(panel.dataset.deviceCount || 0),
      uptime: val(resource, 'uptime'),
      version: val(resource, 'version'),
      board: val(resource, 'board-name'),
      architecture: val(resource, 'architecture-name'),
      platform: val(resource, 'platform'),
      temperature_c: temp,
      voltage_v: voltage,
      stale: false
    };
  }

  function buildFromLink(data, panel) {
    const totalMem = Number(data.mem_total || 0);
    const freeMem = Number(data.mem_free || 0);
    const usedMem = Math.max(0, totalMem - freeMem);

    return {
      source: 'TapTap Link',
      cpu_load: Number(data.cpu || 0),
      cpu_count: 0,
      cpu_frequency_mhz: 0,
      cpu_name: '',
      memory_total: totalMem,
      memory_free: freeMem,
      memory_used: usedMem,
      memory_percent: pct(usedMem, totalMem),
      disk_total: 0,
      disk_free: 0,
      disk_used: 0,
      disk_percent: 0,
      devices: Number(data.sessions || panel.dataset.deviceCount || 0),
      uptime: data.uptime || '',
      version: data.version || '',
      board: data.board || '',
      architecture: '',
      platform: 'MikroTik',
      temperature_c: null,
      voltage_v: null,
      stale: !data.online
    };
  }

  function render(panel, h) {
    const box = panel.querySelector('.chassis-health');
    if (!box) return;

    const cpu = Number(h.cpu_load || 0);
    const mem = Number(h.memory_percent || 0);
    const disk = Number(h.disk_percent || 0);
    const temp = h.temperature_c == null ? null : Number(h.temperature_c);
    const devices = Number(h.devices || 0);

    const cpuDetail = [
      h.cpu_count ? h.cpu_count + ' core' + (h.cpu_count === 1 ? '' : 's') : '',
      h.cpu_frequency_mhz ? h.cpu_frequency_mhz + ' MHz' : '',
      h.cpu_name || ''
    ].filter(Boolean).join(' · ');

    const memDetail = h.memory_total
      ? bytes(h.memory_used) + ' / ' + bytes(h.memory_total)
      : 'Memory unavailable';

    const diskDetail = h.disk_total
      ? bytes(h.disk_used) + ' / ' + bytes(h.disk_total)
      : 'Storage unavailable';

    const tempValue = temp == null ? '—' : temp.toFixed(temp % 1 ? 1 : 0) + '°C';
    const tempDetail = temp == null
      ? 'Sensor unavailable'
      : (h.voltage_v == null ? 'Board sensor' : h.voltage_v + ' V');

    box.innerHTML = `
      <div class="ch-health-grid">
        ${stat('bi-cpu', 'CPU', cpu + '%', cpuDetail || 'Processor load', cpu, 'cpu')}
        ${stat('bi-memory', 'Memory', h.memory_total ? mem.toFixed(0) + '%' : '—', memDetail, h.memory_total ? mem : null, 'memory')}
        ${stat('bi-device-hdd', 'Storage', h.disk_total ? disk.toFixed(0) + '%' : '—', diskDetail, h.disk_total ? disk : null, 'disk')}
        ${stat('bi-people', 'Devices', devices.toLocaleString(), panel.dataset.mode === 'agent' ? 'HotSpot sessions' : 'Online discovered', null, 'devices')}
        ${stat('bi-thermometer-half', 'Temperature', tempValue, tempDetail, temp == null ? null : Math.min(100, temp), 'temperature')}
      </div>
      <div class="ch-health-meta">
        ${h.board ? `<span><i class="bi bi-router"></i>${escapeHtml(h.board)}</span>` : ''}
        ${h.version ? `<span><i class="bi bi-box"></i>RouterOS ${escapeHtml(h.version)}</span>` : ''}
        ${h.architecture ? `<span><i class="bi bi-motherboard"></i>${escapeHtml(h.architecture)}</span>` : ''}
        ${h.uptime ? `<span><i class="bi bi-clock-history"></i>Up ${escapeHtml(h.uptime)}</span>` : ''}
        <span><i class="bi bi-broadcast"></i>${escapeHtml(h.source || 'RouterOS')}</span>
        ${h.stale ? '<span class="stale"><i class="bi bi-exclamation-circle"></i>Last Link heartbeat</span>' : ''}
        ${h.health && Object.keys(h.health).length ? `<span title="${escapeHtml(Object.entries(h.health).map(([k,v])=>k+': '+v).join(' | '))}"><i class="bi bi-heart-pulse"></i>${Object.keys(h.health).length} health sensor${Object.keys(h.health).length === 1 ? '' : 's'}</span>` : ''}
      </div>`;

    classify(box.querySelector('[data-health-stat="cpu"]'), cpu);
    if (h.memory_total) classify(box.querySelector('[data-health-stat="memory"]'), mem);
    if (h.disk_total) classify(box.querySelector('[data-health-stat="disk"]'), disk);

    const tempEl = box.querySelector('[data-health-stat="temperature"]');
    if (tempEl && temp != null) {
      if (temp >= 75) tempEl.classList.add('hot');
      else if (temp >= 60) tempEl.classList.add('warn');
    }
  }

  function renderError(panel, message) {
    const box = panel.querySelector('.chassis-health');
    if (!box) return;
    box.innerHTML = `
      <div class="ch-health-loading bad">
        <i class="bi bi-exclamation-triangle"></i>
        ${escapeHtml(message || 'System health unavailable')}
      </div>`;
  }

  async function readResource(id, path) {
    const url = routeFor(resourceRoute, id) + '?path=' + encodeURIComponent(path);
    const response = await fetch(url, {
      headers: {'X-Requested-With': 'XMLHttpRequest'},
      cache: 'no-store'
    });
    const data = await responseJson(response);
    if (!response.ok || !data.success) {
      throw new Error(data.message || 'RouterOS resource read failed');
    }
    return firstRow(data);
  }

  async function loadDirect(panel, id) {
    // System health is optional on some MikroTik models, so resource must
    // succeed while health may fail harmlessly.
    const resource = await readResource(id, '/system/resource');
    let health = {};
    try {
      health = await readResource(id, '/system/health');
    } catch (_) {
      health = {};
    }
    render(panel, buildFromResource(resource, health, panel));
  }

  async function loadLink(panel, id) {
    const response = await fetch(routeFor(linkRoute, id), {
      headers: {'X-Requested-With': 'XMLHttpRequest'},
      cache: 'no-store'
    });
    const data = await responseJson(response);

    if (!response.ok || !data.enrolled) {
      throw new Error('TapTap Link health unavailable');
    }

    // The normal Link status endpoint remains the fastest live source for the
    // basic CPU / memory / session fields.
    const live = buildFromLink(data, panel);

    // TapTap Link v6 also publishes the complete heartbeat resource/health maps
    // through the existing read-only resource endpoint. Read both menus so
    // storage, CPU details, temperature, voltage, fans and any board-specific
    // health sensor can be used without a direct RouterOS API connection.
    try {
      const resource = await readResource(id, '/system/resource');

      let health = {};
      try {
        health = await readResource(id, '/system/health');
      } catch (_) {
        health = {};
      }

      const extra = buildFromResource(resource, health, panel);

      live.cpu_load = extra.cpu_load || live.cpu_load;
      live.cpu_count = extra.cpu_count || live.cpu_count;
      live.cpu_frequency_mhz = extra.cpu_frequency_mhz || live.cpu_frequency_mhz;
      live.cpu_name = extra.cpu_name || live.cpu_name;

      live.memory_total = extra.memory_total || live.memory_total;
      live.memory_free = extra.memory_free || live.memory_free;
      live.memory_used = extra.memory_total ? extra.memory_used : live.memory_used;
      live.memory_percent = extra.memory_total ? extra.memory_percent : live.memory_percent;

      live.disk_total = extra.disk_total || live.disk_total;
      live.disk_free = extra.disk_free || live.disk_free;
      live.disk_used = extra.disk_total ? extra.disk_used : live.disk_used;
      live.disk_percent = extra.disk_total ? extra.disk_percent : live.disk_percent;

      live.architecture = extra.architecture || live.architecture;
      live.platform = extra.platform || live.platform;
      live.board = live.board || extra.board;
      live.version = live.version || extra.version;
      live.uptime = live.uptime || extra.uptime;

      if (extra.temperature_c != null) live.temperature_c = extra.temperature_c;
      if (extra.voltage_v != null) live.voltage_v = extra.voltage_v;

      live.health = health;
      live.resource = resource;
    } catch (_) {
      // During the short period before an old Link script self-updates to v6,
      // the basic Link heartbeat still keeps CPU/memory/session values visible.
    }

    render(panel, live);
  }

  function bindRestart(panel) {
    const button = panel.querySelector('[data-router-restart]');
    if (!button || button.dataset.bound === '1') return;

    button.dataset.bound = '1';
    button.addEventListener('click', function () {
      rebootRouter(panel, button);
    });
  }

  async function load(panel) {
    bindRestart(panel);

    const id = panel.id.replace('router-', '');
    if (!id) return;

    try {
      if (panel.dataset.mode === 'agent') {
        await loadLink(panel, id);
      } else {
        await loadDirect(panel, id);
      }
    } catch (err) {
      renderError(panel, err.message || 'Could not read RouterOS system health');
    }
  }

  async function refreshAll() {
    if (document.hidden) return;
    await Promise.allSettled(panels.map(load));
  }

  panels.forEach(bindRestart);
  refreshAll();

  const timer = window.setInterval(refreshAll, REFRESH_MS);

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) refreshAll();
  });

  window.addEventListener('beforeunload', function () {
    window.clearInterval(timer);
  }, {once: true});
})();
