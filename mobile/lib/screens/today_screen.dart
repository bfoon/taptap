import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../widgets/charts.dart';
import '../widgets/common.dart';
import 'sales_detail_screen.dart';

/// The first page: today's sales, live — plus what needs you right now (complaints, missed customer calls, messages).
class TodayScreen extends StatefulWidget {
  const TodayScreen({super.key, required this.goTab, required this.askFit});
  final void Function(int tab) goTab;
  final void Function(String question) askFit;
  @override
  State<TodayScreen> createState() => _TodayScreenState();
}

class _TodayScreenState extends State<TodayScreen> {
  DateTime _day = DateUtils.dateOnly(DateTime.now());
  Map<String, dynamic>? _s;          // /sales/ for the chosen day
  Map<String, dynamic>? _home;       // /home/ (pulse + customer calls) — today only
  String? _err;
  String _fp = '';
  Timer? _live;
  int _lastSaleId = 0;
  Map<String, dynamic>? _flash;      // "+D25 · 24 Hours" when a sale comes in

  bool get _isToday => _day == DateUtils.dateOnly(DateTime.now());

  @override
  void initState() {
    super.initState();
    _load();
    _live = Timer.periodic(const Duration(seconds: 15), (_) => _refresh(quiet: true));
  }

  @override
  void dispose() {
    _live?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _s = null;
      _err = null;
      _fp = '';
    });
    await _refresh();
  }

  Future<void> _refresh({bool quiet = false}) async {
    final app = context.read<AppState>();
    try {
      final futures = <Future>[];
      if (app.canDo('sales')) {
        futures.add(Api.I.get('/api/mobile/v1/sales/', query: {'date': ymd(_day), if (quiet && _fp.isNotEmpty) 'fp': _fp}).then((d) {
          if (d['changed'] == false) return;
          _fp = '${d['fp']}';
          final sales = (d['sales'] as List? ?? []);
          final newest = sales.isEmpty ? 0 : toI((sales.first as Map)['id']);
          if (quiet && _isToday && _lastSaleId > 0 && newest > _lastSaleId) _celebrate(sales.first as Map);
          _lastSaleId = newest;
          _s = d;
        }));
      }
      if (_isToday) futures.add(Api.I.get('/api/mobile/v1/home/').then((d) => _home = d));
      await Future.wait(futures);
      if (mounted) setState(() => _err = null);
    } on ApiError catch (e) {
      if (!quiet && mounted) setState(() => _err = e.message);
    }
  }

  void _celebrate(Map sale) {
    HapticFeedback.mediumImpact();
    setState(() => _flash = sale.cast<String, dynamic>());
    Timer(const Duration(seconds: 4), () {
      if (mounted) setState(() => _flash = null);
    });
  }

  void _go(int days) {
    final next = _day.add(Duration(days: days));
    if (next.isAfter(DateUtils.dateOnly(DateTime.now()))) return;
    HapticFeedback.selectionClick();
    setState(() => _day = next);
    _load();
  }

  @override
  Widget build(BuildContext context) {
    final app = context.watch<AppState>();
    final cur = app.currency;
    final s = _s;
    return Scaffold(
      body: Stack(children: [
        RefreshIndicator(
          onRefresh: () => _refresh(),
          child: CustomScrollView(slivers: [
            SliverAppBar(
              pinned: true,
              expandedHeight: 96,
              backgroundColor: Theme.of(context).scaffoldBackgroundColor,
              flexibleSpace: FlexibleSpaceBar(
                titlePadding: const EdgeInsetsDirectional.only(start: 20, bottom: 12),
                title: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('${_home?['greeting'] ?? 'Hello'}, ${app.user['first'] ?? ''}',
                      style: TextStyle(fontSize: 11, color: Theme.of(context).colorScheme.onSurfaceVariant, fontWeight: FontWeight.w600)),
                  Text('${app.business['name'] ?? 'TapTap'}',
                      style: TextStyle(fontSize: 19, fontWeight: FontWeight.w800, color: Theme.of(context).colorScheme.onSurface)),
                ]),
              ),
              actions: [
                if (app.canDo('fit'))
                  IconButton(
                    tooltip: 'Brief me',
                    icon: const Icon(Icons.auto_awesome),
                    onPressed: () => widget.askFit('Brief me on today: sales, anything wrong on the network, open customer reports.'),
                  ),
              ],
            ),
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 110),
              sliver: SliverList.list(children: [
                if (_err != null) ErrorCard(message: _err!, onRetry: _load),
                if (!app.canDo('sales'))
                  const InfoCard(icon: Icons.lock_outline, title: 'Daily sales are not part of your role', text: 'Ask the owner if you need them.')
                else ...[
                  _DayStrip(day: _day, onPrev: () => _go(-1), onNext: _isToday ? null : () => _go(1)),
                  const SizedBox(height: 10),
                  GestureDetector(
                    onHorizontalDragEnd: (d) {
                      if ((d.primaryVelocity ?? 0) > 300) _go(-1);
                      if ((d.primaryVelocity ?? 0) < -300) _go(1);
                    },
                    child: s == null ? const SkeletonCard(height: 200) : _Hero(s: s, cur: cur, isToday: _isToday, brand: app.brand),
                  ),
                ],
                if (_isToday && _home != null) ...[
                  const SizedBox(height: 14),
                  _Pulse(home: _home!, app: app, goTab: widget.goTab),
                ],
                if (_isToday && (_home?['calls'] as List? ?? []).isNotEmpty) ...[
                  const SizedBox(height: 14),
                  _CallsCard(calls: (_home!['calls'] as List).cast<Map>()),
                ],
                if (s != null && app.canDo('sales')) ...[
                  const SizedBox(height: 14),
                  SectionCard(
                    title: 'Hour by hour',
                    trailing: s['busiest'] == null
                        ? null
                        : Text('Busiest ${(s['busiest'] as Map)['label']}:00', style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700)),
                    child: HourBars(hours: (s['hours'] as List? ?? List.generate(24, (_) => {'amount': 0, 'count': 0})), currency: cur, isToday: _isToday),
                  ),
                  const SizedBox(height: 14),
                  SectionCard(
                    title: 'Last 7 days',
                    child: WeekBars(
                      week: s['week'] as List? ?? [],
                      currency: cur,
                      onTapDay: (d) {
                        setState(() => _day = DateUtils.dateOnly(d));
                        _load();
                      },
                    ),
                  ),
                  const SizedBox(height: 14),
                  _Plans(s: s, cur: cur, day: _day),
                  const SizedBox(height: 14),
                  _Sellers(s: s, cur: cur, day: _day),
                  const SizedBox(height: 14),
                  _Methods(s: s, cur: cur),
                  const SizedBox(height: 14),
                  _Recent(s: s, cur: cur),
                  if (app.canDo('fit')) ...[
                    const SizedBox(height: 14),
                    _AskFitCard(day: _day, onAsk: widget.askFit),
                  ],
                ],
              ]),
            ),
          ]),
        ),
        AnimatedPositioned(
          duration: const Duration(milliseconds: 350),
          curve: Curves.easeOutBack,
          top: _flash == null ? -80 : MediaQuery.of(context).padding.top + 8,
          left: 24,
          right: 24,
          child: _flash == null
              ? const SizedBox.shrink()
              : Material(
                  color: const Color(0xFF16A34A),
                  borderRadius: BorderRadius.circular(18),
                  elevation: 8,
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
                    child: Row(children: [
                      const Text('💰', style: TextStyle(fontSize: 22)),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text('+${money(toD(_flash!['amount']), cur)} · ${_flash!['plan']} · ${_flash!['seller']}',
                            style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w800)),
                      ),
                    ]),
                  ),
                ),
        ),
      ]),
    );
  }
}

class _DayStrip extends StatelessWidget {
  const _DayStrip({required this.day, required this.onPrev, required this.onNext});
  final DateTime day;
  final VoidCallback onPrev;
  final VoidCallback? onNext;
  @override
  Widget build(BuildContext context) => Row(children: [
        IconButton.filledTonal(onPressed: onPrev, icon: const Icon(Icons.chevron_left)),
        Expanded(
          child: Center(
            child: Text(dayLabel(day), style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w800)),
          ),
        ),
        IconButton.filledTonal(onPressed: onNext, icon: const Icon(Icons.chevron_right)),
      ]);
}

/// The big number, with the day's pace against a usual day.
class _Hero extends StatelessWidget {
  const _Hero({required this.s, required this.cur, required this.isToday, required this.brand});
  final Map<String, dynamic> s;
  final String cur;
  final bool isToday;
  final Color brand;

  @override
  Widget build(BuildContext context) {
    final total = toD(s['total']);
    final change = s['change'];
    final week = (s['week'] as List? ?? []).cast<Map>();
    final before = week.length > 1 ? week.sublist(0, week.length - 1).map((d) => toD(d['amount'])).toList() : <double>[];
    final usual = before.isEmpty ? 0.0 : before.reduce((a, b) => a + b) / before.length;
    final pace = usual > 0 ? total / usual : 0.0;
    final up = change is num && change >= 0;
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(26),
        gradient: LinearGradient(begin: Alignment.topLeft, end: Alignment.bottomRight,
            colors: [brand, Color.lerp(brand, const Color(0xFF0B1220), .55)!]),
        boxShadow: [BoxShadow(color: brand.withValues(alpha: .35), blurRadius: 24, offset: const Offset(0, 10))],
      ),
      child: Row(children: [
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(isToday ? 'Sales today' : 'Sales', style: const TextStyle(color: Colors.white70, fontWeight: FontWeight.w600)),
            const SizedBox(height: 6),
            TweenAnimationBuilder<double>(
              tween: Tween(begin: 0, end: total),
              duration: const Duration(milliseconds: 900),
              curve: Curves.easeOutCubic,
              builder: (_, v, __) => FittedBox(
                fit: BoxFit.scaleDown,
                alignment: Alignment.centerLeft,
                child: Text(money(v.roundToDouble(), cur),
                    style: const TextStyle(color: Colors.white, fontSize: 40, fontWeight: FontWeight.w900, letterSpacing: -1)),
              ),
            ),
            const SizedBox(height: 4),
            Text('${toI(s['count'])} voucher${toI(s['count']) == 1 ? '' : 's'} · avg ${money(toD(s['avg']), cur)}',
                style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w600)),
            const SizedBox(height: 10),
            Wrap(spacing: 8, runSpacing: 6, children: [
              if (change is num)
                _Chip(text: '${up ? '▲' : '▼'} ${change.abs().toStringAsFixed(change.abs() < 10 ? 1 : 0)}% vs day before',
                    color: up ? const Color(0xFF22C55E) : const Color(0xFFF87171)),
              if (s['can_money'] == true && toD(s['commission']) > 0)
                _Chip(text: 'Net ${money(toD(s['net']), cur)}', color: Colors.white24),
            ]),
          ]),
        ),
        if (usual > 0)
          SizedBox(
            width: 92,
            height: 92,
            child: Stack(alignment: Alignment.center, children: [
              TweenAnimationBuilder<double>(
                tween: Tween(begin: 0, end: pace.clamp(0, 1).toDouble()),
                duration: const Duration(milliseconds: 1100),
                builder: (_, v, __) => SizedBox(
                  width: 92,
                  height: 92,
                  child: CircularProgressIndicator(value: v, strokeWidth: 9, backgroundColor: Colors.white24,
                      color: pace >= 1 ? const Color(0xFF4ADE80) : Colors.white, strokeCap: StrokeCap.round),
                ),
              ),
              Column(mainAxisSize: MainAxisSize.min, children: [
                Text('${(pace * 100).round()}%', style: const TextStyle(color: Colors.white, fontSize: 20, fontWeight: FontWeight.w900)),
                const Text('of usual', style: TextStyle(color: Colors.white70, fontSize: 10.5)),
              ]),
            ]),
          ),
      ]),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({required this.text, required this.color});
  final String text;
  final Color color;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
        decoration: BoxDecoration(color: color.withValues(alpha: .25), borderRadius: BorderRadius.circular(999)),
        child: Text(text, style: const TextStyle(color: Colors.white, fontSize: 12, fontWeight: FontWeight.w700)),
      );
}

/// What needs you now.
class _Pulse extends StatelessWidget {
  const _Pulse({required this.home, required this.app, required this.goTab});
  final Map<String, dynamic> home;
  final AppState app;
  final void Function(int) goTab;
  @override
  Widget build(BuildContext context) {
    final p = (home['pulse'] as Map?)?.cast<String, dynamic>() ?? {};
    final missed = (home['calls'] as List? ?? []).where((c) => (c as Map)['missed'] == true).length;
    final tiles = <Widget>[
      if (p['complaints'] != null)
        _PulseTile(icon: Icons.report_gmailerrorred, label: 'Complaints', value: '${p['complaints']}',
            hot: toI(p['complaints']) > 0, onTap: () => goTab(1)),
      _PulseTile(icon: Icons.chat_bubble_outline, label: 'Unread', value: '${app.unread}', hot: app.unread > 0, onTap: () => goTab(2)),
      _PulseTile(icon: Icons.phone_missed, label: 'Missed calls', value: '$missed', hot: missed > 0, onTap: () => goTab(4)),
    ];
    return Row(children: [for (var i = 0; i < tiles.length; i++) ...[if (i > 0) const SizedBox(width: 10), Expanded(child: tiles[i])]]);
  }
}

class _PulseTile extends StatelessWidget {
  const _PulseTile({required this.icon, required this.label, required this.value, required this.hot, required this.onTap});
  final IconData icon;
  final String label, value;
  final bool hot;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final c = hot ? const Color(0xFFEF4444) : cs.onSurfaceVariant;
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(22),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Icon(icon, color: c),
            const SizedBox(height: 8),
            Text(value, style: TextStyle(fontSize: 24, fontWeight: FontWeight.w900, color: hot ? c : cs.onSurface)),
            Text(label, style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant, fontWeight: FontWeight.w600)),
          ]),
        ),
      ),
    );
  }
}

/// Today's calls from the Wi-Fi pages, with a one-tap call back for missed ones.
class _CallsCard extends StatelessWidget {
  const _CallsCard({required this.calls});
  final List<Map> calls;
  @override
  Widget build(BuildContext context) => SectionCard(
        title: 'Customer calls today',
        child: Column(children: [
          for (final c in calls)
            ListTile(
              contentPadding: EdgeInsets.zero,
              leading: CircleAvatar(
                backgroundColor: c['missed'] == true ? const Color(0xFFFEE2E2) : const Color(0xFFDCFCE7),
                child: Icon(c['missed'] == true ? Icons.phone_missed : Icons.phone_in_talk,
                    color: c['missed'] == true ? const Color(0xFFDC2626) : const Color(0xFF16A34A), size: 20),
              ),
              title: Text('${c['text']}'.replaceFirst('📶 ', ''), maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13.5)),
              subtitle: Text(ago(c['at'])),
              trailing: '${c['phone']}'.isEmpty
                  ? null
                  : IconButton.filledTonal(
                      icon: const Icon(Icons.call),
                      onPressed: () => launchUrl(Uri.parse('tel:${'${c['phone']}'.replaceAll(RegExp(r'[^\d+]'), '')}')),
                    ),
            ),
        ]),
      );
}

class _Plans extends StatelessWidget {
  const _Plans({required this.s, required this.cur, required this.day});
  final Map<String, dynamic> s;
  final String cur;
  final DateTime day;
  @override
  Widget build(BuildContext context) {
    final rows = (s['by_plan'] as List? ?? []).cast<Map>();
    return SectionCard(
      title: 'Plans',
      child: rows.isEmpty
          ? const EmptyNote('No sales yet.')
          : Column(children: [
              for (final r in rows)
                ShareRow(
                  title: '${r['plan_name']}',
                  subtitle: '${toI(r['n'])} sold${r['vs_avg'] is num ? ' · ${(r['vs_avg'] as num) >= 0 ? '+' : ''}${(r['vs_avg'] as num).round()}% vs usual' : ''}',
                  value: money(toD(r['v']), cur),
                  share: toD(r['share']) / 100,
                  onTap: () => openSalesDetail(context, day: day, plan: '${r['plan_name']}'),
                ),
            ]),
    );
  }
}

class _Sellers extends StatelessWidget {
  const _Sellers({required this.s, required this.cur, required this.day});
  final Map<String, dynamic> s;
  final String cur;
  final DateTime day;
  @override
  Widget build(BuildContext context) {
    final rows = (s['by_seller'] as List? ?? []).cast<Map>();
    if (rows.isEmpty) return const SizedBox.shrink();
    return SectionCard(
      title: 'Who sold',
      child: Column(children: [
        for (final r in rows)
          ShareRow(
            leading: CircleAvatar(radius: 16, child: Text(r['is_shop'] == true ? '🏪' : '${r['name']}'.characters.first.toUpperCase())),
            title: '${r['name']}',
            subtitle: '${toI(r['n'])} sold${'${r['top_plan']}'.isNotEmpty ? ' · mostly ${r['top_plan']}' : ''}',
            value: money(toD(r['v']), cur),
            share: toD(r['share']) / 100,
            onTap: () => openSalesDetail(context, day: day, seller: '${r['key']}'),
          ),
      ]),
    );
  }
}

class _Methods extends StatelessWidget {
  const _Methods({required this.s, required this.cur});
  final Map<String, dynamic> s;
  final String cur;
  @override
  Widget build(BuildContext context) {
    final rows = (s['by_method'] as List? ?? []).cast<Map>();
    if (rows.isEmpty) return const SizedBox.shrink();
    return SectionCard(
      title: 'How customers paid',
      child: Wrap(spacing: 8, runSpacing: 8, children: [
        for (final r in rows)
          Chip(
            avatar: const Icon(Icons.payments_outlined, size: 18),
            label: Text('${r['label'] ?? r['payment_method']} · ${money(toD(r['v']), cur)}'),
          ),
      ]),
    );
  }
}

class _Recent extends StatelessWidget {
  const _Recent({required this.s, required this.cur});
  final Map<String, dynamic> s;
  final String cur;
  @override
  Widget build(BuildContext context) {
    final rows = (s['sales'] as List? ?? []).cast<Map>().take(12).toList();
    if (rows.isEmpty) return const SizedBox.shrink();
    return SectionCard(
      title: 'Latest sales',
      child: Column(children: [
        for (final r in rows)
          ListTile(
            dense: true,
            contentPadding: EdgeInsets.zero,
            leading: Text(clock(r['at']), style: const TextStyle(fontWeight: FontWeight.w700, fontFeatures: [FontFeature.tabularFigures()])),
            title: Text('${r['plan']}', style: const TextStyle(fontWeight: FontWeight.w600)),
            subtitle: Text([r['seller'], r['method'], r['site']].where((x) => '$x'.isNotEmpty).join(' · ')),
            trailing: Text(money(toD(r['amount']), cur), style: const TextStyle(fontWeight: FontWeight.w800)),
          ),
      ]),
    );
  }
}

class _AskFitCard extends StatelessWidget {
  const _AskFitCard({required this.day, required this.onAsk});
  final DateTime day;
  final void Function(String) onAsk;
  @override
  Widget build(BuildContext context) {
    final d = dayLabel(day).toLowerCase();
    final qs = [
      'Why were sales ${d == 'today' ? 'like this today' : 'like this on ${dayLabel(day)}'}?',
      'Which agents’ sales dropped this week?',
      'Draw sales per hour, today vs yesterday',
    ];
    return SectionCard(
      title: 'Ask Fit',
      trailing: const Icon(Icons.auto_awesome, size: 18),
      child: Wrap(spacing: 8, runSpacing: 8, children: [for (final q in qs) ActionChip(label: Text(q), onPressed: () => onAsk(q))]),
    );
  }
}
