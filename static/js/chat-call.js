/* TapTap call window — voice, video and secure screen sharing.

   Media goes browser-to-browser (WebRTC, always encrypted with DTLS-SRTP). The server only passes one offer
   and one answer (core/chat_call.py). Everything after that — mute state, screen-share state, pointer,
   reactions, "may I see your screen?" — travels over an encrypted data channel inside the call.

   No renegotiation is ever needed: the caller sets up three slots at the start (audio, camera, screen).
   Turning the camera on or sharing the screen just fills a slot (replaceTrack).

   Security on top of the encryption:
     • Safety emoji — 4 emoji made from BOTH sides' DTLS certificate fingerprints. If they match on both
       screens, nobody (not even the TapTap server) sits in the middle of the call.
     • Screen share grade — a TapTap tab is safest (the picker cannot offer the whole screen); a window is OK;
       the whole screen warns that notifications from other apps will show.
     • Privacy shield — while you share, every TapTap tab blurs passwords, keys and tokens, and hides chat pop-ups.
     • Share lease — sharing stops by itself after 15 minutes unless you keep it going.
     • Consent — the other person can only ask to see your screen; you choose what to share.
     • Watermark — the viewer's screen shows their own name and the time over your shared screen.
     • Snapshots are announced to the sharer and land in the chat (deleted with all photos after a week).
*/
(function () {
  'use strict';
  var R = document.getElementById('ttCall'); if (!R) return;
  var D = R.dataset, CSRF = D.csrf, EMBED = D.embedded === '1';
  var LEASE_MS = 15 * 60 * 1000, WARN_MS = 60 * 1000;
  var bc = window.BroadcastChannel ? new BroadcastChannel('tt-call') : null;
  var S = { cid: '', role: D.join ? 'callee' : 'caller', thread: +D.thread || 0, video: D.video === '1', pc: null, dc: null,
            T: {}, mic: null, micProbe: null, cam: null, share: null, peer: { name: '', initials: '?' }, remote: { mic: true, cam: false, screen: null },
            state: 'init', liveAt: 0, ice: null, verified: { me: false, them: false }, relay: false, timers: {}, lastAsk: 0, quality: 4, lowered: false, ended: false };

  // ───────── helpers ─────────
  function $(s, el) { return (el || document).querySelector(s); }
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function api(url, body) {
    var o = body === undefined ? { credentials: 'same-origin', headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' } }
      : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF, 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }, body: JSON.stringify(body) };
    return fetch(url, o).then(function (r) { return r.json(); }).catch(function () { return { ok: false, message: 'No connection to TapTap.' }; });
  }
  function act(action, extra) { var b = extra || {}; b.action = action; b.id = S.cid; return api('/chat/call/act/', b); }
  function clock(ms) { var s = Math.max(0, Math.floor(ms / 1000)); return Math.floor(s / 60) + ':' + ('0' + s % 60).slice(-2); }
  function send(msg) { try { if (S.dc && S.dc.readyState === 'open') S.dc.send(JSON.stringify(msg)); } catch (e) {} }
  function bcast(msg) { try { bc && bc.postMessage(msg); } catch (e) {} }
  function first(n) { return String(n || '').split(' ')[0]; }

  var ICON = {
    mic: '<svg viewBox="0 0 24 24"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v5"/></svg>',
    micoff: '<svg viewBox="0 0 24 24"><path d="M2 2l20 20M9 9v1a3 3 0 0 0 5.1 2.1M15 9.3V5a3 3 0 0 0-5.7-1.3M19 10a7 7 0 0 1-1.2 3.9M5 10a7 7 0 0 0 11 5.7M12 17v5"/></svg>',
    cam: '<svg viewBox="0 0 24 24"><rect x="2" y="6" width="14" height="12" rx="2"/><path d="M16 10l6-3v10l-6-3z"/></svg>',
    camoff: '<svg viewBox="0 0 24 24"><path d="M2 2l20 20M16 16H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h2M10 6h4a2 2 0 0 1 2 2v3l6-3v9"/></svg>',
    screen: '<svg viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4M9 10l3-3 3 3M12 7v6"/></svg>',
    stop: '<svg viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M9 7l6 6M15 7l-6 6M8 21h8"/></svg>',
    end: '<svg viewBox="0 0 24 24"><path d="M3 15.5c5-4.7 13-4.7 18 0l-2.4 2.4a1 1 0 0 1-1.3.1l-2.5-1.8a1 1 0 0 1-.4-.9l.2-1.9a11 11 0 0 0-5.2 0l.2 1.9a1 1 0 0 1-.4.9L6.7 18a1 1 0 0 1-1.3-.1z" fill="currentColor" stroke="none"/></svg>',
    eye: '<svg viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
    snap: '<svg viewBox="0 0 24 24"><path d="M4 7h3l2-3h6l2 3h3a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="4"/></svg>',
    pause: '<svg viewBox="0 0 24 24"><path d="M9 5v14M15 5v14"/></svg>',
    play: '<svg viewBox="0 0 24 24"><path d="M7 4l13 8-13 8z"/></svg>',
    lock: '<svg viewBox="0 0 24 24"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
    smile: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 14s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01"/></svg>',
    full: '<svg viewBox="0 0 24 24"><path d="M3 9V3h6M21 9V3h-6M3 15v6h6M21 15v6h-6"/></svg>'
  };

  // ───────── layout ─────────
  R.className = 'cw';
  R.innerHTML =
    '<header class="cw-top"><button class="cw-lock" data-a="safety" title="Encryption and safety emoji">' + ICON.lock + '<span>Encrypted</span></button>' +
      '<div class="cw-title"><b class="cw-peername">…</b><small class="cw-status">Starting…</small></div><span class="cw-bars" title="Connection quality"><i></i><i></i><i></i><i></i></span></header>' +
    '<main class="cw-stage">' +
      '<div class="cw-screen cw-h"><video class="cw-rscreen" playsinline autoplay muted></video><div class="cw-wm"></div><div class="cw-curtain cw-h"><b>Screen paused</b><small>They paused sharing for a moment</small></div><div class="cw-ptrs"></div>' +
        '<div class="cw-screen-tools"><span class="cw-chip cw-hint">Click anywhere to point</span><button class="cw-ib" data-a="snap" title="Snapshot to chat">' + ICON.snap + '</button><button class="cw-ib" data-a="fullscreen" title="Full screen">' + ICON.full + '</button></div></div>' +
      '<div class="cw-peer"><video class="cw-rcam cw-h" playsinline autoplay muted></video><div class="cw-avatar"><span class="cw-aura"></span><span class="cw-aura two"></span><b class="cw-ini">?</b></div>' +
        '<div class="cw-pname"><span class="cw-pmic cw-h">' + ICON.micoff + '</span><span class="cw-pn"></span></div></div>' +
      '<div class="cw-self"><video class="cw-lcam cw-h" playsinline autoplay muted></video><b class="cw-selfini"></b><span class="cw-mylevel"></span></div>' +
      '<div class="cw-myshare cw-h"><video class="cw-lscreen" playsinline autoplay muted></video><div class="cw-ptrs"></div><span class="cw-grade"></span></div>' +
      '<div class="cw-banners"></div><div class="cw-react"></div>' +
    '</main>' +
    '<section class="cw-safety cw-h"></section>' +
    '<footer class="cw-bar">' +
      '<button class="cw-btn" data-a="mic" title="Mute (M)">' + ICON.mic + '<small>Mute</small></button>' +
      '<button class="cw-btn" data-a="cam" title="Camera (V)">' + ICON.camoff + '<small>Camera</small></button>' +
      '<div class="cw-sharewrap"><button class="cw-btn" data-a="sharemenu" title="Share your screen">' + ICON.screen + '<small>Share</small></button></div>' +
      '<button class="cw-btn cw-h" data-a="pause" title="Pause sharing">' + ICON.pause + '<small>Pause</small></button>' +
      '<button class="cw-btn" data-a="ask" title="Ask to see their screen">' + ICON.eye + '<small>Ask</small></button>' +
      '<button class="cw-btn" data-a="react" title="React">' + ICON.smile + '<small>React</small></button>' +
      '<button class="cw-btn cw-end" data-a="hangup" title="Hang up">' + ICON.end + '<small>End</small></button>' +
    '</footer>' +
    '<div class="cw-over"></div><audio class="cw-raudio" autoplay></audio>';
  var el = {
    over: $('.cw-over', R), status: $('.cw-status', R), peername: $('.cw-peername', R), bars: $('.cw-bars', R), lock: $('.cw-lock', R),
    screen: $('.cw-screen', R), rscreen: $('.cw-rscreen', R), wm: $('.cw-wm', R), curtain: $('.cw-curtain', R), rptrs: $('.cw-screen .cw-ptrs', R),
    peer: $('.cw-peer', R), rcam: $('.cw-rcam', R), ini: $('.cw-ini', R), pn: $('.cw-pn', R), pmic: $('.cw-pmic', R), aura: $('.cw-avatar', R),
    self: $('.cw-self', R), lcam: $('.cw-lcam', R), selfini: $('.cw-selfini', R), mylevel: $('.cw-mylevel', R),
    myshare: $('.cw-myshare', R), lscreen: $('.cw-lscreen', R), lptrs: $('.cw-myshare .cw-ptrs', R), grade: $('.cw-grade', R),
    banners: $('.cw-banners', R), react: $('.cw-react', R), safety: $('.cw-safety', R), raudio: $('.cw-raudio', R), bar: $('.cw-bar', R)
  };
  el.selfini.textContent = D.meInitials || 'ME';
  if (!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia)) $('[data-a=sharemenu]', R).closest('.cw-sharewrap').classList.add('cw-h');

  function status(t) { el.status.textContent = t; }
  function banner(id, html, kind, ms) {
    var b = $('[data-b="' + id + '"]', el.banners);
    if (!html) { if (b) b.remove(); return; }
    if (!b) { b = document.createElement('div'); b.dataset.b = id; el.banners.appendChild(b); }
    b.className = 'cw-banner ' + (kind || ''); b.innerHTML = html;
    if (ms) setTimeout(function () { if (b.isConnected) b.remove(); }, ms);
    return b;
  }
  function overlay(html) { el.over.innerHTML = html || ''; el.over.classList.toggle('on', !!html); }

  // ───────── sound (made on the fly, no files) ─────────
  var AC = null;
  function ac() { try { AC = AC || new (window.AudioContext || window.webkitAudioContext)(); if (AC.state === 'suspended') AC.resume(); } catch (e) {} return AC; }
  document.addEventListener('pointerdown', ac);
  function tone(freqs, len, gap, vol) {
    var a = ac(); if (!a) return;
    freqs.forEach(function (f, i) {
      var o = a.createOscillator(), g = a.createGain(), t = a.currentTime + i * (gap || 0);
      o.frequency.value = f; o.connect(g); g.connect(a.destination);
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(vol || 0.08, t + 0.03); g.gain.exponentialRampToValueAtTime(0.0001, t + len);
      o.start(t); o.stop(t + len + 0.05);
    });
  }
  function ringback(on) {
    clearInterval(S.timers.ringback);
    if (on) { var beep = function () { tone([440, 480], 1.2, 0, 0.05); }; beep(); S.timers.ringback = setInterval(beep, 3000); }
  }

  // ───────── safety emoji (short authentication string from DTLS fingerprints) ─────────
  var SAS = ('🐶 dog,🐱 cat,🦊 fox,🦁 lion,🐯 tiger,🐼 panda,🐨 koala,🐸 frog,🐵 monkey,🐤 chick,🐧 penguin,🦉 owl,🐳 whale,🐬 dolphin,🐙 octopus,🦋 butterfly,' +
    '🐝 bee,🐢 turtle,🐍 snake,🦄 unicorn,🌵 cactus,🌴 palm,🌻 sunflower,🍄 mushroom,🍎 apple,🍋 lemon,🍌 banana,🍇 grapes,🍒 cherry,🍓 strawberry,🍍 pineapple,🥕 carrot,' +
    '🌽 corn,🌶️ chili,🍞 bread,🧀 cheese,🍕 pizza,🎂 cake,🍩 donut,☕ coffee,🚀 rocket,✈️ plane,🚗 car,🚲 bike,⛵ boat,⚓ anchor,🔔 bell,🔑 key,' +
    '🔨 hammer,🎸 guitar,🥁 drum,🏆 trophy,🎈 balloon,🎁 gift,☂️ umbrella,⭐ star,🌙 moon,☀️ sun,🔥 fire,❄️ snowflake,🌈 rainbow,⚽ football,🎩 hat,👑 crown').split(',');
  function fingerprint(sdp) { var m = /a=fingerprint:(\S+) ([0-9A-Fa-f:]+)/.exec(sdp || ''); return m ? (m[1].toLowerCase() + ' ' + m[2].toUpperCase()) : ''; }
  function computeSafety() {
    if (!S.pc || !S.pc.localDescription || !S.pc.remoteDescription || !window.crypto || !crypto.subtle) return;
    var a = fingerprint(S.pc.localDescription.sdp), b = fingerprint(S.pc.remoteDescription.sdp); if (!a || !b) return;
    var both = [a, b].sort().join('|');
    crypto.subtle.digest('SHA-256', new TextEncoder().encode('taptap-call-sas-v1|' + both)).then(function (h) {
      var bytes = new Uint8Array(h); S.sas = [0, 1, 2, 3].map(function (i) { return SAS[bytes[i] % 64].split(' '); });
      renderSafety();
    });
  }
  function renderSafety() {
    if (!S.sas) return;
    var v = S.verified.me && S.verified.them;
    el.lock.classList.toggle('ok', v); el.lock.querySelector('span').textContent = v ? 'Verified' : (S.relay ? 'Encrypted · relay' : 'Encrypted');
    el.safety.innerHTML = '<div class="cw-sas">' + S.sas.map(function (x) { return '<span><b>' + x[0] + '</b><small>' + x[1] + '</small></span>'; }).join('') + '</div>' +
      '<p>Ask ' + esc(first(S.peer.name) || 'them') + ' to read their 4 safety emoji. <b>If they match yours, this call is private</b> — encrypted end-to-end, with nobody in the middle, not even TapTap.' +
      (S.relay ? ' The call goes through a relay, which only sees encrypted data and hides both your IP addresses.' : ' You are connected directly, peer-to-peer.') + '</p>' +
      '<div class="cw-sas-act">' + (S.verified.me ? '<span class="cw-chip ok">✓ You confirmed</span>' : '<button class="cw-pill" data-a="verify">They match ✓</button><button class="cw-pill ghost" data-a="mismatch">They don’t match</button>') +
      (S.verified.them ? '<span class="cw-chip ok">✓ ' + esc(first(S.peer.name)) + ' confirmed</span>' : '') + '</div>';
  }

  // ───────── media ─────────
  function getMic() {
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }).then(function (s) {
      S.mic = s.getAudioTracks()[0]; S.micProbe = S.mic.clone(); level(new MediaStream([S.micProbe]), el.mylevel, true); return S.mic;
    });
  }
  function getCam() {
    return navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 640 }, height: { ideal: 360 }, frameRate: { max: 24 }, facingMode: 'user' } }).then(function (s) {
      S.cam = s.getVideoTracks()[0]; S.cam.contentHint = 'motion'; el.lcam.srcObject = new MediaStream([S.cam]); el.lcam.classList.remove('cw-h'); el.self.classList.add('has-video'); return S.cam;
    });
  }
  function mediaError(e) {
    var n = e && e.name;
    return n === 'NotAllowedError' ? 'Microphone access is blocked. Click the 🔒 next to the address bar, allow the microphone, then try again.'
      : n === 'NotFoundError' ? 'No microphone was found on this device.' : n === 'NotReadableError' ? 'Another app is using your microphone or camera.'
      : !window.isSecureContext ? 'Calls need a secure (https://) TapTap address.' : 'Could not start your microphone (' + (n || 'unknown error') + ').';
  }

  // Voice aura / my mic level: a gentle glow that follows the volume.
  function level(stream, node, mine) {
    var a = ac(); if (!a) return;
    try {
      var an = a.createAnalyser(); an.fftSize = 256; a.createMediaStreamSource(stream).connect(an);
      var buf = new Uint8Array(an.frequencyBinCount), loudSince = 0;
      (function tick() {
        if (S.ended) return;
        an.getByteFrequencyData(buf); var sum = 0; for (var i = 0; i < buf.length; i++) sum += buf[i];
        var v = Math.min(1, sum / buf.length / 70);
        node.style.setProperty('--lvl', v.toFixed(2));
        if (mine && S.mic && !S.mic.enabled) {              // talking while muted? nudge
          if (v > 0.35) { loudSince = loudSince || Date.now(); if (Date.now() - loudSince > 1200) banner('muted', '🎙️ You are muted — <button class="cw-link" data-a="mic">unmute</button>', 'warn', 4000); }
          else loudSince = 0;
        }
        requestAnimationFrame(tick);
      })();
    } catch (e) {}
  }

  // ───────── peer connection ─────────
  function makePC() {
    var cfg = { iceServers: (S.ice && S.ice.iceServers) || [], iceTransportPolicy: (S.ice && S.ice.iceTransportPolicy) || 'all', bundlePolicy: 'max-bundle' };
    var pc = new RTCPeerConnection(cfg); S.pc = pc;
    S.dc = pc.createDataChannel('ctl', { negotiated: true, id: 0, ordered: true });
    S.dc.onopen = function () { send({ t: 'hello', name: D.me, initials: D.meInitials }); sendState(); };
    S.dc.onmessage = function (e) { try { onCtl(JSON.parse(e.data)); } catch (x) {} };
    pc.ontrack = function (e) {
      var s = new MediaStream([e.track]);
      if (e.track.kind === 'audio') { el.raudio.srcObject = s; el.raudio.play().catch(function () { banner('audio', '🔈 <button class="cw-link" data-a="unblock">Tap to hear the call</button>', 'warn'); }); level(s, el.aura); }
      else {
        // slot by position (audio, camera, screen) — on the answering side this fires before S.T is filled in
        var slot = pc.getTransceivers().indexOf(e.transceiver);
        if (slot === 1) el.rcam.srcObject = s; else if (slot === 2) el.rscreen.srcObject = s;
      }
    };
    pc.onconnectionstatechange = function () {
      var st = pc.connectionState;
      if (st === 'connected') { banner('net', ''); clearTimeout(S.timers.lost); onLive(); }
      else if (st === 'disconnected') { banner('net', '📶 Connection dropped — reconnecting…', 'warn'); S.timers.lost = setTimeout(function () { hangup('lost'); }, 15000); }
      else if (st === 'failed') hangup('failed');
    };
    return pc;
  }
  function gathered(pc, ms) {
    return new Promise(function (res) {
      if (pc.iceGatheringState === 'complete') return res();
      var t = setTimeout(res, ms), done = function () { if (pc.iceGatheringState === 'complete') { clearTimeout(t); res(); } };
      pc.addEventListener('icegatheringstatechange', done);
    });
  }
  function tuneSenders() {
    try {
      var p = S.T.screen.sender.getParameters(); if (!p.encodings || !p.encodings.length) p.encodings = [{}];
      p.encodings[0].maxBitrate = 1500000; p.encodings[0].maxFramerate = 15; p.degradationPreference = 'maintain-resolution';
      S.T.screen.sender.setParameters(p).catch(function () {});
    } catch (e) {}
    camBitrate(S.lowered ? 250000 : 700000, S.lowered ? 2 : 1);
  }
  function camBitrate(bps, down) {
    try { var p = S.T.cam.sender.getParameters(); if (!p.encodings || !p.encodings.length) p.encodings = [{}]; p.encodings[0].maxBitrate = bps; p.encodings[0].scaleResolutionDownBy = down; S.T.cam.sender.setParameters(p).catch(function () {}); } catch (e) {}
  }

  // ───────── caller ─────────
  function startCaller() {
    overlay(ringHTML('Calling…', 'Starting your microphone'));
    getMic().then(function () { return S.video ? getCam().catch(function () { S.video = false; }) : null; })
      .then(function () { return api('/chat/call/act/', { action: 'start', thread: S.thread, video: S.video }); })
      .then(function (d) {
        if (!d.ok) throw new Error(d.message);
        S.cid = d.call.id; S.ice = d.ice; S.peer.name = d.title; paintPeer(); overlay(ringHTML('Calling ' + esc(d.title) + '…', 'Ringing'));
        var pc = makePC();
        S.T.audio = pc.addTransceiver(S.mic, { direction: 'sendrecv' });
        S.T.cam = pc.addTransceiver(S.cam || 'video', { direction: 'sendrecv' });
        S.T.screen = pc.addTransceiver('video', { direction: 'sendrecv' });
        return pc.createOffer().then(function (o) { return pc.setLocalDescription(o); })
          .then(function () { return gathered(pc, 2500); })
          .then(function () { return act('offer', { desc: { type: 'offer', sdp: pc.localDescription.sdp } }); })
          .then(function (r) { if (!r.ok) throw new Error(r.message); ringback(true); pollCall(1000); });
      })
      .catch(function (e) { fail(e && e.name ? mediaError(e) : (e && e.message) || 'Could not start the call.'); });
  }

  // ───────── callee ─────────
  function startCallee() {
    S.cid = D.join; overlay(ringHTML('Joining…', 'Connecting securely'));
    act('accept').then(function (d) {
      if (!d.ok) throw new Error(d.message);
      S.ice = d.ice; S.peer.name = d.from; S.video = S.video || false; paintPeer();
      return getMic().then(function () { return S.video ? getCam().catch(function () { S.video = false; }) : null; })
        .then(function () { return d.offer || waitOffer(); });
    }).then(function (offer) {
      var pc = makePC();
      return pc.setRemoteDescription(offer).then(function () {
        var t = pc.getTransceivers(); S.T.audio = t[0]; S.T.cam = t[1]; S.T.screen = t[2];
        t.forEach(function (x) { x.direction = 'sendrecv'; });
        return Promise.all([S.T.audio.sender.replaceTrack(S.mic), S.T.cam.sender.replaceTrack(S.cam || null)]);
      }).then(function () { return pc.createAnswer(); }).then(function (a) { return pc.setLocalDescription(a); })
        .then(function () { return gathered(pc, 2500); })
        .then(function () { return act('answer', { desc: { type: 'answer', sdp: pc.localDescription.sdp } }); })
        .then(function (r) { if (!r.ok) throw new Error(r.message); status('Connecting…'); computeSafety(); pollCall(2500); });
    }).catch(function (e) { fail(e && e.name ? mediaError(e) : (e && e.message) || 'Could not join the call.'); });
  }
  function waitOffer() {
    return new Promise(function (res, rej) {
      var n = 0; (function again() {
        api('/chat/call/poll/?id=' + encodeURIComponent(S.cid)).then(function (d) {
          if (d.call && d.call.offer) return res(d.call.offer);
          if (!d.call || d.call.state === 'ended' || ++n > 20) return rej(new Error('The call ended before it connected.'));
          setTimeout(again, 700);
        });
      })();
    });
  }

  // ───────── server poll: answer arrives, heartbeat, ended elsewhere ─────────
  function pollCall(ms) {
    clearTimeout(S.timers.poll);
    S.timers.poll = setTimeout(function () {
      api('/chat/call/poll/?id=' + encodeURIComponent(S.cid)).then(function (d) {
        if (S.ended) return;
        var c = d.call || {};
        if (c.peer) {                       // the caller learns who actually answered (e.g. which support agent)
          if (S.role === 'caller') S.peer.name = c.peer.name;
          S.peer.initials = c.peer.initials; paintPeer();
        }
        if (c.state === 'ended') return hangup(c.reason || 'ended', true);
        if (S.role === 'caller' && c.answer && S.pc && !S.pc.remoteDescription) {
          ringback(false); overlay(ringHTML('Connecting…', 'Setting up an encrypted line'));
          S.pc.setRemoteDescription(c.answer).then(computeSafety).catch(function () { hangup('failed'); });
        }
        if (S.role === 'caller' && c.state === 'connecting' && !c.answer) status('Answering…');
        pollCall(S.liveAt ? 3000 : 1000);
      });
    }, ms);
  }

  // ───────── live ─────────
  function onLive() {
    if (S.liveAt) return;
    S.liveAt = Date.now(); ringback(false); overlay(''); tone([660, 880], 0.18, 0.12, 0.05); tuneSenders();
    S.timers.clock = setInterval(function () {
      var t = clock(Date.now() - S.liveAt); status(t + (S.share ? ' · sharing your screen' : S.remote.screen ? ' · viewing their screen' : ''));
      bcast({ t: 'call', state: 'live', peer: S.peer.name, since: S.liveAt, sharing: !!S.share, viewing: !!S.remote.screen });
      leaseTick();
    }, 1000);
    S.timers.stats = setInterval(stats, 2000); stats();
    computeSafety(); syncButtons();
  }
  function paintPeer() {
    el.peername.textContent = S.peer.name || 'Call'; el.pn.textContent = first(S.peer.name);
    var ini = S.peer.initials && S.peer.initials !== '?' ? S.peer.initials : String(S.peer.name || '?').split(/\s+/).map(function (w) { return w[0]; }).join('').slice(0, 2).toUpperCase();
    el.ini.textContent = ini; document.title = (S.peer.name || 'Call') + ' — TapTap call';
  }

  // Network quality from WebRTC stats; on a weak line the camera drops to save the audio.
  var prev = {};
  function stats() {
    if (!S.pc) return;
    S.pc.getStats().then(function (r) {
      var pair = null, local = {}, rtt = 0, lost = 0, recv = 0;
      r.forEach(function (s) {
        if (s.type === 'transport' && s.selectedCandidatePairId) pair = r.get(s.selectedCandidatePairId);
        if (s.type === 'candidate-pair' && s.nominated && s.state === 'succeeded' && !pair) pair = s;
        if (s.type === 'inbound-rtp' && s.kind === 'audio') { lost = s.packetsLost || 0; recv = s.packetsReceived || 0; }
      });
      if (pair) { rtt = (pair.currentRoundTripTime || 0) * 1000; local = r.get(pair.localCandidateId) || {}; var rem = r.get(pair.remoteCandidateId) || {}; S.relay = local.candidateType === 'relay' || rem.candidateType === 'relay'; }
      var dl = lost - (prev.lost || 0), dr = recv - (prev.recv || 0); prev = { lost: lost, recv: recv };
      var loss = dr + dl > 0 ? dl / (dr + dl) * 100 : 0;
      var q = rtt < 150 && loss < 2 ? 4 : rtt < 300 && loss < 5 ? 3 : rtt < 600 && loss < 12 ? 2 : 1;
      S.quality = q; el.bars.dataset.q = q; el.bars.title = 'Connection: ' + ['', 'poor', 'weak', 'good', 'excellent'][q] + ' · ' + Math.round(rtt) + ' ms' + (S.relay ? ' · via relay' : ' · direct');
      if (q <= 2 && !S.lowered && S.cam) { S.lowered = true; camBitrate(250000, 2); banner('low', '📶 Weak network — your video was lowered so the voice stays clear', 'info', 6000); }
      else if (q >= 4 && S.lowered) { S.lowered = false; camBitrate(700000, 1); }
      renderSafety();
    }).catch(function () {});
  }

  // ───────── controls ─────────
  function sendState() {
    send({ t: 'state', mic: !!(S.mic && S.mic.enabled), cam: !!(S.cam && S.cam.readyState === 'live'),
           screen: S.share ? { surface: S.share.surface, paused: S.share.paused } : null });
  }
  function syncButtons() {
    var micOn = S.mic && S.mic.enabled, camOn = S.cam && S.cam.readyState === 'live';
    var b = $('[data-a=mic]', el.bar); b.innerHTML = (micOn ? ICON.mic : ICON.micoff) + '<small>' + (micOn ? 'Mute' : 'Unmute') + '</small>'; b.classList.toggle('off', !micOn);
    b = $('[data-a=cam]', el.bar); b.innerHTML = (camOn ? ICON.cam : ICON.camoff) + '<small>Camera</small>'; b.classList.toggle('on', !!camOn);
    b = $('[data-a=sharemenu]', el.bar); b.innerHTML = (S.share ? ICON.stop : ICON.screen) + '<small>' + (S.share ? 'Stop' : 'Share') + '</small>'; b.classList.toggle('live', !!S.share);
    b = $('[data-a=pause]', el.bar); b.classList.toggle('cw-h', !S.share); if (S.share) b.innerHTML = (S.share.paused ? ICON.play : ICON.pause) + '<small>' + (S.share.paused ? 'Resume' : 'Pause') + '</small>';
    $('[data-a=ask]', el.bar).classList.toggle('cw-h', !!S.remote.screen || !S.liveAt);
  }
  function toggleMic() { if (!S.mic) return; S.mic.enabled = !S.mic.enabled; banner('muted', ''); syncButtons(); sendState(); }
  function toggleCam() {
    if (S.cam && S.cam.readyState === 'live') {
      S.cam.stop(); S.cam = null; S.T.cam && S.T.cam.sender.replaceTrack(null); el.lcam.classList.add('cw-h'); el.self.classList.remove('has-video'); syncButtons(); sendState(); return;
    }
    getCam().then(function (t) { return S.T.cam && S.T.cam.sender.replaceTrack(t); }).then(function () { tuneSenders(); syncButtons(); sendState(); })
      .catch(function (e) { banner('cam', '📷 ' + (e && e.name === 'NotAllowedError' ? 'Camera access is blocked in your browser.' : 'Could not start the camera.'), 'warn', 5000); });
  }

  // ───────── screen sharing ─────────
  var GRADE = {
    browser: ['safe', '🟢 Tab only', 'Only that tab is visible — the safest way to share.'],
    window: ['ok', '🟡 One window', 'Only that window is visible.'],
    monitor: ['risky', '🟠 Whole screen', 'Everything on your screen is visible, including pop-ups and notifications from other apps.']
  };
  function shareMenu() {
    if (S.share) return stopShare();
    var wrap = $('.cw-sharewrap', el.bar), old = $('.cw-menu', wrap); if (old) { old.remove(); return; }
    var m = document.createElement('div'); m.className = 'cw-menu';
    m.innerHTML = '<button data-a="share-tab"><b>🟢 A TapTap tab</b><small>Safest. Passwords and keys are blurred, and they can point on your page.</small></button>' +
      '<button data-a="share-any"><b>🟡 A window or screen</b><small>Show another app. Close private things first.</small></button>';
    wrap.appendChild(m);
  }
  function startShare(kind) {
    var m = $('.cw-menu', el.bar); if (m) m.remove();
    var opts = { video: { frameRate: { ideal: 15, max: 30 }, width: { max: 1920 }, height: { max: 1080 }, displaySurface: kind === 'tab' ? 'browser' : 'window' },
                 audio: false, selfBrowserSurface: 'exclude', surfaceSwitching: 'include', systemAudio: 'exclude', monitorTypeSurfaces: kind === 'tab' ? 'exclude' : 'include' };
    if (kind === 'tab') bcast({ t: 'shield', on: true, pending: true });   // shield first, so nothing leaks in the first frame
    navigator.mediaDevices.getDisplayMedia(opts).then(function (stream) {
      var track = stream.getVideoTracks()[0]; track.contentHint = 'detail';
      var st = track.getSettings ? track.getSettings() : {}, surface = st.displaySurface || (kind === 'tab' ? 'browser' : 'window');
      var handle = ''; try { var h = track.getCaptureHandle && track.getCaptureHandle(); handle = (h && h.handle) || ''; } catch (e) {}
      S.share = { track: track, surface: surface, handle: handle, paused: false, until: Date.now() + LEASE_MS };
      track.onended = function () { stopShare(); };
      return S.T.screen.sender.replaceTrack(track).then(function () {
        tuneSenders();
        el.lscreen.srcObject = new MediaStream([track]); el.myshare.classList.remove('cw-h');
        var g = GRADE[surface] || GRADE.window; el.grade.className = 'cw-grade ' + g[0]; el.grade.textContent = g[1]; el.grade.title = g[2];
        banner('grade', '<b>' + g[1] + '</b> · ' + g[2] + (surface === 'browser' ? ' Secrets on TapTap pages are blurred while you share.' : ''), g[0] === 'risky' ? 'warn' : 'info', 7000);
        bcast({ t: 'shield', on: true, handle: handle, surface: surface });
        sendState(); syncButtons(); tone([523, 784], 0.15, 0.1, 0.04);
      });
    }).catch(function (e) {
      bcast({ t: 'shield', on: false });
      if (e && e.name !== 'NotAllowedError' && e.name !== 'AbortError') banner('share', 'Could not share your screen (' + esc(e.name || e) + ').', 'warn', 5000);
    });
  }
  function stopShare() {
    if (!S.share) return;
    try { S.share.track.onended = null; S.share.track.stop(); } catch (e) {}
    S.share = null; S.T.screen && S.T.screen.sender.replaceTrack(null);
    el.myshare.classList.add('cw-h'); el.lscreen.srcObject = null; banner('lease', ''); banner('grade', '');
    bcast({ t: 'shield', on: false }); sendState(); syncButtons();
  }
  function togglePause() {
    if (!S.share) return; S.share.paused = !S.share.paused; S.share.track.enabled = !S.share.paused;
    el.myshare.classList.toggle('paused', S.share.paused); sendState(); syncButtons();
  }
  function leaseTick() {
    if (!S.share) return;
    var left = S.share.until - Date.now();
    el.grade.dataset.left = clock(left);
    if (left <= 0) { stopShare(); banner('lease', '⏱️ Screen sharing stopped after 15 minutes, to keep your screen private. Share again any time.', 'info', 8000); }
    else if (left <= WARN_MS) banner('lease', '⏱️ Sharing stops in ' + Math.ceil(left / 1000) + 's — <button class="cw-link" data-a="extend">keep sharing</button>', 'warn');
  }

  // Pointer: the viewer clicks on the shared screen → a ripple shows to the sharer (and on the shared TapTap tab).
  function videoPoint(video, ev) {
    var r = video.getBoundingClientRect(), vw = video.videoWidth, vh = video.videoHeight; if (!vw || !vh) return null;
    var sc = Math.min(r.width / vw, r.height / vh), w = vw * sc, h = vh * sc, ox = (r.width - w) / 2, oy = (r.height - h) / 2;
    var x = (ev.clientX - r.left - ox) / w, y = (ev.clientY - r.top - oy) / h;
    return x < 0 || y < 0 || x > 1 || y > 1 ? null : { x: x, y: y, box: { l: ox, t: oy, w: w, h: h } };
  }
  function ripple(layer, video, x, y, label) {
    var r = video.getBoundingClientRect(), vw = video.videoWidth || 16, vh = video.videoHeight || 9;
    var sc = Math.min(r.width / vw, r.height / vh), w = vw * sc, h = vh * sc;
    var d = document.createElement('i'); d.className = 'cw-ping'; d.style.left = ((r.width - w) / 2 + x * w) + 'px'; d.style.top = ((r.height - h) / 2 + y * h) + 'px';
    if (label) d.dataset.label = label;
    layer.appendChild(d); setTimeout(function () { d.remove(); }, 1600);
  }
  el.rscreen.addEventListener('click', function (ev) {
    var p = videoPoint(el.rscreen, ev); if (!p) return;
    ripple(el.rptrs, el.rscreen, p.x, p.y); send({ t: 'ptr', x: +p.x.toFixed(4), y: +p.y.toFixed(4) });
    $('.cw-hint', el.screen).classList.add('cw-h');
  });

  function snapshot() {
    var v = el.rscreen; if (!v.videoWidth) return;
    var c = document.createElement('canvas'), sc = Math.min(1, 1920 / Math.max(v.videoWidth, v.videoHeight));
    c.width = Math.round(v.videoWidth * sc); c.height = Math.round(v.videoHeight * sc); c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
    el.screen.classList.add('flash'); setTimeout(function () { el.screen.classList.remove('flash'); }, 350);
    c.toBlob(function (blob) {
      if (!blob) return;
      var f = new FormData(); f.append('thread', S.thread || ''); f.append('file', blob, 'snapshot.webp'); f.append('body', '📸 Snapshot of ' + first(S.peer.name) + '’s screen');
      fetch('/chat/upload/', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest' }, body: f })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (d.ok) { send({ t: 'snap' }); banner('snap', '📸 Saved to the chat — deleted automatically after ' + (D.imageDays || 7) + ' days', 'info', 4000); }
          else banner('snap', esc(d.message || 'Could not save the snapshot.'), 'warn', 5000);
        });
    }, 'image/webp', 0.85);
  }

  function watermark() {
    var c = document.createElement('canvas'); c.width = 360; c.height = 200; var g = c.getContext('2d');
    g.translate(180, 100); g.rotate(-0.35); g.font = '600 15px system-ui,sans-serif'; g.fillStyle = 'rgba(255,255,255,.13)'; g.textAlign = 'center';
    g.fillText('Viewed by ' + (D.me || ''), 0, -6); g.fillText(new Date().toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }), 0, 14);
    el.wm.style.backgroundImage = 'url(' + c.toDataURL() + ')';
  }

  // ───────── data channel messages ─────────
  function onCtl(m) {
    if (m.t === 'hello') { if (m.name) { S.peer.name = S.peer.name || m.name; S.peer.initials = m.initials || S.peer.initials; paintPeer(); } }
    else if (m.t === 'state') {
      S.remote.mic = m.mic; S.remote.cam = m.cam; var was = S.remote.screen; S.remote.screen = m.screen;
      el.pmic.classList.toggle('cw-h', !!m.mic); el.rcam.classList.toggle('cw-h', !m.cam); el.peer.classList.toggle('has-video', !!m.cam);
      el.screen.classList.toggle('cw-h', !m.screen); R.classList.toggle('viewing', !!m.screen);
      el.curtain.classList.toggle('cw-h', !(m.screen && m.screen.paused));
      if (m.screen && !was) { watermark(); S.timers.wm = setInterval(watermark, 60000); $('.cw-hint', el.screen).classList.remove('cw-h'); banner('ask', ''); tone([784, 1046], 0.15, 0.1, 0.04); }
      if (!m.screen && was) clearInterval(S.timers.wm);
      if (m.screen) { var g = GRADE[m.screen.surface]; banner('vgrade', g ? first(S.peer.name) + ' is sharing <b>' + g[1].slice(3) + '</b>' : '', 'info', 4000); }
      syncButtons();
    }
    else if (m.t === 'ptr' && S.share) {
      ripple(el.lptrs, el.lscreen, m.x, m.y, first(S.peer.name));
      bcast({ t: 'ptr', x: m.x, y: m.y, handle: S.share.handle, surface: S.share.surface, who: first(S.peer.name) });
    }
    else if (m.t === 'ask') {
      if (S.share) return;
      tone([880, 988], 0.15, 0.12, 0.05);
      banner('ask', '👀 <b>' + esc(first(S.peer.name)) + ' asks to see your screen.</b> <button class="cw-link" data-a="share-tab">Share a TapTap tab</button> · <button class="cw-link" data-a="share-any">Something else</button> · <button class="cw-link" data-a="askno">Not now</button>', 'ask');
    }
    else if (m.t === 'askno') banner('asked', first(S.peer.name) + ' would rather not share right now.', 'info', 4000);
    else if (m.t === 'snap') banner('snapped', '📸 ' + esc(first(S.peer.name)) + ' saved a snapshot of your screen to the chat.', 'warn', 6000);
    else if (m.t === 'verified') { S.verified.them = true; renderSafety(); }
    else if (m.t === 'mismatch') { banner('mm', '⚠️ ' + esc(first(S.peer.name)) + ' says the safety emoji do not match. Hang up and call again.', 'danger'); }
    else if (m.t === 'react') floatEmoji(m.e);
    else if (m.t === 'bye') hangup('ended', true);
  }
  function floatEmoji(e) {
    var n = document.createElement('span'); n.textContent = e; n.style.left = (20 + Math.random() * 60) + '%'; el.react.appendChild(n);
    setTimeout(function () { n.remove(); }, 2600);
  }

  // ───────── clicks & keys ─────────
  R.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-a]'); if (!b) return;
    var a = b.dataset.a;
    if (a === 'mic') toggleMic();
    else if (a === 'cam') toggleCam();
    else if (a === 'sharemenu') shareMenu();
    else if (a === 'share-tab') { banner('ask', ''); startShare('tab'); }
    else if (a === 'share-any') { banner('ask', ''); startShare('any'); }
    else if (a === 'askno') { banner('ask', ''); send({ t: 'askno' }); }
    else if (a === 'pause') togglePause();
    else if (a === 'extend') { if (S.share) { S.share.until = Date.now() + LEASE_MS; banner('lease', ''); } }
    else if (a === 'ask') {
      if (Date.now() - S.lastAsk < 20000) return banner('ask2', 'You already asked — give them a moment.', 'info', 3000);
      S.lastAsk = Date.now(); send({ t: 'ask' }); banner('ask2', 'Asked ' + esc(first(S.peer.name)) + ' to share their screen.', 'info', 3500);
    }
    else if (a === 'snap') snapshot();
    else if (a === 'fullscreen') { var f = el.screen; (document.fullscreenElement ? document.exitFullscreen() : f.requestFullscreen && f.requestFullscreen()); }
    else if (a === 'safety') { el.safety.classList.toggle('cw-h'); renderSafety(); }
    else if (a === 'verify') { S.verified.me = true; send({ t: 'verified' }); renderSafety(); }
    else if (a === 'mismatch') { send({ t: 'mismatch' }); banner('mm', '⚠️ If the emoji differ, someone may be in the middle. Hang up and call again.', 'danger'); }
    else if (a === 'react') {
      var wrap = b, old = $('.cw-emojis', R); if (old) { old.remove(); return; }
      var p = document.createElement('div'); p.className = 'cw-emojis';
      p.innerHTML = ['👍', '👏', '😂', '❤️', '🎉', '🙏', '👀', '✅'].map(function (e) { return '<button data-e="' + e + '">' + e + '</button>'; }).join('');
      p.addEventListener('click', function (x) { var e = x.target.closest('[data-e]'); if (!e) return; send({ t: 'react', e: e.dataset.e }); floatEmoji(e.dataset.e); p.remove(); });
      wrap.parentNode.insertBefore(p, wrap.nextSibling);
    }
    else if (a === 'unblock') { el.raudio.play(); banner('audio', ''); ac(); }
    else if (a === 'hangup') hangup('ended');
    else if (a === 'close') closeWindow();
    else if (a === 'again') location.reload();
  });
  document.addEventListener('keydown', function (e) {
    if (e.target.closest && e.target.closest('input,textarea')) return;
    if (e.key === 'm' || e.key === 'M') toggleMic();
    if (e.key === 'v' || e.key === 'V') toggleCam();
  });

  // ───────── ending ─────────
  var REASON = { ended: 'Call ended', missed: 'No answer', declined: 'Call declined', cancelled: 'Call cancelled', lost: 'The connection was lost',
                 failed: 'Could not connect — your networks may block direct calls. Ask your TapTap admin to set up a TURN relay.', gone: 'Call ended' };
  function hangup(reason, remote) {
    if (S.ended) return; S.ended = true;
    if (!remote) { send({ t: 'bye' }); if (S.cid) act('end', { reason: reason }); }
    ringback(false); stopShare();
    Object.keys(S.timers).forEach(function (k) { clearTimeout(S.timers[k]); clearInterval(S.timers[k]); });
    [S.mic, S.micProbe, S.cam].forEach(function (t) { try { t && t.stop(); } catch (e) {} });
    try { S.pc && S.pc.close(); } catch (e) {}
    bcast({ t: 'call', state: 'ended' }); bcast({ t: 'shield', on: false });
    tone([660, 440], 0.2, 0.15, 0.05);
    var dur = S.liveAt ? clock(Date.now() - S.liveAt) : '';
    overlay('<div class="cw-done"><div class="cw-bigav">' + esc(el.ini.textContent) + '</div><h2>' + esc(REASON[reason] || 'Call ended') + '</h2>' +
      (dur ? '<p>' + esc(S.peer.name) + ' · ' + dur + '</p>' : '') +
      '<div class="cw-row">' + (S.role === 'caller' && S.thread ? '<button class="cw-pill" data-a="again">Call again</button>' : '') + '<button class="cw-pill ghost" data-a="close">Close</button></div></div>');
    if (reason === 'ended' || reason === 'cancelled') setTimeout(closeWindow, 5000);
  }
  function fail(msg) { overlay('<div class="cw-done"><h2>Can’t start the call</h2><p>' + esc(msg) + '</p><div class="cw-row"><button class="cw-pill ghost" data-a="close">Close</button></div></div>'); if (S.cid) act('end', { reason: 'failed' }); S.ended = true; bcast({ t: 'call', state: 'ended' }); }
  function closeWindow() {
    if (EMBED) { try { parent.postMessage({ ttcall: 'close' }, location.origin); } catch (e) {} return; }
    window.close();
  }
  window.addEventListener('pagehide', function () {
    if (S.ended || !S.cid) return;
    bcast({ t: 'shield', on: false }); bcast({ t: 'call', state: 'ended' });
    var f = new FormData(); f.append('action', 'end'); f.append('id', S.cid); f.append('csrfmiddlewaretoken', CSRF);
    try { navigator.sendBeacon('/chat/call/act/', f); } catch (e) {}
  });
  window.addEventListener('beforeunload', function (e) { if (S.liveAt && !S.ended) { e.preventDefault(); e.returnValue = ''; } });

  function ringHTML(title, sub) {
    return '<div class="cw-ringing"><div class="cw-bigav pulse">' + esc(el.ini.textContent) + '</div><h2>' + title + '</h2><p>' + esc(sub) + '</p>' +
      '<p class="cw-e2e">' + ICON.lock + ' End-to-end encrypted</p><button class="cw-btn cw-end" data-a="hangup">' + ICON.end + '<small>' + (S.role === 'caller' ? 'Cancel' : 'Hang up') + '</small></button></div>';
  }

  // ───────── go ─────────
  if (!window.RTCPeerConnection || !navigator.mediaDevices) return fail('This browser cannot make calls. Try the latest Chrome, Edge, Firefox or Safari.');
  if (!window.isSecureContext) return fail('Calls need a secure (https://) TapTap address.');
  if (bc) bc.onmessage = function (e) {
    var m = e.data || {};
    if (m.t === 'focus-call' && !EMBED) window.focus();
    // a TapTap tab opened mid-call: give it the shield (and the call pill) straight away
    if (m.t === 'hello?' && S.share) bcast({ t: 'shield', on: true, handle: S.share.handle, surface: S.share.surface });
  };
  syncButtons();
  if (S.role === 'callee') startCallee(); else startCaller();
})();
