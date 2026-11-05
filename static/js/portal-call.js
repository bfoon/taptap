/* TapTap — a customer on the Wi-Fi page calls the business, inside the browser.

   Same call system as TapTap chat (core/chat_call.py, static/js/chat-call.js): this page is the caller, the staff
   member's call window is the callee. To match it exactly the offer carries three media slots — audio, camera,
   screen — and a pre-agreed data channel (id 0). The customer only sends audio; if staff turn on their camera or
   share their screen (to show how to type the code, say), it appears here.

   The voice goes phone ↔ staff directly, encrypted (DTLS-SRTP). The server only passes the offer and the answer.
   A dropped line reconnects with an ICE restart (same call, new path) for up to a minute.
*/
(function () {
  'use strict';
  var C = JSON.parse(document.getElementById('pcData').textContent);
  var $ = function (id) { return document.getElementById(id); };
  var root = $('pc'), panel = $('pcPanel'), sub = $('pcSub'), timer = $('pcTimer'), audio = $('pcAudio');
  var GATHER_MS = 3500, GIVE_UP = 60000, RESTART_AFTER = 2500;
  // Slow Wi-Fi / a staff phone on mobile data: wait longer for the relay path, and when the first connection is
  // slow or fails, try another path (ICE restart) before giving up — it used to hang up after 25 s.
  var GATHER_RELAY_MS = 10000, CONNECT_RESCUE = 12000, CONNECT_GIVE_UP = 55000;
  var S = { id: '', secret: '', ice: null, pc: null, dc: null, mic: null, state: 'idle', liveAt: 0, ended: false, t: {}, rgen: 0, rgenApplied: 0, rask: 0, restarting: 0, status: null };
  var ICON = {
    phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3h4l2 5-2.5 1.5a11 11 0 0 0 6 6L16 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 5a2 2 0 0 1 2-2z"/></svg>',
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
    micoff: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 3l18 18M9 9v2a3 3 0 0 0 5 2.2M15 9.3V6a3 3 0 0 0-5.7-1.3M5 11a7 7 0 0 0 11 5.7M19 11a7 7 0 0 1-.6 2.8M12 18v3"/></svg>',
    spk: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a8 8 0 0 1 0 11"/></svg>'
  };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function api(action, extra) {
    var b = extra || {}; b.action = action; if (S.id) { b.id = S.id; b.secret = S.secret; }
    return fetch(C.api, { method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: JSON.stringify(b), cache: 'no-store' })
      .then(function (r) { return r.json(); }).catch(function () { return { ok: false, net: true, message: 'No connection to TapTap. Check the Wi-Fi and try again.' }; });
  }
  // A step that must get through (the offer): tried again on a slow line.
  function apiSure(action, extra, tries) {
    tries = tries || 5;
    return new Promise(function (res) {
      var n = 0; (function again() {
        api(action, extra).then(function (r) {
          if (r && r.net && ++n < tries && !S.ended) return setTimeout(again, 1000 + 1500 * n);
          res(r);
        });
      })();
    });
  }
  function setRoot(cls) { root.className = 'pc ' + cls; }
  function clock(ms) { var s = Math.max(0, Math.floor(ms / 1000)); return Math.floor(s / 60) + ':' + ('0' + s % 60).slice(-2); }
  function store(k, v) { try { if (v === undefined) return localStorage.getItem('tp-call-' + k) || ''; localStorage.setItem('tp-call-' + k, v); } catch (e) { return ''; } }

  // ───────── tones (Web Audio: no files to load) ─────────
  var AC;
  function ac() { try { AC = AC || new (window.AudioContext || window.webkitAudioContext)(); if (AC.resume) AC.resume(); return AC; } catch (e) { return null; } }
  function tone(freqs, start, len, vol) {
    var a = ac(); if (!a) return;
    freqs.forEach(function (f) { var o = a.createOscillator(), g = a.createGain(), t = a.currentTime + (start || 0);
      o.frequency.value = f; o.connect(g); g.connect(a.destination); g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(vol || 0.06, t + 0.04); g.gain.exponentialRampToValueAtTime(0.0001, t + len); o.start(t); o.stop(t + len + 0.05); });
  }
  function ringback(on) {
    clearInterval(S.t.rb);
    if (!on) return;
    var play = function () { tone([440, 480], 0, 1.6, 0.05); };
    play(); S.t.rb = setInterval(play, 4000);
  }

  // ───────── screens ─────────
  function intro(err) {
    S.state = 'idle'; setRoot('idle'); timer.hidden = true;
    var st = S.status, online = st && st.online > 0;
    if (st && !st.open) { sub.textContent = 'Calls are not available right now.'; }
    else if (st && st.busy) { sub.innerHTML = '<span class="dot off"></span>Everyone is on another call'; }
    else if (st) { sub.innerHTML = online ? '<span class="dot"></span>Someone is here to help now' : '<span class="dot off"></span>We will ring our team for you'; }
    var can = !st || st.open;
    panel.innerHTML = '<div class="pc-form">' + (err ? '<p class="pc-err">' + esc(err) + '</p>' : '') +
      (can ? '<input class="pc-in" id="pcN" maxlength="60" autocomplete="name" placeholder="Your name" value="' + esc(C.name || store('name')) + '">' +
        '<input class="pc-in" id="pcP" maxlength="30" inputmode="tel" autocomplete="tel" placeholder="Your phone number (so we can call back)" value="' + esc(store('phone')) + '">' +
        '<p class="pc-small">Free — it uses this Wi-Fi, not your phone credit. We only use your number to call you back.</p>' +
        '<button class="pc-go" id="pcGo" type="button">' + ICON.phone + 'Call ' + esc(C.business) + '</button>' : '') +
      (C.phone ? '<a class="pc-alt" href="tel:' + esc(String(C.phone).replace(/[^\d+]/g, '')) + '">' + ICON.phone.replace('<svg', '<svg width="18" height="18"') + ' Normal phone call · ' + esc(C.phone) + '</a>' : '') + '</div>';
    var go = $('pcGo'); if (go) go.onclick = start;
  }
  function inCall() {
    var muted = S.mic && !S.mic.enabled;
    panel.innerHTML = '<p class="pc-banner" id="pcBanner"></p><div class="pc-bar">' +
      '<button class="pc-btn' + (muted ? ' off' : '') + '" data-a="mute" type="button"><span>' + (muted ? ICON.micoff : ICON.mic) + '</span>' + (muted ? 'Unmute' : 'Mute') + '</button>' +
      '<button class="pc-btn end" data-a="hangup" type="button"><span>' + ICON.phone + '</span>' + (S.state === 'live' ? 'Hang up' : 'Cancel') + '</button>' +
      '<button class="pc-btn' + (spk() ? ' on' : '') + '" data-a="speaker" type="button" aria-pressed="' + (spk() ? 'true' : 'false') + '"><span>' + ICON.spk + '</span>Speaker</button></div>';
  }
  function spk() { return !!(window.TapSpeaker && TapSpeaker.isOn()); }
  function banner(t) { var b = $('pcBanner'); if (b) b.textContent = t || ''; }
  function done(reason, msg) {
    S.ended = true; ringback(false); stopLevel(); clearAll();
    try { if (S.pc) S.pc.close(); } catch (e) {} S.pc = null;
    if (window.TapSpeaker) TapSpeaker.stop();
    if (S.mic) { S.mic.stop(); }
    setRoot('done'); timer.hidden = !S.liveAt;
    var talk = S.liveAt ? 'Call ended · ' + clock(Date.now() - S.liveAt) : '';
    var text = msg || {
      ended: 'Thanks for calling.', missed: 'Nobody could answer just now.' + (S.phone ? ' We have your number and will call you back.' : ''),
      declined: 'Our team cannot take the call right now.' + (S.phone ? ' We will call you back.' : ''),
      cancelled: 'Call cancelled.', lost: 'The line dropped.', failed: 'The call could not connect over this Wi-Fi.', gone: 'The call has ended.'
    }[reason] || 'The call has ended.';
    sub.textContent = talk || text;
    tone([480, 360], 0, 0.35, 0.05);
    panel.innerHTML = '<div class="pc-form pc-done"><p>' + esc(talk ? text : '') + '</p>' +
      '<button class="pc-go" id="pcAgain" type="button">' + ICON.phone + 'Call again</button>' +
      (C.phone ? '<a class="pc-alt" href="tel:' + esc(String(C.phone).replace(/[^\d+]/g, '')) + '">Normal phone call · ' + esc(C.phone) + '</a>' : '') + '</div>';
    $('pcAgain').onclick = function () { location.reload(); };
    $('pcVideo').hidden = true; root.classList.remove('has-video');
    try { if (window.parent !== window) window.parent.postMessage({ tpCall: 'ended' }, '*'); } catch (e) {}
  }
  function clearAll() { Object.keys(S.t).forEach(function (k) { clearTimeout(S.t[k]); clearInterval(S.t[k]); }); }

  // ───────── start ─────────
  function micError(e) {
    var n = e && e.name;
    if (!window.isSecureContext || !navigator.mediaDevices) return 'This browser cannot use the microphone here. Open this page in Chrome or Safari, or use the normal phone call.';
    return n === 'NotAllowedError' ? 'Please allow the microphone — tap the 🔒 or aA next to the address, allow Microphone, then try again.'
      : n === 'NotFoundError' ? 'No microphone was found on this device.' : 'Could not start the microphone (' + (n || 'error') + ').';
  }
  function start() {
    var name = ($('pcN') || {}).value || '', phone = ($('pcP') || {}).value || '';
    store('name', name.trim()); store('phone', phone.trim()); S.name = name.trim(); S.phone = phone.trim();
    ac();                                         // unlock sound on this tap (iOS)
    var go = $('pcGo'); if (go) { go.disabled = true; go.textContent = 'Starting your microphone…'; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return intro(micError());
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }, video: false })
      .then(function (stream) {
        S.mic = stream.getAudioTracks()[0];
        return api('start', { name: name, phone: phone });
      })
      .then(function (d) {
        if (!d.ok) throw new Error(d.message || 'Could not start the call.');
        S.id = d.id; S.secret = d.secret; S.ice = d.ice;
        S.state = 'ringing'; setRoot('ringing'); sub.textContent = 'Ringing…'; inCall(); ringback(true);
        return offer();
      })
      .then(function () { poll(800); })
      .catch(function (e) { if (S.mic) S.mic.stop(); S.mic = null; intro(e && e.name ? micError(e) : (e && e.message) || 'Could not start the call.'); });
  }
  // With a TURN relay, wait for its path (works through any NAT; slow to arrive on a weak line) — not longer.
  function gathered(pc, ms) {
    return new Promise(function (res) {
      if (pc.iceGatheringState === 'complete') return res();
      var ice = S.ice || {}, turn = (ice.iceServers || []).some(function (x) { return String(x.urls || '').indexOf('turn') >= 0; });
      var t0 = Date.now(), relay = false, fin = false, g = 0;
      var finish = function () { if (fin) return; fin = true; clearTimeout(t); clearTimeout(g); pc.removeEventListener('icecandidate', cand); res(); };
      var cand = function (e) {
        if (e.candidate && / typ relay/.test(e.candidate.candidate || '') && !relay) { relay = true; g = setTimeout(finish, Math.max(0, 1200 - (Date.now() - t0))); }
      };
      var t = setTimeout(finish, turn ? Math.max(ms, GATHER_RELAY_MS) : ms);
      pc.addEventListener('icecandidate', cand);
      pc.addEventListener('icegatheringstatechange', function () { if (pc.iceGatheringState === 'complete') finish(); });
    });
  }
  function watchConnect() {
    if (!S.answeredAt) S.answeredAt = Date.now();
    clearInterval(S.t.cwatch);
    S.t.cwatch = setInterval(function () {
      if (S.ended || S.liveAt) return clearInterval(S.t.cwatch);
      var ms = Date.now() - S.answeredAt;
      if (ms > CONNECT_GIVE_UP) { clearInterval(S.t.cwatch); return hangup('failed'); }
      if (ms > CONNECT_RESCUE) { sub.textContent = 'Weak connection — still connecting…'; restart(); }
    }, 4000);
  }
  function offer() {
    var ice = S.ice || {};
    var pc = new RTCPeerConnection({ iceServers: ice.iceServers || [], iceTransportPolicy: ice.iceTransportPolicy || 'all', bundlePolicy: 'max-bundle' });
    S.pc = pc;
    // Same layout as a TapTap chat call: audio, camera, screen + data channel 0.
    pc.addTransceiver(S.mic, { direction: 'sendrecv' });
    pc.addTransceiver('video', { direction: 'sendrecv' });
    pc.addTransceiver('video', { direction: 'sendrecv' });
    S.dc = pc.createDataChannel('ctl', { negotiated: true, id: 0, ordered: true });
    S.dc.onopen = function () { send({ t: 'hello', name: (S.name || 'Wi-Fi customer') + (S.phone ? ' · ' + S.phone : ''), initials: (S.name || '☎').charAt(0).toUpperCase() }); sendState(); };
    S.dc.onmessage = function (e) { try { onCtl(JSON.parse(e.data)); } catch (x) {} };
    pc.ontrack = function (e) {
      var s = new MediaStream([e.track]);
      if (e.track.kind === 'audio') {
        audio.srcObject = s; audio.play().catch(function () { banner('Tap “Speaker” to hear the call'); }); level(s);
        if (window.TapSpeaker) TapSpeaker.attach(audio, s);          // keeps the speaker choice if it was already on
      }
      else { var slot = pc.getTransceivers().indexOf(e.transceiver); if (slot === 1) $('pcRcam').srcObject = s; else if (slot === 2) $('pcRscreen').srcObject = s; }
    };
    pc.onconnectionstatechange = function () {
      var st = pc.connectionState;
      if (st === 'connected') onConnected();
      else if (st === 'disconnected' && S.liveAt) { dropped(); clearTimeout(S.t.rs); S.t.rs = setTimeout(restart, RESTART_AFTER); }
      else if (st === 'failed') {
        if (!S.liveAt) {                          // not connected yet: try another path before giving up
          if (S.answeredAt && Date.now() - S.answeredAt < CONNECT_GIVE_UP) { sub.textContent = 'Weak connection — trying another way…'; S.restarting = 0; restart(); poll(500); return; }
          return hangup('failed');
        }
        dropped(); restart();
      }
    };
    return pc.createOffer().then(function (o) { return pc.setLocalDescription(o); })
      .then(function () { return gathered(pc, GATHER_MS); })
      .then(function () { return apiSure('offer', { desc: { type: 'offer', sdp: pc.localDescription.sdp } }); })
      .then(function (r) { if (!r.ok) throw new Error(r.message); });
  }
  function send(m) { try { if (S.dc && S.dc.readyState === 'open') S.dc.send(JSON.stringify(m)); } catch (e) {} }
  function sendState() { send({ t: 'state', mic: !!(S.mic && S.mic.enabled), cam: false, facing: '', screen: null }); }
  function onCtl(m) {
    if (m.t === 'state') {
      var cam = !!m.cam, scr = !!m.screen;
      $('pcRcam').hidden = !cam || scr; $('pcRscreen').hidden = !scr;
      $('pcVideo').hidden = !(cam || scr); root.classList.toggle('has-video', cam || scr);
    }
    // the customer is on a phone browser, not in TapTap: there is no TapTap tab to share or control
    else if (m.t === 'rc-ask') send({ t: 'rc-no', why: 'The caller is on the Wi-Fi page and cannot share a screen.' });
  }

  // ───────── poll: answer, heartbeat, ended by staff, reconnect ─────────
  function poll(ms) {
    clearTimeout(S.t.poll);
    S.t.poll = setTimeout(function () {
      api('poll').then(function (d) {
        if (S.ended) return;
        var c = d.call;
        if (!c) return poll(2000);                             // a blip: try again
        if (c.state === 'ended') return done(c.reason || 'ended');
        if (c.peer && c.peer.name && !S.peerShown) { S.peerShown = true; $('pcName').textContent = c.peer.name; if (c.peer.initials) $('pcAv').textContent = c.peer.initials; }   // who answered
        if (c.state === 'ringing') sub.textContent = 'Ringing ' + C.business + '…';
        if (c.state === 'connecting' && !S.liveAt) { ringback(false); sub.textContent = (c.peer ? c.peer.name : 'Someone') + ' is answering…'; }
        if (c.answer && S.pc && !S.pc.remoteDescription) {
          S.pc.setRemoteDescription(c.answer).then(watchConnect).catch(function () { hangup('failed'); });
        }
        if (S.pc && c.reanswer && c.reanswer.gen === S.rgen && S.rgenApplied !== S.rgen && S.pc.signalingState === 'have-local-offer') {
          S.rgenApplied = S.rgen; S.pc.setRemoteDescription(c.reanswer.desc).catch(function () { S.rgenApplied = 0; S.restarting = 0; });
        }
        if (c.rask && c.rask > S.rask) {
          S.rask = c.rask;
          if (S.liveAt) { S.restarting = 0; dropped(); restart(); }
          else if (S.answeredAt) { S.restarting = 0; restart(); }      // staff could not connect yet: another path
        }
        poll(!S.liveAt || S.reconnecting ? 1000 : 3000);
      });
    }, ms);
  }
  function onConnected() {
    clearTimeout(S.t.lost); S.t.lost = 0; clearInterval(S.t.cwatch); S.restarting = 0;
    if (S.reconnecting) { S.reconnecting = false; banner('Reconnected'); setTimeout(function () { banner(''); }, 2000); }
    if (!S.liveAt) {
      S.liveAt = Date.now(); S.state = 'live'; setRoot('live'); ringback(false); tone([660, 880], 0, 0.18, 0.05);
      sub.textContent = 'Connected'; timer.hidden = false; inCall(); sendState();
      S.t.clock = setInterval(function () { timer.textContent = clock(Date.now() - S.liveAt); }, 1000);
      if (navigator.wakeLock) navigator.wakeLock.request('screen').then(function (l) { S.lock = l; }).catch(function () {});
    }
  }
  function dropped() {
    if (S.ended) return;
    if (!S.reconnecting) { S.reconnecting = true; banner('Connection dropped — reconnecting…'); }
    if (!S.t.lost) S.t.lost = setTimeout(function () { hangup('lost'); }, GIVE_UP);
    poll(800);
  }
  function restart() {
    var pc = S.pc; if (!pc || S.ended || pc.signalingState !== 'stable') return;
    if (S.restarting && Date.now() - S.restarting < 8000) return;
    S.restarting = Date.now(); var gen = ++S.rgen;
    pc.createOffer({ iceRestart: true }).then(function (o) { return pc.setLocalDescription(o); })
      .then(function () { return gathered(pc, GATHER_MS); })
      .then(function () { return api('reoffer', { gen: gen, desc: { type: 'offer', sdp: pc.localDescription.sdp } }); })
      .catch(function () { S.restarting = 0; });
  }
  window.addEventListener('online', function () { if (S.liveAt && !S.ended) { S.restarting = 0; restart(); } });

  // ───────── voice glow ─────────
  function level(stream) {
    var a = ac(); if (!a) return;
    try {
      var an = a.createAnalyser(); an.fftSize = 256; a.createMediaStreamSource(stream).connect(an);
      var buf = new Uint8Array(an.frequencyBinCount);
      (function tick() { if (S.ended) return; an.getByteFrequencyData(buf); var s = 0; for (var i = 0; i < buf.length; i++) s += buf[i];
        root.style.setProperty('--lvl', Math.min(1, s / buf.length / 60).toFixed(2)); S.t.raf = requestAnimationFrame(tick); })();
    } catch (e) {}
  }
  function stopLevel() { cancelAnimationFrame(S.t.raf); root.style.setProperty('--lvl', 0); }

  // ───────── buttons ─────────
  function hangup(reason) {
    if (S.ended) return;
    api('end', { reason: reason === 'lost' || reason === 'failed' ? reason : '' });
    done(reason === 'lost' || reason === 'failed' ? reason : (S.liveAt ? 'ended' : 'cancelled'));
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-a]'); if (!b) return;
    var a = b.getAttribute('data-a');
    if (a === 'hangup') hangup();
    else if (a === 'mute' && S.mic) { S.mic.enabled = !S.mic.enabled; sendState(); inCall(); }
    else if (a === 'speaker') {
      ac(); audio.play().catch(function () {});
      if (!window.TapSpeaker) return;
      TapSpeaker.toggle().then(function (r) {
        inCall(); banner(r.note);
        clearTimeout(S.t.spk); S.t.spk = setTimeout(function () { banner(''); }, r.mode === 'ios' || r.mode === 'none' ? 6000 : 2000);
      });
    }
    else if (a === 'close') { if (S.id && !S.ended) hangup(); try { window.parent.postMessage({ tpCall: 'close' }, '*'); } catch (x) {} }
  });
  // Closing the page ends the call for staff straight away (instead of waiting for the heartbeat).
  window.addEventListener('pagehide', function () {
    if (!S.id || S.ended) return;
    try { navigator.sendBeacon(C.api, new Blob([JSON.stringify({ action: 'end', id: S.id, secret: S.secret })], { type: 'text/plain' })); } catch (e) {}
  });

  // ───────── boot ─────────
  if (C.logo) { /* logo already in the page */ }
  intro();
  api('status').then(function (d) { if (d && d.ok) { S.status = d; if (S.state === 'idle') intro(); } else { sub.textContent = 'Tap Call — we will ring our team.'; } });
})();
