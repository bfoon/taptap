/* TapTap menu: find, pin, fold.
 *
 * Works on the menu exactly as the server sent it (every add-on's links and the plan locks included) and only
 * changes how it is shown — no link is added, removed or changed, so nothing that builds the menu can break.
 *
 *  • Find  — a box on top of the menu ("/" or Ctrl+K): type a few letters, ↑ ↓ to choose, Enter to open.
 *            It also understands everyday words ("money" → Finance, "wifi code" → Vouchers…).
 *  • Pin   — the ☆ next to any page puts it in "Pinned" at the top (kept on this device).
 *  • Fold  — tap a section title to fold it; the section of the page you are on always stays open.
 *  • Phone — the same find box and pins in the phone menu, and the links grouped under the same titles.
 */
(function () {
  'use strict';
  var KEY_PINS = 'tt-nav-pins', KEY_FOLD = 'tt-nav-fold';
  var WORDS = {   // everyday words → menu words (all lowercase)
    money: 'finance sales', cash: 'finance sales agents', income: 'finance sales reports', sales: 'daily sales finance',
    code: 'vouchers generate', codes: 'vouchers generate', ticket: 'vouchers', wifi: 'vouchers mikrotik', voucher: 'vouchers',
    print: 'generate batches voucher designer', make: 'generate', create: 'generate', customer: 'members active users customer reports',
    customers: 'members active users', users: 'active users members', online: 'active users online sales', router: 'mikrotik inventory topology',
    mikrotik: 'mikrotik control', map: 'topology', speed: 'traffic network tools', slow: 'traffic network tools', internet: 'mikrotik network tools traffic',
    block: 'security ip binding devices', phone: 'devices active users', staff: 'team', worker: 'team', password: 'my password',
    pay: 'subscription finance my pay', plan: 'plans subscription', price: 'plans', login: 'portal studio', page: 'portal studio',
    design: 'voucher designer portal studio', advert: 'adverts', ad: 'adverts', email: 'email alerts', alarm: 'alerts', help: 'support',
    report: 'reports customer reports', complaint: 'customer reports', deleted: 'bin', missing: 'missing vouchers', buy: 'market', sell: 'market'
  };

  function store(k, v) { try { if (v === undefined) return JSON.parse(localStorage.getItem(k) || 'null'); localStorage.setItem(k, JSON.stringify(v)); } catch (e) { return null; } }
  function text(a) { return (a.textContent || '').replace(/\s+/g, ' ').trim(); }
  function hrefOf(a) { return a.getAttribute('href') || ''; }
  function esc(t) { return String(t || '').replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function norm(s) { s = String(s || '').toLowerCase(); return s.normalize ? s.normalize('NFD').replace(/[\u0300-\u036f]/g, '') : s; }

  // ── the desktop menu ──
  var side = document.querySelector('aside.sidebar nav');
  var links = [];       // {a, label, group}
  if (side) {
    var group = '', groupEl = null, groups = [];
    [].slice.call(side.children).forEach(function (el) {
      if (el.classList.contains('nav-label')) { group = text(el); groupEl = el; groups.push({ name: group, label: el, items: [] }); return; }
      if (el.tagName === 'A') { var item = { a: el, label: text(el), group: group }; links.push(item); if (groups.length) groups[groups.length - 1].items.push(item); }
    });
    buildFold(groups);
    buildFind(side, links, true);
    buildPins(side, links);
    // the page you are on is visible without scrolling the menu by hand (only the menu scrolls, never the page)
    var act = side.querySelector('a.active:not(.tt-pinned)');
    if (act && side.scrollHeight > side.clientHeight) {
      var top = act.getBoundingClientRect().top - side.getBoundingClientRect().top + side.scrollTop;
      if (top > side.clientHeight - 60) side.scrollTop = top - side.clientHeight / 2;
    }
  }

  function buildFold(groups) {
    var folded = store(KEY_FOLD) || {};
    groups.forEach(function (g) {
      if (!g.items.length) return;
      var here = g.items.some(function (i) { return i.a.classList.contains('active'); });
      var btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'nav-label tt-fold'; btn.setAttribute('aria-expanded', 'true');
      btn.innerHTML = '<span>' + esc(g.name) + '</span><em>' + g.items.length + '</em><i class="bi bi-chevron-down"></i>';
      g.label.parentNode.replaceChild(btn, g.label);
      g.btn = btn;
      var set = function (closed, save) {
        btn.setAttribute('aria-expanded', closed ? 'false' : 'true'); btn.classList.toggle('closed', closed);
        g.items.forEach(function (i) { i.a.classList.toggle('tt-folded', closed); });
        if (save) { folded[g.name] = closed; store(KEY_FOLD, folded); }
      };
      set(!!folded[g.name] && !here, false);
      btn.addEventListener('click', function () { set(btn.getAttribute('aria-expanded') === 'true', true); });
    });
  }

  function score(item, q) {
    var label = norm(item.label), words = q.split(/\s+/).filter(Boolean), extra = '';
    words.forEach(function (w) { if (WORDS[w]) extra += ' ' + WORDS[w]; });
    var hay = label + ' ' + norm(item.group);
    var s = 0;
    words.forEach(function (w) {
      if (label.indexOf(w) === 0) s += 6; else if ((' ' + label).indexOf(' ' + w) >= 0) s += 4; else if (hay.indexOf(w) >= 0) s += 2;
    });
    if (extra) extra.trim().split(' ').forEach(function (w) { if (w && label.indexOf(w) >= 0) s += 3; });
    return s;
  }

  function buildFind(container, items, desktop) {
    if (!items.length) return null;
    var box = document.createElement('div'); box.className = 'tt-find' + (desktop ? '' : ' m');
    box.innerHTML = '<i class="bi bi-search"></i><input type="search" placeholder="Find a page…" aria-label="Find a page in the menu" autocomplete="off">' +
      (desktop ? '<kbd title="Press / anywhere">/</kbd>' : '');
    var input = box.querySelector('input'), list = document.createElement('div'); list.className = 'tt-find-list'; list.hidden = true;
    box.appendChild(list);
    container.insertBefore(box, container.firstChild);
    var hits = [], at = 0;
    function draw() {
      var q = norm(input.value).trim();
      if (!q) { list.hidden = true; list.innerHTML = ''; return; }
      var seen = {};
      hits = items.map(function (i) { return { i: i, s: score(i, q) }; }).filter(function (x) { var h = hrefOf(x.i.a); if (!x.s || seen[h]) return false; seen[h] = 1; return true; })
        .sort(function (a, b) { return b.s - a.s; }).slice(0, 8);
      at = 0;
      if (!hits.length) { list.innerHTML = '<div class="tt-find-none">Nothing called “' + esc(input.value) + '”. Try another word.</div>'; list.hidden = false; return; }
      list.innerHTML = hits.map(function (x, n) {
        var ic = x.i.a.querySelector('i.bi'); var icon = ic ? ic.className : 'bi bi-dot';
        return '<a href="' + esc(hrefOf(x.i.a)) + '" class="' + (n === 0 ? 'on' : '') + '"><i class="' + esc(icon) + '"></i><span>' + esc(x.i.label) + '</span>' +
          (x.i.group ? '<small>' + esc(x.i.group) + '</small>' : '') + '</a>';
      }).join('');
      list.hidden = false;
    }
    function move(d) { var as = list.querySelectorAll('a'); if (!as.length) return; at = (at + d + as.length) % as.length; [].forEach.call(as, function (a, n) { a.classList.toggle('on', n === at); }); as[at].scrollIntoView({ block: 'nearest' }); }
    input.addEventListener('input', draw);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') { var a = list.querySelectorAll('a')[at]; if (a) { e.preventDefault(); location.href = a.getAttribute('href'); } }
      else if (e.key === 'Escape') { input.value = ''; draw(); input.blur(); }
    });
    input.addEventListener('blur', function () { setTimeout(function () { if (document.activeElement !== input) list.hidden = true; }, 200); });
    input.addEventListener('focus', draw);
    if (desktop) document.addEventListener('keydown', function (e) {
      var t = e.target, typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
      if ((e.key === '/' && !typing) || ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K'))) {
        if (!box.offsetParent) return;   // menu hidden (phone layout)
        e.preventDefault(); input.focus(); input.select();
      }
    });
    return input;
  }

  function pinsOf() { var p = store(KEY_PINS); return Array.isArray(p) ? p : []; }

  function buildPins(nav, items) {
    var pinned = pinsOf(), box = document.createElement('div'); box.className = 'tt-pins';
    var find = nav.querySelector('.tt-find');
    nav.insertBefore(box, find ? find.nextSibling : nav.firstChild);
    function draw() {
      pinned = pinsOf();
      var rows = pinned.map(function (h) { return items.filter(function (i) { return hrefOf(i.a) === h; })[0]; }).filter(Boolean);
      box.innerHTML = rows.length ? '<div class="tt-pins-h"><i class="bi bi-star-fill"></i> Pinned</div>' : '';
      rows.forEach(function (i) {
        var c = i.a.cloneNode(true); c.classList.remove('nav-sub', 'tt-folded'); c.classList.add('tt-pinned');
        c.removeAttribute('id'); [].forEach.call(c.querySelectorAll('.tt-star'), function (x) { x.parentNode.removeChild(x); }); box.appendChild(c);
      });
      items.forEach(function (i) { var s = i.a.querySelector('.tt-star'); if (s) { var on = pinned.indexOf(hrefOf(i.a)) >= 0; s.classList.toggle('on', on); s.title = on ? 'Unpin' : 'Pin to the top'; s.innerHTML = on ? '★' : '☆'; } });
    }
    items.forEach(function (i) {
      if (!hrefOf(i.a) || i.a.getAttribute('data-tt-locked')) return;
      var s = document.createElement('span'); s.className = 'tt-star'; s.setAttribute('role', 'button'); s.setAttribute('tabindex', '0');
      s.setAttribute('aria-label', 'Pin ' + i.label);
      var toggle = function (e) {
        e.preventDefault(); e.stopPropagation();
        var p = pinsOf(), h = hrefOf(i.a), k = p.indexOf(h);
        if (k >= 0) p.splice(k, 1); else if (p.length < 8) p.push(h);
        store(KEY_PINS, p); draw(); mobileDraw();
      };
      s.addEventListener('click', toggle);
      s.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') toggle(e); });
      i.a.appendChild(s);
    });
    draw();
  }

  // ── the phone menu ──
  var mobile = document.querySelector('.mobile-nav'), mobilePins = null;
  function mobileDraw() {
    if (!mobilePins) return;
    var p = pinsOf(), mine = [].slice.call(mobile.querySelectorAll('a:not(.tt-pinned)'));
    mobilePins.innerHTML = '';
    p.forEach(function (h) {
      var a = mine.filter(function (x) { return hrefOf(x) === h; })[0]; if (!a) return;
      var c = a.cloneNode(true); c.classList.add('tt-pinned'); c.innerHTML = '★ ' + c.innerHTML; mobilePins.appendChild(c);
    });
    mobilePins.hidden = !mobilePins.children.length;
  }
  if (mobile) {
    var mlinks = [].slice.call(mobile.querySelectorAll('a'));
    // the same titles as the desktop menu, in its order; links only the phone menu has go under "More"
    var order = [], byHref = {};
    links.forEach(function (i) { var h = hrefOf(i.a).split('?')[0], g = i.group || 'Main'; if (!(h in byHref)) byHref[h] = g; if (order.indexOf(g) < 0) order.push(g); });
    if (links.length) {
      var buckets = {};
      mlinks.forEach(function (a) { var g = byHref[hrefOf(a).split('?')[0]] || byHref[hrefOf(a)] || 'More'; (buckets[g] = buckets[g] || []).push(a); });
      var frag = document.createDocumentFragment();
      order.concat(['More']).forEach(function (g) {
        if (!buckets[g] || !buckets[g].length) return;
        if (g !== 'Main') { var hd = document.createElement('span'); hd.className = 'm-label'; hd.textContent = g; frag.appendChild(hd); }
        buckets[g].forEach(function (a) { frag.appendChild(a); });
      });
      mobile.appendChild(frag);
    }
    mobilePins = document.createElement('span'); mobilePins.className = 'tt-mpins'; mobile.insertBefore(mobilePins, mobile.firstChild);
    mobileDraw();
    var head = document.querySelector('#mobileMenu .offcanvas-header');
    if (head) {
      var items = mlinks.map(function (a) { return { a: a, label: text(a), group: byHref[hrefOf(a).split('?')[0]] || '' }; });
      var wrap = document.createElement('div'); wrap.className = 'tt-mfind'; head.parentNode.insertBefore(wrap, head.nextSibling);
      buildFind(wrap, items, false);
    }
  }
})();
