import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../data/store.dart';
import '../data/sync_engine.dart';
import '../widgets/common.dart';
import '../widgets/voucher_widgets.dart';
import 'new_vouchers_screen.dart';
import 'voucher_detail_screen.dart';

/// Vouchers — the website's Vouchers and Batches pages on the phone: find any voucher, see where it stands,
/// make new ones. What a person may do follows their TapTap role (server: core/views_mobile_vouchers.py).
class VouchersScreen extends StatefulWidget {
  const VouchersScreen({super.key});
  @override
  State<VouchersScreen> createState() => _VouchersScreenState();
}

class _VouchersScreenState extends State<VouchersScreen> {
  bool _batches = false;
  final _list = GlobalKey<VoucherListState>();
  final _batchList = GlobalKey<BatchListState>();

  Future<void> _new() async {
    final made = await Navigator.of(context).push<bool>(MaterialPageRoute(builder: (_) => const NewVouchersScreen()));
    if (made == true) {
      _list.currentState?.reload();
      _batchList.currentState?.reload();
    }
  }

  @override
  Widget build(BuildContext context) {
    final app = context.watch<AppState>();
    return Scaffold(
      appBar: AppBar(
        title: const Text('Vouchers'),
        actions: [
          if (app.canDo('vouchers_create'))
            Padding(
              padding: const EdgeInsets.only(right: 12),
              child: FilledButton.icon(
                style: FilledButton.styleFrom(minimumSize: const Size(0, 40)),
                onPressed: _new,
                icon: const Icon(Icons.add, size: 20),
                label: const Text('New'),
              ),
            ),
        ],
      ),
      body: Column(children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
          child: SizedBox(
            width: double.infinity,
            child: SegmentedButton<bool>(
              segments: const [
                ButtonSegment(value: false, label: Text('Vouchers'), icon: Icon(Icons.confirmation_number_outlined)),
                ButtonSegment(value: true, label: Text('Batches'), icon: Icon(Icons.inventory_2_outlined)),
              ],
              selected: {_batches},
              onSelectionChanged: (s) => setState(() => _batches = s.first),
            ),
          ),
        ),
        Expanded(
          child: IndexedStack(index: _batches ? 1 : 0, children: [
            VoucherList(key: _list),
            BatchList(key: _batchList),
          ]),
        ),
      ]),
    );
  }
}

/// One batch's vouchers (from Batches, a push, or after making a batch).
void openBatch(BuildContext context, int id, String title) {
  Navigator.of(context).push(MaterialPageRoute(
    builder: (_) => Scaffold(appBar: AppBar(title: Text(title)), body: VoucherList(batchId: id)),
  ));
}

/// A searchable, filterable, endless list of vouchers — the whole business, or one batch.
class VoucherList extends StatefulWidget {
  const VoucherList({super.key, this.batchId});
  final int? batchId;
  @override
  State<VoucherList> createState() => VoucherListState();
}

class VoucherListState extends State<VoucherList> {
  final _q = TextEditingController();
  Timer? _debounce;
  String _state = '';
  String _plan = '';
  List<Map<String, dynamic>> _rows = [];
  List<Map<String, dynamic>> _tabs = [];
  List<String> _plans = [];
  Map<String, dynamic>? _batch;
  int _page = 1;
  bool _more = false, _loading = false, _loaded = false;
  String? _err;
  int _seq = 0;
  StreamSubscription<String>? _saved;

  /// The query for page [page] with the current search, tab and plan.
  Map<String, String> _query(int page) => {
        if (_state.isNotEmpty) 'state': _state,
        if (_q.text.trim().isNotEmpty) 'q': _q.text.trim(),
        if (_plan.isNotEmpty) 'plan': _plan,
        if (widget.batchId != null) 'batch': '${widget.batchId}',
        'page': '$page',
      };

  /// First pages without a search are kept on the phone (searches are not: every letter typed would be one).
  bool get _keeps => _q.text.trim().isEmpty;

  @override
  void initState() {
    super.initState();
    _fromPhone();
    _saved = Store.I.changes.listen((key) {
      // the background sync brought a newer first page: show it, unless more pages are open or one is loading
      if (!mounted || !_keeps || _page != 1 || _loading || key != Store.keyOf(Pages.vouchers, _query(1))) return;
      final d = Store.I.peekKey(key);
      if (d != null) setState(() => _apply(d, 1));
    });
    _load();
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _saved?.cancel();
    _q.dispose();
    super.dispose();
  }

  /// This search's first page as saved on the phone — on screen at once (no setState: callers do that).
  bool _fromPhone() {
    final d = _keeps ? Store.I.peek(Pages.vouchers, _query(1)) : null;
    if (d == null) return false;
    _apply(d, 1);
    return true;
  }

  void _apply(Map<String, dynamic> d, int page) {
    final rows = asMaps(d['vouchers']);
    _rows = page == 1 ? rows : [..._rows, ...rows];
    _tabs = asMaps(d['tabs']);
    _plans = ((d['plans'] as List?) ?? const []).map((e) => '$e').toList();
    _batch = (d['batch'] as Map?)?.cast<String, dynamic>();
    _more = d['more'] == true;
    _err = null;
    _loaded = true;
  }

  Future<void> reload() => _load(reset: true);

  Future<void> _load({bool reset = true}) async {
    var saved = false;
    if (reset) {
      _page = 1;
      saved = _fromPhone();                          // a tab or plan seen before: its saved page at once
    }
    final page = _page;
    final seq = ++_seq;
    setState(() => _loading = true);
    try {
      final q = _query(page);
      final d = page == 1 && _keeps ? await Store.I.fetch(Pages.vouchers, query: q) : await Api.I.get(Pages.vouchers, query: q);
      if (!mounted || seq != _seq) return;
      setState(() {
        _apply(d, page);
        _loading = false;
      });
    } on ApiError catch (e) {
      if (!mounted || seq != _seq) return;
      setState(() {
        // no internet but this page is on screen from the phone: the offline strip says so
        _err = (e.status == 0 && saved) ? null : e.message;
        _loading = false;
        _loaded = true;
      });
    }
  }

  void _next() {
    if (_loading || !_more) return;
    _page += 1;
    _load(reset: false);
  }

  void _search(String _) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 380), () => _load());
  }

  Future<void> _pickPlan() async {
    final chosen = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: ListView(shrinkWrap: true, children: [
          const Padding(padding: EdgeInsets.fromLTRB(20, 0, 20, 8), child: Text('Plan', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w800))),
          ListTile(
            leading: Icon(_plan.isEmpty ? Icons.radio_button_checked : Icons.radio_button_off),
            title: const Text('All plans'),
            onTap: () => Navigator.pop(ctx, ''),
          ),
          for (final p in _plans)
            ListTile(
              leading: Icon(_plan == p ? Icons.radio_button_checked : Icons.radio_button_off),
              title: Text(p),
              onTap: () => Navigator.pop(ctx, p),
            ),
        ]),
      ),
    );
    if (chosen != null && chosen != _plan) {
      setState(() => _plan = chosen);
      _load();
    }
  }

  Future<void> _open(Map<String, dynamic> row) async {
    final after = await openVoucher(context, toI(row['id']));
    if (!mounted || after == null) return;
    setState(() {
      final i = _rows.indexWhere((r) => r['id'] == row['id']);
      if (i < 0) return;
      if (after['deleted'] == true) {
        _rows.removeAt(i);
      } else {
        _rows[i] = after;
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final cur = context.read<AppState>().currency;
    final cs = Theme.of(context).colorScheme;
    return RefreshIndicator(
      onRefresh: reload,
      child: NotificationListener<ScrollNotification>(
        onNotification: (n) {
          if (n.metrics.extentAfter < 500) _next();
          return false;
        },
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 110),
          children: [
            if (_batch != null) ...[BatchSummary(batch: _batch!, currency: cur), const SizedBox(height: 12)],
            TextField(
              controller: _q,
              onChanged: _search,
              textInputAction: TextInputAction.search,
              onSubmitted: (_) => _load(),
              decoration: InputDecoration(
                hintText: 'Code, serial, customer or batch — C* works too',
                prefixIcon: const Icon(Icons.search),
                suffixIcon: Row(mainAxisSize: MainAxisSize.min, children: [
                  if (_q.text.isNotEmpty)
                    IconButton(
                      tooltip: 'Clear',
                      icon: const Icon(Icons.close),
                      onPressed: () {
                        _q.clear();
                        _load();
                      },
                    ),
                  if (_plans.length > 1)
                    IconButton(
                      tooltip: 'Plan',
                      icon: Icon(Icons.filter_list, color: _plan.isEmpty ? null : cs.primary),
                      onPressed: _pickPlan,
                    ),
                ]),
              ),
            ),
            const SizedBox(height: 10),
            SizedBox(
              height: 40,
              child: ListView(scrollDirection: Axis.horizontal, children: [
                if (_plan.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: InputChip(
                      label: Text(_plan),
                      avatar: const Icon(Icons.sell_outlined, size: 18),
                      onDeleted: () {
                        setState(() => _plan = '');
                        _load();
                      },
                    ),
                  ),
                for (final t in _tabs)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text('${t['label']} · ${t['count']}'),
                      selected: _state == '${t['key']}',
                      onSelected: (_) {
                        setState(() => _state = '${t['key']}');
                        _load();
                      },
                    ),
                  ),
              ]),
            ),
            const SizedBox(height: 10),
            if (_err != null) ErrorCard(message: _err!, onRetry: reload),
            if (!_loaded && _err == null) ...[
              for (var i = 0; i < 4; i++) ...[const SkeletonCard(height: 76), const SizedBox(height: 10)],
            ],
            if (_loaded && _err == null && _rows.isEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 60),
                child: Column(children: [
                  Icon(Icons.confirmation_number_outlined, size: 56, color: cs.onSurfaceVariant),
                  const SizedBox(height: 8),
                  Text(_q.text.isEmpty && _state.isEmpty && _plan.isEmpty ? 'No vouchers yet' : 'Nothing matches',
                      style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800)),
                  Text(_q.text.isEmpty && _state.isEmpty && _plan.isEmpty ? 'Tap New to make your first batch.' : 'Try another search or tab.'),
                ]),
              ),
            for (final r in _rows) ...[
              VoucherTile(v: r, currency: cur, onTap: () => _open(r)),
              const SizedBox(height: 8),
            ],
            if (_more)
              Padding(
                padding: const EdgeInsets.all(12),
                child: Center(
                  child: _loading
                      ? const SizedBox(width: 24, height: 24, child: CircularProgressIndicator(strokeWidth: 2.5))
                      : TextButton(onPressed: _next, child: const Text('Show more')),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// Batches, newest first, with how many are still in stock.
class BatchList extends StatefulWidget {
  const BatchList({super.key});
  @override
  State<BatchList> createState() => BatchListState();
}

class BatchListState extends State<BatchList> {
  List<Map<String, dynamic>> _rows = [];
  int _page = 1;
  bool _more = false, _loading = false, _loaded = false;
  String? _err;
  StreamSubscription<String>? _saved;

  @override
  void initState() {
    super.initState();
    final d = Store.I.peek(Pages.batches, Pages.firstPage);      // saved on the phone: shown at once
    if (d != null) _apply(d, 1);
    _saved = Store.I.changes.listen((key) {
      if (!mounted || _page != 1 || _loading || key != Store.keyOf(Pages.batches, Pages.firstPage)) return;
      final d = Store.I.peekKey(key);
      if (d != null) setState(() => _apply(d, 1));
    });
    _load();
  }

  @override
  void dispose() {
    _saved?.cancel();
    super.dispose();
  }

  void _apply(Map<String, dynamic> d, int page) {
    final rows = asMaps(d['batches']);
    _rows = page == 1 ? rows : [..._rows, ...rows];
    _more = d['more'] == true;
    _err = null;
    _loaded = true;
  }

  Future<void> reload() => _load();

  Future<void> _load({bool reset = true}) async {
    if (reset) _page = 1;
    final page = _page;
    setState(() => _loading = true);
    try {
      final d = page == 1
          ? await Store.I.fetch(Pages.batches, query: Pages.firstPage)
          : await Api.I.get(Pages.batches, query: {'page': '$page'});
      if (!mounted) return;
      setState(() {
        _apply(d, page);
        _loading = false;
      });
    } on ApiError catch (e) {
      if (!mounted) return;
      setState(() {
        _err = (e.status == 0 && page == 1 && _rows.isNotEmpty) ? null : e.message;
        _loading = false;
        _loaded = true;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final cur = context.read<AppState>().currency;
    return RefreshIndicator(
      onRefresh: reload,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 110),
        children: [
          if (_err != null) ErrorCard(message: _err!, onRetry: reload),
          if (!_loaded && _err == null) ...[
            for (var i = 0; i < 3; i++) ...[const SkeletonCard(height: 96), const SizedBox(height: 10)],
          ],
          if (_loaded && _err == null && _rows.isEmpty) const EmptyNote('No batches yet.'),
          for (final b in _rows) ...[
            BatchSummary(batch: b, currency: cur, onTap: () => openBatch(context, toI(b['id']), '${b['name']}')),
            const SizedBox(height: 10),
          ],
          if (_more)
            Center(
              child: _loading
                  ? const Padding(padding: EdgeInsets.all(12), child: CircularProgressIndicator(strokeWidth: 2.5))
                  : TextButton(
                      onPressed: () {
                        _page += 1;
                        _load(reset: false);
                      },
                      child: const Text('Show more'),
                    ),
            ),
        ],
      ),
    );
  }
}
