import 'dart:async';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_callkit_incoming/entities/entities.dart';
import 'package:flutter_callkit_incoming/flutter_callkit_incoming.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:uuid/uuid.dart';

import '../app/api.dart';

/// Push notifications (Firebase) and the phone's own incoming-call screen.
///
/// Optional. Without Firebase config files (google-services.json / GoogleService-Info.plist) the app simply checks
/// for calls and messages while it is open, like the website. With them:
///   * a ringing call wakes the phone with the native call screen (also customer calls from the Wi-Fi pages)
///   * it stops ringing when someone else answers or the caller hangs up
///   * chat messages, customer complaints, missed customer calls, business alerts (stock low, router offline,
///     tracked vouchers…) and vouchers made by the team arrive as notifications that pop up on the screen
///   * while TapTap is open they pop up inside the app instead (widgets/push_banner.dart, set up by the shell)
///
/// On Android every kind has its own notification channel at high importance (made in MainActivity by
/// tool/configure_platforms.py), so the phone shows it as a pop-up and people can tune each kind in Settings.
/// Which kinds this phone gets is chosen on the Notifications screen (stored on the server, per phone).
class Push {
  static bool ready = false;
  static void Function(String cid, String from, bool video)? onAccept;     // set by the shell: open the call
  static void Function(Map<String, dynamic> data)? onOpen;                // set by the shell: notification tapped
  static void Function(String title, String body, Map<String, dynamic> data)? onBanner;   // set by the shell: arrived while open
  static Timer? _buzz;
  static bool _refreshing = false;
  static const _native = MethodChannel('taptap/notifications');           // android/…/MainActivity.kt

  static Future<void> init() async {
    try {
      await Firebase.initializeApp();
    } catch (e) {
      debugPrint('Push off (no Firebase config): $e');
      return;
    }
    ready = true;
    FirebaseMessaging.onBackgroundMessage(firebaseBackground);
    FirebaseMessaging.onMessage.listen(_foreground);
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
      if (!_refreshing) {
        _refreshing = true;
        fm.onTokenRefresh.listen((t) => Api.I.post('/api/mobile/v1/push/', {'token': t}).catchError((_) => <String, dynamic>{}));
      }
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

  /// Notifications permission on this phone: 'allowed', 'denied', 'ask' (not decided yet) or 'off' (this build of
  /// the app has no Firebase config, so nothing can be pushed to it).
  static Future<String> permission() async {
    if (!ready) return 'off';
    try {
      final s = await FirebaseMessaging.instance.getNotificationSettings();
      return switch (s.authorizationStatus) {
        AuthorizationStatus.authorized || AuthorizationStatus.provisional => 'allowed',
        AuthorizationStatus.denied => 'denied',
        _ => 'ask',
      };
    } catch (_) {
      return 'ask';
    }
  }

  /// The phone's own notification settings for TapTap (pop-up, sound and lock screen per kind on Android).
  static Future<bool> openPhoneSettings() async {
    try {
      if (Platform.isAndroid) return await _native.invokeMethod<bool>('openSettings') ?? false;
      return await launchUrl(Uri.parse('app-settings:'));
    } catch (e) {
      debugPrint('Could not open notification settings: $e');
      return false;
    }
  }

  /// A push while the app is on screen: calls ring in the app; everything else pops up as a banner.
  static Future<void> _foreground(RemoteMessage m) async {
    final d = m.data;
    if (d['type'] == 'call' || d['type'] == 'call_end') return handle(d, foreground: true);
    final title = m.notification?.title ?? '';
    if (title.isEmpty) return;
    onBanner?.call(title, m.notification?.body ?? '', d);
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
