import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:provider/provider.dart';

import '../app/api.dart';
import '../app/sounds.dart';
import '../app/state.dart';
import '../push/push.dart';
import 'call_engine.dart';

/// Opens a call screen (outgoing from a conversation, or joining a ringing call).
Future<void> openCall(BuildContext context, CallEngine engine) async {
  final app = context.read<AppState>();
  if (app.activeCall != null) return;
  app.activeCall = engine.callId ?? 'outgoing';
  await Sounds.stopAll();
  await Navigator.of(context, rootNavigator: true).push(MaterialPageRoute(
    fullscreenDialog: true,
    builder: (_) => ChangeNotifierProvider.value(value: engine, child: const CallScreen()),
  ));
  app.activeCall = null;
  Push.endCallUi();
}

class CallScreen extends StatefulWidget {
  const CallScreen({super.key});
  @override
  State<CallScreen> createState() => _CallScreenState();
}

class _CallScreenState extends State<CallScreen> {
  bool _closing = false;
  late final CallEngine _e;

  @override
  void initState() {
    super.initState();
    final app = context.read<AppState>();
    final e = _e = context.read<CallEngine>();
    e.start(myName: '${app.user['name'] ?? 'Me'}', myInitials: '${app.user['initials'] ?? ''}');
    e.addListener(_watch);
  }

  void _watch() {
    final e = _e;
    if (e.ended && !_closing) {
      _closing = true;
      HapticFeedback.mediumImpact();
      Timer(const Duration(milliseconds: 1800), () {
        if (mounted) Navigator.of(context).pop();
      });
    }
  }

  @override
  void dispose() {
    _e.removeListener(_watch);
    _e.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final e = context.watch<CallEngine>();
    final app = context.read<AppState>();
    final showVideo = e.remoteScreen || e.remoteCam;
    final initials = e.peerInitials.isNotEmpty ? e.peerInitials : _initials(e.peerName);
    return PopScope(
      canPop: e.ended,
      child: Scaffold(
        backgroundColor: const Color(0xFF0B1220),
        body: Stack(children: [
          Positioned.fill(
            child: DecoratedBox(
              decoration: BoxDecoration(
                gradient: RadialGradient(
                  center: const Alignment(0, -.8),
                  radius: 1.2,
                  colors: [app.brand.withValues(alpha: .55), const Color(0xFF0B1220)],
                ),
              ),
            ),
          ),
          if (showVideo)
            Positioned.fill(
              child: RTCVideoView(e.remoteScreen ? e.remoteScreenView : e.remoteCamView,
                  objectFit: e.remoteScreen
                      ? RTCVideoViewObjectFit.RTCVideoViewObjectFitContain
                      : RTCVideoViewObjectFit.RTCVideoViewObjectFitCover),
            ),
          SafeArea(
            child: Column(children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
                child: Row(children: [
                  const Icon(Icons.lock, size: 14, color: Colors.white70),
                  const SizedBox(width: 6),
                  const Text('Encrypted', style: TextStyle(color: Colors.white70, fontSize: 12, fontWeight: FontWeight.w600)),
                  const Spacer(),
                  if (e.phase == CallPhase.live || e.phase == CallPhase.reconnecting)
                    Text(e.clock, style: const TextStyle(color: Colors.white, fontFeatures: [FontFeature.tabularFigures()], fontWeight: FontWeight.w700)),
                ]),
              ),
              Expanded(
                child: showVideo
                    ? Align(
                        alignment: Alignment.topCenter,
                        child: Padding(
                          padding: const EdgeInsets.only(top: 12),
                          child: _Pill(text: '${e.peerName} · ${e.remoteScreen ? 'sharing their screen' : 'camera'}'),
                        ),
                      )
                    : Column(mainAxisAlignment: MainAxisAlignment.center, children: [
                        _Pulse(active: e.phase == CallPhase.ringing || e.phase == CallPhase.connecting, color: app.brand,
                            child: CircleAvatar(
                              radius: 64,
                              backgroundColor: app.brand,
                              child: Text(initials, style: const TextStyle(fontSize: 44, fontWeight: FontWeight.w800, color: Colors.white)),
                            )),
                        const SizedBox(height: 22),
                        Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 24),
                          child: Text(e.peerName, textAlign: TextAlign.center,
                              style: const TextStyle(color: Colors.white, fontSize: 26, fontWeight: FontWeight.w800)),
                        ),
                        const SizedBox(height: 8),
                        Text(e.error ?? e.status, textAlign: TextAlign.center,
                            style: TextStyle(color: e.error != null ? const Color(0xFFFCA5A5) : Colors.white70, fontSize: 15)),
                        if ('${e.customerCard?['line'] ?? ''}'.isNotEmpty) ...[
                          const SizedBox(height: 16),
                          CustomerInfo(line: '${e.customerCard!['line']}', why: '${e.customerCard!['why'] ?? ''}'),
                        ],
                      ]),
              ),
              if (e.camOn)
                Align(
                  alignment: Alignment.centerRight,
                  child: Container(
                    width: 104,
                    height: 148,
                    margin: const EdgeInsets.only(right: 16, bottom: 12),
                    clipBehavior: Clip.antiAlias,
                    decoration: BoxDecoration(borderRadius: BorderRadius.circular(16), border: Border.all(color: Colors.white24)),
                    child: RTCVideoView(e.localView, mirror: e.frontCam, objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitCover),
                  ),
                ),
              if (!e.ended)
                Padding(
                  padding: const EdgeInsets.fromLTRB(12, 0, 12, 28),
                  child: Wrap(alignment: WrapAlignment.center, spacing: 18, runSpacing: 16, children: [
                    _Btn(icon: e.micOn ? Icons.mic : Icons.mic_off, label: e.micOn ? 'Mute' : 'Unmute', on: !e.micOn, onTap: e.toggleMic),
                    _Btn(icon: Icons.volume_up, label: 'Speaker', on: e.speakerOn, onTap: e.toggleSpeaker),
                    _Btn(icon: e.camOn ? Icons.videocam : Icons.videocam_off, label: 'Camera', on: e.camOn, onTap: e.toggleCamera),
                    if (e.camOn) _Btn(icon: Icons.cameraswitch, label: 'Flip', onTap: e.flipCamera),
                    _Btn(icon: Icons.call_end, label: e.liveAt == null && e.caller ? 'Cancel' : 'End', danger: true, onTap: () => e.hangup()),
                  ]),
                ),
            ]),
          ),
        ]),
      ),
    );
  }
}

String _initials(String name) {
  final p = name.split(RegExp(r'[\s·]+')).where((x) => x.isNotEmpty && RegExp(r'[A-Za-z]').hasMatch(x[0])).toList();
  if (p.isEmpty) return '☎';
  return (p[0][0] + (p.length > 1 ? p[1][0] : '')).toUpperCase();
}

class _Btn extends StatelessWidget {
  const _Btn({required this.icon, required this.label, required this.onTap, this.on = false, this.danger = false});
  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final bool on, danger;
  @override
  Widget build(BuildContext context) {
    final bg = danger ? const Color(0xFFEF4444) : (on ? Colors.white : Colors.white.withValues(alpha: .14));
    final fg = danger ? Colors.white : (on ? const Color(0xFF0B1220) : Colors.white);
    return GestureDetector(
      onTap: () {
        HapticFeedback.selectionClick();
        onTap();
      },
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        AnimatedContainer(
          duration: const Duration(milliseconds: 180),
          width: 66,
          height: 66,
          decoration: BoxDecoration(color: bg, shape: BoxShape.circle),
          child: Icon(icon, color: fg, size: 28),
        ),
        const SizedBox(height: 7),
        Text(label, style: const TextStyle(color: Colors.white, fontSize: 12, fontWeight: FontWeight.w600)),
      ]),
    );
  }
}

class _Pill extends StatelessWidget {
  const _Pill({required this.text});
  final String text;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
        decoration: BoxDecoration(color: Colors.black54, borderRadius: BorderRadius.circular(999)),
        child: Text(text, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
      );
}

/// Soft rings around the avatar while it rings.
class _Pulse extends StatefulWidget {
  const _Pulse({required this.child, required this.active, required this.color});
  final Widget child;
  final bool active;
  final Color color;
  @override
  State<_Pulse> createState() => _PulseState();
}

class _PulseState extends State<_Pulse> with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 2400))..repeat();
  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 240,
      height: 240,
      child: AnimatedBuilder(
        animation: _c,
        builder: (_, child) => Stack(alignment: Alignment.center, children: [
          if (widget.active)
            for (var i = 0; i < 3; i++)
              Builder(builder: (_) {
                final t = (_c.value + i / 3) % 1;
                return Container(
                  width: 128 + 110 * t,
                  height: 128 + 110 * t,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    border: Border.all(color: widget.color.withValues(alpha: (1 - t) * .7), width: 2),
                  ),
                );
              }),
          child!,
        ]),
        child: widget.child,
      ),
    );
  }
}

/// Full-screen incoming call while the app is open (when closed, the phone's own call screen shows — push.dart).
class IncomingCallScreen extends StatefulWidget {
  const IncomingCallScreen({super.key, required this.ring});
  final Ring ring;
  @override
  State<IncomingCallScreen> createState() => _IncomingCallScreenState();
}

class _IncomingCallScreenState extends State<IncomingCallScreen> {
  Timer? _check;
  bool _closed = false;

  void _close() {
    if (_closed || !mounted) return;
    _closed = true;
    _check?.cancel();
    Push.ringtone(false);
    Navigator.of(context).pop();
  }

  @override
  void initState() {
    super.initState();
    HapticFeedback.heavyImpact();
    Push.ringtone(true);
    final started = DateTime.now();
    _check = Timer.periodic(const Duration(seconds: 2), (_) {
      if (!mounted || _closed) return;
      final app = context.read<AppState>();
      // answered elsewhere / hung up — or nothing heard for longer than any call rings (a line too slow to tell us)
      if (!app.ringStillOn(widget.ring.id) || DateTime.now().difference(started).inSeconds > 50) _close();
    });
  }

  @override
  void dispose() {
    _check?.cancel();
    Push.ringtone(false);
    super.dispose();
  }

  Future<void> _decline() async {
    final id = widget.ring.id;
    _close();                                     // stop ringing at once; tell TapTap in the background
    for (var i = 0; i < 3; i++) {
      try {
        await Api.I.post('/chat/call/act/', {'action': 'decline', 'id': id});
        return;
      } on ApiError catch (e) {
        if (e.status != 0) return;                // TapTap answered (e.g. the call already ended)
        await Future.delayed(Duration(seconds: 2 * (i + 1)));
      }
    }
  }

  void _answer() {
    if (_closed) return;
    final ctx = navigatorKey.currentContext!;
    _close();
    openCall(ctx, CallEngine.incoming(id: widget.ring.id, from: widget.ring.from, video: widget.ring.video));
  }

  @override
  Widget build(BuildContext context) {
    final r = widget.ring;
    final app = context.read<AppState>();
    final accent = r.fromCustomer ? const Color(0xFF22C55E) : app.brand;
    return Scaffold(
      backgroundColor: const Color(0xFF0B1220),
      body: DecoratedBox(
        decoration: BoxDecoration(
          gradient: LinearGradient(begin: Alignment.topCenter, end: Alignment.bottomCenter,
              colors: [accent.withValues(alpha: .55), const Color(0xFF0B1220)]),
        ),
        child: SafeArea(
          child: Column(children: [
            const SizedBox(height: 40),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 7),
              decoration: BoxDecoration(color: Colors.white12, borderRadius: BorderRadius.circular(999)),
              child: Text(r.fromCustomer ? '📶 Customer on your Wi-Fi page' : (r.video ? 'TapTap video call' : 'TapTap call'),
                  style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w700)),
            ),
            const Spacer(),
            _Pulse(
                active: true,
                color: accent,
                child: CircleAvatar(
                    radius: 64,
                    backgroundColor: accent,
                    child: Text(r.initials, style: const TextStyle(fontSize: 44, fontWeight: FontWeight.w800, color: Colors.white)))),
            const SizedBox(height: 20),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 24),
              child: Text(r.from, textAlign: TextAlign.center,
                  style: const TextStyle(color: Colors.white, fontSize: 26, fontWeight: FontWeight.w800)),
            ),
            const SizedBox(height: 6),
            const Text('is calling…', style: TextStyle(color: Colors.white70, fontSize: 16)),
            if (r.info.isNotEmpty) ...[
              const SizedBox(height: 16),
              CustomerInfo(line: r.info, why: r.why),
            ],
            const Spacer(),
            Padding(
              padding: const EdgeInsets.fromLTRB(40, 0, 40, 48),
              child: Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
                _Big(icon: Icons.call_end, color: const Color(0xFFEF4444), label: 'Decline', onTap: _decline),
                _Big(icon: Icons.call, color: const Color(0xFF22C55E), label: 'Answer', onTap: _answer),
              ]),
            ),
          ]),
        ),
      ),
    );
  }
}

/// Who is calling from the Wi-Fi page: voucher and time left, MAC address — and why they are probably calling.
class CustomerInfo extends StatelessWidget {
  const CustomerInfo({super.key, required this.line, this.why = ''});
  final String line;
  final String why;
  @override
  Widget build(BuildContext context) => Container(
        margin: const EdgeInsets.symmetric(horizontal: 24),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(color: Colors.white12, borderRadius: BorderRadius.circular(14)),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          SelectableText(line,
              textAlign: TextAlign.center, style: const TextStyle(color: Colors.white, fontSize: 13.5, fontWeight: FontWeight.w700)),
          if (why.isNotEmpty) ...[
            const SizedBox(height: 4),
            Text('💡 $why', textAlign: TextAlign.center, style: const TextStyle(color: Color(0xFFFDE68A), fontSize: 12.5)),
          ],
        ]),
      );
}

class _Big extends StatelessWidget {
  const _Big({required this.icon, required this.color, required this.label, required this.onTap});
  final IconData icon;
  final Color color;
  final String label;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) => Column(children: [
        GestureDetector(
          onTap: onTap,
          child: Container(
            width: 78,
            height: 78,
            decoration: BoxDecoration(color: color, shape: BoxShape.circle, boxShadow: [BoxShadow(color: color.withValues(alpha: .5), blurRadius: 24)]),
            child: Icon(icon, color: Colors.white, size: 34),
          ),
        ),
        const SizedBox(height: 8),
        Text(label, style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w700)),
      ]);
}
