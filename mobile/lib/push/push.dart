import 'dart:async';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_callkit_incoming/entities/entities.dart';
import 'package:flutter_callkit_incoming/flutter_callkit_incoming.dart';
import 'package:uuid/uuid.dart';

import '../app/api.dart';

/// Push notifications (Firebase) and the phone's own incoming-call screen.
///
/// Optional. Without Firebase config files (google-services.json / GoogleService-Info.plist) the app simply checks
/// for calls and messages while it is open, like the website. With them:
///   * a ringing call wakes the phone with the native call screen (also customer calls from the Wi-Fi pages)
///   * it stops ringing when someone else answers or the caller hangs up
///   * chat messages, customer complaints and missed customer calls arrive as notifications
class Push {
  static bool ready = false;
  static void Function(String cid, String from, bool video)? onAccept;     // set by the shell: open the call
  static void Function(Map<String, dynamic> data)? onOpen;                // set by the shell: chat / complaint tapped
  static Timer? _buzz;

  static Future<void> init() async {
    try {
      await Firebase.initializeApp();
    } catch (e) {
      debugPrint('Push off (no Firebase config): $e');
      return;
    }
    ready = true;
    FirebaseMessaging.onBackgroundMessage(firebaseBackground);
    FirebaseMessaging.onMessage.listen((m) => handle(m.data, foreground: true));
    FirebaseMessaging.onMessageOpenedApp.listen((m) => onOpen?.call(m.data));
    final first = await FirebaseMessaging.instance.getInitialMessage();
    if (first != null) Timer(const Duration(seconds: 1), () => onOpen?.call(first.data));
    FlutterCallkitIncoming.onEvent.listen(_callkitEvent);
  }

  /// Ask for permission and send this phone's push token to TapTap (after sign-in).
  static Future<void> register() async {
    if (!ready || !Api.I.signedIn) return;
    try {
      final fm = FirebaseMessaging.instance;
      await fm.requestPermission(alert: true, badge: true, sound: true);
      if (Platform.isAndroid) {
        await FlutterCallkitIncoming.requestNotificationPermission({'title': 'Notifications', 'rationaleMessagePermission':
            'TapTap needs notifications to ring you for calls.', 'postNotificationMessageRequired': 'Allow notifications for TapTap.'});
      }
      final tok = await fm.getToken();
      if (tok != null) await Api.I.post('/api/mobile/v1/push/', {'token': tok});
      fm.onTokenRefresh.listen((t) => Api.I.post('/api/mobile/v1/push/', {'token': t}).catchError((_) => <String, dynamic>{}));
    } catch (e) {
      debugPrint('Push register failed: $e');
    }
  }

  static Future<void> unregister() async {
    if (!ready) return;
    try {
      await FirebaseMessaging.instance.deleteToken();
    } catch (_) {}
  }

  static String uuidFor(String cid) => const Uuid().v5(Namespace.url.value, 'taptap-call:$cid');

  /// A push arrived (foreground or background isolate).
  static Future<void> handle(Map<String, dynamic> d, {bool foreground = false}) async {
    switch (d['type']) {
      case 'call':
        if (foreground) return;            // the app's own poll shows the in-app ringing screen
        await showIncoming('${d['id']}', '${d['from'] ?? 'Call'}', d['kind'] == 'portal', d['video'] == '1');
        break;
      case 'call_end':
        await FlutterCallkitIncoming.endCall(uuidFor('${d['id']}'));
        break;
    }
  }

  static Future<void> showIncoming(String cid, String from, bool customer, bool video) async {
    await FlutterCallkitIncoming.showCallkitIncoming(CallKitParams(
      id: uuidFor(cid),
      nameCaller: from,
      appName: 'TapTap',
      handle: customer ? '📶 Customer on your Wi-Fi page' : 'TapTap call',
      type: video ? 1 : 0,
      duration: 40000,
      missedCallNotification: const NotificationParams(showNotification: true, isShowCallback: false, subtitle: 'Missed TapTap call'),
      extra: <String, dynamic>{'cid': cid, 'from': from, 'video': video},
      android: AndroidParams(
        isCustomNotification: true,
        isShowLogo: false,
        ringtonePath: 'system_ringtone_default',
        backgroundColor: customer ? '#0F3D2E' : '#0B1220',
        actionColor: '#22C55E',
        textColor: '#FFFFFF',
        isShowFullLockedScreen: true,
        textAccept: 'Answer',
        textDecline: 'Decline',
        incomingCallNotificationChannelName: 'Incoming calls',
        missedCallNotificationChannelName: 'Missed calls',
      ),
      ios: const IOSParams(handleType: 'generic', supportsVideo: true, ringtonePath: 'system_ringtone_default'),
    ));
  }

  static Future<void> _callkitEvent(CallEvent? e) async {
    if (e is CallEventActionCallAccept) {
      final x = e.callKitParams.extra ?? {};
      final cid = '${x['cid'] ?? ''}';
      if (cid.isNotEmpty) onAccept?.call(cid, '${x['from'] ?? 'Call'}', x['video'] == true);
    } else if (e is CallEventActionCallDecline) {
      final cid = '${(e.callKitParams.extra ?? {})['cid'] ?? ''}';
      if (cid.isEmpty) return;
      await Api.I.load();
      try {
        await Api.I.post('/chat/call/act/', {'action': 'decline', 'id': cid});
      } catch (_) {}
    }
  }

  /// A call accepted on the native screen before the app had started (cold start).
  static Future<Map<String, dynamic>?> acceptedAtLaunch() async {
    try {
      final calls = await FlutterCallkitIncoming.activeCalls();
      for (final c in calls) {
        if (c.isAccepted) return c.extra;
      }
    } catch (_) {}
    return null;
  }

  static void endCallUi() {
    FlutterCallkitIncoming.endAllCalls().catchError((_) {});
  }

  /// In-app ringing: a buzz pattern while the incoming-call screen is up.
  static void ringtone(bool on) {
    _buzz?.cancel();
    if (!on) return;
    _buzz = Timer.periodic(const Duration(milliseconds: 1400), (_) => HapticFeedback.vibrate());
  }
}

/// Runs in a background isolate when a push arrives and the app is closed.
@pragma('vm:entry-point')
Future<void> firebaseBackground(RemoteMessage m) async {
  await Firebase.initializeApp();
  await Push.handle(m.data);
}
