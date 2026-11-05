import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// Every sound the app makes while it is open (when it is closed, the phone's own call screen and notification
/// channels make the sound — push.dart and tool/configure_platforms.py).
///
///   ringing   an incoming call: the phone's own ringtone, looped, with a buzz — until answered or declined
///   ringback  you are calling someone: the phone's "tuu… tuu…" call-progress tone, until they answer
///   notify    a message, complaint or alert arrived while TapTap is on screen (the phone's notification sound)
///
/// Played natively by MainActivity (android/…/MainActivity.kt, written by tool/configure_platforms.py) — no extra
/// package, so nothing pins the Android SDK version. On iPhone only the buzz plays (the call screen rings there).
/// One sound at a time: an incoming ring always wins over the ringback.
class Sounds {
  Sounds._();

  static const _native = MethodChannel('taptap/notifications');
  static Timer? _buzz;
  static String _playing = '';                    // '' · 'ring' · 'ringback'
  static DateTime _lastNotify = DateTime.fromMillisecondsSinceEpoch(0);

  /// Incoming call ringing (in-app ringing screen).
  static Future<void> ringing(bool on) async {
    _buzz?.cancel();
    _buzz = null;
    if (!on) {
      if (_playing == 'ring') await _stop();
      return;
    }
    await _stop();
    _playing = 'ring';
    await _play('ring');
    HapticFeedback.heavyImpact();
    _buzz = Timer.periodic(const Duration(milliseconds: 1100), (_) => HapticFeedback.heavyImpact());
  }

  /// Outgoing call: the ringback tone while the other side rings.
  static Future<void> ringback(bool on) async {
    if (!on) {
      if (_playing == 'ringback') await _stop();
      return;
    }
    if (_playing.isNotEmpty) return;              // already ringing back (or an incoming ring is playing)
    _playing = 'ringback';
    await _play('ringback');
  }

  /// A short notification sound (at most one every 2 seconds, never over a ringing call).
  static Future<void> notify() async {
    if (_playing.isNotEmpty) return;
    final now = DateTime.now();
    if (now.difference(_lastNotify).inMilliseconds < 2000) return;
    _lastNotify = now;
    await _play('notify');
  }

  /// Stop everything (a call screen opened or closed).
  static Future<void> stopAll() async {
    _buzz?.cancel();
    _buzz = null;
    await _stop();
  }

  static Future<void> _play(String kind) async {
    if (kIsWeb || !Platform.isAndroid) return;
    try {
      await _native.invokeMethod('sound', {'kind': kind});
    } catch (e) {
      debugPrint('Sound "$kind" failed: $e');
    }
  }

  static Future<void> _stop() async {
    _playing = '';
    if (kIsWeb || !Platform.isAndroid) return;
    try {
      await _native.invokeMethod('soundStop');
    } catch (_) {}
  }
}
