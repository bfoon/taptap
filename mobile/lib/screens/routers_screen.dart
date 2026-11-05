import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../data/store.dart';
import '../data/sync_engine.dart';
import '../widgets/common.dart';
import 'quick_sell.dart';
import 'router_move_sheet.dart';
import 'router_schedules_screen.dart';

const _green = Color(0xFF16A34A);
const _amber = Color(0xFFF59E0B);

/// Routers — every MikroTik on its own tab, like the website's router tabs (server: core/views_mobile_routers.py).
/// "All" shows each router as a card; a router's tab shows who is online there, its own activity and what you can
/// do with it: quick sell on it, sync, test, disconnect someone, reboot, move it to another business.
class RoutersScreen extends StatefulWidget {
  const RoutersScreen({super.key, this.initialRouter});
  final int? initialRouter;
  @override
  State<RoutersScreen> createState() => _RoutersScreenState();
}

class _RoutersScreenState extends State<RoutersScreen> with TickerProviderStateMixin {
  Map<String, dynamic>? _d;
  String? _err;
  TabController? _tabs;
  Timer? _timer;
  bool _first = true;

  List<Map<String, dynamic>> get _routers => asMaps(_d?['routers']);
  Map<String, dynamic> get _can => (_d?['can'] as Map?)?.cast<String, dynamic>() ?? {};

  StreamSubscription<String>? _saved;

  @override
  void initState() {
    super.initState();
    final saved = Store.I.peek(Pages.routers);          // saved on the phone: the routers at once
    if (saved != null) _apply(saved);
    _saved = Store.I.changes.listen((key) {
      if (!mounted || key != Pages.routers) return;
      final d = Store.I.peekKey(key);
      if (d != null) setState(() => _apply(d));
    });
    _load();
    _timer = Timer.periodic(const Duration(seconds: 20), (_) => _load(quiet: true));
  }

  @override
  void dispose() {
    _timer?.cancel();
    _saved?.cancel();
    _tabs?.dispose();
    super.dispose();
  }

  /// Take a routers answer (no setState: callers do that).
  void _apply(Map<String, dynamic> d) {
    final routers = asMaps(d['routers']);
    final n = routers.length + 1;
    if (_tabs == null || _tabs!.length != n) {
      var start = (_tabs?.index ?? 0).clamp(0, n - 1);
      if (_first && widget.initialRouter != null) {
        final i = routers.indexWhere((r) => toI(r['id']) == widget.initialRouter);
        if (i >= 0) start = i + 1;
      }
      final old = _tabs;
      _tabs = TabController(length: n, vsync: this, initialIndex: start)
        ..addListener(() {
          if (mounted) setState(() {});
        });
      if (old != null) WidgetsBinding.instance.addPostFrameCallback((_) => old.dispose());
    }
    // the router asked for (from a push or a link) is only picked once it is in the list
    if (widget.initialRouter == null || routers.any((r) => toI(r['id']) == widget.initialRouter)) _first = false;
    _d = d;
    _err = null;
  }

  Future<void> _load({bool quiet = false}) async {
    try {
      final d = await Store.I.fetch(Pages.routers);
      if (!mounted) return;
      setState(() => _apply(d));
    } on ApiError catch (e) {
      if (mounted && !quiet && !(e.status == 0 && _d != null)) setState(() => _err = e.message);
    }
  }

  int? get _currentRouter {
    final i = _tabs?.index ?? 0;
    return i == 0 || i > _routers.length ? null : toI(_routers[i - 1]['id']);
  }

  @override
  Widget build(BuildContext context) {
    final app = context.watch<AppState>();
    final cs = Theme.of(context).colorScheme;
    final canSell = app.canDo('vouchers_create');
    return Scaffold(
      appBar: AppBar(
        title: const Text('Routers'),
        actions: [
          IconButton(tooltip: 'Schedules', onPressed: () => openRouterSchedules(context), icon: const Icon(Icons.event_repeat)),
          IconButton(tooltip: 'Refresh', onPressed: _load, icon: const Icon(Icons.refresh)),
        ],
        bottom: _tabs == null || _routers.length < 2
            ? null
            : TabBar(
                controller: _tabs,
                isScrollable: true,
                tabAlignment: TabAlignment.start,
                tabs: [
                  const Tab(text: 'All'),
                  for (final r in _routers)
                    Tab(
                      child: Row(mainAxisSize: MainAxisSize.min, children: [
                        Icon(Icons.circle, size: 9, color: r['online'] == true ? _green : cs.error),
                        const SizedBox(width: 6),
                        Text('${r['name']}'),
                      ]),
                    ),
                ],
              ),
      ),
      floatingActionButton: canSell && _routers.isNotEmpty
          ? FloatingActionButton.extended(
              onPressed: () => showQuickSell(context, routerId: _currentRouter),
              icon: const Icon(Icons.bolt),
              label: const Text('Quick sell'),
            )
          : null,
      body: _d == null
          ? Padding(
              padding: const EdgeInsets.all(16),
              child: _err == null ? const SkeletonCard(height: 180) : ErrorCard(message: _err!, onRetry: _load),
            )
          : _routers.isEmpty
              ? const Padding(
                  padding: EdgeInsets.all(16),
                  child: InfoCard(
                    icon: Icons.router_outlined,
                    title: 'No routers yet',
                    text: 'Add your MikroTik on the website (MikroTik Control › TapTap Link). It shows up here at once.',
                  ),
                )
              : _routers.length == 1
                  ? RouterTab(card: _routers.first, can: _can, onChanged: _load)
                  : TabBarView(controller: _tabs, children: [
                      _AllTab(
                        routers: _routers,
                        currency: '${_d?['currency'] ?? app.currency}',
                        onOpen: (i) => _tabs?.animateTo(i + 1),
                        onRefresh: _load,
                      ),
                      for (final r in _routers) RouterTab(key: ValueKey(r['id']), card: r, can: _can, onChanged: _load),
                    ]),
    );
  }
}

// ─────────────────────────── All routers ───────────────────────────
class _AllTab extends StatelessWidget {
  const _AllTab({required this.routers, required this.currency, required this.onOpen, required this.onRefresh});
  final List<Map<String, dynamic>> routers;
  final String currency;
  final void Function(int) onOpen;
  final Future<void> Function() onRefresh;

  @override
  Widget build(BuildContext context) {
    final online = routers.where((r) => r['online'] == true).length;
    final people = routers.fold<int>(0, (a, r) => a + toI(r['users_online']));
    final revenue = routers.fold<double>(0, (a, r) => a + toD(r['revenue_today']));
    final cs = Theme.of(context).colorScheme;
    return RefreshIndicator(
      onRefresh: onRefresh,
      child: ListView(padding: const EdgeInsets.fromLTRB(16, 12, 16, 110), children: [
        Row(children: [
          _Stat(label: 'Online', value: '$online/${routers.length}', icon: Icons.router, color: online == routers.length ? _green : _amber),
          const SizedBox(width: 8),
          _Stat(label: 'People on Wi-Fi', value: '$people', icon: Icons.people_alt_outlined, color: cs.primary),
          const SizedBox(width: 8),
          _Stat(label: 'Sold today', value: compactMoney(revenue, currency), icon: Icons.payments_outlined, color: _green),
        ]),
        const SizedBox(height: 12),
        for (var i = 0; i < routers.length; i++) ...[
          _RouterCard(r: routers[i], currency: currency, onTap: () => onOpen(i)),
          const SizedBox(height: 10),
        ],
      ]),
    );
  }
}

class _Stat extends StatelessWidget {
  const _Stat({required this.label, required this.value, required this.icon, required this.color});
  final String label, value;
  final IconData icon;
  final Color color;
  @override
  Widget build(BuildContext context) => Expanded(
        child: Card(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 12),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Icon(icon, color: color, size: 20),
              const SizedBox(height: 6),
              FittedBox(child: Text(value, style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w900))),
              Text(label, maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 11.5, color: Theme.of(context).colorScheme.onSurfaceVariant)),
            ]),
          ),
        ),
      );
}

class _RouterCard extends StatelessWidget {
  const _RouterCard({required this.r, required this.currency, required this.onTap});
  final Map<String, dynamic> r;
  final String currency;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final on = r['online'] == true;
    final warnings = asMaps(r['warnings']);
    final sync = (r['sync'] as Map?)?.cast<String, dynamic>();
    final syncing = sync != null && (sync['status'] == 'queued' || sync['status'] == 'running');
    return Card(
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              _Pulse(on: on),
              const SizedBox(width: 10),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('${r['name']}', style: const TextStyle(fontSize: 16.5, fontWeight: FontWeight.w800)),
                  Text(
                    [r['status'], r['mode'], if ('${r['seen']}'.isNotEmpty) 'seen ${r['seen']}'].join(' · '),
                    style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant),
                  ),
                ]),
              ),
              Icon(Icons.chevron_right, color: cs.onSurfaceVariant),
            ]),
            const SizedBox(height: 12),
            Row(children: [
              _Mini(icon: Icons.wifi, text: '${toI(r['users_online'])} online'),
              _Mini(icon: Icons.timelapse, text: '${toI(r['in_use'])} in use'),
              _Mini(icon: Icons.inventory_2_outlined, text: '${toI(r['in_stock'])} in stock'),
              _Mini(icon: Icons.payments_outlined, text: compactMoney(toD(r['revenue_today']), currency)),
            ]),
            if (syncing) ...[
              const SizedBox(height: 10),
              Text('Syncing · ${sync['phase'] ?? ''}', style: const TextStyle(fontSize: 12)),
              const SizedBox(height: 4),
              LinearProgressIndicator(value: toI(sync['progress']) / 100),
            ],
            for (final w in warnings.take(2)) ...[
              const SizedBox(height: 10),
              _Warning(w: w),
            ],
          ]),
        ),
      ),
    );
  }
}

class _Mini extends StatelessWidget {
  const _Mini({required this.icon, required this.text});
  final IconData icon;
  final String text;
  @override
  Widget build(BuildContext context) => Expanded(
        child: Row(children: [
          Icon(icon, size: 15, color: Theme.of(context).colorScheme.primary),
          const SizedBox(width: 4),
          Flexible(child: Text(text, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w600))),
        ]),
      );
}

class _Warning extends StatelessWidget {
  const _Warning({required this.w});
  final Map<String, dynamic> w;
  @override
  Widget build(BuildContext context) {
    final danger = w['level'] == 'danger';
    final c = danger ? Theme.of(context).colorScheme.error : const Color(0xFFB45309);
    return Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(color: c.withValues(alpha: .1), borderRadius: BorderRadius.circular(12)),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Icon(danger ? Icons.error_outline : Icons.warning_amber, size: 18, color: c),
        const SizedBox(width: 8),
        Expanded(child: Text('${w['text']}', style: const TextStyle(fontSize: 12.5))),
      ]),
    );
  }
}

/// A breathing dot: green and pulsing when the router answers, red when it does not.
class _Pulse extends StatefulWidget {
  const _Pulse({required this.on});
  final bool on;
  @override
  State<_Pulse> createState() => _PulseState();
}

class _PulseState extends State<_Pulse> with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 1600))..repeat();
  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final color = widget.on ? _green : Theme.of(context).colorScheme.error;
    return SizedBox(
      width: 40,
      height: 40,
      child: Stack(alignment: Alignment.center, children: [
        if (widget.on && !MediaQuery.of(context).disableAnimations)
          AnimatedBuilder(
            animation: _c,
            builder: (_, __) => Container(
              width: 16 + 22 * _c.value,
              height: 16 + 22 * _c.value,
              decoration: BoxDecoration(shape: BoxShape.circle, color: color.withValues(alpha: .25 * (1 - _c.value))),
            ),
          ),
        Container(
          width: 32,
          height: 32,
          decoration: BoxDecoration(shape: BoxShape.circle, color: color.withValues(alpha: .15)),
          child: Icon(Icons.router, size: 18, color: color),
        ),
      ]),
    );
  }
}

// ─────────────────────────── one router ───────────────────────────
class RouterTab extends StatefulWidget {
  const RouterTab({super.key, required this.card, required this.can, required this.onChanged});
  final Map<String, dynamic> card;
  final Map<String, dynamic> can;
  final Future<void> Function({bool quiet}) onChanged;
  @override
  State<RouterTab> createState() => _RouterTabState();
}

class _RouterTabState extends State<RouterTab> {
  Map<String, dynamic>? _d;
  String? _err;
  Timer? _timer;
  final _q = TextEditingController();
  final Set<String> _busy = {};

  int get _id => toI(widget.card['id']);
  String get _name => '${widget.card['name']}';
  String get _path => '/api/mobile/v1/routers/$_id/';
  Map<String, dynamic> get _r => (_d?['router'] as Map?)?.cast<String, dynamic>() ?? widget.card;

  @override
  void initState() {
    super.initState();
    _d = Store.I.peek(_path);                          // saved on the phone: shown at once
    _load();
    _timer = Timer.periodic(const Duration(seconds: 15), (_) => _load(quiet: true));
  }

  @override
  void dispose() {
    _timer?.cancel();
    _q.dispose();
    super.dispose();
  }

  Future<void> _load({bool quiet = false}) async {
    try {
      final d = await Store.I.fetch(_path);
      if (!mounted) return;
      setState(() {
        _d = d;
        _err = null;
      });
    } on ApiError catch (e) {
      if (mounted && !quiet && !(e.status == 0 && _d != null)) setState(() => _err = e.message);
    }
  }

  void _toast(String m, {bool error = false}) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(m),
        behavior: SnackBarBehavior.floating,
        backgroundColor: error ? Theme.of(context).colorScheme.error : null,
      ));

  Future<void> _act(String action, [Map<String, dynamic> extra = const {}]) async {
    final key = '$action${extra['user'] ?? ''}';
    setState(() => _busy.add(key));
    try {
      final d = await Api.I.post(_path, {'action': action, ...extra});
      HapticFeedback.selectionClick();
      _toast('${d['message'] ?? 'Done.'}');
      await _load(quiet: true);
      await widget.onChanged(quiet: true);
    } on ApiError catch (e) {
      _toast(e.message, error: true);
    } finally {
      if (mounted) setState(() => _busy.remove(key));
    }
  }

  Future<void> _kick(Map<String, dynamic> s) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: Text('Disconnect ${s['user']}?'),
        content: const Text('They are logged out now. Their voucher keeps its time and they can log in again.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Disconnect')),
        ],
      ),
    );
    if (ok == true) await _act('kick', {'user': s['user'], 'item_id': s['item_id']});
  }

  Future<void> _reboot() async {
    final typed = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, setDialog) => AlertDialog(
          title: Text('Reboot $_name?'),
          content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Text('Everyone on this router loses the Internet for 1–2 minutes.'),
            const SizedBox(height: 12),
            TextField(controller: typed, onChanged: (_) => setDialog(() {}), decoration: InputDecoration(labelText: 'Type “$_name”')),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
            FilledButton(
              style: FilledButton.styleFrom(backgroundColor: Theme.of(c).colorScheme.error),
              onPressed: typed.text.trim().toLowerCase() == _name.trim().toLowerCase() ? () => Navigator.pop(c, true) : null,
              child: const Text('Reboot'),
            ),
          ],
        ),
      ),
    );
    final confirm = typed.text.trim();
    // the dialog's closing animation still shows the field for a moment
    Future.delayed(const Duration(milliseconds: 400), typed.dispose);
    if (ok == true) await _act('reboot', {'confirm': confirm});
  }

  Future<void> _move() async {
    final msg = await showMoveRouter(context, routerId: _id, routerName: _name);
    if (msg == null || !mounted) return;
    _toast(msg);
    await widget.onChanged(quiet: false);
  }

  @override
  Widget build(BuildContext context) {
    final app = context.watch<AppState>();
    final cs = Theme.of(context).colorScheme;
    final r = _r;
    final on = r['online'] == true;
    final sessions = asMaps(_d?['sessions']);
    final q = _q.text.trim().toUpperCase();
    final shown = q.isEmpty
        ? sessions
        : sessions.where((s) => '${s['user']} ${s['mac']} ${s['ip']}'.toUpperCase().contains(q)).toList();
    final activity = asMaps(_d?['activity']);
    final sessionErrors = (_d?['session_errors'] as List? ?? const []);
    final manage = widget.can['manage'] == true;
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(padding: const EdgeInsets.fromLTRB(16, 12, 16, 110), children: [
        if (_err != null) ErrorCard(message: _err!, onRetry: _load),
        // hero
        Container(
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(24),
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: on ? [const Color(0xFF0F766E), const Color(0xFF16A34A)] : [const Color(0xFF7F1D1D), const Color(0xFFB91C1C)],
            ),
          ),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              const Icon(Icons.router, color: Colors.white),
              const SizedBox(width: 8),
              Expanded(child: Text(_name, style: const TextStyle(color: Colors.white, fontSize: 20, fontWeight: FontWeight.w900))),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                decoration: BoxDecoration(color: Colors.white24, borderRadius: BorderRadius.circular(99)),
                child: Text('${r['status']}', style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w800)),
              ),
            ]),
            const SizedBox(height: 6),
            Text(
              [r['mode'], if ('${r['board']}'.isNotEmpty) r['board'], if ('${r['ros']}'.isNotEmpty) 'RouterOS ${r['ros']}',
                if ('${r['seen']}'.isNotEmpty) 'seen ${r['seen']}'].join(' · '),
              style: const TextStyle(color: Colors.white70, fontSize: 12.5),
            ),
            const SizedBox(height: 14),
            Row(children: [
              _HeroNum(value: '${sessions.isNotEmpty ? sessions.length : toI(r['users_online'])}', label: 'online now'),
              _HeroNum(value: '${toI(r['in_use'])}', label: 'in use'),
              _HeroNum(value: '${toI(r['sales_today'])}', label: 'sold today'),
              _HeroNum(value: compactMoney(toD(r['revenue_today']), app.currency), label: 'today'),
            ]),
          ]),
        ),
        for (final w in asMaps(r['warnings'])) ...[const SizedBox(height: 10), _Warning(w: w)],
        const SizedBox(height: 12),
        Wrap(spacing: 8, runSpacing: 8, children: [
          if (app.canDo('vouchers_create'))
            FilledButton.icon(
              onPressed: () => showQuickSell(context, routerId: _id),
              icon: const Icon(Icons.bolt, size: 18),
              label: const Text('Sell here'),
            ),
          if (manage) ...[
            _ActionButton(icon: Icons.sync, label: 'Sync', busy: _busy.contains('sync'), onTap: () => _act('sync')),
            _ActionButton(icon: Icons.network_check, label: 'Test', busy: _busy.contains('test'), onTap: () => _act('test')),
            _ActionButton(icon: Icons.restart_alt, label: 'Reboot', busy: _busy.contains('reboot'), onTap: _reboot),
            _ActionButton(icon: Icons.event_repeat, label: 'Schedule', busy: false, onTap: () => openRouterSchedules(context, routerId: _id)),
          ],
          if (widget.can['move'] == true)
            _ActionButton(icon: Icons.drive_file_move_outline, label: 'Move', busy: false, onTap: _move),
        ]),
        const SizedBox(height: 14),
        SectionCard(
          title: 'People online',
          trailing: Text('${sessions.length}', style: TextStyle(color: cs.onSurfaceVariant, fontWeight: FontWeight.w700)),
          child: Column(children: [
            if (sessions.length > 6)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: TextField(
                  controller: _q,
                  onChanged: (_) => setState(() {}),
                  decoration: const InputDecoration(isDense: true, prefixIcon: Icon(Icons.search), hintText: 'Code, MAC or IP'),
                ),
              ),
            if (_d == null)
              const LinearProgressIndicator()
            else if (sessions.isEmpty)
              EmptyNote(sessionErrors.isNotEmpty ? '${sessionErrors.first}' : 'Nobody is online on this router right now.')
            else
              for (final s in shown.take(60))
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: CircleAvatar(backgroundColor: cs.primaryContainer, child: Icon(Icons.smartphone, color: cs.onPrimaryContainer, size: 20)),
                  title: Text('${s['user']}', style: const TextStyle(fontFamily: 'monospace', fontWeight: FontWeight.w800)),
                  subtitle: Text([s['uptime'], s['ip'], s['mac']].where((x) => '$x'.isNotEmpty).join(' · '),
                      style: const TextStyle(fontSize: 12)),
                  trailing: widget.can['kick'] == true
                      ? (_busy.contains('kick${s['user']}')
                          ? const SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2))
                          : IconButton(tooltip: 'Disconnect', icon: const Icon(Icons.logout), onPressed: () => _kick(s)))
                      : null,
                ),
          ]),
        ),
        const SizedBox(height: 14),
        SectionCard(
          title: 'Activity on $_name',
          child: activity.isEmpty
              ? const EmptyNote('Nothing recorded on this router yet.')
              : Column(children: [
                  for (final a in activity)
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 6),
                      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Icon(a['ok'] == false ? Icons.error_outline : Icons.check_circle_outline, size: 18,
                            color: a['ok'] == false ? cs.error : _green),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                            Text('${a['type']}', style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 13)),
                            Text('${a['text']}', style: const TextStyle(fontSize: 12.5)),
                          ]),
                        ),
                        Text('${a['ago']}', style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant)),
                      ]),
                    ),
                ]),
        ),
      ]),
    );
  }
}

class _HeroNum extends StatelessWidget {
  const _HeroNum({required this.value, required this.label});
  final String value, label;
  @override
  Widget build(BuildContext context) => Expanded(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          FittedBox(child: Text(value, style: const TextStyle(color: Colors.white, fontSize: 22, fontWeight: FontWeight.w900))),
          Text(label, style: const TextStyle(color: Colors.white70, fontSize: 11.5)),
        ]),
      );
}

class _ActionButton extends StatelessWidget {
  const _ActionButton({required this.icon, required this.label, required this.busy, required this.onTap});
  final IconData icon;
  final String label;
  final bool busy;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) => FilledButton.tonalIcon(
        onPressed: busy ? null : onTap,
        icon: busy ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : Icon(icon, size: 18),
        label: Text(label),
      );
}
