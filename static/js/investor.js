/* TapTap investors: payback meter, charts (Chart.js 4), live refresh, Fit chat, opportunity calculator.
   Used by the investor portal and by the owner's deal page. */
(function (g) {
  'use strict';
  var CAP = '#2f6fdb', RET = '#12a27a', INK = '#14213d', LIVE = '#f2b134', MUTED = '#8a97ad';
  var charts = {};

  function cur() { return g.TTI_CUR || 'D'; }
  function money(v, dp) {
    v = +v || 0; var a = Math.abs(v);
    return (v < 0 ? '−' : '') + cur() + a.toLocaleString(undefined, { minimumFractionDigits: dp == null ? 2 : dp, maximumFractionDigits: dp == null ? 2 : dp });
  }
  function short(v) {
    v = +v || 0; var a = Math.abs(v);
    return (v < 0 ? '−' : '') + cur() + (a >= 1e6 ? (a / 1e6).toFixed(1) + 'M' : a >= 1e4 ? (a / 1e3).toFixed(1) + 'k' : Math.round(a).toLocaleString());
  }
  function date(iso) { if (!iso) return '—'; if (iso === 'repaid') return 'Already repaid'; var d = new Date(iso + (iso.length === 10 ? 'T00:00:00' : '')); return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function set(id, text) { var el = document.getElementById(id); if (el) el.textContent = text; }
  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || ''; }

  /* The payback meter: the track is the capital (marker = 100 %). Blue fills as capital returns,
     green follows for the profit; past payback the track grows so the green keeps showing. */
  function meter(root, principal, repaid, returned) {
    if (!root) return;
    var total = repaid + returned, scale = Math.max(principal, total) * 1.04 || 1;
    var cap = root.querySelector('.meter-cap'), ret = root.querySelector('.meter-ret'), mark = root.querySelector('.meter-mark');
    cap.style.width = (repaid / scale * 100) + '%';
    ret.style.left = (repaid / scale * 100) + '%';
    ret.style.width = (returned / scale * 100) + '%';
    if (repaid <= 0) ret.style.borderRadius = '13px';
    mark.style.left = 'calc(' + (principal / scale * 100) + '% - 1px)';
    root.setAttribute('aria-label', money(repaid) + ' of ' + money(principal) + ' capital back, ' + money(returned) + ' profit');
  }

  function base(type, data, opts) {
    return {
      type: type, data: data,
      options: Object.assign({
        responsive: true, maintainAspectRatio: false, animation: { duration: 500 },
        interaction: { mode: 'index', intersect: false },
        plugins: { legend: { labels: { boxWidth: 12, usePointStyle: true } },
                   tooltip: { callbacks: { label: function (c) { return ' ' + c.dataset.label + ': ' + money(c.parsed.y); } } } },
        scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8, color: MUTED } },
                  y: { grid: { color: '#eef2f0' }, ticks: { color: MUTED, callback: function (v) { return short(v); } } } }
      }, opts || {})
    };
  }
  function draw(id, cfg) {
    var el = document.getElementById(id);
    if (!el || !g.Chart) return null;
    if (charts[id]) charts[id].destroy();
    charts[id] = new g.Chart(el, cfg);
    return charts[id];
  }
  function dayLabels(list) { return list.map(function (d) { return new Date(d + 'T00:00:00').toLocaleDateString(undefined, { day: 'numeric', month: 'short' }); }); }

  function earnings(id, s) {
    var cfg = base('bar', {
      labels: dayLabels(s.labels),
      datasets: [
        { type: 'line', label: '7-day average', data: s.avg7, borderColor: INK, borderWidth: 2, pointRadius: 0, tension: .35, order: 0 },
        { label: 'Capital back', data: s.capital, backgroundColor: CAP, stack: 'e', borderRadius: 3, order: 1 },
        { label: 'Your profit', data: s['return'], backgroundColor: RET, stack: 'e', borderRadius: 3, order: 1 }
      ]
    });
    cfg.options.scales.x.stacked = true; cfg.options.scales.y.stacked = true;
    return draw(id, cfg);
  }

  function projection(id, actual, p, scenario) {
    if (!p) return null;
    var start = actual.length ? actual[actual.length - 1].earned : 0;
    var labels = actual.map(function (a) { return a.day; }).concat(p.labels);
    var pad = actual.map(function () { return null; });
    var ds = [{ label: 'Earned so far', data: actual.map(function (a) { return a.earned; }).concat(p.labels.map(function () { return null; })),
                borderColor: INK, backgroundColor: 'rgba(20,33,61,.08)', fill: true, borderWidth: 2.5, pointRadius: 0, tension: .2 }];
    var names = { cautious: 'Cautious', expected: 'Expected', strong: 'Strong' };
    ['cautious', 'expected', 'strong'].forEach(function (k) {
      var sc = p.scenarios[k]; if (!sc) return;
      var on = !scenario || scenario === k;
      ds.push({ label: names[k], data: pad.concat(sc.weekly.map(function (v) { return v; })), hidden: !on && scenario,
                borderColor: k === 'expected' ? RET : (k === 'strong' ? CAP : MUTED), borderDash: k === 'expected' ? [] : [6, 5],
                borderWidth: k === 'expected' ? 2.5 : 1.5, pointRadius: 0, tension: .25 });
    });
    ds.push({ label: 'Your capital', data: labels.map(function () { return p.principal; }), borderColor: CAP, borderWidth: 1, borderDash: [2, 4], pointRadius: 0 });
    if (p.total_due) ds.push({ label: 'Full agreed return', data: labels.map(function () { return p.total_due; }), borderColor: RET, borderWidth: 1, borderDash: [2, 4], pointRadius: 0 });
    var cfg = base('line', { labels: dayLabels(labels), datasets: ds });
    return draw(id, cfg);
  }

  function basis(id, b) {
    if (!b) return null;
    var cfg = base('bar', { labels: dayLabels(b.labels), datasets: [
      { type: 'line', label: 'Profit', data: b.profit, borderColor: INK, borderWidth: 2, pointRadius: 0, tension: .3 },
      { label: 'Revenue', data: b.revenue, backgroundColor: 'rgba(18,162,122,.35)', borderRadius: 3 },
      { label: 'Costs', data: b.costs.map(function (v) { return -v; }), backgroundColor: 'rgba(194,65,12,.35)', borderRadius: 3 }
    ] });
    cfg.options.scales.x.stacked = true;
    return draw(id, cfg);
  }

  function scenarioCards(box, p) {
    if (!box || !p) return;
    var names = { cautious: 'Cautious', expected: 'Expected', strong: 'Strong' };
    box.innerHTML = ['cautious', 'expected', 'strong'].map(function (k) {
      var s = p.scenarios[k];
      return '<div class="scen' + (k === 'expected' ? ' mid' : '') + '"><small>' + names[k] + '</small><b>' + money(s.monthly, 0) + '<span class="small muted"> a month</span></b>' +
        '<div class="small">Capital back: ' + esc(date(s.payback_date)) + '</div>' +
        (s.complete_date ? '<div class="small">Fully paid: ' + esc(date(s.complete_date)) + '</div>' : '') + '</div>';
    }).join('');
  }

  /* ── dashboard live refresh ── */
  function dashboard(url) {
    function paint(d) {
      var t = d.totals;
      set('ttiEarned', money(t.earned)); set('ttiToday', money(t.today)); set('ttiInvested', money(t.invested, 0));
      set('ttiRepaid', money(t.repaid)); set('ttiReturned', money(t.returned)); set('ttiPaid', money(t.paid_out));
      set('ttiBalance', money(t.balance)); set('ttiMtd', money(t.mtd)); set('ttiPct', t.payback_progress + '% of your capital is back');
      set('ttiUpdated', 'Updated ' + new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }));
      meter(document.getElementById('ttiMeter'), t.invested, t.repaid, t.returned);
      var list = document.getElementById('ttiDeals');
      if (list) list.innerHTML = d.deals.length ? d.deals.map(function (c) {
        var tot = Math.max(c.principal, c.repaid + c.returned) || 1;
        return '<a class="deal" href="i/' + esc(c.reference) + '/"><div class="deal-top"><b>' + esc(c.title) + '</b><span class="state ' + esc(c.status) + '">' + esc(c.status_label.split(' — ')[0]) + '</span></div>' +
          '<div class="mini"><span class="c" style="width:' + (c.repaid / tot * 100) + '%"></span><span class="r" style="width:' + (c.returned / tot * 100) + '%"></span></div>' +
          '<div class="small muted">' + money(c.principal, 0) + ' invested, ' + money(c.earned) + ' earned' + (c.status === 'active' ? ', ' + money(c.today) + ' today' : '') + '</div></a>';
      }).join('') : '<p class="empty">No investments yet. Browse the opportunities below to make your first offer.</p>';
      var ob = document.getElementById('ttiOpps');
      if (ob) ob.innerHTML = d.opportunities.length ? d.opportunities.map(function (o) {
        return '<a class="opp" href="o/' + esc(o.reference) + '/"><div class="deal-top"><b>' + esc(o.title) + '</b><span class="state' + (o.status === 'open' ? ' active' : '') + '">' +
          (o.status === 'open' ? 'Open' : 'Fully funded') + '</span></div><div class="small muted">' + esc(o.summary) + '</div>' +
          '<div class="fund"><span style="width:' + o.pct + '%"></span></div><div class="small">' + money(o.raised, 0) + ' of ' + money(o.target, 0) + ' raised, ' +
          esc(o.terms.toLowerCase()) + (o.min_ticket ? ', from ' + money(o.min_ticket, 0) : '') + '</div></a>';
      }).join('') : '<p class="empty">No open opportunities right now. New ones appear here and in your daily email.</p>';
      if (!charts.ttiEarnings) earnings('ttiEarnings', d.series);
      else { var ch = charts.ttiEarnings; ch.data.datasets[0].data = d.series.avg7; ch.data.datasets[1].data = d.series.capital; ch.data.datasets[2].data = d.series['return']; ch.update('none'); }
    }
    function load() {
      fetch(url, { headers: { 'x-requested-with': 'fetch' }, credentials: 'same-origin' })
        .then(function (r) { if (r.status === 401) { location.reload(); throw 0; } return r.json(); }).then(paint).catch(function () {});
    }
    load(); setInterval(function () { if (!document.hidden) load(); }, 20000);
  }

  /* ── one deal (portal or owner page) ── */
  function deal(url) {
    var scenario = null, data = null;
    function paint(d) {
      data = d; var c = d.deal;
      set('ttiEarned', money(c.earned)); set('ttiToday', money(c.today)); set('ttiRepaid', money(c.repaid)); set('ttiReturned', money(c.returned));
      set('ttiPaid', money(c.paid_out)); set('ttiBalance', money(c.balance)); set('ttiPct', c.payback_progress + '% of your capital is back');
      meter(document.getElementById('ttiMeter'), c.principal, c.repaid, c.returned);
      if (!charts.ttiEarnings) earnings('ttiEarnings', d.series);
      if (d.projection && !charts.ttiProjection) { projection('ttiProjection', d.cumulative, d.projection, scenario); scenarioCards(document.getElementById('ttiScen'), d.projection); set('ttiConf', d.projection.confidence); }
      if (d.basis && !charts.ttiBasis) basis('ttiBasis', d.basis);
    }
    function load() {
      fetch(url, { headers: { 'x-requested-with': 'fetch' }, credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(paint).catch(function () {});
    }
    document.querySelectorAll('[data-scenario]').forEach(function (b) {
      b.addEventListener('click', function () {
        scenario = b.dataset.scenario || null;
        document.querySelectorAll('[data-scenario]').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        if (data && data.projection) projection('ttiProjection', data.cumulative, data.projection, scenario);
      });
    });
    load(); setInterval(function () { if (!document.hidden) load(); }, 30000);
  }

  /* ── opportunity calculator ── */
  function calculator(url) {
    var input = document.getElementById('ttiAmount'), out = document.getElementById('ttiCalc'), timer;
    if (!input) return;
    function run() {
      var v = parseFloat(input.value); if (!(v > 0)) { out.innerHTML = ''; return; }
      fetch(url + '?amount=' + encodeURIComponent(v), { headers: { 'x-requested-with': 'fetch' }, credentials: 'same-origin' })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (!d.ok) { out.innerHTML = '<p class="muted">' + esc(d.error) + '</p>'; return; }
          set('ttiShare', d.share_pct.toFixed(3).replace(/\.?0+$/, '') + '% of the profit');
          var sp = document.getElementById('ttiSplit');
          if (sp) sp.innerHTML = '<div class="c" style="width:' + d.split[0] + '%">' + d.split[0] + '% capital back</div><div class="r" style="width:' + d.split[1] + '%">' + d.split[1] + '% your profit</div>';
          set('ttiDue', d.total_due ? 'You are owed ' + money(d.total_due) + ' in total' : 'No cap — you keep earning after your capital is back');
          set('ttiConf', d.confidence);
          var names = { cautious: 'Cautious', expected: 'Expected', strong: 'Strong' };
          out.innerHTML = ['cautious', 'expected', 'strong'].map(function (k) {
            var s = d.scenarios[k];
            return '<div class="scen' + (k === 'expected' ? ' mid' : '') + '"><small>' + names[k] + '</small><b>' + money(s.monthly, 0) + '<span class="small muted"> a month</span></b>' +
              '<div class="small">Capital back in ' + (s.payback_months != null ? s.payback_months + ' months' : 'over 3 years') + '</div>' +
              '<div class="small">First year: ' + money(s.earned_12m, 0) + ' (' + s.roi_12m_pct + '%)</div></div>';
          }).join('');
        }).catch(function () {});
    }
    input.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(run, 350); });
    run();
  }

  /* ── Fit ── */
  function fit(url) {
    var log = document.getElementById('ttiFitLog'), form = document.getElementById('ttiFitForm');
    if (!form) return;
    function add(cls, text) { var d = document.createElement('div'); d.className = 'msg ' + cls; d.textContent = text; log.appendChild(d); log.scrollTop = log.scrollHeight; return d; }
    function send(text) {
      if (!text) return;
      add('me', text);
      var wait = add('fit', 'Looking at your numbers…');
      fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf(), 'x-requested-with': 'fetch' },
                   body: JSON.stringify({ text: text }) })
        .then(function (r) { return r.json(); })
        .then(function (d) { wait.textContent = d.ok ? d.reply : (d.error || 'Fit could not answer that.'); })
        .catch(function () { wait.textContent = 'Fit is not reachable right now.'; });
    }
    form.addEventListener('submit', function (e) { e.preventDefault(); var i = form.querySelector('input'); send(i.value.trim()); i.value = ''; });
    document.querySelectorAll('[data-ask]').forEach(function (b) { b.addEventListener('click', function () { send(b.dataset.ask); }); });
  }

  g.TTI = { money: money, meter: meter, dashboard: dashboard, deal: deal, calculator: calculator, fit: fit, earnings: earnings, projection: projection, basis: basis };
})(window);
