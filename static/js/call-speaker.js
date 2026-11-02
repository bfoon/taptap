/* TapTap — loudspeaker switch for calls (customer call screen and the TapTap call window).

   A web page cannot simply say "use the loudspeaker". During a call (microphone on) phones often send the
   other person's voice to the small ear speaker. This switch tries, in order:

     1. setSinkId — browsers that list audio outputs (Chrome on Android/desktop, Edge): pick the output whose name
        says speaker / loudspeaker, and the earpiece (or the default) when switched off.
     2. Web Audio — when no speaker output can be chosen, the voice is played through the page's audio engine
        instead of the media element, which Android plays on the loudspeaker, with a little extra volume. The media
        element keeps playing silently (Chrome only feeds a call's audio into Web Audio while an element plays it).

   iPhone (Safari) decides the output itself: the switch then turns the volume up and shows that the phone's own
   speaker control (or the volume buttons) is the way — there is no setting a web page can change.

   Usage:  TapSpeaker.attach(audioElement, remoteStream);  TapSpeaker.set(true).then(function (r) { r.mode, r.note });
*/
(function () {
  'use strict';
  var A = { el: null, stream: null, on: false, ctx: null, src: null, gain: null, mode: 'off' };
  var IOS = /iPhone|iPad|iPod/i.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && /Macintosh/.test(navigator.userAgent));
  var SPEAKER = /speaker|loud|haut-?parleur|altavoz|lautsprecher/i, EAR = /earpiece|receiver|handset|ear speaker|écouteur/i;

  function outputs() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return Promise.resolve([]);
    return navigator.mediaDevices.enumerateDevices().then(function (d) { return d.filter(function (x) { return x.kind === 'audiooutput'; }); })
      .catch(function () { return []; });
  }
  function webAudioOff() {
    try { if (A.src) A.src.disconnect(); if (A.gain) A.gain.disconnect(); } catch (e) {}
    A.src = A.gain = null;
    if (A.el) A.el.muted = false;
  }
  function webAudioOn(boost) {
    if (!A.stream || !A.stream.getAudioTracks().length) return false;
    try {
      A.ctx = A.ctx || new (window.AudioContext || window.webkitAudioContext)();
      if (A.ctx.resume) A.ctx.resume();
      webAudioOff();
      A.src = A.ctx.createMediaStreamSource(A.stream);
      A.gain = A.ctx.createGain(); A.gain.gain.value = boost || 1.6;
      A.src.connect(A.gain); A.gain.connect(A.ctx.destination);
      if (A.el) { A.el.muted = true; A.el.play().catch(function () {}); }   // keeps the call audio flowing into Web Audio
      return true;
    } catch (e) { webAudioOff(); return false; }
  }

  function apply() {
    var el = A.el;
    if (!el) return Promise.resolve({ mode: 'none', note: '' });
    el.volume = 1;
    if (!A.on) {
      webAudioOff();
      var back = el.setSinkId ? outputs().then(function (outs) {
        var ear = outs.filter(function (o) { return EAR.test(o.label); })[0];
        return el.setSinkId(ear ? ear.deviceId : 'default').catch(function () {});
      }) : Promise.resolve();
      return back.then(function () { A.mode = 'off'; el.play().catch(function () {}); return { mode: 'off', note: 'Speaker off' }; });
    }
    var viaSink = el.setSinkId ? outputs().then(function (outs) {
      var spk = outs.filter(function (o) { return SPEAKER.test(o.label); })[0];
      if (!spk) return false;
      webAudioOff();
      return el.setSinkId(spk.deviceId).then(function () { return true; }).catch(function () { return false; });
    }) : Promise.resolve(false);
    return viaSink.then(function (ok) {
      if (ok) { A.mode = 'sink'; el.play().catch(function () {}); return { mode: 'sink', note: 'Loudspeaker on' }; }
      if (IOS) { A.mode = 'ios'; el.play().catch(function () {}); return { mode: 'ios', note: 'iPhone picks the speaker itself — turn the volume up, or use the speaker button in Control Centre' }; }
      if (webAudioOn(1.6)) { A.mode = 'webaudio'; return { mode: 'webaudio', note: 'Loudspeaker on' }; }
      A.mode = 'none'; el.play().catch(function () {});
      return { mode: 'none', note: 'This phone does not let the page switch the speaker — use the volume buttons' };
    });
  }

  window.TapSpeaker = {
    attach: function (el, stream) { A.el = el; A.stream = stream; return apply(); },
    set: function (on) { A.on = !!on; return apply(); },
    toggle: function () { A.on = !A.on; return apply(); },
    isOn: function () { return A.on; },
    mode: function () { return A.mode; },
    stop: function () { A.on = false; webAudioOff(); try { if (A.ctx) A.ctx.close(); } catch (e) {} A.ctx = null; }
  };
})();
