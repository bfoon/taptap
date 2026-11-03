import 'dart:async';

import 'package:flutter/material.dart';

import 'api.dart';
import 'format.dart';

final navigatorKey = GlobalKey<NavigatorState>();

/// One ringing call, from TapTap's /chat/call/poll/ (same as the website's ring card).
class Ring {
  Ring(this.data);
  final Map<String, dynamic> data;
  String get id => '${data['id']}';
  String get from => '${data['from'] ?? 'Call'}';
  String get initials => '${data['initials'] ?? '☎'}';
  bool get video => data['video'] == true;
  String get kind => '${data['kind'] ?? 'direct'}';
  bool get fromCustomer => kind == 'portal';
}

/// Who is signed in, what they may do, and the live bits every screen shares (unread, rings).
class AppState extends ChangeNotifier with WidgetsBindingObserver {
  AppState() {
    WidgetsBinding.instance.addObserver(this);
    Api.I.onSignedOut = () {
      _reset();
      notifyListeners();
    };
  }

  bool booting = true;
  Map<String, dynamic>? me;
  String? bootError;

  // chat (shared by the list and the open conversation)
  List<Map<String, dynamic>> threads = [];
  List<Map<String, dynamic>> people = [];
  final _newMessages = StreamController<Map<String, dynamic>>.broadcast();
  Stream<Map<String, dynamic>> get newMessages => _newMessages.stream;
  int _top = -1;
  int? openThread;
  bool typing = false;
  Map<String, dynamic> openExtra = {};

  // calls
  final List<Ring> rings = [];
  final Set<String> _seenRings = {};
  String? activeCall;                          // a call screen is open for this id
  void Function(Ring ring)? onRing;           // shows the incoming-call screen (set by the shell)

  Timer? _chatTimer, _ringTimer;
  bool _foreground = true;

  // ── convenience ──
  bool get signedIn => Api.I.signedIn && me != null;
  Map<String, dynamic> get business => (me?['business'] as Map?)?.cast<String, dynamic>() ?? {};
  Map<String, dynamic> get can => (me?['can'] as Map?)?.cast<String, dynamic>() ?? {};
  Map<String, dynamic> get user => (me?['user'] as Map?)?.cast<String, dynamic>() ?? {};
  String get currency => '${business['currency'] ?? 'D'}';
  Color get brand => hexColor(business['brand'] as String?);
  int get unread => threads.where((t) => t['muted'] != true).fold(0, (a, t) => a + toI(t['unread']));
  bool canDo(String k) => can[k] == true;

  Future<void> boot() async {
    await Api.I.load();
    if (Api.I.signedIn) {
      try {
        await refreshMe();
      } on ApiError catch (e) {
        if (!e.signedOut) bootError = e.message;
      }
    }
    booting = false;
    notifyListeners();
    if (signedIn) _startPolling();
  }

  Future<void> retryBoot() async {
    bootError = null;
    booting = true;
    notifyListeners();
    await boot();
  }

  Future<void> refreshMe() async {
    me = await Api.I.get('/api/mobile/v1/me/');
    notifyListeners();
  }

  Future<void> signedInWith(String token) async {
    await Api.I.save(token: token);
    await refreshMe();
    _startPolling();
  }

  Future<void> signOut() async {
    try {
      await Api.I.post('/api/mobile/v1/auth/logout/');
    } catch (_) {}
    await Api.I.save(token: null);
    _reset();
    notifyListeners();
  }

  Future<void> switchBusiness(int id) async {
    await Api.I.post('/api/mobile/v1/business/', {'id': id});
    _top = -1;
    threads = [];
    await refreshMe();
    await pollChat();
  }

  void _reset() {
    me = null;
    threads = [];
    people = [];
    rings.clear();
    _top = -1;
    _chatTimer?.cancel();
    _ringTimer?.cancel();
  }

  // ── polling: like the website, only while the app is on screen (push covers the rest) ──
  void _startPolling() {
    _chatTimer?.cancel();
    _ringTimer?.cancel();
    pollChat();
    pollRings();
    _chatTimer = Timer.periodic(const Duration(seconds: 5), (_) => _foreground ? pollChat() : null);
    _ringTimer = Timer.periodic(const Duration(seconds: 3), (_) => _foreground ? pollRings() : null);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState s) {
    _foreground = s == AppLifecycleState.resumed;
    if (_foreground && signedIn) {
      pollChat();
      pollRings();
    }
  }

  Future<void> pollChat() async {
    if (!signedIn) return;
    try {
      final q = <String, String>{'since': '$_top'};
      if (openThread != null) {
        q['open'] = '$openThread';
        if (typing) q['typing'] = '1';
      }
      final d = await Api.I.get('/chat/state/', query: q);
      threads = ((d['threads'] as List?) ?? []).cast<Map>().map((e) => e.cast<String, dynamic>()).toList();
      people = ((d['people'] as List?) ?? []).cast<Map>().map((e) => e.cast<String, dynamic>()).toList();
      openExtra = {'typing': d['typing'], 'seen_upto': d['seen_upto']};
      for (final m in ((d['new'] as List?) ?? []).cast<Map>()) {
        _newMessages.add(m.cast<String, dynamic>());
      }
      _top = toI(d['top']);
      notifyListeners();
    } catch (_) {}
  }

  Future<void> pollRings() async {
    if (!signedIn) return;
    try {
      final d = await Api.I.get('/chat/call/poll/', timeout: const Duration(seconds: 8));
      final now = ((d['rings'] as List?) ?? []).cast<Map>().map((e) => Ring(e.cast<String, dynamic>())).toList();
      rings
        ..clear()
        ..addAll(now);
      for (final r in now) {
        if (_seenRings.add(r.id) && activeCall == null) onRing?.call(r);
      }
      notifyListeners();
    } catch (_) {}
  }

  bool ringStillOn(String id) => rings.any((r) => r.id == id);

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _reset();
    super.dispose();
  }
}
