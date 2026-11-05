import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../data/store.dart';
import '../data/sync_engine.dart';
import '../widgets/charts.dart';
import '../widgets/common.dart';

void openSalesDetail(BuildContext context, {required DateTime day, String? plan, String? seller}) {
  Navigator.of(context).push(MaterialPageRoute(builder: (_) => SalesDetailScreen(day: day, plan: plan, seller: seller)));
}

/// One plan or one seller for a day — the website's plan / agent detail.
class SalesDetailScreen extends StatefulWidget {
  const SalesDetailScreen({super.key, required this.day, this.plan, this.seller});
  final DateTime day;
  final String? plan, seller;
  @override
  State<SalesDetailScreen> createState() => _SalesDetailScreenState();
}

class _SalesDetailScreenState extends State<SalesDetailScreen> {
  Map<String, dynamic>? _d;
  String? _err;

  Map<String, String> get _query => {
        'date': ymd(widget.day),
        if (widget.plan != null) 'plan': widget.plan!,
        if (widget.seller != null) 'seller': widget.seller!,
      };

  Map<String, dynamic>? _detail(Map<String, dynamic> d) =>
      ((widget.plan != null ? d['plan_detail'] : d['seller_detail']) as Map?)?.cast<String, dynamic>();

  @override
  void initState() {
    super.initState();
    final saved = Store.I.peek(Pages.sales, _query);       // seen before: on screen at once
    if (saved != null) _d = _detail(saved);
    _load();
  }

  Future<void> _load() async {
    try {
      final d = await Store.I.fetch(Pages.sales, query: _query);
      if (!mounted) return;
      setState(() {
        _d = _detail(d) ?? {};
        _err = null;
      });
    } on ApiError catch (e) {
      if (!mounted) return;
      if (e.status == 0 && _d != null) return;              // offline with the saved one on screen
      setState(() => _err = e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final cur = context.read<AppState>().currency;
    final d = _d;
    return Scaffold(
      appBar: AppBar(title: Text(widget.plan ?? (d?['name'] as String?) ?? 'Seller')),
      body: _err != null
          ? Padding(padding: const EdgeInsets.all(16), child: ErrorCard(message: _err!, onRetry: _load))
          : d == null
              ? const Center(child: CircularProgressIndicator())
              : ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 40), children: [
                  Text(dayLabel(widget.day), style: TextStyle(color: Theme.of(context).colorScheme.onSurfaceVariant, fontWeight: FontWeight.w600)),
                  const SizedBox(height: 10),
                  _numbers(d, cur),
                  const SizedBox(height: 14),
                  SectionCard(title: 'Hour by hour', child: HourBars(hours: d['hours'] as List? ?? [], currency: cur, isToday: DateUtils.isSameDay(widget.day, DateTime.now()))),
                  const SizedBox(height: 14),
                  if (widget.plan != null) ..._planParts(d, cur) else ..._sellerParts(d, cur),
                  const SizedBox(height: 14),
                  SectionCard(
                    title: 'Sales',
                    child: Column(children: [
                      for (final r in (d['sales'] as List? ?? []).cast<Map>())
                        ListTile(
                          dense: true,
                          contentPadding: EdgeInsets.zero,
                          leading: Text(clock(r['at']), style: const TextStyle(fontWeight: FontWeight.w700)),
                          title: Text(widget.plan != null ? '${r['seller']}' : '${r['plan']}'),
                          subtitle: Text([r['code'], r['method'], r['customer']].where((x) => '$x'.isNotEmpty).join(' · ')),
                          trailing: Text(money(toD(r['amount']), cur), style: const TextStyle(fontWeight: FontWeight.w800)),
                        ),
                    ]),
                  ),
                ]),
    );
  }

  Widget _numbers(Map<String, dynamic> d, String cur) {
    final vsAvg = d['vs_avg'];
    Widget tile(String label, String value, [Color? c]) => Expanded(
          child: Card(
            child: Padding(
              padding: const EdgeInsets.all(14),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(label, style: TextStyle(fontSize: 12, color: Theme.of(context).colorScheme.onSurfaceVariant)),
                const SizedBox(height: 4),
                FittedBox(child: Text(value, style: TextStyle(fontSize: 22, fontWeight: FontWeight.w900, color: c))),
              ]),
            ),
          ),
        );
    return Column(children: [
      Row(children: [
        tile('Sold', money(toD(d['amount']), cur)),
        const SizedBox(width: 10),
        tile('Vouchers', '${toI(d['count'])}'),
      ]),
      const SizedBox(height: 10),
      Row(children: [
        tile('Share of the day', '${toD(d['share']).round()}%'),
        const SizedBox(width: 10),
        tile('vs usual day', vsAvg is num ? '${vsAvg >= 0 ? '+' : ''}${vsAvg.round()}%' : '—',
            vsAvg is num ? (vsAvg >= 0 ? const Color(0xFF16A34A) : const Color(0xFFDC2626)) : null),
      ]),
    ]);
  }

  List<Widget> _planParts(Map<String, dynamic> d, String cur) {
    final stock = (d['stock'] as Map?)?.cast<String, dynamic>() ?? {};
    return [
      SectionCard(
        title: 'Who sold it',
        child: Column(children: [
          for (final r in (d['sellers'] as List? ?? []).cast<Map>())
            ShareRow(title: '${r['name']}', subtitle: '${toI(r['n'])} sold', value: money(toD(r['v']), cur), share: toD(r['share']) / 100),
        ]),
      ),
      const SizedBox(height: 14),
      SectionCard(
        title: 'Stock left',
        trailing: Text('${toI(stock['total'])} vouchers', style: const TextStyle(fontWeight: FontWeight.w800)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('Shop ${toI(stock['shop'])} · agents ${toI(stock['agents'])}'),
          for (final a in (stock['by_agent'] as List? ?? []).cast<Map>())
            Padding(padding: const EdgeInsets.only(top: 4), child: Text('• ${a['agent__name']}: ${toI(a['n'])}')),
          if (toI(stock['total']) < 10)
            const Padding(
              padding: EdgeInsets.only(top: 8),
              child: Text('⚠️ Running low — generate more on TapTap or ask Fit.', style: TextStyle(fontWeight: FontWeight.w700)),
            ),
        ]),
      ),
    ];
  }

  List<Widget> _sellerParts(Map<String, dynamic> d, String cur) {
    final bal = (d['balance'] as Map?)?.cast<String, dynamic>();
    return [
      SectionCard(
        title: 'What they sold',
        child: Column(children: [
          for (final r in (d['plans'] as List? ?? []).cast<Map>())
            ShareRow(title: '${r['name']}', subtitle: '${toI(r['n'])} sold', value: money(toD(r['v']), cur), share: toD(r['share']) / 100),
        ]),
      ),
      const SizedBox(height: 14),
      SectionCard(
        title: 'Money',
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          _kv('Commission earned', money(toD(d['commission']), cur)),
          _kv('Owed for the day', money(toD(d['net']), cur)),
          if (d['collected'] != null && toD(d['collected']) > 0) _kv('Cash handed in today', money(toD(d['collected']), cur)),
          if (bal != null) _kv('Running balance', money(toD(bal['outstanding']), cur)),
          _kv('Vouchers still held', '${toI(d['holding_n'])} · ${money(toD(d['holding_v']), cur)}'),
        ]),
      ),
    ];
  }

  Widget _kv(String k, String v) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(children: [Expanded(child: Text(k)), Text(v, style: const TextStyle(fontWeight: FontWeight.w800))]),
      );
}
