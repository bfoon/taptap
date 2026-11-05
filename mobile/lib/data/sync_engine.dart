import 'dart:async';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import 'store.dart';

/// The pages the app keeps up to date by itself. The screens ask for exactly these keys, so what the sync pulls
/// is what they show.
class Pages {
  static const home = '/api/mobile/v1/home/';
  static const sales = '/api/mobile/v1/sales/';
  static const complaints = '/api/mobile/v1/complaints/';
  static const customerCalls = '/api/mobile/v1/customer-calls/';
  static const devices = '/api/mobile/v1/devices/';
  static const vouchers = '/api/mobile/v1/vouchers/';
  static const batches = '/api/mobile/v1/batches/';
  static const voucherForm = '/api/mobile/v1/vouchers/new/';
  static const routers = '/api/mobile/v1/routers/';
  static const pulse = '/api/mobile/v1/sync/';
  static const fit = '/fit/state/';
  static String thread(int id) => '/chat/t/$id/';
  static const chatList = 'local:chat';             // the conversation list, as the last chat check saw it

  static const firstPage = {'page': '1'};
  static Map<String, String> day(DateTime d) => {'date': ymd(d)};
}

class _Page {
  const _Page(this.path, {this.query, required this.every});
  final String path;
  final Map<String, String>? query;
  final Duration every;                              // pulled again when older than this, even if no stamp moved
  String get key => Store.keyOf(path, query);
}

/// While TapTap is open on the phone: every few seconds it asks the server's pulse (/api/mobile/v1/sync/) what
/// moved — sales, customer reports, Wi-Fi page calls, vouchers, who-am-I — and pulls only those pages, plus any
/// page older than its own refresh time. A push that arrives (a complaint, a missed call, vouchers) pulls its page
/// at once. Conversations with new messages are saved ahead (?peek=1, so they stay unread), and messages from the
/// regular chat check are added to the saved conversations — opening a chat is instant.
///
/// Everything lands in the Store; the open screens see it on Store.changes and update on their own.
class SyncEngine {
  SyncEngine(this.app);
  final AppState app;

  static const every = Duration(seconds: 20);
  static const _parallel = 2;                        // gentle on a slow line
  static const _threadsAhead = 6;                    // conversations saved ahead per round
  static const _keepMessages = 60;

  Timer? _timer;
  bool _on = false, _busy = false, _again = false;
  final Set<String> _due = {};                       // pull these on the next round (a stamp moved, a push came)
  Map<String, dynamic> _stamps = {};
  String _pulseTag = '';
  final Map<int, int> _threadTried = {};             // thread → the last message id it was saved ahead for

  /// The end of the last complete round.
  DateTime? lastSync;

  bool get _serverSyncs => toI(app.me?['sync']) >= 1;
  bool get running => _on;

  void start() {
    stop();
    _on = true;
    _timer = Timer.periodic(every, (_) {
      if (app.foreground) run();
    });
    run();
  }

  void stop() {
    _on = false;
    _timer?.cancel();
    _timer = null;
    _stamps = {};
    _pulseTag = '';
    _due.clear();
    _threadTried.clear();
  }

  /// A push came in while the app is open: pull what it is about now.
  void nudge(String type) {
    switch (type) {
      case 'complaint':
        _moved('complaints');
      case 'missed_call':
        _moved('calls');
      case 'vouchers':
        _moved('vouchers');
      case 'alert':
        _due.add(Pages.home);
      case 'chat':
        unawaited(app.pollChat());
        return;
      default:
        return;
    }
    run();
  }

  Future<void> run() async {
    if (!_on || !app.signedIn || !Store.I.isOpen) return;
    if (_busy) {
      _again = true;                                 // asked again while a round runs: one more round after it
      return;
    }
    _busy = true;
    try {
      do {
        _again = false;
        await _round();
      } while (_again && _on);
    } catch (_) {
      // a sync round never breaks the app; the next round tries again
    } finally {
      _busy = false;
    }
  }

  List<_Page> _plan() {
    if (app.business.isEmpty) return const [];
    return [
      const _Page(Pages.home, every: Duration(minutes: 1)),
      if (app.canDo('sales')) _Page(Pages.sales, query: Pages.day(DateTime.now()), every: const Duration(minutes: 2)),
      if (app.canDo('complaints')) const _Page(Pages.complaints, every: Duration(minutes: 3)),
      const _Page(Pages.customerCalls, every: Duration(minutes: 5)),
      if (app.canDo('vouchers')) ...const [
        _Page(Pages.vouchers, query: Pages.firstPage, every: Duration(minutes: 3)),
        _Page(Pages.batches, query: Pages.firstPage, every: Duration(minutes: 5)),
      ],
      if (app.canDo('vouchers_create')) const _Page(Pages.voucherForm, every: Duration(minutes: 30)),
      if (app.canDo('routers')) const _Page(Pages.routers, every: Duration(minutes: 1)),
      const _Page(Pages.devices, every: Duration(minutes: 30)),
    ];
  }

  /// A stamp moved: the pages it covers are out of date.
  void _moved(String stamp) {
    switch (stamp) {
      case 'sales':
        _due.addAll([Pages.home, Store.keyOf(Pages.sales, Pages.day(DateTime.now()))]);
      case 'complaints':
        _due.addAll([Pages.home, Pages.complaints]);
      case 'calls':
        _due.addAll([Pages.home, Pages.customerCalls]);
      case 'vouchers':
        _due.addAll([Store.keyOf(Pages.vouchers, Pages.firstPage), Store.keyOf(Pages.batches, Pages.firstPage)]);
      case 'me':
        app.refreshMe().catchError((_) {});
    }
  }

  Future<void> _round() async {
    final scope = Store.I.scope;
    if (_serverSyncs) await _pulse();
    if (!_on || scope != Store.I.scope) return;
    final now = DateTime.now();
    final due = <_Page>[];
    for (final p in _plan()) {
      final at = Store.I.savedAtKey(p.key);
      if (at == null || now.difference(at) >= p.every || _due.contains(p.key)) due.add(p);
    }
    for (var i = 0; i < due.length; i += _parallel) {
      if (!_on || scope != Store.I.scope || (i > 0 && !Api.I.online.value)) return;   // offline: try next round
      await Future.wait(due.skip(i).take(_parallel).map((p) async {
        try {
          await Store.I.fetch(p.path, query: p.query);
          _due.remove(p.key);
        } catch (_) {}
      }));
    }
    if (_serverSyncs && Api.I.online.value) await _threads(scope);
    lastSync = DateTime.now();
  }

  Future<void> _pulse() async {
    try {
      final r = await Api.I.getFresh(Pages.pulse, etag: _pulseTag, timeout: const Duration(seconds: 10));
      if (r.notModified) return;                     // nothing moved
      final s = ((r.data?['stamps'] as Map?) ?? const {}).cast<String, dynamic>();
      final before = _stamps;
      _stamps = s;
      _pulseTag = r.etag;
      if (before.isEmpty) return;                    // the first look only learns where things stand
      for (final k in s.keys) {
        if (before.containsKey(k) && before[k] != s[k]) _moved(k);
      }
    } catch (_) {}
  }

  /// Save ahead the conversations that have messages the phone does not have yet.
  Future<void> _threads(String scope) async {
    var n = 0;
    for (final t in List<Map<String, dynamic>>.of(app.threads)) {
      if (n >= _threadsAhead || !_on || scope != Store.I.scope || !Api.I.online.value) return;
      final id = toI(t['id']);
      final last = toI(t['last_id']);
      if (id <= 0 || last <= 0 || id == app.openThread || _threadTried[id] == last) continue;
      final have = Store.I.peek(Pages.thread(id));
      final msgs = asMaps(have?['messages']);
      if (have != null && msgs.isNotEmpty && toI(msgs.last['id']) >= last) continue;
      n++;
      try {
        await Store.I.fetch(Pages.thread(id), extra: const {'peek': '1'});
        _threadTried[id] = last;                     // saved: not again until a newer message comes
      } catch (_) {}
    }
  }

  /// Messages from the regular chat check (AppState.pollChat): add them to the saved conversations, so a
  /// conversation opens with everything in it. [since] is the ?since= that check asked with: new messages are only
  /// added to a saved conversation that already reaches that far (never leaving a gap).
  void mergeChat({required int since, required List<Map<String, dynamic>> fresh, required List<Map<String, dynamic>> changed}) {
    if (!Store.I.isOpen || (fresh.isEmpty && changed.isEmpty)) return;
    final ids = <int>{for (final m in fresh) toI(m['thread']), for (final m in changed) toI(m['thread'])};
    for (final id in ids) {
      if (id <= 0) continue;
      final path = Pages.thread(id);
      final page = Store.I.peek(path);
      if (page == null) continue;
      final msgs = asMaps(page['messages']);
      var dirty = false;
      for (final m in changed.where((m) => toI(m['thread']) == id)) {
        final i = msgs.indexWhere((x) => toI(x['id']) == toI(m['id']));
        if (i >= 0) {
          msgs[i] = _clean(m);
          dirty = true;
        }
      }
      final add = fresh.where((m) => toI(m['thread']) == id).toList();
      final lastHave = msgs.isEmpty ? 0 : toI(msgs.last['id']);
      if (add.isNotEmpty && since >= 0 && lastHave >= since) {
        for (final m in add) {
          if (!msgs.any((x) => toI(x['id']) == toI(m['id']))) {
            msgs.add(_clean(m));
            dirty = true;
          }
        }
        msgs.sort((a, b) => toI(a['id']).compareTo(toI(b['id'])));
      }
      if (!dirty) continue;
      if (msgs.length > _keepMessages) {
        msgs.removeRange(0, msgs.length - _keepMessages);
        page['more'] = true;
      }
      page['messages'] = msgs;
      Store.I.put(path, page);
    }
  }

  static Map<String, dynamic> _clean(Map<String, dynamic> m) => Map<String, dynamic>.from(m)..remove('notify');
}
