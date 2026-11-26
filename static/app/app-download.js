/* The TapTap phone app (core/app_download.py): the right way to get it on each device, and a phone that comes alive. */
(function () {
  'use strict';
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  var ua = navigator.userAgent || '';
  var os = /Android/i.test(ua) ? 'android'
    : (/iPhone|iPad|iPod/i.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) ? 'iphone' : 'desktop';

  function qr(el) {
    if (typeof qrcode !== 'function') return;
    try { var q = qrcode(0, 'H'); q.addData(el.getAttribute('data-ap-qr')); q.make(); el.innerHTML = q.createSvgTag({ cellSize: 4, margin: 0, scalable: true }); }
    catch (e) { el.remove(); }
  }

  [].forEach.call(document.querySelectorAll('[data-ap-get]'), function (box) {
    box.setAttribute('data-os', os);
    [].forEach.call(box.querySelectorAll('[data-ap-qr]'), qr);
    var page = box.getAttribute('data-page') || location.href;
    var share = box.querySelector('[data-ap-share]');
    if (share) share.addEventListener('click', function () {
      var text = 'The TapTap app — open this on your Android phone to install it:';
      if (navigator.share) { navigator.share({ title: 'TapTap app', text: text, url: page }).catch(function () { }); return; }
      window.open('https://wa.me/?text=' + encodeURIComponent(text + ' ' + page), '_blank', 'noopener');
    });
    /* Android: once the download starts, walk the person through the three install steps */
    var dl = box.querySelector('[data-ap-dl]'), steps = box.querySelector('[data-ap-steps]');
    if (dl && steps) dl.addEventListener('click', function () {
      var li = steps.querySelectorAll('li'), i = 0;
      steps.classList.add('go');
      (function next() {
        [].forEach.call(li, function (x, k) { x.classList.toggle('now', k === i); });
        if (++i < li.length) setTimeout(next, 4500);
      })();
      dl.querySelector('b').textContent = 'Downloading…';
      setTimeout(function () { dl.querySelector('b').textContent = 'Download again'; }, 6000);
    });
  });

  /* the phone on the page: sales arrive, the total counts up, a notification drops in */
  [].forEach.call(document.querySelectorAll('[data-ap-phone]'), function (ph) {
    var total = ph.querySelector('[data-ap-total]'), list = ph.querySelector('[data-ap-list]'), push = ph.querySelector('[data-ap-push]');
    var bars = ph.querySelectorAll('[data-ap-bars] i');
    [].forEach.call(bars, function (b) { b.style.setProperty('--h', 6 + Math.round(Math.random() * 15)); });
    if (reduce || !total || !list) return;
    var sales = [['Day pass', 25], ['3 Hours', 10], ['Week', 120], ['Month', 400], ['Day pass', 25], ['1 Hour', 5]];
    var who = ['Awa · Serrekunda shop', 'Lamin · cash', 'Online · Wave', 'Fatou · Brikama', 'Online · QMoney', 'Modou · agent'];
    var sum = parseInt(total.textContent.replace(/\D/g, ''), 10) || 0, last = bars[bars.length - 1];
    function count(to) {
      var from = sum, t0 = performance.now();
      (function step(t) {
        var k = Math.min(1, (t - t0) / 900), v = Math.round(from + (to - from) * k);
        total.textContent = v.toLocaleString('en');
        if (k < 1) requestAnimationFrame(step);
      })(t0);
      sum = to;
    }
    function sale() {
      if (document.hidden) return;
      var s = sales[Math.floor(Math.random() * sales.length)], w = who[Math.floor(Math.random() * who.length)];
      push.querySelector('[data-ap-push-amt]').textContent = 'D' + s[1];
      push.querySelector('[data-ap-push-who]').textContent = s[0] + ' · ' + w;
      push.classList.add('on');
      setTimeout(function () { push.classList.remove('on'); }, 2600);
      setTimeout(function () {
        var row = document.createElement('div');
        row.innerHTML = '<i class="bi ' + (/Online/.test(w) ? 'bi-phone' : 'bi-ticket-perforated') + '"></i><span><b></b><small></small></span><em></em>';
        row.querySelector('b').textContent = s[0]; row.querySelector('small').textContent = w; row.querySelector('em').textContent = 'D' + s[1];
        list.insertBefore(row, list.firstChild);
        while (list.children.length > 3) list.removeChild(list.lastChild);
        count(sum + s[1]);
        if (last) last.style.setProperty('--h', Math.min(24, (parseFloat(last.style.getPropertyValue('--h')) || 10) + 1));
      }, 700);
    }
    var started = false;
    function go() { if (started) return; started = true; setTimeout(sale, 1200); setInterval(sale, 5200); }
    if ('IntersectionObserver' in window) new IntersectionObserver(function (es, io) {
      es.forEach(function (e) { if (e.isIntersecting) { io.disconnect(); go(); } });
    }, { threshold: .3 }).observe(ph);
    else go();
  });
})();
