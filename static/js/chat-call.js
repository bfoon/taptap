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

   In-call tools:
     • Flip camera — anyone can switch between the front and the back camera (or cycle webcams on a desktop),
       and a phone can show its back camera in the big screen slot ("Show with camera") to point at a router.
     • Capture an area — drag over their screen or camera: just that rectangle goes to the chat as a photo,
       and the other side sees the outline of what was captured.
     • Short recording — up to CHAT_VIDEO_SECONDS (30 s) of their screen or camera, video only (no audio),
       saved to the chat as a clip. The other side sees a red "recording" notice the whole time and can ask
       to stop it. Like every chat file it is deleted for good after a week.
     • Remote control — on a shared TapTap tab the viewer can ask for control. Once the sharer allows it,
       the viewer's clicks (shown as ripples), scrolling and typing happen in that tab. Never in passwords,
       secrets, the chat, other websites or sign-out. The sharer takes it back with Esc or one tap, and it
       ends by itself when sharing stops, pauses, or after 15 minutes. A note is left in the chat.
*/
(function () {
  'use strict';
  var R = document.getElementById('ttCall'); if (!R) return;
  var D = R.dataset, CSRF = D.csrf, EMBED = D.embedded === '1';
  var LEASE_MS = 15 * 60 * 1000, WARN_MS = 60 * 1000, CTL_MS = 15 * 60 * 1000;
  var REC_MS = Math.max(5, +D.videoSecs || 30) * 1000, VIDEO_MAX = Math.max(1, +D.videoMb || 16) * 1024 * 1024;
  var MOBILE = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && /Macintosh/.test(navigator.userAgent));
  var bc = window.BroadcastChannel ? new BroadcastChannel('tt-call') : null;
  var S = { cid: '', role: D.join ? 'callee' : 'caller', thread: +D.thread || 0, video: D.video === '1', pc: null, dc: null,
            T: {}, mic: null, micProbe: null, cam: null, share: null, peer: { name: '', initials: '?' }, remote: { mic: true, cam: false, screen: null },
            state: 'init', liveAt: 0, ice: null, verified: { me: false, them: false }, relay: false, timers: {}, lastAsk: 0, quality: 4, lowered: false, ended: false,
            facing: 'user', camDevice: '', camIds: [],      // camera flip
            rc: 'none', rcAt: 0,                          // as the viewer: none | asked | on
            ctl: { on: false, since: 0, until: 0, n: 0, sec: 0 },   // as the sharer: is the other person controlling my tab?
            rec: null, snip: false };

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
    full: '<svg viewBox="0 0 24 24"><path d="M3 9V3h6M21 9V3h-6M3 15v6h6M21 15v6h-6"/></svg>',
    flip: '<svg viewBox="0 0 24 24"><path d="M4 7h3l2-3h6l2 3h3a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1z"/><path d="M9 12a3 3 0 0 1 5.2-2M15 14a3 3 0 0 1-5.2 2M14.5 8v2.2h-2.2M9.5 18v-2.2h2.2"/></svg>',
    snip: '<svg viewBox="0 0 24 24"><path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4"/><rect x="8" y="8" width="8" height="8" rx="1" stroke-dasharray="2 2"/></svg>',
    rec: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4" fill="currentColor"/></svg>',
    mouse: '<svg viewBox="0 0 24 24"><path d="M5 3l14 7-6 2-2 6z"/><path d="M13 12l5 5"/></svg>'
  };

  // ───────── layout ─────────
  R.className = 'cw';
  R.innerHTML =
    '<header class="cw-top"><button class="cw-lock" data-a="safety" title="Encryption and safety emoji">' + ICON.lock + '<span>Encrypted</span></button>' +
      '<div class="cw-title"><b class="cw-peername">…</b><small class="cw-status">Starting…</small></div><span class="cw-bars" title="Connection quality"><i></i><i></i><i></i><i></i></span></header>' +
    '<main class="cw-stage">' +
      '<div class="cw-screen cw-h"><video class="cw-rscreen" playsinline autoplay muted></video><div class="cw-wm"></div><div class="cw-curtain cw-h"><b>Screen paused</b><small>They paused sharing for a moment</small></div><div class="cw-ptrs"></div>' +
        '<div class="cw-screen-tools"><span class="cw-chip cw-hint">Click to point · drag to capture an area</span>' +
          '<button class="cw-ib cw-ctlbtn cw-h" data-a="rc" title="Ask for control of their TapTap tab">' + ICON.mouse + '<em>Control</em></button>' +
          '<button class="cw-ib" data-a="snip" title="Capture an area to the chat (or just drag over the screen)">' + ICON.snip + '</button>' +
          '<button class="cw-ib cw-recbtn" data-a="rec" title="Record a short clip to the chat">' + ICON.rec + '<em></em></button>' +
          '<button class="cw-ib" data-a="snap" title="Snapshot of the whole screen to chat">' + ICON.snap + '</button><button class="cw-ib" data-a="fullscreen" title="Full screen">' + ICON.full + '</button></div><div class="cw-snipbox cw-h"></div></div>' +
      '<div class="cw-peer"><video class="cw-rcam cw-h" playsinline autoplay muted></video><div class="cw-avatar"><span class="cw-aura"></span><span class="cw-aura two"></span><b class="cw-ini">?</b></div>' +
        '<div class="cw-pname"><span class="cw-pmic cw-h">' + ICON.micoff + '</span><span class="cw-pn"></span><span class="cw-pfacing cw-h">· back camera</span></div>' +
        '<div class="cw-peer-tools cw-h"><span class="cw-chip cw-hint2">Drag to capture an area</span><button class="cw-ib" data-a="snip" title="Capture an area of their camera">' + ICON.snip + '</button>' +
          '<button class="cw-ib cw-recbtn" data-a="rec" title="Record a short clip of their camera">' + ICON.rec + '<em></em></button></div><div class="cw-snipbox cw-h"></div></div>' +
      '<div class="cw-self"><video class="cw-lcam cw-h" playsinline autoplay muted></video><b class="cw-selfini"></b><span class="cw-mylevel"></span></div>' +
      '<div class="cw-myshare cw-h"><video class="cw-lscreen" playsinline autoplay muted></video><div class="cw-ptrs"></div><span class="cw-grade"></span></div>' +
      '<div class="cw-banners"></div><div class="cw-react"></div>' +
    '</main>' +
    '<section class="cw-safety cw-h"></section>' +
    '<footer class="cw-bar">' +
      '<button class="cw-btn" data-a="mic" title="Mute (M)">' + ICON.mic + '<small>Mute</small></button>' +
      '<button class="cw-btn" data-a="cam" title="Camera (V)">' + ICON.camoff + '<small>Camera</small></button>' +
      '<button class="cw-btn cw-h" data-a="flip" title="Switch front / back camera (F)">' + ICON.flip + '<small>Flip</small></button>' +
      '<div class="cw-sharewrap"><button class="cw-btn" data-a="sharemenu" title="Share your screen">' + ICON.screen + '<small>Share</small></button></div>' +
      '<button class="cw-btn cw-h" data-a="pause" title="Pause sharing">' + ICON.pause + '<small>Pause</small></button>' +
      '<button class="cw-btn cw-h live" data-a="ctlstop" title="Take back control (Esc in the shared tab)">' + ICON.mouse + '<small>Take back</small></button>' +
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
    banners: $('.cw-banners', R), react: $('.cw-react', R), safety: $('.cw-safety', R), raudio: $('.cw-raudio', R), bar: $('.cw-bar', R),
    ptools: $('.cw-peer-tools', R), pfacing: $('.cw-pfacing', R), ctlbtn: $('.cw-ctlbtn', R)
  };
  el.selfini.textContent = D.meInitials || 'ME';
  var CAN_DISPLAY = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  function canCamShare() { return MOBILE || S.camIds.length > 0; }
  function shareVisible() { $('.cw-sharewrap', R).classList.toggle('cw-h', !CAN_DISPLAY && !canCamShare()); }
  shareVisible();

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
  // Camera: phones choose front/back by facingMode; desktops with several webcams cycle through them by deviceId.
  function camVideo(opts) {
    var v = { width: { ideal: opts.big ? 1280 : 640 }, height: { ideal: opts.big ? 720 : 360 }, frameRate: { max: 24 } };
    if (opts.device) v.deviceId = { exact: opts.device }; else v.facingMode = opts.facing === 'environment' ? { ideal: 'environment' } : 'user';
    return v;
  }
  function facingOf(track, asked) {
    var st = track.getSettings ? track.getSettings() : {}, label = (track.label || '').toLowerCase();
    return st.facingMode || (/back|rear|environment/.test(label) ? 'environment' : /front|user|face/.test(label) ? 'user' : asked || 'user');
  }
  function listCams() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return Promise.resolve();
    return navigator.mediaDevices.enumerateDevices().then(function (ds) {
      S.camIds = ds.filter(function (d) { return d.kind === 'videoinput'; }).map(function (d) { return d.deviceId; });
      shareVisible(); syncButtons();
    }).catch(function () {});
  }
  try { navigator.mediaDevices.addEventListener('devicechange', listCams); } catch (e) {}
  function getCam(opts) {
    opts = opts || {}; var facing = opts.facing || S.facing;
    return navigator.mediaDevices.getUserMedia({ video: camVideo({ facing: facing, device: opts.device }) }).then(function (s) {
      S.cam = s.getVideoTracks()[0]; S.cam.contentHint = 'motion';
      S.facing = facingOf(S.cam, facing); S.camDevice = (S.cam.getSettings && S.cam.getSettings().deviceId) || '';
      el.lcam.srcObject = new MediaStream([S.cam]); el.lcam.classList.remove('cw-h'); el.self.classList.add('has-video');
      el.self.classList.toggle('back', S.facing === 'environment');           // only the selfie view is mirrored
      listCams(); return S.cam;
    });
  }
  function canFlip() { return MOBILE || S.camIds.length > 1; }
  function nextCamOpts(cur) {
    if (MOBILE || S.camIds.length < 2) return { facing: cur.facing === 'environment' ? 'user' : 'environment' };
    var i = S.camIds.indexOf(cur.device); return { device: S.camIds[(i + 1) % S.camIds.length] };
  }
  function flipCam() {
    if (S.share && S.share.surface === 'camera') return flipShareCam();
    if (!S.cam || S.cam.readyState !== 'live') return;
    var was = { facing: S.facing, device: S.camDevice }, next = nextCamOpts(was), btn = $('[data-a=flip]', el.bar);
    if (btn.disabled) return;
    S.cam.stop(); S.cam = null; btn.disabled = true;     // most phones cannot open two cameras at once
    getCam(next).then(function (t) { return S.T.cam && S.T.cam.sender.replaceTrack(t); }).then(function () {
      tuneSenders();
      banner('flip', S.facing === 'environment' ? '📷 Back camera — they see what your phone is pointing at' : '🤳 Front camera', 'info', 2500);
    }).catch(function () {
      banner('flip', '📷 Could not switch camera — staying on the one you had.', 'warn', 4000);
      return getCam(was).then(function (t) { return S.T.cam && S.T.cam.sender.replaceTrack(t); })
        .catch(function () { el.lcam.classList.add('cw-h'); el.self.classList.remove('has-video'); });
    }).then(function () { btn.disabled = false; syncButtons(); sendState(); });
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
      if (d.call && d.call.thread) S.thread = d.call.thread;       // so snapshots and clips land in the right chat
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
        if (c.thread && !S.thread) S.thread = c.thread;
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
  function canControl() { return !!(S.share && S.share.surface === 'browser' && S.share.handle && !S.share.paused); }
  function sendState() {
    send({ t: 'state', mic: !!(S.mic && S.mic.enabled), cam: !!(S.cam && S.cam.readyState === 'live'), facing: S.cam ? S.facing : '',
           screen: S.share ? { surface: S.share.surface, paused: S.share.paused, rc: canControl(), ctl: S.ctl.on } : null });
  }
  function syncButtons() {
    var micOn = S.mic && S.mic.enabled, camOn = S.cam && S.cam.readyState === 'live';
    var b = $('[data-a=mic]', el.bar); b.innerHTML = (micOn ? ICON.mic : ICON.micoff) + '<small>' + (micOn ? 'Mute' : 'Unmute') + '</small>'; b.classList.toggle('off', !micOn);
    b = $('[data-a=cam]', el.bar); b.innerHTML = (camOn ? ICON.cam : ICON.camoff) + '<small>Camera</small>'; b.classList.toggle('on', !!camOn);
    b = $('[data-a=sharemenu]', el.bar); b.innerHTML = (S.share ? ICON.stop : ICON.screen) + '<small>' + (S.share ? 'Stop' : 'Share') + '</small>'; b.classList.toggle('live', !!S.share);
    b = $('[data-a=pause]', el.bar); b.classList.toggle('cw-h', !S.share); if (S.share) b.innerHTML = (S.share.paused ? ICON.play : ICON.pause) + '<small>' + (S.share.paused ? 'Resume' : 'Pause') + '</small>';
    $('[data-a=ask]', el.bar).classList.toggle('cw-h', !!S.remote.screen || !S.liveAt);
    var camLive = (S.cam && S.cam.readyState === 'live') || (S.share && S.share.surface === 'camera');
    $('[data-a=flip]', el.bar).classList.toggle('cw-h', !camLive || !canFlip());
    b = $('[data-a=ctlstop]', el.bar); b.classList.toggle('cw-h', !S.ctl.on);
    if (S.ctl.on) b.querySelector('small').textContent = 'Take back · ' + clock(S.ctl.until - Date.now());
    // viewer: the Control button on their shared TapTap tab
    var rs = S.remote.screen, showCtl = !!(rs && !rs.paused && (rs.rc || S.rc === 'on'));
    el.ctlbtn.classList.toggle('cw-h', !showCtl);
    el.ctlbtn.classList.toggle('on', S.rc === 'on'); el.ctlbtn.classList.toggle('wait', S.rc === 'asked');
    el.ctlbtn.querySelector('em').textContent = S.rc === 'on' ? 'Release' : S.rc === 'asked' ? 'Waiting…' : 'Control';
    el.ctlbtn.title = S.rc === 'on' ? 'Give control back' : 'Ask for control of their TapTap tab';
    R.classList.toggle('controlling', S.rc === 'on');
    R.classList.toggle('snipping', S.snip);
    el.ptools.classList.toggle('cw-h', !(S.remote.cam && !rs));
    [].forEach.call(R.querySelectorAll('.cw-recbtn'), function (x) {
      x.classList.toggle('live', !!S.rec); x.querySelector('em').textContent = S.rec ? clock(REC_MS - (Date.now() - S.rec.at)) : '';
      x.title = S.rec ? 'Stop recording and save it to the chat' : 'Record a short clip (up to ' + Math.round(REC_MS / 1000) + ' s) to the chat';
    });
  }
  function toggleMic() { if (!S.mic) return; S.mic.enabled = !S.mic.enabled; banner('muted', ''); syncButtons(); sendState(); }
  function toggleCam() {
    if (S.cam && S.cam.readyState === 'live') {
      S.cam.stop(); S.cam = null; S.T.cam && S.T.cam.sender.replaceTrack(null); el.lcam.classList.add('cw-h'); el.self.classList.remove('has-video'); syncButtons(); sendState(); return;
    }
    if (S.share && S.share.surface === 'camera') return banner('cam', '📷 Your camera is in the big view. Stop “Show with camera” first.', 'info', 4000);
    getCam().then(function (t) { return S.T.cam && S.T.cam.sender.replaceTrack(t); }).then(function () { tuneSenders(); syncButtons(); sendState(); })
      .catch(function (e) { banner('cam', '📷 ' + (e && e.name === 'NotAllowedError' ? 'Camera access is blocked in your browser.' : 'Could not start the camera.'), 'warn', 5000); });
  }

  // ───────── screen sharing ─────────
  var GRADE = {
    camera: ['safe', '📷 Camera view', 'Only what your camera is pointing at is visible.'],
    browser: ['safe', '🟢 Tab only', 'Only that tab is visible — the safest way to share.'],
    window: ['ok', '🟡 One window', 'Only that window is visible.'],
    monitor: ['risky', '🟠 Whole screen', 'Everything on your screen is visible, including pop-ups and notifications from other apps.']
  };
  function shareMenu() {
    if (S.share) return stopShare();
    var wrap = $('.cw-sharewrap', el.bar), old = $('.cw-menu', wrap); if (old) { old.remove(); return; }
    var m = document.createElement('div'); m.className = 'cw-menu';
    m.innerHTML = (CAN_DISPLAY ? '<button data-a="share-tab"><b>🟢 A TapTap tab</b><small>Safest. Passwords and keys are blurred. They can point, and you can let them take control to help you.</small></button>' +
      '<button data-a="share-any"><b>🟡 A window or screen</b><small>Show another app. Close private things first.</small></button>' : '') +
      (canCamShare() ? '<button data-a="share-cam"><b>📷 Show with camera</b><small>Point your ' + (MOBILE ? 'back ' : '') + 'camera at something — a router, cables, lights. They see it big and can capture it.</small></button>' : '');
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
      // the shared tab moved to another page: follow it (its handle normally stays the same — see chat.js)
      try { track.addEventListener('capturehandlechange', function () {
        if (!S.share || S.share.track !== track) return;
        var h2 = track.getCaptureHandle && track.getCaptureHandle(); S.share.handle = (h2 && h2.handle) || '';
        bcast({ t: 'shield', on: true, handle: S.share.handle, surface: S.share.surface });
        if (S.ctl.on) { if (S.share.handle) rcMode(true); else ctlStop('left'); }
        sendState();
      }); } catch (e) {}
      return S.T.screen.sender.replaceTrack(track).then(function () {
        tuneSenders();
        el.lscreen.srcObject = new MediaStream([track]); el.myshare.classList.remove('cw-h');
        var g = GRADE[surface] || GRADE.window; el.grade.className = 'cw-grade ' + g[0]; el.grade.textContent = g[1]; el.grade.title = g[2];
        banner('grade', '<b>' + g[1] + '</b> · ' + g[2] + (surface === 'browser' ? ' Secrets on TapTap pages are blurred while you share.' +
          (handle ? '' : ' (Remote control needs Chrome or Edge on your side.)') : ''), g[0] === 'risky' ? 'warn' : 'info', 7000);
        bcast({ t: 'shield', on: true, handle: handle, surface: surface });
        sendState(); syncButtons(); tone([523, 784], 0.15, 0.1, 0.04);
      });
    }).catch(function (e) {
      bcast({ t: 'shield', on: false });
      if (e && e.name !== 'NotAllowedError' && e.name !== 'AbortError') banner('share', 'Could not share your screen (' + esc(e.name || e) + ').', 'warn', 5000);
    });
  }
  // "Show with camera": the camera goes into the big screen slot, sharper than the face camera.
  function startCamShare() {
    var m = $('.cw-menu', el.bar); if (m) m.remove();
    if (S.cam) { S.cam.stop(); S.cam = null; S.T.cam && S.T.cam.sender.replaceTrack(null); el.lcam.classList.add('cw-h'); el.self.classList.remove('has-video'); }
    var opts = MOBILE || S.camIds.length < 2 ? { facing: 'environment', big: true } : { device: S.camDevice || S.camIds[0], big: true };
    camShareTrack(opts).catch(function (e) {
      banner('share', '📷 ' + (e && e.name === 'NotAllowedError' ? 'Camera access is blocked in your browser.' : 'Could not start the camera.'), 'warn', 5000);
      sendState(); syncButtons();
    });
  }
  function camShareTrack(opts) {
    return navigator.mediaDevices.getUserMedia({ video: camVideo(opts) }).then(function (s) {
      var track = s.getVideoTracks()[0]; track.contentHint = 'detail';
      var facing = facingOf(track, opts.facing || 'environment'), device = (track.getSettings && track.getSettings().deviceId) || '';
      var keepUntil = S.share && S.share.surface === 'camera' ? S.share.until : Date.now() + LEASE_MS;
      S.share = { track: track, surface: 'camera', handle: '', paused: false, until: keepUntil, facing: facing, device: device };
      track.onended = function () { if (S.share && S.share.track === track) stopShare(); };
      return S.T.screen.sender.replaceTrack(track).then(function () {
        tuneSenders(); listCams();
        el.lscreen.srcObject = new MediaStream([track]); el.myshare.classList.remove('cw-h'); el.myshare.classList.toggle('mirror', facing === 'user');
        var g = GRADE.camera; el.grade.className = 'cw-grade ' + g[0]; el.grade.textContent = g[1]; el.grade.title = g[2];
        banner('grade', '<b>' + g[1] + '</b> · ' + g[2] + (canFlip() ? ' Tap <b>Flip</b> to switch camera.' : ''), 'info', 6000);
        sendState(); syncButtons(); tone([523, 784], 0.15, 0.1, 0.04);
      });
    });
  }
  function flipShareCam() {
    var cur = S.share, next = nextCamOpts({ facing: cur.facing, device: cur.device }); next.big = true;
    try { cur.track.onended = null; cur.track.stop(); } catch (e) {}
    camShareTrack(next).catch(function () {
      banner('flip', '📷 Could not switch camera.', 'warn', 4000);
      return camShareTrack({ facing: cur.facing, device: cur.device, big: true }).catch(function () { stopShare(); });
    });
  }
  function stopShare() {
    if (!S.share) return;
    ctlStop('share');
    try { S.share.track.onended = null; S.share.track.stop(); } catch (e) {}
    S.share = null; S.T.screen && S.T.screen.sender.replaceTrack(null);
    el.myshare.classList.add('cw-h'); el.myshare.classList.remove('mirror', 'recorded'); el.lscreen.srcObject = null; banner('lease', ''); banner('grade', ''); banner('recd', '');
    bcast({ t: 'shield', on: false }); sendState(); syncButtons();
  }
  function togglePause() {
    if (!S.share) return; S.share.paused = !S.share.paused; S.share.track.enabled = !S.share.paused;
    if (S.share.paused) ctlStop('pause');
    el.myshare.classList.toggle('paused', S.share.paused); sendState(); syncButtons();
  }
  function leaseTick() {
    if (S.ctl.on) { if (Date.now() > S.ctl.until) ctlStop('time'); else syncButtons(); }
    if (S.rec) syncButtons();
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
  // Where a video frame really sits inside its element (object-fit contain or cover).
  function fit(video) {
    var r = video.getBoundingClientRect(), vw = video.videoWidth, vh = video.videoHeight; if (!vw || !vh) return null;
    var cover = getComputedStyle(video).objectFit === 'cover', sc = cover ? Math.max(r.width / vw, r.height / vh) : Math.min(r.width / vw, r.height / vh);
    var w = vw * sc, h = vh * sc; return { r: r, sc: sc, ox: (r.width - w) / 2, oy: (r.height - h) / 2, w: w, h: h, vw: vw, vh: vh };
  }
  function ripple(layer, video, x, y, label, cls) {
    var f = fit(video) || { ox: 0, oy: 0, w: video.clientWidth, h: video.clientHeight };
    var d = document.createElement('i'); d.className = 'cw-ping' + (cls ? ' ' + cls : ''); d.style.left = (f.ox + x * f.w) + 'px'; d.style.top = (f.oy + y * f.h) + 'px';
    if (label) d.dataset.label = label;
    layer.appendChild(d); setTimeout(function () { d.remove(); }, 1600);
  }
  // Outline of a captured area (x, y, w, h are 0..1 of the video frame).
  function outline(layer, video, b, label) {
    var f = fit(video); if (!f) return;
    var d = document.createElement('i'); d.className = 'cw-hl';
    d.style.left = (f.ox + b.x * f.w) + 'px'; d.style.top = (f.oy + b.y * f.h) + 'px'; d.style.width = (b.w * f.w) + 'px'; d.style.height = (b.h * f.h) + 'px';
    if (label) d.dataset.label = label;
    layer.appendChild(d); setTimeout(function () { d.remove(); }, 2600);
  }

  // ───────── pointer on their screen / camera: click = point (or click, with control) · drag = capture an area ─────────
  function gesture(video, box, onClick, src) {
    var g = null;
    video.addEventListener('pointerdown', function (ev) {
      if (ev.button !== 0 || !video.videoWidth) return;
      g = { x: ev.clientX, y: ev.clientY, id: ev.pointerId, moved: false }; try { video.setPointerCapture(ev.pointerId); } catch (e) {}
      ev.preventDefault();
    });
    video.addEventListener('pointermove', function (ev) {
      if (!g || ev.pointerId !== g.id) return;
      if (!g.moved && Math.abs(ev.clientX - g.x) + Math.abs(ev.clientY - g.y) < 10) return;
      g.moved = true; var pr = box.parentNode.getBoundingClientRect();
      box.style.left = (Math.min(g.x, ev.clientX) - pr.left) + 'px'; box.style.top = (Math.min(g.y, ev.clientY) - pr.top) + 'px';
      box.style.width = Math.abs(ev.clientX - g.x) + 'px'; box.style.height = Math.abs(ev.clientY - g.y) + 'px'; box.classList.remove('cw-h');
    });
    function up(ev) {
      if (!g || ev.pointerId !== g.id) return;
      var start = g; g = null; box.classList.add('cw-h');
      if (ev.type === 'pointercancel') return;
      if (start.moved) snipArea(video, start, ev, src);
      else if (S.snip) banner('snip', '✂️ Drag over the part you want to capture.', 'info', 2500);
      else onClick(ev);
    }
    video.addEventListener('pointerup', up); video.addEventListener('pointercancel', up);
  }
  var lastTap = 0;
  gesture(el.rscreen, $('.cw-screen .cw-snipbox', R), function (ev) {
    var p = videoPoint(el.rscreen, ev); if (!p) return;
    var x = +p.x.toFixed(4), y = +p.y.toFixed(4), dbl = Date.now() - lastTap < 350; lastTap = Date.now();
    if (S.rc === 'on') { ripple(el.rptrs, el.rscreen, x, y, '', 'ctl'); send({ t: 'rc', op: dbl ? 'dbl' : 'click', x: x, y: y }); }
    else { ripple(el.rptrs, el.rscreen, x, y); send({ t: 'ptr', x: x, y: y }); }
    $('.cw-hint', el.screen).classList.add('cw-h');
  }, 'screen');
  gesture(el.rcam, $('.cw-peer .cw-snipbox', R), function () {}, 'cam');

  // What captures and recordings use: their screen, else their camera, else my own share.
  function target() {
    if (S.remote.screen && el.rscreen.videoWidth && !S.remote.screen.paused) return { v: el.rscreen, src: 'screen' };
    if (S.remote.cam && el.rcam.videoWidth) return { v: el.rcam, src: 'cam' };
    if (S.share && el.lscreen.videoWidth && !S.share.paused) return { v: el.lscreen, src: 'own' };
    return null;
  }
  function whose(src) {
    if (src === 'own') return 'my ' + (S.share && S.share.surface === 'camera' ? 'camera' : 'screen');
    return first(S.peer.name) + '’s ' + (src === 'cam' ? 'camera' : (S.remote.screen && S.remote.screen.surface === 'camera' ? 'camera view' : 'screen'));
  }
  function uploadMedia(blob, name, body, extra) {
    if (!S.thread) return Promise.resolve({ ok: false, message: 'This call is not linked to a chat yet — try again in a moment.' });
    var f = new FormData(); f.append('thread', S.thread); f.append('file', blob, name); f.append('body', body);
    Object.keys(extra || {}).forEach(function (k) { f.append(k, extra[k]); });
    return fetch('/chat/upload/', { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest' }, body: f })
      .then(function (r) { return r.json(); }).catch(function () { return { ok: false, message: 'No connection to TapTap.' }; });
  }
  function encodeImg(c) {
    return new Promise(function (res) { c.toBlob(function (b) { if (b && b.type === 'image/webp') res(b); else c.toBlob(res, 'image/jpeg', 0.88); }, 'image/webp', 0.88); });
  }
  function flash(node) { node.classList.add('flash'); setTimeout(function () { node.classList.remove('flash'); }, 350); }

  function snipArea(video, a, b, src) {
    var f = fit(video); if (!f) return;
    function vx(cx) { return Math.max(0, Math.min(f.vw, (cx - f.r.left - f.ox) / f.sc)); }
    function vy(cy) { return Math.max(0, Math.min(f.vh, (cy - f.r.top - f.oy) / f.sc)); }
    var x1 = vx(Math.min(a.x, b.clientX)), x2 = vx(Math.max(a.x, b.clientX)), y1 = vy(Math.min(a.y, b.clientY)), y2 = vy(Math.max(a.y, b.clientY));
    var sw = x2 - x1, sh = y2 - y1;
    if (sw < 8 || sh < 8) return banner('snip', 'That area is too small — drag a bigger box.', 'info', 2500);
    var sc = Math.min(1, 1920 / Math.max(sw, sh)), c = document.createElement('canvas');
    c.width = Math.round(sw * sc); c.height = Math.round(sh * sc); c.getContext('2d').drawImage(video, x1, y1, sw, sh, 0, 0, c.width, c.height);
    var box = { x: +(x1 / f.vw).toFixed(4), y: +(y1 / f.vh).toFixed(4), w: +(sw / f.vw).toFixed(4), h: +(sh / f.vh).toFixed(4) };
    outline(video === el.rscreen ? el.rptrs : video.parentNode, video, box, '✂️');
    S.snip = false; banner('snip', ''); syncButtons();
    encodeImg(c).then(function (blob) {
      if (!blob) return;
      return uploadMedia(blob, 'area.' + (blob.type.split('/')[1] || 'jpg'), '✂️ Highlighted area of ' + whose(src)).then(function (d) {
        if (d.ok) { send({ t: 'hl', src: src, box: box }); banner('snap', '✂️ Area saved to the chat — deleted automatically after ' + (D.imageDays || 7) + ' days', 'info', 3500); }
        else banner('snap', esc(d.message || 'Could not save that area.'), 'warn', 5000);
      });
    });
  }

  function snapshot() {
    var t = target(); if (!t) return banner('snap', 'Nothing to capture yet.', 'info', 2500);
    var v = t.v, c = document.createElement('canvas'), sc = Math.min(1, 1920 / Math.max(v.videoWidth, v.videoHeight));
    c.width = Math.round(v.videoWidth * sc); c.height = Math.round(v.videoHeight * sc); c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
    flash(t.src === 'screen' ? el.screen : t.src === 'cam' ? el.peer : el.myshare);
    encodeImg(c).then(function (blob) {
      if (!blob) return;
      return uploadMedia(blob, 'snapshot.' + (blob.type.split('/')[1] || 'jpg'), '📸 Snapshot of ' + whose(t.src)).then(function (d) {
        if (d.ok) { if (t.src !== 'own') send({ t: 'snap' }); banner('snap', '📸 Saved to the chat — deleted automatically after ' + (D.imageDays || 7) + ' days', 'info', 4000); }
        else banner('snap', esc(d.message || 'Could not save the snapshot.'), 'warn', 5000);
      });
    });
  }

  // ───────── short recording: video only, up to REC_MS, saved to the chat as a clip ─────────
  function recMime() {
    if (!window.MediaRecorder) return null;
    var list = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm', 'video/mp4;codecs=avc1', 'video/mp4'];
    for (var i = 0; i < list.length; i++) { try { if (MediaRecorder.isTypeSupported(list[i])) return list[i]; } catch (e) {} }
    return '';
  }
  function recToggle() {
    if (S.rec) return recStop();
    var mime = recMime(); if (mime === null) return banner('rec', '🎬 This browser cannot record. Try Chrome, Edge or Firefox.', 'warn', 4000);
    var t = target(), track = t && t.v.srcObject && t.v.srcObject.getVideoTracks()[0];
    if (!track || track.readyState !== 'live') return banner('rec', 'Nothing to record yet.', 'info', 2500);
    var rec, chunks = [], size = 0;
    try { rec = new MediaRecorder(new MediaStream([track]), mime ? { mimeType: mime, videoBitsPerSecond: 1500000 } : { videoBitsPerSecond: 1500000 }); }
    catch (e) { return banner('rec', '🎬 Could not start recording (' + esc(e.name || e) + ').', 'warn', 4000); }
    var info = { rec: rec, at: Date.now(), src: t.src, w: t.v.videoWidth, h: t.v.videoHeight, mime: (rec.mimeType || mime || 'video/webm').split(';')[0] };
    S.rec = info;
    rec.ondataavailable = function (e) {
      if (!e.data || !e.data.size) return; size += e.data.size; chunks.push(e.data);
      if (size > VIDEO_MAX * 0.95 && rec.state === 'recording') { banner('rec', '🎬 Size limit reached — saving the clip.', 'info', 3000); rec.stop(); }
    };
    rec.onstop = function () { recSave(chunks, info); };
    rec.start(1000);
    if (t.src !== 'own') send({ t: 'rec', on: true, src: t.src, secs: Math.round(REC_MS / 1000) });
    S.timers.rec = setTimeout(function () { if (rec.state === 'recording') rec.stop(); }, REC_MS);
    banner('rec', '🔴 Recording ' + esc(whose(t.src)) + ' (video only, up to ' + Math.round(REC_MS / 1000) + ' s) — <button class="cw-link" data-a="rec">stop and save</button>', 'warn');
    tone([880], 0.12, 0, 0.04); syncButtons();
  }
  function recStop() { if (S.rec && S.rec.rec.state === 'recording') S.rec.rec.stop(); }
  function recSave(chunks, r) {
    clearTimeout(S.timers.rec); if (S.rec === r) S.rec = null; banner('rec', ''); syncButtons();
    if (r.src !== 'own') send({ t: 'rec', on: false });
    var secs = Math.max(1, Math.round((Date.now() - r.at) / 1000));
    var blob = new Blob(chunks, { type: r.mime || 'video/webm' }); if (!blob.size) return banner('rec', '🎬 The recording was empty.', 'warn', 4000);
    if (blob.size > VIDEO_MAX) return banner('rec', '🎬 The clip is too big to save. Record a shorter one.', 'warn', 5000);
    banner('recsave', '🎬 Saving the clip to the chat…', 'info');
    uploadMedia(blob, 'recording.' + (/mp4/.test(r.mime) ? 'mp4' : 'webm'), '🎬 Recording of ' + whose(r.src) + ' · ' + clock(secs * 1000), { kind: 'video', w: r.w || 0, h: r.h || 0 })
      .then(function (d) {
        if (d.ok) banner('recsave', '🎬 Clip saved to the chat — deleted automatically after ' + (D.imageDays || 7) + ' days', 'info', 4000);
        else banner('recsave', esc(d.message || 'Could not save the clip.'), 'warn', 6000);
      });
  }

  // ───────── remote control of a shared TapTap tab ─────────
  // As the sharer: the other person's clicks/keys arrive here and go to the shared tab (chat.js), matched by its capture handle.
  function rcMode(on) { if (S.share) bcast({ t: 'rc-mode', on: !!on, handle: S.share.handle, who: first(S.peer.name) }); }
  function ctlAllow() {
    banner('rcask', '');
    if (!canControl()) { send({ t: 'rc-no', why: 'Control works only while a TapTap tab is shared from Chrome or Edge.' }); return; }
    S.ctl = { on: true, since: Date.now(), until: Date.now() + CTL_MS, n: 0, sec: 0 };
    rcMode(true); send({ t: 'rc-ok', mins: Math.round(CTL_MS / 60000) }); sendState(); syncButtons();
    banner('ctl', '🖱 <b>' + esc(first(S.peer.name)) + ' is controlling your TapTap tab.</b> Their clicks show as ripples. Press <b>Esc</b> in that tab or <button class="cw-link" data-a="ctlstop">take back control</button>.', 'ask');
  }
  function ctlStop(why) {
    if (!S.ctl.on) return;
    var secs = Math.round((Date.now() - S.ctl.since) / 1000);
    S.ctl.on = false; rcMode(false); send({ t: 'rc-off', why: why }); banner('ctl', ''); sendState(); syncButtons();
    if (S.cid) act('log', { event: 'control', secs: secs });
    var msg = { you: 'You took back control.', time: 'Remote control ended after ' + Math.round(CTL_MS / 60000) + ' minutes.', released: first(S.peer.name) + ' gave control back.',
                pause: 'Remote control stopped while sharing is paused.', share: 'Remote control ended with the screen share.', left: 'Remote control stopped — the shared tab was closed.' }[why];
    if (msg && !S.ended) banner('ctlend', '🖱 ' + esc(msg), 'info', 4000);
  }
  function ctlRun(m) {
    if (!S.ctl.on || !canControl()) { send({ t: 'rc-off', why: 'gone' }); return; }
    var sec = Math.floor(Date.now() / 1000); if (S.ctl.sec !== sec) { S.ctl.sec = sec; S.ctl.n = 0; }
    if (++S.ctl.n > 30) return;                                       // a flood of events is dropped, not queued
    var op = String(m.op || ''); if (['click', 'dbl', 'scroll', 'key', 'text'].indexOf(op) < 0) return;
    var out = { t: 'rc', handle: S.share.handle, op: op, who: first(S.peer.name) };
    if (typeof m.x === 'number' && typeof m.y === 'number') { out.x = Math.max(0, Math.min(1, m.x)); out.y = Math.max(0, Math.min(1, m.y)); }
    if (op === 'scroll') { out.dx = Math.max(-2000, Math.min(2000, +m.dx || 0)); out.dy = Math.max(-2000, Math.min(2000, +m.dy || 0)); }
    if (op === 'key') { out.key = String(m.key || '').slice(0, 12); out.shift = !!m.shift; }
    if (op === 'text') out.text = String(m.text || '').slice(0, 500);
    if ((op === 'click' || op === 'dbl') && out.x != null) ripple(el.lptrs, el.lscreen, out.x, out.y, first(S.peer.name), 'ctl');
    bcast(out);
  }
  // As the viewer.
  function rcAsk() {
    if (S.rc === 'on') { send({ t: 'rc-release' }); S.rc = 'none'; banner('rc', ''); syncButtons(); return; }
    if (S.rc === 'asked' && Date.now() - S.rcAt < 30000) return banner('rc2', 'You already asked — give them a moment.', 'info', 3000);
    S.rc = 'asked'; S.rcAt = Date.now(); send({ t: 'rc-ask' }); syncButtons();
    banner('rc2', '🖱 Asked ' + esc(first(S.peer.name)) + ' for control of their TapTap tab.', 'info', 3500);
    clearTimeout(S.timers.rcask); S.timers.rcask = setTimeout(function () { if (S.rc === 'asked') { S.rc = 'none'; syncButtons(); } }, 30000);
  }
  function rcOff(text) { if (S.rc === 'none') return; S.rc = 'none'; banner('rc', ''); if (text) banner('rc2', '🖱 ' + esc(text), 'info', 4000); syncButtons(); }
  var wheel = { dx: 0, dy: 0, t: null, x: 0.5, y: 0.5 };
  el.rscreen.addEventListener('wheel', function (ev) {
    if (S.rc !== 'on') return; ev.preventDefault();
    var p = videoPoint(el.rscreen, ev); if (p) { wheel.x = +p.x.toFixed(4); wheel.y = +p.y.toFixed(4); }
    var k = ev.deltaMode === 1 ? 32 : ev.deltaMode === 2 ? 600 : 1; wheel.dx += ev.deltaX * k; wheel.dy += ev.deltaY * k;
    if (!wheel.t) wheel.t = setTimeout(function () {
      send({ t: 'rc', op: 'scroll', x: wheel.x, y: wheel.y, dx: Math.round(wheel.dx), dy: Math.round(wheel.dy) }); wheel.dx = wheel.dy = 0; wheel.t = null;
    }, 70);
  }, { passive: false });
  var RC_KEYS = ['Backspace', 'Delete', 'Enter', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'];
  document.addEventListener('paste', function (e) {
    if (S.rc !== 'on' || (e.target.closest && e.target.closest('input,textarea'))) return;
    var t = (e.clipboardData && e.clipboardData.getData('text/plain')) || ''; if (!t) return;
    e.preventDefault(); send({ t: 'rc', op: 'text', text: t.slice(0, 500) });
  });

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
      S.remote.mic = m.mic; S.remote.cam = m.cam; var was = S.remote.screen, wasFacing = S.remote.facing; S.remote.screen = m.screen; S.remote.facing = m.facing || '';
      el.pmic.classList.toggle('cw-h', !!m.mic); el.rcam.classList.toggle('cw-h', !m.cam); el.peer.classList.toggle('has-video', !!m.cam);
      el.pfacing.classList.toggle('cw-h', !(m.cam && m.facing === 'environment'));
      if (m.cam && wasFacing && m.facing && m.facing !== wasFacing) banner('pflip', esc(first(S.peer.name)) + ' switched to the ' + (m.facing === 'environment' ? 'back' : 'front') + ' camera', 'info', 2500);
      if (S.rc !== 'none' && !(m.screen && m.screen.rc && !m.screen.paused)) rcOff(m.screen ? '' : 'Control ended — they stopped sharing.');
      if (S.rec && ((S.rec.src === 'screen' && !m.screen) || (S.rec.src === 'cam' && !m.cam))) recStop();
      el.screen.classList.toggle('cw-h', !m.screen); R.classList.toggle('viewing', !!m.screen);
      el.curtain.classList.toggle('cw-h', !(m.screen && m.screen.paused));
      if (m.screen && !was) { watermark(); S.timers.wm = setInterval(watermark, 60000); $('.cw-hint', el.screen).classList.remove('cw-h'); banner('ask', ''); tone([784, 1046], 0.15, 0.1, 0.04); }
      if (!m.screen && was) clearInterval(S.timers.wm);
      if (m.screen) { var g = GRADE[m.screen.surface]; banner('vgrade', g ? first(S.peer.name) + ' is sharing <b>' + g[1].slice(3) + '</b>' : '', 'info', 4000); }
      syncButtons();
    }
    else if (m.t === 'rc' && S.share) ctlRun(m);
    else if (m.t === 'rc-ask') {
      if (!canControl()) { send({ t: 'rc-no', why: S.share ? 'Control works only on a shared TapTap tab (Chrome or Edge).' : 'They need to share a TapTap tab first.' }); return; }
      if (S.ctl.on) return;
      tone([880, 988], 0.15, 0.12, 0.05);
      banner('rcask', '🖱 <b>' + esc(first(S.peer.name)) + ' asks to control your TapTap tab</b> to help you. They can click, scroll and type there — never in passwords, the chat, or other websites. ' +
        '<button class="cw-link" data-a="ctlallow">Allow for ' + Math.round(CTL_MS / 60000) + ' min</button> · <button class="cw-link" data-a="ctlno">No</button>', 'ask');
    }
    else if (m.t === 'rc-release') ctlStop('released');
    else if (m.t === 'rc-ok') {
      if (S.rc !== 'asked') return;                 // only an answer to a request we actually made
      clearTimeout(S.timers.rcask); S.rc = 'on'; syncButtons(); tone([660, 880], 0.15, 0.1, 0.05);
      banner('rc', '🖱 <b>You control ' + esc(first(S.peer.name)) + '’s TapTap tab.</b> Click, scroll and type on their screen — every click shows as a ripple. Drag still captures an area. <button class="cw-link" data-a="rc">Give back</button>', 'ask');
    }
    else if (m.t === 'rc-no') { clearTimeout(S.timers.rcask); S.rc = 'none'; syncButtons(); banner('rc2', '🖱 ' + esc(m.why || (first(S.peer.name) + ' would rather keep control.')), 'info', 4500); }
    else if (m.t === 'rc-off') rcOff({ you: first(S.peer.name) + ' took back control.', time: 'Control time is up.', pause: 'They paused sharing.', share: 'They stopped sharing.', left: 'The shared tab was closed.', gone: 'Control has ended.' }[m.why] || '');
    else if (m.t === 'rc-res') { if (m.why) banner('rcres', (m.ok ? 'ℹ️ ' : '🚫 ') + esc(m.why), m.ok ? 'info' : 'warn', 3000); }
    else if (m.t === 'hl' && m.box) {
      var hb = { x: +m.box.x || 0, y: +m.box.y || 0, w: +m.box.w || 0, h: +m.box.h || 0 }, lbl = '✂️ ' + first(S.peer.name);
      if (m.src === 'screen' && S.share) { outline(el.lptrs, el.lscreen, hb, lbl); if (S.share.surface === 'browser') bcast({ t: 'hl', handle: S.share.handle, box: hb, who: first(S.peer.name) }); }
      else if (m.src === 'cam' && S.cam) outline(el.self, el.lcam, hb, lbl);
      banner('snapped', '✂️ ' + esc(first(S.peer.name)) + ' captured an area of your ' + (m.src === 'cam' ? 'camera' : 'screen') + ' to the chat.', 'warn', 5000);
    }
    else if (m.t === 'rec') {
      el.myshare.classList.toggle('recorded', !!m.on && m.src === 'screen'); el.self.classList.toggle('recorded', !!m.on && m.src === 'cam');
      if (m.on) { tone([988, 784], 0.15, 0.12, 0.05); banner('recd', '🔴 <b>' + esc(first(S.peer.name)) + ' is recording your ' + (m.src === 'cam' ? 'camera' : 'screen') + '</b> (video only, up to ' + (+m.secs || 30) + ' s) — it will be saved to the chat. <button class="cw-link" data-a="recno">Ask to stop</button>', 'danger'); }
      else banner('recd', '');
    }
    else if (m.t === 'recstop') { if (S.rec) { recStop(); banner('rec2', esc(first(S.peer.name)) + ' asked you to stop recording — the clip so far is saved.', 'info', 4500); } }
    else if (m.t === 'ptr' && S.share) {
      ripple(el.lptrs, el.lscreen, m.x, m.y, first(S.peer.name));
      bcast({ t: 'ptr', x: m.x, y: m.y, handle: S.share.handle, surface: S.share.surface, who: first(S.peer.name) });
    }
    else if (m.t === 'ask') {
      if (S.share) return;
      tone([880, 988], 0.15, 0.12, 0.05);
      banner('ask', '👀 <b>' + esc(first(S.peer.name)) + ' asks to see your screen.</b> ' +
        (CAN_DISPLAY ? '<button class="cw-link" data-a="share-tab">Share a TapTap tab</button> · <button class="cw-link" data-a="share-any">Something else</button> · ' : '') +
        (canCamShare() ? '<button class="cw-link" data-a="share-cam">Show with camera</button> · ' : '') + '<button class="cw-link" data-a="askno">Not now</button>', 'ask');
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
    else if (a === 'share-cam') { banner('ask', ''); if (S.share) stopShare(); startCamShare(); }
    else if (a === 'flip') flipCam();
    else if (a === 'snip') { S.snip = !S.snip; syncButtons(); banner('snip', S.snip ? '✂️ Drag over the part of their ' + (S.remote.screen ? 'screen' : 'camera') + ' you want to send to the chat.' : '', 'info', 3500); }
    else if (a === 'rec') recToggle();
    else if (a === 'recno') { send({ t: 'recstop' }); banner('recd', '🔴 Asked ' + esc(first(S.peer.name)) + ' to stop recording.', 'danger', 3000); }
    else if (a === 'rc') rcAsk();
    else if (a === 'ctlallow') ctlAllow();
    else if (a === 'ctlno') { banner('rcask', ''); send({ t: 'rc-no' }); }
    else if (a === 'ctlstop') ctlStop('you');
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
    if (S.rc === 'on') {                       // while controlling, the keyboard types on their tab
      if (e.ctrlKey || e.metaKey || e.altKey) return;                    // shortcuts stay local (paste is forwarded above)
      if (e.key.length === 1 || RC_KEYS.indexOf(e.key) >= 0) { e.preventDefault(); send({ t: 'rc', op: 'key', key: e.key, shift: e.shiftKey }); }
      return;
    }
    if (e.key === 'Escape' && S.snip) { S.snip = false; syncButtons(); banner('snip', ''); }
    if (e.key === 'm' || e.key === 'M') toggleMic();
    if (e.key === 'v' || e.key === 'V') toggleCam();
    if (e.key === 'f' || e.key === 'F') flipCam();
  });

  // ───────── ending ─────────
  var REASON = { ended: 'Call ended', missed: 'No answer', declined: 'Call declined', cancelled: 'Call cancelled', lost: 'The connection was lost',
                 failed: 'Could not connect — your networks may block direct calls. Ask your TapTap admin to set up a TURN relay.', gone: 'Call ended' };
  function hangup(reason, remote) {
    if (S.ended) return; S.ended = true;
    ctlStop('share'); recStop();                // a recording in progress is still saved to the chat
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
    if (m.t === 'hello?' && S.share) { bcast({ t: 'shield', on: true, handle: S.share.handle, surface: S.share.surface }); if (S.ctl.on) rcMode(true); }
    // from the shared tab: its owner pressed Esc / "Take back control", or a command result to pass on
    if (m.t === 'rc-stop' && S.share && m.handle === S.share.handle) ctlStop('you');
    if (m.t === 'rc-res' && S.share && m.handle === S.share.handle && S.ctl.on) send({ t: 'rc-res', ok: !!m.ok, why: String(m.why || '').slice(0, 120) });
  };
  syncButtons(); listCams();
  if (S.role === 'callee') startCallee(); else startCaller();
})();
