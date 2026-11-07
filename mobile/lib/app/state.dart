import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';

import '../data/local_db.dart';
import '../data/store.dart';
import '../data/sync_engine.dart';
import '../push/push.dart';
import 'api.dart';
import 'format.dart';
import 'sounds.dart';

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
      _reset(wipe: true);
      notifyListeners();
    };
  }

  /// Keeps the phone's saved pages up to date while the app is open (data/sync_engine.dart).
  late final SyncEngine sync = SyncEngine(this);

  bool booting = true;
  Map<String, dynamic>? me;
  String? bootError;

  // chat (shared by the list and the open conversation)
  List<Map<String, dynamic>> threads = [];
  List<Map<String, dynamic>> people = [];
  final _newMessages = StreamController<Map<String, dynamic>>.broadcast();
  Stream<Map<String, dynamic>> get newMessages => _newMessages.stream;
  final _changedMessages = StreamController<Map<String, dynamic>>.broadcast();
  /// Messages their sender edited or deleted (the open conversation swaps them in).
  Stream<Map<String, dynamic>> get changedMessages => _changedMessages.stream;
  int _top = -1;
  double _chg = 0;                              // TapTap's clock at the last check: edits / deletes after it
  int? openThread;
  bool typing = false;
  Map<String, dynamic> openExtra = {};

  // calls
  final List<Ring> rings = [];
  final Set<String> _seenRings = {};
  final Map<String, DateTime> _pushRings = {};  // rang from a push, before the next check has seen it
  final Set<String> _endedRings = {};           // a push said this call stopped ringing
  String? activeCall;                          // a call screen is open for this id
  void Function(Ring ring)? onRing;           // shows the incoming-call screen (set by the shell)

  Timer? _chatTimer, _ringTimer;
  bool _foreground = true;
  bool _chatPrimed = false;                     // the first check only learns what is there: no sound for it

  // ── convenience ──
  bool get signedIn => Api.I.signedIn && me != null;
  Map<String, dynamic> get business => (me?['business'] as Map?)?.cast<String, dynamic>() ?? {};
  Map<String, dynamic> get can => (me?['can'] as Map?)?.cast<String, dynamic>() ?? {};
  Map<String, dynamic> get user => (me?['user'] as Map?)?.cast<String, dynamic>() ?? {};
  String get currency => '${business['currency'] ?? 'D'}';
  Color get brand => hexColor(business['brand'] as String?);
  int get unread => threads.where((t) => t['muted'] != true).fold(0, (a, t) => a + toI(t['unread']));
  bool canDo(String k) => can[k] == true;
  bool get foreground => _foreground;

  // ── what the phone keeps (data/store.dart): per server · person · business ──
  String get _accountScope => 'account|${Api.I.server}';
  static String _scopeFor(Map<String, dynamic> m) {
    final u = (m['user'] as Map?) ?? const {};
    final b = (m['business'] as Map?) ?? const {};
    return '${Api.I.server}|u${toI(u['id'])}|b${toI(b['id'])}';
  }

  /// A fingerprint of the sign-in token (FNV-1a), kept with the saved "me": a saved profile is only used again
  /// with the very token it came with.
  static String _tokenMark(String? t) {
    var h = 0x811c9dc5;
    for (final c in (t ?? '').codeUnits) {
      h ^= c;
      h = (h * 0x01000193) & 0xffffffff;
    }
    return 'tok:${h.toRadixString(16)}';
  }

  Future<Map<String, dynamic>?> _savedMe() async {
    final p = await LocalDb.I.one(_accountScope, 'me');
    if (p == null || p.etag != _tokenMark(Api.I.token)) return null;
    try {
      final d = jsonDecode(p.body);
      return d is Map<String, dynamic> && d['user'] is Map ? d : null;
    } catch (_) {
      return null;
    }
  }

  Future<void> _saveMe(Map<String, dynamic> m) async {
    try {
      await LocalDb.I.save(_accountScope,
          SavedPage(key: 'me', body: jsonEncode(m), etag: _tokenMark(Api.I.token), savedAt: DateTime.now()));
    } catch (_) {}
  }

  Future<void> boot() async {
    await Api.I.load();
    if (Api.I.signedIn) {
      // straight in with what this phone kept last time — TapTap confirms it right after (or signs the phone out)
      final saved = await _savedMe();
      if (saved != null) {
        await Store.I.open(_scopeFor(saved));
        me = saved;
        _loadSavedChat();
        booting = false;
        notifyListeners();
        _startPolling();
        unawaited(refreshMe().catchError((_) {}));
        return;
      }
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

  Future<void> refreshMe({Duration timeout = const Duration(seconds: 20)}) async {
    final m = await Api.I.get('/api/mobile/v1/me/', timeout: timeout);
    if (!Api.I.signedIn) return;                       // signed out while it was on its way
    final scope = _scopeFor(m);
    var restart = false;
    if (scope != Store.I.scope) {
      // another business (switched here or elsewhere): its own saved pages and conversations
      final first = !Store.I.isOpen;
      await Store.I.open(scope);
      if (!first) {
        threads = [];
        people = [];
        _top = -1;
        _chg = 0;
        _chatPrimed = false;
      }
      _loadSavedChat();
      restart = !first && sync.running;
    }
    me = m;
    unawaited(_saveMe(m));
    if (restart) sync.start();                         // the new business's own stamps and pages
    // TapTap lost this phone's push address (cleared after an error, another sign-in…): send it again, quietly
    if (me?['push_registered'] == false && Push.registered) Push.serverForgot();
    notifyListeners();
  }

  Future<void> signedInWith(String token) async {
    try {
      // a fresh sign-in starts from a clean phone (never waits long on the phone's database)
      await Store.I.close(wipe: true).timeout(const Duration(seconds: 5));
    } catch (_) {}
    await Api.I.save(token: token);
    try {
      await refreshMe(timeout: const Duration(seconds: 30));
    } on ApiError catch (e) {
      if (e.signedOut || e.status != 0) rethrow;
      await refreshMe(timeout: const Duration(seconds: 45));   // a slow line: once more, on a fresh connection
    }
    _startPolling();
  }

  Future<void> signOut() async {
    try {
      await Api.I.post('/api/mobile/v1/auth/logout/');
    } catch (_) {}
    await Api.I.save(token: null);
    _reset(wipe: true);
    notifyListeners();
  }

  Future<void> switchBusiness(int id) async {
    await Api.I.post('/api/mobile/v1/business/', {'id': id});
    _top = -1;
    _chg = 0;
    threads = [];
    await refreshMe();
    await pollChat();
  }

  /// Leave the current business (you were added to its team). Returns TapTap's message. When it was your only
  /// business TapTap signs this phone out, and so does the app.
  Future<String> leaveBusiness(String confirmName) async {
    final id = business['id'];
    final d = await Api.I.post('/api/mobile/v1/business/leave/', {'id': id, 'confirm': confirmName});
    final msg = '${d['message'] ?? 'You left the business.'}';
    if (d['signed_out'] == true) {
      try {
        await Push.unregister();
      } catch (_) {}
      await Api.I.save(token: null);
      _reset(wipe: true);
      notifyListeners();
      return msg;
    }
    _top = -1;
    _chg = 0;
    threads = [];
    await refreshMe();
    await pollChat();
    return msg;
  }

  void _reset({bool wipe = false}) {
    sync.stop();
    if (wipe) unawaited(Store.I.close(wipe: true));     // nothing of this account stays on the phone
    me = null;
    threads = [];
    people = [];
    rings.clear();
    _pushRings.clear();
    _endedRings.clear();
    _chatPrimed = false;
    _top = -1;
    _chg = 0;
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
    sync.start();
  }

  /// The conversation list as the phone last saw it, so Chat opens full (the next check corrects it).
  void _loadSavedChat() {
    if (threads.isNotEmpty) return;
    final d = Store.I.peekKey(Pages.chatList);
    if (d == null) return;
    threads = asMaps(d['threads']);
    people = asMaps(d['people']);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState s) {
    _foreground = s == AppLifecycleState.resumed;
    if (_foreground && signedIn) {
      pollChat();
      pollRings();
      sync.run();                                       // back on screen: what moved while away
      // quietly: a token that did not reach TapTap on a slow line, a changed permission (back from the phone's
      // settings), or every 30 minutes — push.dart decides whether anything needs sending
      Push.register(ask: false);
    }
  }

  Future<void> pollChat() async {
    if (!signedIn) return;
    try {
      final since = _top;
      final scope = Store.I.scope;
      final q = <String, String>{'since': '$since', if (_chg > 0) 'chg': '$_chg'};
      if (openThread != null) {
        q['open'] = '$openThread';
        if (typing) q['typing'] = '1';
      }
      final d = await Api.I.get('/chat/state/', query: q);
      if (scope != Store.I.scope || !signedIn) return;  // switched business / signed out meanwhile
      final before = unread;
      threads = ((d['threads'] as List?) ?? []).cast<Map>().map((e) => e.cast<String, dynamic>()).toList();
      people = ((d['people'] as List?) ?? []).cast<Map>().map((e) => e.cast<String, dynamic>()).toList();
      openExtra = {'typing': d['typing'], 'seen_upto': d['seen_upto']};
      for (final m in ((d['new'] as List?) ?? []).cast<Map>()) {
        _newMessages.add(m.cast<String, dynamic>());
      }
      _top = toI(d['top']);
      if (d['now'] != null) _chg = toD(d['now']);
      for (final m in ((d['changed'] as List?) ?? []).cast<Map>()) {
        _changedMessages.add(m.cast<String, dynamic>());
      }
      // keep it on the phone: the list for next start, the messages in the saved conversations
      Store.I.putKey(Pages.chatList, {'threads': d['threads'] ?? const <dynamic>[], 'people': d['people'] ?? const <dynamic>[]}, announce: false);
      sync.mergeChat(since: since, fresh: asMaps(d['new']), changed: asMaps(d['changed']));
      // a new message while TapTap is open: the notification sound — unless a push already brings it (with sound)
      if (_chatPrimed && unread > before && !_pushCoversChat) Sounds.notify();
      _chatPrimed = true;
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
        ..addAll(now.where((r) => !_endedRings.contains(r.id)));
      for (final r in rings) {
        _pushRings.remove(r.id);                // the check has it now
        if (_seenRings.add(r.id) && activeCall == null) onRing?.call(r);
      }
      notifyListeners();
    } catch (_) {}
  }

  /// A call push arrived while TapTap is open: ring at once (the next check confirms or ends it).
  void ringFromPush(Map<String, dynamic> data) {
    if (!signedIn) return;
    final r = Ring(data);
    if (r.id.isEmpty || _endedRings.contains(r.id)) return;
    _pushRings[r.id] = DateTime.now();
    if (!rings.any((x) => x.id == r.id)) rings.add(r);
    if (_seenRings.add(r.id) && activeCall == null) onRing?.call(r);
    notifyListeners();
    pollRings();
  }

  /// A push said this call stopped ringing (answered elsewhere, or the caller hung up).
  void ringEnded(String id) {
    _endedRings.add(id);
    _pushRings.remove(id);
    rings.removeWhere((r) => r.id == id);
    notifyListeners();
  }

  /// Still ringing? Yes while TapTap's last check lists it — or, for a call that came by push, for a while even
  /// when a check on a slow line has not seen it yet.
  bool ringStillOn(String id) {
    if (_endedRings.contains(id)) return false;
    if (rings.any((r) => r.id == id)) return true;
    final at = _pushRings[id];
    return at != null && DateTime.now().difference(at).inSeconds < 12;
  }

  bool get _pushCoversChat => Push.ready && Push.registered && me?['push'] == true;

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _reset();                                           // (no wipe: the app is only closing)
    super.dispose();
  }
}
