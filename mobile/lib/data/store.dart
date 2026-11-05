import 'dart:async';
import 'dart:convert';

import '../app/api.dart';
import 'local_db.dart';

/// What the phone keeps from TapTap — the screens' first source.
///
/// A screen asks [peek] for the page it shows and draws it at once (no spinner, also with no internet), then calls
/// [fetch] to bring it up to date. [fetch] sends TapTap the ETag of what the phone has: when nothing changed, TapTap
/// answers an empty 304 and nothing is downloaded. Every new answer is kept in the phone's database (LocalDb) and
/// announced on [changes], so a screen that is open updates by itself when the background sync (SyncEngine) pulls
/// something new.
///
/// Pages are kept per scope (server · person · business): switching business shows that business's own pages, and
/// signing out wipes everything.
class Store {
  Store._();
  static final Store I = Store._();

  String _scope = '';
  final Map<String, SavedPage> _pages = {};
  final Map<String, Future<Map<String, dynamic>>> _inflight = {};
  final _changes = StreamController<String>.broadcast();

  /// The key of every page that just changed on the phone.
  Stream<String> get changes => _changes.stream;

  String get scope => _scope;
  bool get isOpen => _scope.isNotEmpty;

  /// The key a page is kept under: the path plus its query, sorted.
  static String keyOf(String path, [Map<String, String>? query]) {
    if (query == null || query.isEmpty) return path;
    final names = query.keys.toList()..sort();
    final q = names.map((k) => '${Uri.encodeQueryComponent(k)}=${Uri.encodeQueryComponent(query[k]!)}').join('&');
    return '$path?$q';
  }

  /// Load a scope's pages into memory (a few milliseconds), so [peek] can answer straight away.
  Future<void> open(String scope) async {
    if (scope.isEmpty || scope == _scope) return;
    _scope = scope;
    _pages.clear();
    _inflight.clear();
    final rows = await LocalDb.I.load(scope);
    if (_scope != scope) return;                      // switched again meanwhile
    for (final r in rows) {
      _pages.putIfAbsent(r.key, () => r);             // a fresher answer that came in meanwhile wins
    }
    unawaited(LocalDb.I.prune(scope));
  }

  /// Forget the scope in memory; with [wipe], everything kept on the phone too (sign-out).
  Future<void> close({bool wipe = false}) async {
    _scope = '';
    _pages.clear();
    _inflight.clear();
    if (wipe) await LocalDb.I.wipe();
  }

  /// What the phone has for this page — a fresh copy each time (screens may change what they are given) — or null.
  Map<String, dynamic>? peek(String path, [Map<String, String>? query]) => _decode(_pages[keyOf(path, query)]);

  Map<String, dynamic>? peekKey(String key) => _decode(_pages[key]);

  /// When TapTap last confirmed this page, or null when the phone does not have it.
  DateTime? savedAt(String path, [Map<String, String>? query]) => _pages[keyOf(path, query)]?.savedAt;

  DateTime? savedAtKey(String key) => _pages[key]?.savedAt;

  Iterable<String> get keys => _pages.keys;

  static Map<String, dynamic>? _decode(SavedPage? p) {
    if (p == null) return null;
    try {
      final d = jsonDecode(p.body);
      return d is Map<String, dynamic> ? d : null;
    } catch (_) {
      return null;
    }
  }

  /// Bring a page up to date from TapTap and keep it. Returns TapTap's answer — or, when nothing changed (304,
  /// or [unchanged] says so), what the phone already has. Errors exactly like Api.get (ApiError).
  ///
  /// [extra] is sent along but is not part of the page's key (e.g. ?fp= for "only if changed", ?peek=1).
  /// [fresh]: when the page was confirmed less than this long ago, it is returned without asking TapTap.
  /// Two calls for the same page at the same time share one request.
  Future<Map<String, dynamic>> fetch(
    String path, {
    Map<String, String>? query,
    Map<String, String>? extra,
    Duration timeout = const Duration(seconds: 20),
    bool Function(Map<String, dynamic> d)? unchanged,
    Duration? fresh,
  }) {
    final key = keyOf(path, query);
    if (fresh != null) {
      final p = _pages[key];
      final d = _decode(p);
      if (p != null && d != null && DateTime.now().difference(p.savedAt) < fresh) return Future.value(d);
    }
    final flight = '$key#${keyOf('', extra)}';
    final running = _inflight[flight];
    if (running != null) return running.then(_copy);
    final f = _fetch(path, key, query, extra, timeout, unchanged);
    _inflight[flight] = f;
    f.whenComplete(() {
      if (identical(_inflight[flight], f)) _inflight.remove(flight);
    }).ignore();                                       // the caller gets (and handles) the error from f itself
    return f;
  }

  static Map<String, dynamic> _copy(Map<String, dynamic> d) {
    try {
      final c = jsonDecode(jsonEncode(d));
      return c is Map<String, dynamic> ? c : d;
    } catch (_) {
      return d;
    }
  }

  Future<Map<String, dynamic>> _fetch(String path, String key, Map<String, String>? query, Map<String, String>? extra,
      Duration timeout, bool Function(Map<String, dynamic> d)? unchanged) async {
    final scope = _scope;
    final had = _pages[key];
    final sendTag = had != null && _decode(had) != null ? had.etag : '';
    final r = await Api.I.getFresh(path, query: {...?query, ...?extra}, etag: sendTag, timeout: timeout);
    final now = DateTime.now();
    if (scope != _scope || scope.isEmpty) {
      // signed out or switched business while it was on its way: hand it over, but do not keep it
      if (r.data != null) return r.data!;
      throw ApiError('Please try again.');
    }
    if (r.notModified) {
      final cur = _pages[key] ?? had;
      final d = _decode(cur);
      if (cur != null && d != null) {
        _pages[key] = cur.touched(now, r.etag);
        unawaited(LocalDb.I.touch(scope, key, now, r.etag));
        return d;
      }
      // the phone lost its copy meanwhile: ask again for the whole answer
      final again = await Api.I.getFresh(path, query: {...?query, ...?extra}, timeout: timeout);
      if (again.data == null) throw ApiError('TapTap sent an unexpected answer.');
      if (scope == _scope) _keep(scope, key, again, now);
      return again.data!;
    }
    final data = r.data!;
    if (unchanged != null && unchanged(data)) {
      final cur = _pages[key];
      final d = _decode(cur);
      if (cur != null && d != null) {
        _pages[key] = cur.touched(now);
        unawaited(LocalDb.I.touch(scope, key, now, ''));
        return d;
      }
      return data;                                     // nothing kept to fall back on: the caller decides
    }
    _keep(scope, key, r, now);
    return data;
  }

  void _keep(String scope, String key, Fetched r, DateTime now) {
    final old = _pages[key];
    final page = SavedPage(key: key, body: r.body, etag: r.etag, savedAt: now);
    _pages[key] = page;
    unawaited(LocalDb.I.save(scope, page));
    if (old == null || old.body != r.body) _changes.add(key);
  }

  /// Keep something the phone worked out itself (e.g. a chat message that arrived with the regular chat check).
  void put(String path, Map<String, dynamic> data, {Map<String, String>? query, bool announce = true}) {
    putKey(keyOf(path, query), data, announce: announce);
  }

  void putKey(String key, Map<String, dynamic> data, {bool announce = true}) {
    if (_scope.isEmpty) return;
    String body;
    try {
      body = jsonEncode(data);
    } catch (_) {
      return;
    }
    final old = _pages[key];
    if (old != null && old.body == body) return;
    // the ETag no longer describes this copy: the next fetch downloads it whole and puts TapTap's own version back
    final page = SavedPage(key: key, body: body, etag: '', savedAt: DateTime.now());
    _pages[key] = page;
    unawaited(LocalDb.I.save(_scope, page));
    if (announce) _changes.add(key);
  }

  void remove(String path, [Map<String, String>? query]) {
    final key = keyOf(path, query);
    if (_pages.remove(key) != null && _scope.isNotEmpty) unawaited(LocalDb.I.remove(_scope, key));
  }
}
