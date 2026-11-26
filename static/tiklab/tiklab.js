/* TikLab downloads (core/tiklab.py): the big button offers the file for the visitor's own computer.
 * Phones get "send it to my computer" instead — TikLab runs on a PC. The start-up window types itself. */
(function () {
  'use strict';
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  function guess(cb) {
    var ua = navigator.userAgent || '', uad = navigator.userAgentData;
    var phone = /Android|iPhone|iPad|iPod|Mobile/i.test(ua) || (uad && uad.mobile) ||
      (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1);           /* iPad asking for the desktop site */
    if (phone) return cb('phone');
    var plat = ((uad && uad.platform) || navigator.platform || ua).toLowerCase();
    if (/win/.test(plat)) return cb('windows');
    if (/mac/.test(plat)) return cb('mac');
    if (/linux|x11|cros/.test(plat) || /Linux/.test(ua)) {
      if (/aarch64|arm/i.test(ua + ' ' + (navigator.platform || ''))) return cb('linux-arm64');
      if (uad && uad.getHighEntropyValues) {
        uad.getHighEntropyValues(['architecture']).then(function (v) { cb(/arm/i.test(v.architecture || '') ? 'linux-arm64' : 'linux-x64'); },
          function () { cb('linux-x64'); });
        return;
      }
      return cb('linux-x64');
    }
    cb('windows');
  }

  function share(url, text) {
    if (navigator.share) { navigator.share({ title: 'TikLab — MikroTik simulator', text: text, url: url }).catch(function () { }); return; }
    window.open('https://wa.me/?text=' + encodeURIComponent(text + ' ' + url), '_blank', 'noopener');
  }

  [].forEach.call(document.querySelectorAll('[data-tl-picker]'), function (box) {
    var files = {};
    try { files = JSON.parse(box.getAttribute('data-tl-files') || '{}'); } catch (e) { }
    var btn = box.querySelector('[data-tl-main]'), hint = box.querySelector('[data-tl-hint]'), alt = box.querySelector('[data-tl-alt]');
    var pageUrl = box.getAttribute('data-tl-page') || location.href;
    function show(key) {
      var f = files[key];
      if (!f || !btn) return;
      btn.href = f.href;
      btn.querySelector('[data-tl-label]').textContent = 'Download for ' + f.label;
      btn.querySelector('[data-tl-size]').textContent = f.size;
      var ic = btn.querySelector('i'); if (ic) ic.className = 'bi ' + f.icon;
      [].forEach.call(box.querySelectorAll('[data-tl-plat]'), function (a) { a.classList.toggle('is-yours', a.getAttribute('data-tl-plat') === key); });
    }
    guess(function (os) {
      box.setAttribute('data-tl-os', os);
      if (os === 'phone') {
        btn.removeAttribute('download');
        btn.href = '#';
        btn.querySelector('[data-tl-label]').textContent = 'Send it to my computer';
        btn.querySelector('[data-tl-size]').textContent = 'WhatsApp, email…';
        var ic = btn.querySelector('i'); if (ic) ic.className = 'bi bi-send';
        btn.addEventListener('click', function (e) {
          e.preventDefault();
          share(pageUrl, 'TikLab, the free MikroTik simulator — open this on your computer to download it:');
        });
        if (hint) hint.innerHTML = '<i class="bi bi-phone"></i> You are on a phone. TikLab runs on a Windows or Linux computer — send the link to yourself and download it there. You can still read the manual here.';
        return;
      }
      if (os === 'mac') {
        show('windows');
        if (hint) hint.innerHTML = '<i class="bi bi-apple"></i> There is no Mac version yet. Use a Windows or Linux PC — or Windows / Linux in a virtual machine on your Mac.';
        return;
      }
      show(os);
      if (hint && files[os]) hint.textContent = 'We picked ' + files[os].label + ' for this computer. ' + files[os].who + '.';
    });
    if (alt) alt.addEventListener('click', function (e) {
      var a = e.target.closest('[data-tl-plat]');
      if (a && box.getAttribute('data-tl-os') !== 'phone') show(a.getAttribute('data-tl-plat'));
    });
  });

  /* the start window types itself, line after line, then the console takes over */
  [].forEach.call(document.querySelectorAll('[data-tl-term]'), function (term) {
    var lines = [].slice.call(term.querySelectorAll('[data-l]'));
    var shot = term.parentNode.querySelector('[data-tl-shot]');
    if (reduce || !('IntersectionObserver' in window)) { lines.forEach(function (l) { l.classList.add('on'); }); return; }
    lines.forEach(function (l) { l.classList.remove('on'); });
    var started = false;
    function run() {
      var i = 0;
      lines.forEach(function (l) { l.classList.remove('on'); });
      if (shot) shot.classList.remove('on');
      (function next() {
        if (i < lines.length) { lines[i++].classList.add('on'); setTimeout(next, 170 + Math.random() * 160); return; }
        if (shot) setTimeout(function () { shot.classList.add('on'); }, 900);
        setTimeout(run, 9000);
      })();
    }
    new IntersectionObserver(function (es, io) {
      es.forEach(function (e) { if (e.isIntersecting && !started) { started = true; io.disconnect(); run(); } });
    }, { threshold: .3 }).observe(term);
  });

  /* copy a checksum */
  [].forEach.call(document.querySelectorAll('[data-tl-copy]'), function (b) {
    b.addEventListener('click', function () {
      var v = b.getAttribute('data-tl-copy');
      (navigator.clipboard ? navigator.clipboard.writeText(v) : Promise.reject()).then(function () {
        var t = b.innerHTML; b.innerHTML = '<i class="bi bi-check2"></i> Copied'; setTimeout(function () { b.innerHTML = t; }, 1400);
      }, function () { window.prompt('SHA-256', v); });
    });
  });
})();
