import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';

/// Quick sell: a customer is standing in front of you. Pick the plan, tap Sell, read out (or send) the code.
///
/// One paid voucher on one router, straight away — the same as the website's "Single voucher" with "Paid" on
/// (server: POST /api/mobile/v1/vouchers/new/ kind=single). The sale is booked, the voucher is put on the router,
/// and the sheet stays open for the next customer.
Future<void> showQuickSell(BuildContext context, {int? routerId}) => showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (_) => QuickSellSheet(routerId: routerId),
    );

class QuickSellSheet extends StatefulWidget {
  const QuickSellSheet({super.key, this.routerId});
  final int? routerId;
  @override
  State<QuickSellSheet> createState() => _QuickSellSheetState();
}

class _QuickSellSheetState extends State<QuickSellSheet> {
  Map<String, dynamic>? _o;
  String? _err;
  bool _busy = false;
  int? _plan;
  int? _router;
  String _method = 'cash';
  final _phone = TextEditingController();
  Map<String, dynamic>? _made; // the voucher just sold
  List<Map<String, dynamic>> _notes = [];
  int _soldHere = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _phone.dispose();
    super.dispose();
  }

  List<Map<String, dynamic>> get _plans => asMaps(_o?['plans']);
  List<Map<String, dynamic>> get _routers => asMaps(_o?['routers']);
  List<Map<String, dynamic>> get _methods => asMaps(_o?['methods']);
  String get _cur => '${_o?['currency'] ?? 'D'}';
  Map<String, dynamic>? get _planRow {
    for (final p in _plans) {
      if (toI(p['id']) == _plan) return p;
    }
    return null;
  }

  Future<void> _load() async {
    try {
      final d = await Api.I.get('/api/mobile/v1/vouchers/new/');
      if (!mounted) return;
      final routers = asMaps(d['routers']);
      final plans = asMaps(d['plans']);
      int? router = widget.routerId;
      if (router == null && routers.isNotEmpty) {
        final online = routers.where((r) => r['status'] == 'Online').toList();
        router = toI((online.isNotEmpty ? online.first : routers.first)['id']);
      }
      setState(() {
        _o = d;
        _router = router;
        _plan = plans.length == 1 ? toI(plans.first['id']) : null;
        _err = null;
      });
    } on ApiError catch (e) {
      if (mounted) setState(() => _err = e.message);
    }
  }

  Future<void> _sell() async {
    if (_plan == null || _busy) return;
    setState(() => _busy = true);
    try {
      final d = await Api.I.post('/api/mobile/v1/vouchers/new/', {
        'kind': 'single',
        'plan': _plan,
        if (_router != null) 'router': _router,
        'paid': true,
        'method': _method,
        if (_phone.text.trim().isNotEmpty) 'customer_phone': _phone.text.trim(),
        'note': 'Quick sell (app)',
      });
      if (!mounted) return;
      HapticFeedback.mediumImpact();
      setState(() {
        _made = (d['voucher'] as Map?)?.cast<String, dynamic>() ?? {};
        _notes = asMaps(d['notes']);
        _soldHere += 1;
      });
    } on ApiError catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message), behavior: SnackBarBehavior.floating));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _next() => setState(() {
        _made = null;
        _notes = [];
        _phone.clear();
      });

  String _message(String code) {
    final p = _planRow;
    final biz = context.read<AppState>().business['name'] ?? 'TapTap';
    return 'Your Wi-Fi code: $code\n'
        '${p == null ? '' : 'Plan: ${p['name']}${'${p['duration'] ?? ''}'.isEmpty ? '' : ' (${p['duration']})'}\n'}'
        'Connect to our Wi-Fi and type the code. — $biz';
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final bottom = MediaQuery.of(context).viewInsets.bottom;
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 0, 16, 16 + bottom),
      child: _o == null
          ? SizedBox(
              height: 220,
              child: Center(
                child: _err == null
                    ? const CircularProgressIndicator()
                    : Column(mainAxisSize: MainAxisSize.min, children: [
                        Text(_err!, textAlign: TextAlign.center),
                        const SizedBox(height: 8),
                        FilledButton.tonal(onPressed: _load, child: const Text('Try again')),
                      ]),
              ),
            )
          : AnimatedSwitcher(
              duration: const Duration(milliseconds: 250),
              child: _made == null ? _form(context, cs) : _result(context, cs),
            ),
    );
  }

  Widget _form(BuildContext context, ColorScheme cs) {
    final plan = _planRow;
    return SingleChildScrollView(
      key: const ValueKey('form'),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          const Icon(Icons.bolt, color: Color(0xFFF59E0B)),
          const SizedBox(width: 6),
          const Expanded(child: Text('Quick sell', style: TextStyle(fontSize: 20, fontWeight: FontWeight.w800))),
          if (_soldHere > 0)
            Chip(
              visualDensity: VisualDensity.compact,
              avatar: const Icon(Icons.check_circle, size: 16, color: Color(0xFF16A34A)),
              label: Text('$_soldHere sold'),
            ),
        ]),
        const SizedBox(height: 4),
        Text('One paid voucher, live on the router at once.', style: TextStyle(color: cs.onSurfaceVariant)),
        if (_routers.length > 1) ...[
          const SizedBox(height: 14),
          const Text('Router', style: TextStyle(fontWeight: FontWeight.w700)),
          const SizedBox(height: 6),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(children: [
              for (final r in _routers)
                Padding(
                  padding: const EdgeInsets.only(right: 8),
                  child: ChoiceChip(
                    selected: toI(r['id']) == _router,
                    onSelected: (_) => setState(() => _router = toI(r['id'])),
                    avatar: Icon(Icons.circle, size: 10, color: r['status'] == 'Online' ? const Color(0xFF16A34A) : cs.error),
                    label: Text('${r['name']}'),
                  ),
                ),
            ]),
          ),
        ],
        const SizedBox(height: 14),
        const Text('Plan', style: TextStyle(fontWeight: FontWeight.w700)),
        const SizedBox(height: 6),
        if (_plans.isEmpty)
          Text('No active plans yet — add one on the website (Plans).', style: TextStyle(color: cs.error))
        else
          GridView.count(
            crossAxisCount: 2,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            mainAxisSpacing: 8,
            crossAxisSpacing: 8,
            childAspectRatio: 2.3,
            children: [
              for (final p in _plans)
                _PlanButton(
                  plan: p,
                  currency: _cur,
                  selected: toI(p['id']) == _plan,
                  onTap: () {
                    HapticFeedback.selectionClick();
                    setState(() => _plan = toI(p['id']));
                  },
                ),
            ],
          ),
        const SizedBox(height: 14),
        Wrap(spacing: 8, runSpacing: 4, children: [
          for (final m in _methods.take(5))
            ChoiceChip(
              selected: m['key'] == _method,
              onSelected: (_) => setState(() => _method = '${m['key']}'),
              label: Text('${m['label']}'),
            ),
        ]),
        const SizedBox(height: 10),
        TextField(
          controller: _phone,
          keyboardType: TextInputType.phone,
          decoration: const InputDecoration(
            labelText: 'Customer phone (optional)',
            helperText: 'To send the code by WhatsApp or SMS',
            prefixIcon: Icon(Icons.phone_outlined),
          ),
        ),
        const SizedBox(height: 16),
        SizedBox(
          width: double.infinity,
          height: 54,
          child: FilledButton.icon(
            onPressed: plan == null || _busy ? null : _sell,
            icon: _busy
                ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                : const Icon(Icons.point_of_sale),
            label: Text(plan == null ? 'Pick a plan' : 'Sell ${plan['name']} · ${money(toD(plan['price']), _cur)}',
                style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w800)),
          ),
        ),
      ]),
    );
  }

  Widget _result(BuildContext context, ColorScheme cs) {
    final code = '${_made?['code'] ?? ''}';
    final phone = _phone.text.replaceAll(RegExp(r'[^\d+]'), '');
    final text = _message(code);
    return SingleChildScrollView(
        key: ValueKey(code),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      const Icon(Icons.check_circle, color: Color(0xFF16A34A), size: 44),
      const SizedBox(height: 6),
      Text('Sold — read this code to the customer', textAlign: TextAlign.center, style: TextStyle(color: cs.onSurfaceVariant)),
      const SizedBox(height: 10),
      InkWell(
        borderRadius: BorderRadius.circular(20),
        onTap: () {
          Clipboard.setData(ClipboardData(text: code));
          HapticFeedback.selectionClick();
          ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$code copied'), behavior: SnackBarBehavior.floating));
        },
        child: Container(
          padding: const EdgeInsets.symmetric(vertical: 22, horizontal: 12),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(20),
            color: cs.primaryContainer,
          ),
          child: Column(children: [
            FittedBox(
              child: Text(code,
                  style: TextStyle(fontFamily: 'monospace', fontSize: 40, fontWeight: FontWeight.w900, letterSpacing: 4, color: cs.onPrimaryContainer)),
            ),
            const SizedBox(height: 4),
            Text('Tap to copy', style: TextStyle(fontSize: 12, color: cs.onPrimaryContainer.withValues(alpha: .7))),
          ]),
        ),
      ),
      for (final n in _notes) ...[
        const SizedBox(height: 8),
        Row(children: [
          Icon(n['level'] == 'warning' ? Icons.warning_amber : Icons.router, size: 18,
              color: n['level'] == 'warning' ? const Color(0xFFB45309) : const Color(0xFF16A34A)),
          const SizedBox(width: 6),
          Expanded(child: Text('${n['text']}', style: const TextStyle(fontSize: 13))),
        ]),
      ],
      const SizedBox(height: 14),
      Row(children: [
        Expanded(
          child: FilledButton.tonalIcon(
            onPressed: () => launchUrl(
                Uri.parse('https://wa.me/${phone.replaceAll('+', '')}?text=${Uri.encodeComponent(text)}'),
                mode: LaunchMode.externalApplication),
            icon: const Icon(Icons.chat, size: 18),
            label: const Text('WhatsApp'),
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: FilledButton.tonalIcon(
            onPressed: () => launchUrl(Uri.parse('sms:$phone?body=${Uri.encodeComponent(text)}')),
            icon: const Icon(Icons.sms_outlined, size: 18),
            label: const Text('SMS'),
          ),
        ),
      ]),
      const SizedBox(height: 8),
      SizedBox(
        height: 52,
        child: FilledButton.icon(
          onPressed: _next,
          icon: const Icon(Icons.add),
          label: const Text('Next customer', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w800)),
        ),
      ),
      TextButton(onPressed: () => Navigator.of(context).pop(), child: const Text('Done')),
    ]));
  }
}

class _PlanButton extends StatelessWidget {
  const _PlanButton({required this.plan, required this.currency, required this.selected, required this.onTap});
  final Map<String, dynamic> plan;
  final String currency;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Material(
      color: selected ? cs.primary : cs.surfaceContainerHighest,
      borderRadius: BorderRadius.circular(16),
      child: InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Column(mainAxisAlignment: MainAxisAlignment.center, crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('${plan['name']}',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontWeight: FontWeight.w800, color: selected ? cs.onPrimary : cs.onSurface)),
            Text(
              '${plan['free'] == true ? 'Free' : money(toD(plan['price']), currency)}'
              '${'${plan['duration'] ?? ''}'.isEmpty ? '' : ' · ${plan['duration']}'}',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: 12.5, color: selected ? cs.onPrimary.withValues(alpha: .85) : cs.onSurfaceVariant),
            ),
          ]),
        ),
      ),
    );
  }
}
