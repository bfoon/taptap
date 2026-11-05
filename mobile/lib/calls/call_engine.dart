import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:wakelock_plus/wakelock_plus.dart';

import '../app/api.dart';
import '../app/sounds.dart';

enum CallPhase { starting, ringing, connecting, live, reconnecting, ended }

/// A TapTap call — the phone side of the website's call window (static/js/chat-call.js).
///
/// Same protocol, so the app can call the web, the web can call the app, and customers on the Wi-Fi page can ring
/// the app: three media slots in this order — audio, camera, screen — plus a pre-agreed data channel (id 0) for
/// small control messages (hello, mic/camera state, bye). The offer and answer travel through TapTap
/// (/chat/call/act/ + /chat/call/poll/); the voice itself goes phone ↔ phone, encrypted (DTLS-SRTP).
/// A dropped line reconnects with an ICE restart: the caller re-offers, the callee asks it to.
///
/// Slow lines (a weak Wi-Fi hop, 2G/3G, a busy hotspot) get extra patience everywhere:
///   * every signalling step (accept, offer, answer, hang-up) is tried again when the request times out, instead
///     of ending the call — only a real "no" from TapTap stops it
///   * the connection details wait for the TURN relay path (which works through any network) up to [gatherMaxMs]
///   * a first connection that is slow or fails is retried with an ICE restart for up to [connectGiveUpMs] before
///     the call is given up — it used to hang up the moment the first attempt failed
class CallEngine extends ChangeNotifier {
  CallEngine.outgoing({required this.thread, required this.title, this.video = false}) : callId = null, caller = true {
    peerName = title;
  }
  CallEngine.incoming({required String id, required String from, this.video = false})
      : callId = id,
        caller = false,
        thread = 0,
        title = from {
    peerName = from;
  }

  static const gatherMs = 4000;          // no TURN relay configured: wait this long for the network paths
  static const gatherMaxMs = 10000;      // with a relay: up to this long for its path on a slow line
  static const connectRescueMs = 12000;  // answered but not connected after this: try another path (ICE restart)
  static const connectGiveUpMs = 55000;  // … and give up only after this
  static const giveUpMs = 60000;

  final bool caller;
  final int thread;
  final String title;
  String? callId;
  bool video;

  CallPhase phase = CallPhase.starting;
  String status = 'Starting…';
  String reason = '';
  String peerName = '';
  String peerInitials = '';
  DateTime? liveAt;
  bool micOn = true;
  bool speakerOn = false;
  bool camOn = false;
  bool frontCam = true;
  bool remoteCam = false;
  bool remoteScreen = false;
  String? error;

  final remoteCamView = RTCVideoRenderer();
  final remoteScreenView = RTCVideoRenderer();
  final localView = RTCVideoRenderer();

  RTCPeerConnection? _pc;
  RTCDataChannel? _dc;
  MediaStream? _micStream, _camStream;
  RTCRtpTransceiver? _tAudio, _tCam, _tScreen;
  Map<String, dynamic> _ice = {};
  Timer? _poll, _lost, _retry, _clock, _connectWatch;
  DateTime? _answeredAt;                 // offer and answer exchanged: from here the line itself must connect
  int _relayCands = 0;                  // relay (TURN) paths found in this round of gathering
  bool _ended = false;
  int _rgen = 0, _rgenApplied = 0, _rgenSeen = 0, _raskSeen = 0;
  DateTime? _restarting, _askAt;

  Duration get elapsed => liveAt == null ? Duration.zero : DateTime.now().difference(liveAt!);

  // ───────── start ─────────
  Future<void> start({required String myName, required String myInitials}) async {
    _me = {'name': myName, 'initials': myInitials};
    await Future.wait([remoteCamView.initialize(), remoteScreenView.initialize(), localView.initialize()]);
    WakelockPlus.enable();
    try {
      await _getMic();
      if (caller) {
        await _startCaller();
      } else {
        await _startCallee();
      }
    } catch (e) {
      _fail(e is ApiError ? e.message : _mediaError(e));
    }
  }

  Map<String, String> _me = {};

  Future<void> _getMic() async {
    _micStream = await navigator.mediaDevices.getUserMedia({
      'audio': {'echoCancellation': true, 'noiseSuppression': true, 'autoGainControl': true},
      'video': false,
    });
  }

  Future<RTCPeerConnection> _makePc() async {
    final pc = await createPeerConnection({
      'iceServers': _ice['iceServers'] ?? [],
      'iceTransportPolicy': _ice['iceTransportPolicy'] ?? 'all',
      'bundlePolicy': 'max-bundle',
      'sdpSemantics': 'unified-plan',
    });
    _pc = pc;
    _dc = await pc.createDataChannel('ctl', RTCDataChannelInit()
      ..negotiated = true
      ..id = 0
      ..ordered = true);
    _dc!.onDataChannelState = (s) {
      if (s == RTCDataChannelState.RTCDataChannelOpen) {
        _send({'t': 'hello', 'name': _me['name'], 'initials': _me['initials']});
        _sendState();
      }
    };
    _dc!.onMessage = (m) {
      try {
        _onCtl(jsonDecode(m.text) as Map<String, dynamic>);
      } catch (_) {}
    };
    pc.onTrack = _onTrack;
    pc.onIceCandidate = (c) {
      final line = c.candidate ?? '';
      if (line.contains(' typ relay')) _relayCands++;
    };
    pc.onConnectionState = (s) {
      if (s == RTCPeerConnectionState.RTCPeerConnectionStateConnected) {
        _connected();
      } else if (s == RTCPeerConnectionState.RTCPeerConnectionStateDisconnected && liveAt != null) {
        _dropped();
        Timer(const Duration(milliseconds: 2500), _recover);
      } else if (s == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
        if (liveAt == null) {
          // never connected yet: on a slow line the first try often fails — try another path before giving up
          if (_answeredAt != null && DateTime.now().difference(_answeredAt!).inMilliseconds < connectGiveUpMs) {
            _set(CallPhase.connecting, 'Weak connection — trying another way…');
            _rescue(force: true);
          } else {
            hangup('failed');
          }
        } else {
          _dropped();
          _recover();
        }
      }
    };
    return pc;
  }

  Future<void> _startCaller() async {
    _set(CallPhase.ringing, 'Calling…');
    final d = await _act('start', {'thread': thread, 'video': video});
    callId = '${(d['call'] as Map)['id']}';
    _ice = (d['ice'] as Map?)?.cast<String, dynamic>() ?? {};
    peerName = '${d['title'] ?? title}';
    final pc = await _makePc();
    final mic = _micStream!.getAudioTracks().first;
    _tAudio = await pc.addTransceiver(
        track: mic,
        kind: RTCRtpMediaType.RTCRtpMediaTypeAudio,
        init: RTCRtpTransceiverInit(direction: TransceiverDirection.SendRecv, streams: [_micStream!]));
    _tCam = await pc.addTransceiver(
        kind: RTCRtpMediaType.RTCRtpMediaTypeVideo, init: RTCRtpTransceiverInit(direction: TransceiverDirection.SendRecv));
    _tScreen = await pc.addTransceiver(
        kind: RTCRtpMediaType.RTCRtpMediaTypeVideo, init: RTCRtpTransceiverInit(direction: TransceiverDirection.SendRecv));
    final offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await _gathered();
    final local = await pc.getLocalDescription();
    await _actSure('offer', {'desc': {'type': 'offer', 'sdp': local!.sdp}});
    _set(CallPhase.ringing, 'Ringing $peerName…');
    _pollIn(1000);
  }

  Future<void> _startCallee() async {
    _set(CallPhase.connecting, 'Joining…');
    final d = await _actSure('accept');
    _ice = (d['ice'] as Map?)?.cast<String, dynamic>() ?? {};
    peerName = '${d['from'] ?? peerName}';
    var offer = (d['offer'] as Map?)?.cast<String, dynamic>();
    offer ??= await _waitOffer();
    final pc = await _makePc();
    await pc.setRemoteDescription(RTCSessionDescription(offer['sdp'] as String, offer['type'] as String));
    final ts = await pc.getTransceivers();
    if (ts.length < 3) throw ApiError('This call is from an older TapTap — update the other side.');
    _tAudio = ts[0];
    _tCam = ts[1];
    _tScreen = ts[2];
    for (final t in ts) {
      await t.setDirection(TransceiverDirection.SendRecv);
    }
    await _tAudio!.sender.replaceTrack(_micStream!.getAudioTracks().first);
    final answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    await _gathered();
    final local = await pc.getLocalDescription();
    await _actSure('answer', {'desc': {'type': 'answer', 'sdp': local!.sdp}});
    _set(CallPhase.connecting, 'Connecting…');
    _watchConnect();
    _pollIn(1500);
  }

  /// The caller's offer, when it was not there yet at "accept". A slow poll is simply tried again.
  Future<Map<String, dynamic>> _waitOffer() async {
    final until = DateTime.now().add(const Duration(seconds: 30));
    while (DateTime.now().isBefore(until) && !_ended) {
      try {
        final d = await Api.I.get('/chat/call/poll/', query: {'id': callId!}, timeout: const Duration(seconds: 10));
        final c = (d['call'] as Map?)?.cast<String, dynamic>() ?? {};
        if (c['offer'] is Map) return (c['offer'] as Map).cast<String, dynamic>();
        if (c['state'] == 'ended') break;
      } on ApiError catch (e) {
        if (!_transient(e)) rethrow;
      }
      await Future.delayed(const Duration(milliseconds: 700));
    }
    throw ApiError('The call ended before it connected.');
  }

  bool get _hasTurn {
    final servers = _ice['iceServers'];
    if (servers is! List) return false;
    return servers.any((x) => x is Map && '${x['urls']}'.contains('turn'));
  }

  /// Wait for this side's network paths (non-trickle: they all travel in the offer / answer). With a TURN relay,
  /// wait for the relay path — the one that works through any NAT — which can take several seconds on a slow line.
  Future<void> _gathered() async {
    final pc = _pc!;
    _relayCands = 0;
    final t0 = DateTime.now();
    final turn = _hasTurn;
    while (!_ended) {
      if (pc.iceGatheringState == RTCIceGatheringState.RTCIceGatheringStateComplete) return;
      final ms = DateTime.now().difference(t0).inMilliseconds;
      if (turn && _relayCands > 0 && ms >= 1200) return;          // a relay path is in: enough to connect anywhere
      if (ms >= (turn ? gatherMaxMs : gatherMs)) return;
      await Future.delayed(const Duration(milliseconds: 150));
    }
  }

  Future<Map<String, dynamic>> _act(String action, [Map<String, dynamic>? extra]) {
    return Api.I.post('/chat/call/act/', {'action': action, if (callId != null) 'id': callId, ...?extra});
  }

  /// No answer from TapTap in time / no internet for a moment — worth trying again (a refusal from TapTap is not).
  static bool _transient(ApiError e) => e.status == 0 || e.status == 502 || e.status == 503 || e.status == 504;

  /// A signalling step that must get through: tried again on a slow or dropped line instead of ending the call.
  /// (Not used for "start": that would ring twice.)
  Future<Map<String, dynamic>> _actSure(String action, [Map<String, dynamic>? extra, int tries = 5, bool evenIfEnded = false]) async {
    for (var i = 0;; i++) {
      try {
        return await _act(action, extra);
      } on ApiError catch (e) {
        if (!_transient(e) || i >= tries - 1 || (_ended && !evenIfEnded)) rethrow;
        if (!_ended) {
          status = 'Weak connection — trying again…';
          notifyListeners();
        }
        await Future.delayed(Duration(milliseconds: 1000 + 1500 * i));
      }
    }
  }

  // ───────── media in ─────────
  Future<void> _onTrack(RTCTrackEvent e) async {
    if (e.track.kind == 'audio') {
      await Helper.setSpeakerphoneOn(speakerOn);      // the voice plays natively; this picks the speaker
      return;
    }
    // which slot is it? the transceivers keep the order audio · camera · screen on both sides
    var slot = 1;
    try {
      final ts = await _pc!.getTransceivers();
      final i = ts.indexWhere((t) => t.receiver.track?.id == e.track.id);
      if (i >= 0) slot = i;
    } catch (_) {}
    final s = await createLocalMediaStream('remote$slot');
    await s.addTrack(e.track);
    if (slot == 2) {
      remoteScreenView.srcObject = s;
    } else {
      remoteCamView.srcObject = s;
    }
    notifyListeners();
  }

  void _onCtl(Map<String, dynamic> m) {
    switch (m['t']) {
      case 'hello':
        if ((m['name'] ?? '').toString().isNotEmpty && caller == false) peerName = '${m['name']}';
        peerInitials = '${m['initials'] ?? peerInitials}';
        break;
      case 'state':
        remoteCam = m['cam'] == true;
        remoteScreen = m['screen'] != null && m['screen'] != false;
        break;
      case 'bye':
        _finish('ended', remote: true);
        return;
      case 'rc-ask':                                   // remote control is for TapTap on a computer, not a phone
        _send({'t': 'rc-no', 'why': 'They are on the TapTap phone app — remote control works from a computer.'});
        break;
    }
    notifyListeners();
  }

  void _send(Map<String, dynamic> m) {
    try {
      if (_dc?.state == RTCDataChannelState.RTCDataChannelOpen) _dc!.send(RTCDataChannelMessage(jsonEncode(m)));
    } catch (_) {}
  }

  void _sendState() => _send({'t': 'state', 'mic': micOn, 'cam': camOn, 'facing': frontCam ? 'user' : 'environment', 'screen': null});

  // ───────── server poll: answer, heartbeat, ended elsewhere, reconnect ─────────
  void _pollIn(int ms) {
    _poll?.cancel();
    _poll = Timer(Duration(milliseconds: ms), _pollNow);
  }

  Future<void> _pollNow() async {
    if (_ended || callId == null) return;
    Map<String, dynamic> c = {};
    try {
      final d = await Api.I.get('/chat/call/poll/', query: {'id': callId!}, timeout: const Duration(seconds: 8));
      c = (d['call'] as Map?)?.cast<String, dynamic>() ?? {};
    } catch (_) {
      return _pollIn(2000);
    }
    if (_ended) return;
    final peer = (c['peer'] as Map?)?.cast<String, dynamic>();
    if (peer != null) {
      if (caller) peerName = '${peer['name']}';
      peerInitials = '${peer['initials'] ?? ''}';
    }
    if (c['state'] == 'ended') return _finish('${c['reason'] ?? 'ended'}', remote: true);
    final pc = _pc;
    if (caller && pc != null && c['answer'] is Map && await pc.getRemoteDescription() == null) {
      final a = (c['answer'] as Map).cast<String, dynamic>();
      _set(CallPhase.connecting, 'Connecting…');
      try {
        await pc.setRemoteDescription(RTCSessionDescription(a['sdp'] as String, a['type'] as String));
        _watchConnect();
      } catch (_) {
        return hangup('failed');
      }
    }
    if (caller && c['state'] == 'connecting' && c['answer'] == null && phase == CallPhase.ringing) status = 'Answering…';
    // reconnect (ICE restart)
    if (caller && pc != null && c['reanswer'] is Map) {
      final ra = (c['reanswer'] as Map).cast<String, dynamic>();
      if (ra['gen'] == _rgen && _rgenApplied != _rgen && pc.signalingState == RTCSignalingState.RTCSignalingStateHaveLocalOffer) {
        _rgenApplied = _rgen;
        final desc = (ra['desc'] as Map).cast<String, dynamic>();
        pc.setRemoteDescription(RTCSessionDescription(desc['sdp'] as String, desc['type'] as String)).catchError((_) {
          _rgenApplied = 0;
          _restarting = null;
        });
      }
    }
    if (caller && c['rask'] is num && (c['rask'] as num) > _raskSeen) {
      _raskSeen = (c['rask'] as num).toInt();
      if (liveAt != null) {
        _restarting = null;
        _dropped();
        _restartIce();
      } else if (_answeredAt != null) {           // the other side could not connect yet: offer another path
        _restarting = null;
        _restartIce();
      }
    }
    if (!caller && pc != null && c['reoffer'] is Map) {
      final ro = (c['reoffer'] as Map).cast<String, dynamic>();
      final gen = (ro['gen'] as num?)?.toInt() ?? 0;
      if (gen > _rgenSeen && await pc.getRemoteDescription() != null) {
        _rgenSeen = gen;
        _answerRestart(gen, (ro['desc'] as Map).cast<String, dynamic>());
      }
    }
    notifyListeners();
    _pollIn(liveAt == null || phase == CallPhase.reconnecting ? 1000 : 3000);
  }

  // ───────── first connection on a slow line ─────────
  /// Offer and answer are exchanged: watch that the line itself connects, and help it along if it does not.
  void _watchConnect() {
    _answeredAt ??= DateTime.now();
    _connectWatch?.cancel();
    _connectWatch = Timer.periodic(const Duration(seconds: 3), (_) => _checkConnect());
  }

  void _checkConnect() {
    if (_ended || liveAt != null || _answeredAt == null) {
      _connectWatch?.cancel();
      return;
    }
    final ms = DateTime.now().difference(_answeredAt!).inMilliseconds;
    if (ms > connectGiveUpMs) {
      _connectWatch?.cancel();
      hangup('failed');
      return;
    }
    if (ms > connectRescueMs) {
      _set(CallPhase.connecting, 'Weak connection — still connecting…');
      _rescue();
    }
  }

  /// Not connected yet: the caller sends a fresh offer (new paths), the callee asks the caller for one.
  void _rescue({bool force = false}) {
    if (_ended) return;
    _pollIn(500);
    if (caller) {
      if (force) _restarting = null;
      _restartIce();
    } else if (force || _askAt == null || DateTime.now().difference(_askAt!).inMilliseconds > 6000) {
      _askAt = DateTime.now();
      _actSure('reask', null, 2).catchError((_) => <String, dynamic>{});
    }
  }

  // ───────── live / reconnect ─────────
  void _connected() {
    _connectWatch?.cancel();
    _lost?.cancel();
    _lost = null;
    _retry?.cancel();
    _restarting = null;
    if (phase == CallPhase.reconnecting) status = 'Reconnected';
    if (liveAt == null) {
      liveAt = DateTime.now();
      _clock = Timer.periodic(const Duration(seconds: 1), (_) => notifyListeners());
      _sendState();
    }
    _set(CallPhase.live, 'Connected');
  }

  void _dropped() {
    if (_ended) return;
    _set(CallPhase.reconnecting, 'Connection dropped — reconnecting…');
    _lost ??= Timer(const Duration(milliseconds: giveUpMs), () => hangup('lost'));
    _pollIn(800);
  }

  Future<void> _recover() async {
    final pc = _pc;
    if (_ended || pc == null || liveAt == null) return;
    if (pc.connectionState == RTCPeerConnectionState.RTCPeerConnectionStateConnected) return;
    _dropped();
    if (caller) {
      _restartIce();
    } else if (_askAt == null || DateTime.now().difference(_askAt!).inMilliseconds > 4000) {
      _askAt = DateTime.now();
      _act('reask').catchError((_) => <String, dynamic>{});
    }
    _retry?.cancel();
    _retry = Timer(const Duration(seconds: 8), () {
      _restarting = null;
      _recover();
    });
  }

  /// Call again after a network change (Wi-Fi ↔ mobile data).
  void networkChanged() {
    if (_ended || liveAt == null) return;
    _pollIn(0);
    if (caller) {
      _restarting = null;
      _restartIce();
    } else {
      _askAt = DateTime.now();
      _act('reask').catchError((_) => <String, dynamic>{});
    }
  }

  Future<void> _restartIce() async {
    final pc = _pc;
    if (pc == null || _ended) return;
    if (_restarting != null && DateTime.now().difference(_restarting!).inMilliseconds < 7500) return;
    _restarting = DateTime.now();
    final gen = ++_rgen;
    try {
      final o = await pc.createOffer({'iceRestart': true});
      await pc.setLocalDescription(o);
      await _gathered();
      if (gen != _rgen || _ended) return;
      final local = await pc.getLocalDescription();
      await _act('reoffer', {'gen': gen, 'desc': {'type': 'offer', 'sdp': local!.sdp}});
    } catch (_) {
      _restarting = null;
    }
  }

  Future<void> _answerRestart(int gen, Map<String, dynamic> desc) async {
    final pc = _pc;
    if (pc == null || _ended) return;
    try {
      if (pc.signalingState == RTCSignalingState.RTCSignalingStateHaveLocalOffer) {
        await pc.setLocalDescription(RTCSessionDescription('', 'rollback'));
      }
      await pc.setRemoteDescription(RTCSessionDescription(desc['sdp'] as String, desc['type'] as String));
      final a = await pc.createAnswer();
      await pc.setLocalDescription(a);
      await _gathered();
      if (_ended || gen != _rgenSeen) return;
      final local = await pc.getLocalDescription();
      await _act('reanswer', {'gen': gen, 'desc': {'type': 'answer', 'sdp': local!.sdp}});
    } catch (_) {
      _rgenSeen = 0;                                   // try this generation again on the next poll
    }
  }

  // ───────── buttons ─────────
  void toggleMic() {
    micOn = !micOn;
    for (final t in _micStream?.getAudioTracks() ?? <MediaStreamTrack>[]) {
      t.enabled = micOn;
    }
    _sendState();
    notifyListeners();
  }

  Future<void> toggleSpeaker() async {
    speakerOn = !speakerOn;
    await Helper.setSpeakerphoneOn(speakerOn);
    notifyListeners();
  }

  /// Show your camera (e.g. point it at the router so a colleague can see the lights).
  Future<void> toggleCamera() async {
    if (_tCam == null) return;
    if (camOn) {
      await _tCam!.sender.replaceTrack(null);
      for (final t in _camStream?.getTracks() ?? <MediaStreamTrack>[]) {
        await t.stop();
      }
      _camStream = null;
      localView.srcObject = null;
      camOn = false;
    } else {
      try {
        _camStream = await navigator.mediaDevices.getUserMedia({
          'audio': false,
          'video': {'facingMode': frontCam ? 'user' : 'environment', 'width': 640, 'height': 480, 'frameRate': 24},
        });
        await _tCam!.sender.replaceTrack(_camStream!.getVideoTracks().first);
        localView.srcObject = _camStream;
        camOn = true;
        if (!speakerOn) await toggleSpeaker();          // a video call is held away from the ear
      } catch (e) {
        error = _mediaError(e);
      }
    }
    _sendState();
    notifyListeners();
  }

  Future<void> flipCamera() async {
    frontCam = !frontCam;
    final t = _camStream?.getVideoTracks().firstOrNull;
    if (t != null) await Helper.switchCamera(t);
    _sendState();
    notifyListeners();
  }

  /// Hang up from this phone.
  Future<void> hangup([String why = 'ended']) => _finish(why);

  Future<void> _finish(String why, {bool remote = false}) async {
    if (_ended) return;
    _ended = true;
    reason = why;
    Sounds.ringback(false);
    if (!remote) {
      _send({'t': 'bye'});
      if (callId != null) {
        // tell TapTap without holding the screen on a slow line; tried again if it does not get through
        _actSure('end', {'reason': why}, 4, true).catchError((_) => <String, dynamic>{});
      }
    }
    _poll?.cancel();
    _connectWatch?.cancel();
    _lost?.cancel();
    _retry?.cancel();
    _clock?.cancel();
    try {
      for (final s in [_micStream, _camStream]) {
        for (final t in s?.getTracks() ?? <MediaStreamTrack>[]) {
          await t.stop();
        }
        await s?.dispose();
      }
      await _dc?.close();
      await _pc?.close();
    } catch (_) {}
    _pc = null;
    WakelockPlus.disable();
    Helper.setSpeakerphoneOn(false).catchError((_) {});
    _set(CallPhase.ended, _endText(why));
  }

  String _endText(String why) {
    final talk = liveAt == null ? '' : ' · ${_fmt(elapsed)}';
    switch (why) {
      case 'missed':
        return 'No answer$talk';
      case 'declined':
        return 'Declined';
      case 'cancelled':
        return 'Cancelled';
      case 'lost':
        return 'The line dropped$talk';
      case 'failed':
        return 'Could not connect';
      case 'gone':
        return 'The call has ended';
      default:
        return 'Call ended$talk';
    }
  }

  static String _fmt(Duration d) => '${d.inMinutes}:${(d.inSeconds % 60).toString().padLeft(2, '0')}';
  String get clock => _fmt(elapsed);

  void _fail(String msg) {
    error = msg;
    _finish('failed');
  }

  String _mediaError(Object e) {
    final s = '$e';
    if (s.contains('Permission') || s.contains('NotAllowed')) return 'Allow the microphone for TapTap in your phone settings.';
    if (s.contains('NotFound')) return 'No microphone was found.';
    return 'Could not start the call. ${s.length < 120 ? s : ''}';
  }

  void _set(CallPhase p, String s) {
    phase = p;
    status = s;
    if (caller) Sounds.ringback(p == CallPhase.ringing && callId != null);   // "tuu… tuu…" while it rings there
    notifyListeners();
  }

  bool get ended => _ended;

  @override
  void dispose() {
    _finish(reason.isEmpty ? 'ended' : reason);
    remoteCamView.dispose();
    remoteScreenView.dispose();
    localView.dispose();
    super.dispose();
  }
}
