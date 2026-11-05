import 'dart:async';
import 'dart:io';
import 'dart:math';
import 'dart:ui' show DartPluginRegistrant;

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_callkit_incoming/entities/entities.dart';
import 'package:flutter_callkit_incoming/flutter_callkit_incoming.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:uuid/uuid.dart';

import '../app/api.dart';
import '../app/sounds.dart';

/// Push notifications (Firebase) and the phone's own incoming-call screen.
///
/// Optional. Without Firebase config files (google-services.json / GoogleService-Info.plist) the app simply checks
/// for calls and messages while it is open, like the website. With them:
///   * a ringing call wakes the phone with the native call screen (also customer calls from the Wi-Fi pages)
///   * it stops ringing when someone else answers or the caller hangs up
///   * chat messages, customer complaints, missed customer calls, business alerts (stock low, router offline,
///     tracked vouchers…) and vouchers made by the team arrive as notifications that pop up on the screen
///   * while TapTap is open they pop up inside the app instead (widgets/push_banner.dart, set up by the shell),
///     with the phone's notification sound — and a call rings at once, without waiting for the next check
///
/// On Android every kind has its own notification channel at high importance (made in MainActivity by
/// tool/configure_platforms.py), so the phone shows it as a pop-up with sound and people can tune each kind in
/// Settings. Which kinds this phone gets is chosen on the Notifications screen (stored on the server, per phone).
///
/// Many Android phones (Tecno, Infinix, itel, Xiaomi, Oppo, Samsung…) stop apps in the background to save battery,
/// and then no push reaches TapTap until it is opened again. [backgroundBlocked] / [allowBackground] ask the phone
/// to let TapTap run (Notifications screen, and once after sign-in).
///
/// Asking for permission: ONE prompt, then the phone's real answer is read back ([permission]). Android 13+ shows
/// nothing at all once someone said "Don't allow" twice — [turnOn] notices that (the prompt "answers" instantly) and
/// opens the phone's settings instead. TapTap is told what the phone allows each time the address is sent, so the
/// server (and `manage.py push_doctor`) can see a phone that receives pushes but hides them.
///
/// Self-healing: the push address is sent again when it changes, when the permission changes, every 30 minutes the
/// app is used, and whenever TapTap says it lost it (me → push_registered: false).
///
/// Check delivery (Notifications screen): [echo] waits for one test push carrying a nonce, so the app can time the
/// whole road — TapTap → Google → this phone — and say where it stops.
class Push {
  static bool ready = false;
  static void Function(String cid, String from, bool video)? onAccept;     // set by the shell: open the call
  static void Function(Map<String, dynamic> data)? onOpen;                // set by the shell: notification tapped
  static void Function(String title, String body, Map<String, dynamic> data)? onBanner;   // set by the shell: arrived while open
  static void Function(Map<String, dynamic> ring)? onRing;               // set by the shell: a call push while open
  static void Function(String cid)? onRingEnded;                         // set by the shell: that call stopped ringing
  static bool _refreshing = false;
  static String? _sentToken;                                              // the token TapTap has for this phone
  static String _sentPerm = '';                                           // …and the permission it was told
  static DateTime? _sentAt;
  static Future<void>? _registering;                                      // one registration at a time
  static bool _registeringAsks = false;                                   // …and whether it shows the prompt
  static Timer? _retry;
  static int _tries = 0;
  static final Map<String, Completer<Duration?>> _echo = {};             // Check delivery: nonce → round trip
  static final Map<String, DateTime> _echoFrom = {};

  /// What TapTap said when this phone last registered: does the app's Firebase project match the server key's?
  static bool? projectMatch;
  static String serverProject = '';

  /// The phone showed its "Allow notifications?" prompt during the last registration (so no reminder right now).
  static bool justAsked = false;
  static const _native = MethodChannel('taptap/notifications');           // android/…/MainActivity.kt
  static const _store = FlutterSecureStorage(aOptions: AndroidOptions(encryptedSharedPreferences: true));

  /// Has TapTap registered this phone's push address (this run of the app)?
  static bool get registered => _sentToken != null;

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

  /// The Firebase project this copy of the app was built with ('' without Firebase).
  static String get projectId {
    if (!ready) return '';
    try {
      return Firebase.app().options.projectId;
    } catch (_) {
      return '';
    }
  }

  /// Ask for permission (once, and only if needed) and send this phone's push address to TapTap — after sign-in, and
  /// again when the app comes back to the screen (then only when something changed or 30 minutes passed). On a slow
  /// line the first try can fail: it tries again by itself until TapTap has it. Callers that arrive while a
  /// registration is running wait for that one.
  static Future<void> register({bool ask = true, bool force = false}) {
    if (!ready || !Api.I.signedIn) return Future<void>.value();
    final running = _registering;
    if (running != null) {
      // a quiet re-send is running and this caller wants the prompt: ask right after it, never skip the prompt
      return ask && !_registeringAsks ? running.then((_) => register(ask: true, force: force)) : running;
    }
    _registeringAsks = ask;
    final f = _register(ask: ask, force: force).whenComplete(() => _registering = null);
    _registering = f;
    return f;
  }

  static Future<void> _register({required bool ask, required bool force}) async {
    try {
      final fm = FirebaseMessaging.instance;
      var perm = await permission();
      if (ask && perm != 'allowed') {
        // One prompt, and its real answer. (The old second prompt from the call plugin raced this one.)
        final sw = Stopwatch()..start();
        await fm.requestPermission(alert: true, badge: true, sound: true);
        justAsked = sw.elapsedMilliseconds > 700;    // a prompt was on screen (instant = the phone no longer asks)
        perm = await permission();
      }
      if (perm == 'allowed') {
        try {
          await _native.invokeMethod('createChannels');
        } catch (_) {}
        if (ask && Platform.isAndroid) await _fullScreenOnce();          // calls over the lock screen — after, not during
      }
      final tok = await fm.getToken();
      final stale = _sentAt == null || DateTime.now().difference(_sentAt!) > const Duration(minutes: 30);
      if (tok != null && (force || stale || tok != _sentToken || perm != _sentPerm)) await _sendToken(tok, perm);
      if (!_refreshing) {
        _refreshing = true;
        fm.onTokenRefresh.listen((t) async => _sendToken(t, await permission()));
      }
    } catch (e) {
      debugPrint('Push register failed: $e');
      _again();
    }
  }

  static Future<Map<String, dynamic>> _post(String tok, String perm) => Api.I.post('/api/mobile/v1/push/', {
        'token': tok,
        'perm': perm,
        'project': projectId,
        'platform': Platform.isIOS ? 'ios' : 'android',
      });

  static Future<void> _sendToken(String tok, String perm) async {
    try {
      final d = await _post(tok, perm);
      _sentToken = tok;
      _sentPerm = perm;
      _sentAt = DateTime.now();
      final m = d['project_match'];
      projectMatch = m is bool ? m : null;
      serverProject = '${d['server_project'] ?? ''}';
      _tries = 0;
      _retry?.cancel();
    } catch (e) {
      debugPrint('Push token not sent yet: $e');
      _again();
    }
  }

  /// TapTap says it does not have this phone's address (me → push_registered: false): send it again.
  static void serverForgot() {
    if (!ready) return;
    _sentToken = null;
    register(ask: false, force: true);
  }

  /// Check delivery: send this phone's address now and return TapTap's answer (server key's project, match…).
  /// Throws with a plain message when Google gives no address (no internet to Google, no Google Play services…).
  static Future<Map<String, dynamic>> registerNow() async {
    if (!ready) throw ApiError('This copy of the app was built without Firebase.');
    String? tok;
    try {
      tok = await FirebaseMessaging.instance.getToken();
    } catch (e) {
      throw ApiError(explainTokenError(e));
    }
    if (tok == null || tok.isEmpty) throw ApiError('Google did not give this phone a push address. Check the internet, then try again.');
    final perm = await permission();
    final d = await _post(tok, perm);
    _sentToken = tok;
    _sentPerm = perm;
    _sentAt = DateTime.now();
    final m = d['project_match'];
    projectMatch = m is bool ? m : null;
    serverProject = '${d['server_project'] ?? ''}';
    return d;
  }

  /// Why Google gave no push address, in plain words.
  static String explainTokenError(Object e) {
    final t = '$e';
    if (t.contains('SERVICE_NOT_AVAILABLE') || t.contains('MISSING_INSTANCEID_SERVICE')) {
      return 'Google Play services cannot be reached on this phone (no internet to Google, or a phone without Google '
          'Play services, like newer Huawei). Pushes cannot reach it; TapTap still works while open.';
    }
    if (t.contains('FIS_AUTH_ERROR') || t.contains('AUTHENTICATION_FAILED') || t.contains('API key')) {
      return 'Google refused this app’s Firebase key (google-services.json is from another project, or its API key '
          'is restricted). Rebuild the app with the right google-services.json.';
    }
    if (t.contains('TOO_MANY_REGISTRATIONS')) return 'This phone has too many apps using Google push. Remove some unused apps.';
    return 'Google did not give this phone a push address: $t';
  }

  /// Turn notifications on: the phone's prompt, or — when it no longer shows one ("Don't allow" twice) — its
  /// settings page. Returns the permission afterwards ('allowed', 'denied', 'ask', 'off').
  static Future<String> turnOn() async {
    if (!ready) return 'off';
    final sw = Stopwatch()..start();
    try {
      await FirebaseMessaging.instance.requestPermission(alert: true, badge: true, sound: true);
    } catch (_) {}
    final p = await permission();
    if (p == 'allowed') {
      await register(ask: false, force: true);
    } else if (sw.elapsedMilliseconds < 700) {
      await openPhoneSettings();                       // no prompt appeared: only the phone's settings can do it
    }
    return p;
  }

  /// Notification kinds switched off in the phone's settings (Android: channel names), or [] when all are on.
  static Future<List<String>> blockedKinds() async {
    if (!Platform.isAndroid) return const [];
    try {
      final r = await _native.invokeMethod<Map<Object?, Object?>>('notificationState');
      final off = r?['off'];
      return off is List ? off.map((e) => '$e').toList() : const [];
    } catch (_) {
      return const [];
    }
  }

  /// The "notifications are off" reminder after start: at most once a day.
  static Future<bool> nudgeDue() async {
    try {
      final today = DateTime.now().toIso8601String().substring(0, 10);
      if (await _store.read(key: 'notify_nudge_day') == today) return false;
      await _store.write(key: 'notify_nudge_day', value: today);
      return true;
    } catch (_) {
      return false;
    }
  }

  /// A fresh nonce for Check delivery.
  static String newNonce() => '${DateTime.now().microsecondsSinceEpoch}-${Random().nextInt(1 << 30)}';

  /// Wait for the test push carrying [nonce] (call before asking TapTap to send it): the time it took, or null
  /// when it did not arrive within [wait].
  static Future<Duration?> echo(String nonce, Duration wait) {
    final c = Completer<Duration?>();
    _echo[nonce] = c;
    _echoFrom[nonce] = DateTime.now();
    Timer(wait, () {
      _echoFrom.remove(nonce);
      final w = _echo.remove(nonce);
      if (w != null && !w.isCompleted) w.complete(null);
    });
    return c.future;
  }

  /// Try again later (no internet, or TapTap too slow to answer): 5 s, 15 s, 30 s, then every minute.
  static void _again() {
    _retry?.cancel();
    final s = const [5, 15, 30, 60][_tries.clamp(0, 3)];
    _tries++;
    _retry = Timer(Duration(seconds: s), () => register(ask: false, force: true));
  }

  static Future<void> unregister() async {
    _retry?.cancel();
    _sentToken = null;
    _sentPerm = '';
    _sentAt = null;
    if (!ready) return;
    try {
      await FirebaseMessaging.instance.deleteToken();
    } catch (_) {}
  }

  /// Android 14+: calls may show over the lock screen only with "full-screen notifications" allowed. Ask once.
  static Future<void> _fullScreenOnce() async {
    try {
      if (await FlutterCallkitIncoming.canUseFullScreenIntent()) return;
      if (await _store.read(key: 'asked_full_screen') == '1') return;
      await _store.write(key: 'asked_full_screen', value: '1');
      await FlutterCallkitIncoming.requestFullIntentPermission();
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

  /// Is the phone's battery saver allowed to stop TapTap in the background (Android)? Then pushes and calls can
  /// be lost while it is closed.
  static Future<bool> backgroundBlocked() async {
    if (!Platform.isAndroid) return false;
    try {
      return await _native.invokeMethod<bool>('batteryOptimized') ?? false;
    } catch (_) {
      return false;
    }
  }

  /// Ask Android to let TapTap run in the background (the phone shows its own "Allow" dialog, or its battery page).
  static Future<bool> allowBackground() async {
    if (!Platform.isAndroid) return false;
    try {
      return await _native.invokeMethod<bool>('allowBackground') ?? false;
    } catch (e) {
      debugPrint('Could not open battery settings: $e');
      return false;
    }
  }

  /// Once after sign-in: when the battery saver may stop TapTap, ask the phone to let it run.
  static Future<bool> shouldAskBackground() async {
    if (!ready || !await backgroundBlocked()) return false;
    if (await _store.read(key: 'asked_background') == '1') return false;
    await _store.write(key: 'asked_background', value: '1');
    return true;
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

  /// A push while the app is on screen: calls ring in the app at once; everything else pops up as a banner.
  static Future<void> _foreground(RemoteMessage m) async {
    final d = m.data;
    final nonce = '${d['nonce'] ?? ''}';
    if (d['type'] == 'test' && nonce.isNotEmpty) {
      final from = _echoFrom.remove(nonce);
      final w = _echo.remove(nonce);
      if (w != null && !w.isCompleted) {
        w.complete(from == null ? Duration.zero : DateTime.now().difference(from));
        return;                                      // Check delivery shows it on its own screen
      }
    }
    if (d['type'] == 'call' || d['type'] == 'call_end') return handle(d, foreground: true);
    final title = m.notification?.title ?? '${d['title'] ?? ''}';
    if (title.isEmpty) return;
    onBanner?.call(title, m.notification?.body ?? '${d['body'] ?? ''}', d);
  }

  static String uuidFor(String cid) => const Uuid().v5(Namespace.url.value, 'taptap-call:$cid');

  /// A push arrived (foreground or background isolate).
  static Future<void> handle(Map<String, dynamic> d, {bool foreground = false}) async {
    switch (d['type']) {
      case 'call':
        if (foreground) {
          // ring now — on a slow line the app's own check can take several seconds to see it
          onRing?.call({
            'id': '${d['id']}',
            'from': '${d['caller'] ?? d['from'] ?? 'Call'}',
            'initials': '${d['initials'] ?? ''}',
            'kind': '${d['kind'] ?? 'direct'}',
            'video': d['video'] == '1',
          });
          return;
        }
        await showIncoming('${d['id']}', '${d['caller'] ?? d['from'] ?? 'Call'}', d['kind'] == 'portal', d['video'] == '1');
        break;
      case 'call_end':
        if (foreground) onRingEnded?.call('${d['id']}');
        try {
          await FlutterCallkitIncoming.endCall(uuidFor('${d['id']}'));
        } catch (_) {}
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
      for (var i = 0; i < 3; i++) {                     // a slow line: make sure the caller hears "declined"
        try {
          await Api.I.post('/chat/call/act/', {'action': 'decline', 'id': cid});
          return;
        } on ApiError catch (err) {
          if (err.status != 0) return;
          await Future.delayed(Duration(seconds: 2 * (i + 1)));
        }
      }
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

  /// In-app ringing: the phone's ringtone (looped) and a buzz while the incoming-call screen is up.
  static void ringtone(bool on) => Sounds.ringing(on);
}

/// Runs in a background isolate when a push arrives and the app is closed.
@pragma('vm:entry-point')
Future<void> firebaseBackground(RemoteMessage m) async {
  DartPluginRegistrant.ensureInitialized();          // the call screen and storage plugins in this isolate
  await Firebase.initializeApp();
  await Push.handle(m.data);
}
