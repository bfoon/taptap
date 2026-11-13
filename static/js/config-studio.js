/* TapTap Config Studio (core/config_studio.py).
 * The browser keeps a board of tiles: [{id, type, params, off}]. Every change is sent to the server, which checks
 * the values, turns the tiles into RouterOS rows, tries them on a virtual router and sends back what to draw.
 * Nothing here writes RouterOS commands, and nothing reaches the router before "Deploy". */
(function () {
  'use strict';
  var root = document.getElementById('cs');
  if (!root) return;
  var boot = JSON.parse(document.getElementById('csBoot').textContent);
  var URLS = boot.urls, CAT = boot.catalogue, TYPES = {}, GROUPS = {};
  CAT.tiles.forEach(function (t) { TYPES[t.type] = t; });
  CAT.groups.forEach(function (g) { GROUPS[g.key] = g; });

  var S = { tiles: [], base: 'router', result: null, state: null, tab: 'board', view: 'after', menu: '', open: {}, flash: {},
            busy: false, timer: null, poll: null, tick: null, seq: 0 };
  var $ = function (id) { return document.getElementById(id); };

  // ───────── small tools ─────────
  function h(tag, attrs) {
    var el = document.createElement(tag), i, k;
    for (k in (attrs || {})) {
      if (attrs[k] === null || attrs[k] === undefined || attrs[k] === false) continue;
      if (k === 'class') el.className = attrs[k];
      else if (k === 'text') el.textContent = attrs[k];
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), attrs[k]);
      else if (k === 'style') el.style.cssText = attrs[k];
      else el.setAttribute(k, attrs[k] === true ? '' : attrs[k]);
    }
    for (i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { add(el, c); }); return; }
    el.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  function icon(name) { return h('i', { class: 'bi ' + name, 'aria-hidden': 'true' }); }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
  function toast(text, kind) {
    var t = h('div', { class: 'cs-toast ' + (kind || '') }, text);
    $('csToasts').appendChild(t);
    setTimeout(function () { t.remove(); }, kind === 'bad' ? 9000 : 4500);
  }
  function api(url, body) {
    var opts = { headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' }, credentials: 'same-origin' };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers['Content-Type'] = 'application/json';
      opts.headers['X-CSRFToken'] = root.dataset.csrf;
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (r) {
      return r.json().catch(function () { return { ok: false, message: 'The server did not answer properly (' + r.status + ').' }; });
    }).catch(function () { return { ok: false, message: 'No connection to TapTap. Check your internet and try again.' }; });
  }
  function ago(iso) {
    var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 90) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return new Date(iso).toLocaleDateString();
  }
  function newId() { S.seq += 1; return 't' + Date.now().toString(36) + S.seq; }
  function defaults(type) {
    var p = {};
    TYPES[type].fields.forEach(function (f) { if (f.default) p[f.name] = f.default; });
    return p;
  }
  function ifaceNames() {
    var pic = S.result && S.result.picture, out = [];
    if (pic) { pic.groups.concat(pic.ports).forEach(function (p) { out.push(p.name); }); }
    return out.length ? out : ['ether1', 'ether2', 'ether3', 'ether4', 'ether5'];
  }

  // ───────── the shelf ─────────
  function drawShelf() {
    var box = clear($('csShelf')), q = $('csFind').value.trim().toLowerCase();
    CAT.groups.forEach(function (g) {
      var list = CAT.tiles.filter(function (t) {
        return t.group === g.key && (!q || (t.label + ' ' + t.sentence + ' ' + t.why).toLowerCase().indexOf(q) >= 0);
      });
      if (!list.length) return;
      box.appendChild(h('div', { class: 'cs-group', style: '--c:' + g.color }, h('b', { text: g.label }), h('small', { text: g.hint })));
      list.forEach(function (t) {
        var el = h('button', { type: 'button', class: 'cs-chip', draggable: 'true', style: '--c:' + g.color, title: t.why + ' — drag onto the board or onto a port, or click to add',
          ondragstart: function (e) { e.dataTransfer.setData('text/cs-type', t.type); e.dataTransfer.effectAllowed = 'copy'; root.classList.add('is-dragging'); },
          ondragend: function () { root.classList.remove('is-dragging'); },
          onclick: function () { addTile(t.type); } },
          h('span', { class: 'cs-chip-ic' }, icon(t.icon)), h('span', null, h('b', { text: t.label }), h('small', { text: t.why })),
          t.risk !== 'normal' ? h('em', { class: 'cs-risk ' + t.risk, text: t.risk === 'high' ? 'careful' : 'advanced' }) : null);
        box.appendChild(el);
      });
    });
    if (!box.firstChild) box.appendChild(h('p', { class: 'cs-empty', text: 'No tile matches. Try the helper box: say what you want in your own words.' }));
  }
  function drawKits() {
    var box = clear($('csKits'));
    box.appendChild(h('small', { class: 'cs-kits-title', text: 'Starter kits' }));
    boot.kits.forEach(function (k) {
      box.appendChild(h('button', { type: 'button', class: 'cs-kit', title: k.hint, onclick: function () {
        if (S.tiles.length && !window.confirm('Replace the tiles on the board with the “' + k.label + '” kit?')) return;
        S.tiles = k.tiles.map(function (t) { return { id: newId(), type: t.type, params: Object.assign(defaults(t.type), t.params), off: false }; });
        drawBoard(); check(0); toast('“' + k.label + '” is on the board. Adjust the blanks to your router.');
      } }, icon(k.icon), ' ' + k.label));
    });
  }

  // ───────── the board ─────────
  function addTile(type, at, preset) {
    var tile = { id: newId(), type: type, params: Object.assign(defaults(type), preset || {}), off: false };
    if (at === undefined || at === null || at > S.tiles.length) at = S.tiles.length;
    S.tiles.splice(at, 0, tile);
    S.flash[tile.id] = 1;
    setTab('board'); drawBoard(); check(0);
    var card = document.querySelector('[data-tile="' + tile.id + '"]');
    if (card) { card.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); var first = card.querySelector('input,select'); if (first) first.focus(); }
  }
  function fieldWidget(tile, f) {
    var value = tile.params[f.name] !== undefined ? tile.params[f.name] : '';
    function set(v) { tile.params[f.name] = v; check(); }
    if (f.type === 'choice') {
      var sel = h('select', { class: 'cs-blank', 'aria-label': f.label, onchange: function () { set(sel.value); } });
      f.options.forEach(function (o) { sel.appendChild(h('option', { value: o, text: o, selected: String(value || f.default) === o })); });
      return sel;
    }
    var input = h('input', { class: 'cs-blank', type: f.type === 'secret' ? 'password' : 'text', value: value, 'aria-label': f.label, 'data-field': f.name,
      placeholder: f.placeholder || f.label, autocomplete: 'off', spellcheck: 'false',
      inputmode: f.type === 'int' ? 'numeric' : null, list: f.type === 'iface' ? 'csIfaces' : null });
    function fit() { input.style.width = Math.max(5, Math.min(34, (input.value || input.placeholder).length + 2.5)) + 'ch'; }
    input.addEventListener('input', function () { fit(); set(input.value); });
    fit();
    if (f.type === 'ifaces' || f.type === 'services') {
      input.addEventListener('focus', function () { picker(input, f.type === 'services' ? f.options : ifaceNames(), function (v) { input.value = v; fit(); set(v); }); });
    }
    return input;
  }
  function picker(input, options, done) {
    var old = document.querySelector('.cs-picker'); if (old) old.remove();
    var chosen = input.value.split(/[,\s]+/).filter(Boolean);
    var box = h('div', { class: 'cs-picker' });
    options.forEach(function (name) {
      var b = h('button', { type: 'button', class: chosen.indexOf(name) >= 0 ? 'is-on' : '', text: name, onmousedown: function (e) { e.preventDefault(); },
        onclick: function () {
          var i = chosen.indexOf(name);
          if (i >= 0) chosen.splice(i, 1); else chosen.push(name);
          b.classList.toggle('is-on'); done(chosen.join(', '));
        } });
      box.appendChild(b);
    });
    input.parentNode.insertBefore(box, input.nextSibling);
    input.addEventListener('blur', function () { setTimeout(function () { box.remove(); }, 120); }, { once: true });
  }
  function sentence(tile, meta) {
    var line = h('div', { class: 'cs-sentence' }), used = {}, parts = meta.sentence.split(/(\{[a-z_]+\})/);
    parts.forEach(function (part) {
      var m = /^\{([a-z_]+)\}$/.exec(part);
      if (!m) { if (part) line.appendChild(document.createTextNode(part)); return; }
      var f = meta.fields.filter(function (x) { return x.name === m[1]; })[0];
      if (f) { used[f.name] = 1; line.appendChild(fieldWidget(tile, f)); }
    });
    var rest = meta.fields.filter(function (f) { return !used[f.name]; });
    return { line: line, rest: rest };
  }
  function drawBoard() {
    var board = clear($('csBoard')), res = S.result || {}, problems = {}, notes = {};
    (res.problems || []).forEach(function (p) { problems[p.tile] = p; });
    (res.findings || []).forEach(function (f) { if (f.tile && (!notes[f.tile] || f.level === 'bad')) notes[f.tile] = f; });
    $('csCountBoard').textContent = S.tiles.length ? S.tiles.length : '';
    if (!S.tiles.length) {
      board.appendChild(h('div', { class: 'cs-drop-empty' }, icon('bi-grid-1x2'),
        h('b', { text: 'Your board is empty' }),
        h('p', { text: 'Drag a tile here or onto a port of the router above, pick a starter kit, or just say what you want in the box. Nothing touches the real router until you press Deploy.' })));
    }
    S.tiles.forEach(function (tile, index) {
      var meta = TYPES[tile.type], g = GROUPS[meta.group], s = sentence(tile, meta), bad = problems[tile.id], note = notes[tile.id];
      var more = h('div', { class: 'cs-more', hidden: !S.open[tile.id] });
      s.rest.forEach(function (f) { more.appendChild(h('label', null, h('span', { text: f.label + (f.optional ? ' (optional)' : '') }), fieldWidget(tile, f))); });
      more.appendChild(h('p', { class: 'cs-why', text: meta.why }));
      var card = h('article', { class: 'cs-tile' + (tile.off ? ' is-off' : '') + (bad ? ' is-bad' : note ? ' is-' + note.level : '') + (S.flash[tile.id] ? ' is-new' : ''),
        'data-tile': tile.id, style: '--c:' + g.color,
        onmouseenter: function () { glow(tile.id, true); }, onmouseleave: function () { glow(tile.id, false); } },
        h('span', { class: 'cs-grip', draggable: 'true', title: 'Drag to move', 'aria-hidden': 'true',
          ondragstart: function (e) { e.dataTransfer.setData('text/cs-move', tile.id); e.dataTransfer.effectAllowed = 'move'; root.classList.add('is-dragging'); },
          ondragend: function () { root.classList.remove('is-dragging'); } }, icon('bi-grip-vertical')),
        h('span', { class: 'cs-tile-ic' }, icon(meta.icon)),
        h('div', { class: 'cs-tile-body' },
          h('div', { class: 'cs-tile-top' }, h('b', { text: meta.label }),
            meta.risk !== 'normal' ? h('em', { class: 'cs-risk ' + meta.risk, text: meta.risk === 'high' ? 'careful' : 'advanced' }) : null),
          s.line, more,
          bad ? h('p', { class: 'cs-msg bad' }, icon('bi-exclamation-octagon'), ' ' + bad.message) : null,
          !bad && note ? h('p', { class: 'cs-msg ' + note.level }, icon(note.level === 'bad' ? 'bi-exclamation-octagon' : 'bi-exclamation-triangle'), ' ' + note.text,
            note.fix ? h('span', { class: 'cs-fix', text: ' ' + note.fix }) : null) : null),
        h('div', { class: 'cs-tile-tools' },
          h('button', { type: 'button', title: tile.off ? 'Switch this tile on' : 'Switch this tile off (it stays on the board)', 'aria-pressed': String(!tile.off),
            onclick: function () { tile.off = !tile.off; drawBoard(); check(0); } }, icon(tile.off ? 'bi-toggle-off' : 'bi-toggle-on')),
          h('button', { type: 'button', title: 'More blanks and what this tile does', onclick: function () { S.open[tile.id] = !S.open[tile.id]; more.hidden = !S.open[tile.id]; } }, icon('bi-three-dots')),
          h('button', { type: 'button', title: 'Move up', disabled: index === 0, onclick: function () { move(tile.id, index - 1); } }, icon('bi-arrow-up')),
          h('button', { type: 'button', title: 'Move down', disabled: index === S.tiles.length - 1, onclick: function () { move(tile.id, index + 2); } }, icon('bi-arrow-down')),
          h('button', { type: 'button', title: 'Make a copy', onclick: function () { addTile(tile.type, index + 1, JSON.parse(JSON.stringify(tile.params))); } }, icon('bi-copy')),
          h('button', { type: 'button', title: 'Take this tile off the board', onclick: function () { S.tiles.splice(index, 1); drawBoard(); check(0); } }, icon('bi-x-lg'))));
      board.appendChild(card);
    });
    S.flash = {};
    var names = $('csIfaces') || document.body.appendChild(h('datalist', { id: 'csIfaces' }));
    clear(names); ifaceNames().forEach(function (n) { names.appendChild(h('option', { value: n })); });
  }
  function move(id, to) {
    var from = S.tiles.findIndex(function (t) { return t.id === id; });
    if (from < 0) return;
    var tile = S.tiles.splice(from, 1)[0];
    if (to > from) to -= 1;
    S.tiles.splice(Math.max(0, Math.min(S.tiles.length, to)), 0, tile);
    drawBoard(); check(0);
  }
  function dropIndex(y) {
    var cards = Array.prototype.slice.call($('csBoard').querySelectorAll('.cs-tile')), i;
    for (i = 0; i < cards.length; i++) { var r = cards[i].getBoundingClientRect(); if (y < r.top + r.height / 2) return i; }
    return cards.length;
  }
  function boardDnD() {
    var board = $('csBoard'), line = h('div', { class: 'cs-insert' });
    board.addEventListener('dragover', function (e) {
      e.preventDefault();
      var cards = board.querySelectorAll('.cs-tile'), i = dropIndex(e.clientY);
      if (cards[i]) board.insertBefore(line, cards[i]); else board.appendChild(line);
    });
    board.addEventListener('dragleave', function (e) { if (!board.contains(e.relatedTarget)) line.remove(); });
    board.addEventListener('drop', function (e) {
      e.preventDefault(); line.remove(); root.classList.remove('is-dragging');
      var type = e.dataTransfer.getData('text/cs-type'), moved = e.dataTransfer.getData('text/cs-move'), i = dropIndex(e.clientY);
      if (type && TYPES[type]) addTile(type, i); else if (moved) move(moved, i);
    });
  }
  function glow(id, on) {
    document.querySelectorAll('#csCode [data-tile]').forEach(function (el) { el.classList.toggle('is-lit', on && el.dataset.tile === id); });
    document.querySelectorAll('.cs-tile').forEach(function (el) { el.classList.toggle('is-lit', on && el.dataset.tile === id); });
  }

  // ───────── the router drawing ─────────
  var ROLE = { wan: 'Internet', lan: 'Local network', guest: 'Guests', hotspot: 'Hotspot' };
  function drawPlate() {
    var box = clear($('csPlate')), res = S.result, pic = res && res.picture;
    if (!pic) {
      box.appendChild(h('div', { class: 'cs-plate-empty' }, icon('bi-router'),
        h('div', null, h('b', { text: 'TapTap has not read this router yet' }),
          h('p', { text: 'Press “Read the router” to see its real ports and settings here — or switch to a practice router at the top and start building right away.' }))));
      return;
    }
    var shell = h('div', { class: 'cs-shell' });
    shell.appendChild(h('div', { class: 'cs-shell-top' }, h('span', { class: 'cs-brand', text: 'MikroTik' }), h('b', { text: pic.identity || boot.router }),
      h('span', { class: 'cs-shell-tag', text: res.info.lab ? 'practice router — nothing here is real' : 'virtual copy' + (res.info.captured_at ? ' · read ' + ago(res.info.captured_at) : '') }),
      h('span', { class: 'cs-legend' }, Object.keys(ROLE).map(function (r) { return h('i', { class: 'r-' + r, text: ROLE[r] }); }))));
    var bank = h('div', { class: 'cs-ports' });
    pic.ports.forEach(function (p) {
      var jack = h('div', { class: 'cs-port r-' + (p.role || 'none') + (p.off ? ' is-off' : '') + (p.running ? ' is-up' : '') + (p.mark ? ' is-touched' : ''),
        title: p.name + (p.role ? ' — ' + ROLE[p.role] : '') + (p.bridge ? ' — in ' + p.bridge : '') + (p.off ? ' — switched off' : '') + '. Drop a tile here to use this port.',
        'data-port': p.name },
        h('span', { class: 'cs-jack' }), h('b', { text: p.name }),
        h('small', { text: p.addresses.length ? p.addresses[0].address : p.bridge || (p.off ? 'off' : p.role ? ROLE[p.role] : 'free') }));
      portDrop(jack, p.name);
      bank.appendChild(jack);
    });
    shell.appendChild(bank);
    if (pic.groups.length) {
      var groups = h('div', { class: 'cs-nets' });
      pic.groups.forEach(function (g) {
        var pill = h('div', { class: 'cs-net r-' + (g.role || 'none') + (g.mark ? ' is-touched' : ''), 'data-port': g.name, title: 'Drop a tile here to use ' + g.name },
          icon(g.type === 'vlan' ? 'bi-layers' : g.type.indexOf('pppoe') === 0 ? 'bi-telephone-outbound' : 'bi-diagram-3'), h('b', { text: g.name }),
          h('small', { text: (g.addresses.length ? g.addresses[0].address + ' · ' : '') + (g.members && g.members.length ? g.members.join(' ') : g.type) }));
        portDrop(pill, g.name);
        groups.appendChild(pill);
      });
      shell.appendChild(groups);
    }
    box.appendChild(shell);
  }
  function portDrop(el, name) {
    el.addEventListener('dragover', function (e) { e.preventDefault(); el.classList.add('is-over'); });
    el.addEventListener('dragleave', function () { el.classList.remove('is-over'); });
    el.addEventListener('drop', function (e) {
      e.preventDefault(); e.stopPropagation(); el.classList.remove('is-over'); root.classList.remove('is-dragging');
      var type = e.dataTransfer.getData('text/cs-type');
      if (!type || !TYPES[type]) return;
      var f = TYPES[type].fields.filter(function (x) { return x.type === 'iface' || x.type === 'ifaces'; })[0], preset = {};
      if (f) preset[f.name] = name; else toast('“' + TYPES[type].label + '” is not about one port, so it went onto the board as it is.');
      addTile(type, null, preset);
    });
  }

  // ───────── code, findings, tests ─────────
  function drawCode() {
    var pre = clear($('csCode')), res = S.result || {}, last = null;
    if (!(res.code || []).length) { pre.appendChild(h('span', { class: 'cs-dim', text: '# The RouterOS commands your tiles stand for appear here as you build.' })); return; }
    res.code.forEach(function (line) {
      if (line.tile !== last) {
        var tile = S.tiles.filter(function (t) { return t.id === line.tile; })[0];
        pre.appendChild(h('span', { class: 'cs-c', 'data-tile': line.tile, text: '# ' + (tile ? TYPES[tile.type].label : 'tile') + '\n' }));
        last = line.tile;
      }
      var m = /^(\S+(?: [a-z-]+)*?) (add|set|remove)( .*)?$/.exec(line.text), row = h('span', { class: 'cs-l', 'data-tile': line.tile, title: line.label,
        onmouseenter: function () { glow(line.tile, true); }, onmouseleave: function () { glow(line.tile, false); } });
      if (m) { add(row, [h('span', { class: 'k-menu', text: m[1] }), ' ', h('span', { class: 'k-verb', text: m[2] }), m[3] || '']); } else row.textContent = line.text;
      row.appendChild(document.createTextNode('\n'));
      pre.appendChild(row);
    });
  }
  function drawLab() {
    var res = S.result || {}, box = clear($('csFindings')), tests = clear($('csTests'));
    var bad = (res.findings || []).filter(function (f) { return f.level === 'bad'; }).length + (res.problems || []).length;
    $('csCountLab').textContent = bad ? bad : '';
    $('csCountLab').className = bad ? 'is-bad' : '';
    (res.problems || []).forEach(function (p) { box.appendChild(finding({ level: 'bad', text: p.message, tile: p.tile })); });
    (res.findings || []).forEach(function (f) { box.appendChild(finding(f)); });
    if (!box.firstChild && res.picture) box.appendChild(h('div', { class: 'cs-finding ok' }, icon('bi-check-circle'), h('div', null, h('b', { text: 'The virtual router found nothing wrong.' }))));
    (res.tests || []).forEach(function (t) { tests.appendChild(testRow(t)); });
    if (!tests.firstChild) tests.appendChild(h('p', { class: 'cs-empty', text: 'Tiles such as Local network, Guest network, Shield the router, Block sites and Open a port are tried with real-looking packets. Add one to see it here.' }));
    var from = $('csFrom'), keep = from.value; clear(from);
    ((res.picture && res.picture.sources) || []).forEach(function (s) { from.appendChild(h('option', { value: s.key, text: s.label, 'data-ip': s.ip, 'data-iface': s.iface })); });
    if (keep) from.value = keep;
  }
  function finding(f) {
    var ic = { bad: 'bi-exclamation-octagon', warn: 'bi-exclamation-triangle', info: 'bi-info-circle' }[f.level] || 'bi-info-circle';
    return h('div', { class: 'cs-finding ' + f.level, onmouseenter: function () { glow(f.tile, true); }, onmouseleave: function () { glow(f.tile, false); } }, icon(ic),
      h('div', null, h('b', { text: f.text }), f.fix ? h('small', { text: f.fix }) : null,
        f.tile ? h('button', { type: 'button', class: 'btn btn-sm btn-link p-0', text: 'Show the tile', onclick: function () { showTile(f.tile); } }) : null));
  }
  function showTile(id) {
    setTab('board');
    var card = document.querySelector('[data-tile="' + id + '"].cs-tile');
    if (card) { card.scrollIntoView({ block: 'center', behavior: 'smooth' }); card.classList.add('is-new'); setTimeout(function () { card.classList.remove('is-new'); }, 1600); }
  }
  var STAGE = { arrive: 'bi-box-arrow-in-right', hotspot: 'bi-wifi', dstnat: 'bi-door-open', route: 'bi-signpost-2', firewall: 'bi-shield', service: 'bi-plug', nat: 'bi-mask', speed: 'bi-speedometer2' };
  function journey(tr) {
    var box = h('ol', { class: 'cs-journey' });
    tr.steps.forEach(function (s, i) {
      box.appendChild(h('li', { class: s.level, style: 'animation-delay:' + (i * 140) + 'ms' }, h('span', { class: 'cs-st' }, icon(STAGE[s.stage] || 'bi-dot')),
        h('div', null, h('span', { text: s.text }), s.rule && s.rule.comment ? h('code', { text: s.rule.comment }) : null)));
    });
    var good = tr.verdict === 'ok', word = { ok: 'Gets through', login: 'After sign-in', blocked: 'Stopped', noroute: 'Nowhere to go', nonat: 'No way back', warn: 'Gets through' }[tr.verdict] || tr.verdict;
    box.appendChild(h('li', { class: 'cs-verdict ' + (good ? 'ok' : tr.verdict === 'login' ? 'warn' : 'bad'), style: 'animation-delay:' + (tr.steps.length * 140) + 'ms' },
      h('span', { class: 'cs-st' }, icon(good ? 'bi-check-lg' : tr.verdict === 'login' ? 'bi-person-lock' : 'bi-x-lg')), h('div', null, h('b', { text: word + ' — ' }), tr.summary)));
    return box;
  }
  function testRow(t) {
    var steps = h('div', { hidden: true });
    return h('div', { class: 'cs-test ' + (t.pass ? 'pass' : 'fail') + (t.lifeline ? ' lifeline' : '') },
      h('button', { type: 'button', class: 'cs-test-head', onclick: function () { if (!steps.firstChild) steps.appendChild(journey(t)); steps.hidden = !steps.hidden; } },
        icon(t.pass ? 'bi-check-circle-fill' : 'bi-x-circle-fill'), h('span', null, h('b', { text: t.title }),
          h('small', { text: (t.want === 'reaches' ? 'Should get through' : 'Should be stopped') + ' · ' + t.summary })), icon('bi-chevron-down')), steps);
  }

  // ───────── every setting ─────────
  function drawMenus() {
    var res = S.result || {}, list = clear($('csMenuList')), groups = {}, touched = 0;
    (res.sections || []).forEach(function (s) { (groups[s.group] = groups[s.group] || []).push(s); touched += s.touched ? 1 : 0; });
    $('csCountMenus').textContent = touched ? touched : '';
    Object.keys(groups).sort().forEach(function (g) {
      list.appendChild(h('small', { class: 'cs-menu-group', text: '/' + g }));
      groups[g].forEach(function (s) {
        list.appendChild(h('button', { type: 'button', class: (S.menu === s.path ? 'is-on ' : '') + (s.touched ? 'is-touched' : ''), onclick: function () { S.menu = s.path; drawMenus(); loadRows(); } },
          h('span', { text: s.label }), h('em', { text: s.touched ? '±' + s.touched : s.count })));
      });
    });
    if (!list.firstChild) list.appendChild(h('p', { class: 'cs-empty', text: 'Nothing to list yet: read the router, or pick a practice router.' }));
  }
  function loadRows() {
    if (!S.menu) return;
    var box = $('csRows');
    api(URLS.section, { path: S.menu, view: S.view }).then(function (r) {
      clear(box);
      if (!r.ok) { box.appendChild(h('p', { class: 'cs-empty', text: r.message })); return; }
      $('csMenuTitle').textContent = r.label + ' — ' + r.path + ' (' + r.total + ')';
      var q = $('csRowFind').value.trim().toLowerCase(), cols = [], seen = {};
      r.rows.forEach(function (x) { Object.keys(x.row).forEach(function (k) { if (!seen[k] && k !== 'id') { seen[k] = 1; cols.push(k); } }); });
      var first = ['name', 'chain', 'action', 'interface', 'address', 'list', 'comment'];
      cols.sort(function (a, b) { return (first.indexOf(a) < 0 ? 99 : first.indexOf(a)) - (first.indexOf(b) < 0 ? 99 : first.indexOf(b)); });
      cols = cols.slice(0, 14);
      var table = h('table', { class: 'table table-sm cs-table' }), body = h('tbody');
      table.appendChild(h('thead', null, h('tr', null, h('th'), cols.map(function (c) { return h('th', { text: c }); }), h('th'))));
      r.rows.forEach(function (x) {
        if (q && JSON.stringify(x.row).toLowerCase().indexOf(q) < 0) return;
        body.appendChild(h('tr', { class: x.mark ? 'm-' + x.mark : '' },
          h('td', null, x.mark ? h('span', { class: 'cs-mark ' + x.mark, text: x.mark }) : null),
          cols.map(function (c) {
            var was = x.was && x.was[c] !== undefined;
            return h('td', { class: was ? 'is-changed' : '' }, was ? h('s', { text: String(x.was[c] || '—') + ' ' }) : null, x.row[c] === undefined ? '' : String(x.row[c]));
          }),
          h('td', null, CAT.menus.some(function (m) { return r.path === m || r.path.indexOf(m + '/') === 0; }) && !x.mark ? h('button', { type: 'button', class: 'btn btn-sm btn-link p-0', title: 'Put this row on the board as an expert tile, to change it',
            onclick: function () { rowTile(r.path, x.row); } }, icon('bi-pencil-square')) : null)));
      });
      if (!body.firstChild) box.appendChild(h('p', { class: 'cs-empty', text: r.rows.length ? 'No row matches the filter.' : 'This menu is empty on the router.' }));
      else { table.appendChild(body); box.appendChild(h('div', { class: 'table-responsive' }, table)); }
      if (r.total > r.rows.length) box.appendChild(h('p', { class: 'cs-empty', text: 'Showing the first ' + r.rows.length + ' of ' + r.total + ' rows.' }));
    });
  }
  function rowTile(path, row) {
    function quote(v) { return /^[\w.:\/,!-]+$/.test(String(v)) ? String(v) : '"' + String(v).replace(/"/g, '') + '"'; }
    var key = ['name', 'comment', 'interface', 'address', 'mac-address'].filter(function (k) { return row[k]; })[0];
    if (!key) { toast('This row has no name or comment to recognise it by, so it cannot become a tile.', 'bad'); return; }
    addTile('custom', null, { menu: path, match: key + '=' + quote(row[key]), set: row.disabled !== undefined ? 'disabled=' + (String(row.disabled) === 'true' ? 'yes' : 'no') : '' });
    toast('The row is on the board as an expert tile. Write what should change after “gets”.');
  }

  // ───────── to the router ─────────
  function step(id, state, text) {
    var li = $(id); li.className = state || ''; li.querySelector('small').textContent = text;
  }
  function drawLaunch() {
    var res = S.result || {}, st = S.state || {}, counts = res.counts || {}, lab = res.info && res.info.lab;
    var active = (st.deploys || []).filter(function (d) { return d.status === 'sending' || d.status === 'applied'; })[0];
    var problems = (res.problems || []).length, bad = (res.findings || []).filter(function (f) { return f.level === 'bad'; }).length;
    var warn = (res.findings || []).filter(function (f) { return f.level === 'warn'; }).length;
    var tests = res.tests || [], passed = tests.filter(function (t) { return t.pass; }).length;
    var changing = (counts.new || 0) + (counts.changed || 0) + (counts.removed || 0), on = S.tiles.filter(function (t) { return !t.off; }).length;
    step('csStepBoard', !on ? '' : problems ? 'bad' : 'ok', !on ? 'Put tiles on the board.' : problems ? problems + ' tile(s) have a blank that is not right.'
      : on + ' tile(s) → ' + (counts.new || 0) + ' new, ' + (counts.changed || 0) + ' changed, ' + (counts.removed || 0) + ' removed row(s)' + (counts.same ? ', ' + counts.same + ' already there' : ''));
    step('csStepLab', !on || res.needs_read ? '' : bad ? 'bad' : warn ? 'warn' : 'ok', res.needs_read ? 'Read the router first.' : !on ? 'Tests run by themselves as you build.'
      : (tests.length ? passed + ' of ' + tests.length + ' packets behave as they should' : 'No packet tests for these tiles') + (bad ? ' · ' + bad + ' problem(s)' : '') + (warn ? ' · ' + warn + ' warning(s)' : ''));
    var b = st.backup, fresh = b && b.fresh;
    step('csStepBackup', fresh ? 'ok' : '', b ? b.name + ' · ' + ago(b.at) + (b.state ? ' · ' + b.state : '') + (fresh ? '' : ' — older than ' + boot.backup_fresh + ' min, make a new one') : 'No backup yet. One is needed before a deploy.');
    var ready = on && !problems && changing && !lab && !res.needs_read && !active;
    step('csStepDeploy', '', lab ? 'This is a practice router. Choose “' + boot.router + ' as it is now” at the top to deploy.' : active ? 'A change is in progress (below).'
      : !on ? 'Nothing to deploy yet.' : !changing && !problems ? 'The router already has everything on this board.'
      : 'Goes over ' + (st.transport || '…') + '. The undo timer is set on the router first.' + (st.online === false ? ' ' + st.offline_reason : ''));
    $('csDeploy').disabled = !ready || S.busy;
    $('csBackup').disabled = S.busy || lab;
    drawLive(active);
    var hist = clear($('csHistory'));
    (st.deploys || []).forEach(function (d) {
      hist.appendChild(h('div', { class: 'cs-hist s-' + d.status },
        h('div', null, h('b', { text: '#' + d.id + ' · ' + d.status_label }), h('small', { text: d.summary + ' · ' + ago(d.at) + (d.by ? ' · ' + d.by : '') + ' · ' + d.transport }),
          d.error ? h('small', { class: 'cs-hist-err', text: d.error }) : null,
          (d.manual_undo || []).map(function (m) { return h('small', { class: 'cs-hist-err', text: m }); })),
        h('span', null,
          h('button', { type: 'button', class: 'btn btn-sm btn-link p-0', title: 'Put this board back on the screen', text: 'Load', onclick: function () {
            if (S.tiles.length && !window.confirm('Replace the tiles on the board with the board of change #' + d.id + '?')) return;
            S.tiles = d.tiles.map(function (t) { return { id: newId(), type: t.type, params: t.params, off: t.off }; }); drawBoard(); check(0);
          } }),
          d.status === 'kept' && d.can_undo ? h('button', { type: 'button', class: 'btn btn-sm btn-link p-0 text-danger', text: 'Undo', onclick: function () { act(d, 'undo'); } }) : null)));
    });
    if (!hist.firstChild) hist.appendChild(h('p', { class: 'cs-empty', text: 'Changes made from this studio are listed here, each with its own undo.' }));
    $('csTransport').textContent = (st.transport || '') + (st.status ? ' · ' + st.status : '');
  }
  function drawLive(d) {
    var box = clear($('csLive'));
    clearInterval(S.tick);
    if (!d) return;
    if (d.status === 'sending') {
      box.appendChild(h('div', { class: 'cs-live sending' }, h('span', { class: 'cs-spin' }), h('div', null, h('b', { text: 'On its way to the router…' }),
        h('small', { text: d.progress ? 'Step ' + d.progress[0] + ' of ' + d.progress[1] + ' through TapTap Link' : 'Applying ' + d.steps + ' rows' }))));
      return;
    }
    var left = d.seconds_left, clock = h('b', { class: 'cs-clock' });
    function paint() { clock.textContent = Math.floor(left / 60) + ':' + ('0' + (left % 60)).slice(-2); }
    paint();
    S.tick = setInterval(function () { left = Math.max(0, left - 1); paint(); if (!left) { clearInterval(S.tick); loadState(); } }, 1000);
    box.appendChild(h('div', { class: 'cs-live applied' },
      h('div', null, h('b', { text: 'It is on the router. Does everything still work?' }),
        h('small', null, 'If nobody presses Keep, the router puts the old setup back in ', clock, '.')),
      h('div', { class: 'cs-live-btns' },
        h('button', { type: 'button', class: 'btn btn-success', onclick: function () { act(d, 'keep'); } }, icon('bi-check-lg'), ' Keep it'),
        h('button', { type: 'button', class: 'btn btn-outline-danger', onclick: function () { act(d, 'undo'); } }, icon('bi-arrow-counterclockwise'), ' Undo now'))));
  }
  function act(d, action) {
    if (action === 'undo' && !window.confirm('Put the router back as it was before change #' + d.id + '?')) return;
    S.busy = true; drawLaunch();
    api(URLS.action.replace(/0\/$/, d.id + '/'), { action: action }).then(function (r) {
      S.busy = false;
      if (!r.ok) toast(r.message, 'bad'); else toast(action === 'keep' ? (r.deploy.status === 'kept' ? 'Kept. The undo timer is off.' : 'Sent — the router confirms in a few seconds.') : 'Undo sent.', 'ok');
      loadState(); check(0);
    });
  }
  function modal(title, body, go, action) {
    $('csModalTitle').textContent = title; clear($('csModalBody')).appendChild(body);
    var btn = $('csModalGo'), m = window.bootstrap.Modal.getOrCreateInstance($('csModal'));
    btn.textContent = go; btn.onclick = function () { if (action() !== false) m.hide(); };
    m.show();
  }
  function deploy(extra) {
    var res = S.result || {}, c = res.counts || {}, st = S.state || {};
    var bad = (res.findings || []).filter(function (f) { return f.level === 'bad'; });
    var word = h('input', { class: 'form-control', placeholder: 'DEPLOY ANYWAY', 'aria-label': 'Type DEPLOY ANYWAY' });
    var nob = h('input', { class: 'form-control', placeholder: 'NO BACKUP', 'aria-label': 'Type NO BACKUP' });
    var fresh = st.backup && st.backup.fresh, minutes = parseInt($('csUndo').value, 10);
    var body = h('div', null,
      h('p', null, 'This puts ', h('b', { text: (c.new || 0) + ' new, ' + (c.changed || 0) + ' changed and ' + (c.removed || 0) + ' removed row(s)' }), ' on ', h('b', { text: boot.router }), ' over ' + (st.transport || 'the router connection') + '.'),
      h('ul', { class: 'cs-modal-list' },
        h('li', null, fresh ? 'Backup “' + st.backup.name + '” was made ' + ago(st.backup.at) + '.' : 'There is no fresh backup.'),
        h('li', null, minutes ? 'The router undoes the change by itself after ' + minutes + ' minutes unless you press Keep.' : 'No undo timer: the change stays even if it cuts the router off.'),
        h('li', null, 'If the router refuses a row, the rows before it are taken back.')),
      bad.length ? h('div', { class: 'alert alert-danger' }, h('b', { text: 'The virtual router found ' + bad.length + ' serious problem(s):' }),
        h('ul', null, bad.slice(0, 4).map(function (f) { return h('li', { text: f.text }); })), 'To go ahead anyway, type DEPLOY ANYWAY:', word) : null,
      !fresh ? h('div', { class: 'alert alert-warning' }, 'Close this and press “Back up now” first. To deploy without a backup, type NO BACKUP:', nob) : null,
      extra ? h('div', { class: 'alert alert-danger', text: extra }) : null);
    modal('Deploy to ' + boot.router + '?', body, 'Deploy now', function () {
      S.busy = true; drawLaunch();
      api(URLS.deploy, { undo_minutes: minutes, confirm: word.value, no_backup: nob.value }).then(function (r) {
        S.busy = false;
        if (r.deploy) { toast(r.ok ? (r.deploy.status === 'sending' ? 'Sent through TapTap Link. Watch the steps on the right.' : 'It is on the router. Check that everything works, then press Keep.') : r.message, r.ok ? 'ok' : 'bad'); }
        else if (r.needs || r.needs_backup) { deploy(r.message); }
        else toast(r.message, 'bad');
        loadState(); check(0);
      });
    });
  }

  // ───────── talking to the server ─────────
  function drawAll() { drawBoard(); drawPlate(); drawCode(); drawLab(); drawMenus(); drawLaunch(); if (S.tab === 'menus') loadRows(); }
  function check(wait) {
    clearTimeout(S.timer);
    S.timer = setTimeout(function () {
      var ticket = S.ticket = (S.ticket || 0) + 1;
      root.classList.add('is-checking');
      api(URLS.check, { tiles: S.tiles, base: S.base }).then(function (r) {
        if (ticket !== S.ticket) return;                 // a newer check is on its way
        root.classList.remove('is-checking');
        if (!r.ok) { toast(r.message, 'bad'); return; }
        S.result = r;
        var active = document.activeElement, editing = active && active.classList && active.classList.contains('cs-blank') && $('csBoard').contains(active);
        if (editing) { softBoard(r); drawPlate(); drawCode(); drawLab(); drawMenus(); drawLaunch(); } else drawAll();
      });
    }, wait === undefined ? 450 : wait);
  }
  function softBoard(res) {
    // While somebody is typing in a blank the cards are not rebuilt (the cursor would jump): only their state changes.
    var problems = {}, notes = {};
    (res.problems || []).forEach(function (p) { problems[p.tile] = p; });
    (res.findings || []).forEach(function (f) { if (f.tile && (!notes[f.tile] || f.level === 'bad')) notes[f.tile] = f; });
    document.querySelectorAll('.cs-tile').forEach(function (card) {
      var id = card.dataset.tile, p = problems[id], n = notes[id], msg = card.querySelector('.cs-msg');
      card.classList.remove('is-bad', 'is-warn', 'is-info');
      if (p) card.classList.add('is-bad'); else if (n) card.classList.add('is-' + n.level);
      if (msg) msg.remove();
      if (p || n) card.querySelector('.cs-tile-body').appendChild(h('p', { class: 'cs-msg ' + (p ? 'bad' : n.level) }, icon(p || n.level === 'bad' ? 'bi-exclamation-octagon' : 'bi-exclamation-triangle'), ' ' + (p ? p.message : n.text)));
    });
  }
  function loadState(first) {
    return api(URLS.state).then(function (r) {
      if (!r.ok) { toast(r.message, 'bad'); return; }
      S.state = r;
      if (first) { S.tiles = r.tiles || []; S.base = r.base || 'router'; $('csBase').value = S.base; if (r.chat && r.chat.length) say(r.chat[r.chat.length - 1]); }
      drawLaunch();
      var active = (r.deploys || []).some(function (d) { return d.status === 'sending' || d.status === 'applied'; });
      clearTimeout(S.poll);
      if (active) S.poll = setTimeout(function () { loadState(); }, 3000);
    });
  }
  function say(m) {
    var box = $('csSay'); clear(box); box.hidden = false;
    box.appendChild(icon('bi-stars')); box.appendChild(h('div', null, h('span', { text: m.text }), m.engine ? h('small', { text: m.engine }) : null));
    box.appendChild(h('button', { type: 'button', class: 'btn-close', 'aria-label': 'Close', onclick: function () { box.hidden = true; } }));
  }
  function setTab(tab) {
    S.tab = tab;
    document.querySelectorAll('.cs-tabs button').forEach(function (b) { b.classList.toggle('is-on', b.dataset.tab === tab); b.setAttribute('aria-selected', String(b.dataset.tab === tab)); });
    document.querySelectorAll('.cs-pane').forEach(function (p) { p.hidden = p.dataset.pane !== tab; });
    if (tab === 'menus') { if (!S.menu && S.result && S.result.sections) { var t = S.result.sections.filter(function (s) { return s.touched; })[0] || S.result.sections[0]; if (t) { S.menu = t.path; drawMenus(); } } loadRows(); }
  }

  // ───────── wiring ─────────
  document.querySelectorAll('.cs-tabs button').forEach(function (b) { b.addEventListener('click', function () { setTab(b.dataset.tab); }); });
  document.querySelectorAll('[data-view]').forEach(function (b) {
    b.addEventListener('click', function () { S.view = b.dataset.view; document.querySelectorAll('[data-view]').forEach(function (x) { x.classList.toggle('active', x === b); }); loadRows(); });
  });
  $('csFind').addEventListener('input', drawShelf);
  $('csRowFind').addEventListener('input', loadRows);
  $('csBase').addEventListener('change', function () { S.base = this.value; S.menu = ''; check(0); });
  $('csTo').addEventListener('change', function () { $('csToIpWrap').hidden = this.value !== 'ip'; });
  $('csExport').addEventListener('click', function (e) { e.preventDefault(); window.location.href = URLS.export; });
  $('csCopy').addEventListener('click', function () {
    var text = ((S.result && S.result.code) || []).map(function (l) { return l.text; }).join('\n');
    if (navigator.clipboard && text) navigator.clipboard.writeText(text).then(function () { toast('Copied. Passwords are hidden in this view; the .rsc file has them.', 'ok'); });
  });
  $('csRead').addEventListener('click', function () {
    var btn = this; btn.disabled = true;
    api(URLS.read, {}).then(function (r) { btn.disabled = false; toast(r.message, r.ok ? 'ok' : 'bad'); if (r.ok) { check(0); if (r.queued) setTimeout(function () { check(0); }, 20000); } });
  });
  $('csBackup').addEventListener('click', function () {
    S.busy = true; drawLaunch();
    api(URLS.backup, {}).then(function (r) {
      S.busy = false; toast(r.ok ? 'Backup started: ' + r.backup.name : r.message, r.ok ? 'ok' : 'bad');
      loadState(); [4000, 12000, 30000].forEach(function (ms) { setTimeout(function () { loadState(); }, ms); });
    });
  });
  $('csDeploy').addEventListener('click', function () { deploy(); });
  $('csAsk').addEventListener('submit', function (e) {
    e.preventDefault();
    var input = $('csAskText'), text = input.value.trim(), btn = $('csAskGo');
    if (!text) { input.focus(); return; }
    btn.disabled = true; btn.textContent = 'Thinking…';
    api(URLS.ai, { text: text, tiles: S.tiles, base: S.base }).then(function (r) {
      btn.disabled = false; btn.textContent = 'Build it';
      if (!r.ok) { toast(r.message, 'bad'); return; }
      input.value = ''; S.tiles = r.tiles; (r.touched || []).forEach(function (id) { S.flash[id] = 1; });
      say({ text: r.say, engine: r.engine }); setTab('board'); drawBoard(); check(0);
    });
  });
  $('csTrace').addEventListener('submit', function (e) {
    e.preventDefault();
    var opt = $('csFrom').selectedOptions[0], to = $('csTo').value, box = clear($('csJourney'));
    if (!opt) { box.appendChild(h('p', { class: 'cs-empty', text: 'The router has no network to send a packet from yet. Add an Internet line or a Local network tile.' })); return; }
    api(URLS.trace, { ip: opt.dataset.ip, iface: opt.dataset.iface, to: to === 'ip' ? $('csToIp').value : to, host: $('csHost').value, proto: $('csProto').value,
      port: $('csPort').value, mac: $('csMac').value, logged_in: true, view: $('csNowOnly').checked ? 'now' : 'after' }).then(function (r) {
      if (!r.ok) { box.appendChild(h('p', { class: 'cs-msg bad', text: r.message })); return; }
      box.appendChild(journey(r.trace));
    });
  });
  boot.undo_choices.forEach(function (m) { $('csUndo').appendChild(h('option', { value: m, text: m ? m + ' minutes' : 'never (no safety net)', selected: m === 5 })); });

  drawShelf(); drawKits(); boardDnD();
  loadState(true).then(function () { drawBoard(); check(0); });
})();
