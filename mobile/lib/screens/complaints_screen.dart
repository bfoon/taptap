import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../widgets/common.dart';

/// Vouchers customers sent from the Wi-Fi page — one case per voucher, hottest first.
class ComplaintsScreen extends StatefulWidget {
  const ComplaintsScreen({super.key, required this.askFit});
  final void Function(String) askFit;
  @override
  State<ComplaintsScreen> createState() => _ComplaintsScreenState();
}

class _ComplaintsScreenState extends State<ComplaintsScreen> {
  List<Map<String, dynamic>>? _cases;
  bool _all = false;
  String? _err;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final d = await Api.I.get('/api/mobile/v1/complaints/', query: {if (_all) 'show': 'all'});
      setState(() {
        _cases = (d['cases'] as List).cast<Map>().map((e) => e.cast<String, dynamic>()).toList();
        _err = null;
      });
    } on ApiError catch (e) {
      setState(() => _err = e.message);
    }
  }

  Future<void> _act(Map<String, dynamic> c, String action, {String note = ''}) async {
    HapticFeedback.selectionClick();
    try {
      final d = await Api.I.post('/api/mobile/v1/complaints/${c['id']}/', {'action': action, 'note': note});
      _toast('${d['message'] ?? 'Done.'}');
      _load();
    } on ApiError catch (e) {
      _toast(e.message, error: true);
    }
  }

  void _toast(String m, {bool error = false}) => ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(m), backgroundColor: error ? Theme.of(context).colorScheme.error : null, behavior: SnackBarBehavior.floating));

  Future<void> _close(Map<String, dynamic> c) async {
    final ctl = TextEditingController();
    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (ctx) => Padding(
        padding: EdgeInsets.fromLTRB(20, 0, 20, MediaQuery.of(ctx).viewInsets.bottom + 24),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Text('Close ${c['code']}', style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800)),
          const SizedBox(height: 4),
          const Text('A short note helps the next person who looks at it.'),
          const SizedBox(height: 14),
          TextField(controller: ctl, autofocus: true, maxLength: 500, decoration: const InputDecoration(hintText: 'e.g. Called her — wrong code, gave the right one')),
          Wrap(spacing: 8, children: [
            for (final q in ['Called the customer — fixed', 'Wrong code typed', 'Sold a new voucher'])
              ActionChip(label: Text(q), onPressed: () => ctl.text = q),
          ]),
          const SizedBox(height: 14),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Close the report')),
        ]),
      ),
    );
    if (ok == true) _act(c, 'close', note: ctl.text);
  }

  @override
  Widget build(BuildContext context) {
    final cases = _cases;
    final open = cases?.where((c) => c['status'] == 'open').length ?? 0;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Complaints'),
        actions: [
          if (context.read<AppState>().canDo('fit') && open > 0)
            TextButton.icon(
              icon: const Icon(Icons.auto_awesome, size: 18),
              label: const Text('Let Fit sort them'),
              onPressed: () => widget.askFit('Go through the open customer reports and fix what you can.'),
            ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 0, 16, 110), children: [
          SegmentedButton<bool>(
            segments: [
              ButtonSegment(value: false, label: Text('Open${cases == null ? '' : ' · $open'}'), icon: const Icon(Icons.inbox)),
              const ButtonSegment(value: true, label: Text('All'), icon: Icon(Icons.history)),
            ],
            selected: {_all},
            onSelectionChanged: (s) {
              setState(() {
                _all = s.first;
                _cases = null;
              });
              _load();
            },
          ),
          const SizedBox(height: 12),
          if (_err != null) ErrorCard(message: _err!, onRetry: _load),
          if (cases == null && _err == null) ...[const SkeletonCard(height: 160), const SizedBox(height: 12), const SkeletonCard(height: 160)],
          if (cases != null && cases.isEmpty)
            const Padding(
              padding: EdgeInsets.only(top: 80),
              child: Column(children: [
                Text('🎉', style: TextStyle(fontSize: 56)),
                SizedBox(height: 8),
                Text('No customer is waiting', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w800)),
                Text('New reports from your Wi-Fi pages show up here.'),
              ]),
            ),
          for (final c in cases ?? <Map<String, dynamic>>[]) ...[
            _CaseCard(c: c, onFix: () => _act(c, 'fix'), onClose: () => _close(c), onReopen: () => _act(c, 'reopen')),
            const SizedBox(height: 12),
          ],
        ]),
      ),
    );
  }
}

class _CaseCard extends StatelessWidget {
  const _CaseCard({required this.c, required this.onFix, required this.onClose, required this.onReopen});
  final Map<String, dynamic> c;
  final VoidCallback onFix, onClose, onReopen;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final open = c['status'] == 'open';
    final heat = '${c['heat']}';
    final heatColor = heat == 'hot' ? const Color(0xFFDC2626) : (heat == 'warm' ? const Color(0xFFF59E0B) : cs.primary);
    final now = (c['now'] as Map?)?.cast<String, dynamic>() ?? {};
    final fix = (c['fix'] as Map?)?.cast<String, dynamic>();
    final msgs = (c['messages'] as List? ?? []).cast<Map>();
    final phones = (c['phones'] as List? ?? []).cast<Map>();
    return Card(
      clipBehavior: Clip.antiAlias,
      child: Container(
        decoration: BoxDecoration(border: Border(left: BorderSide(color: open ? heatColor : cs.outlineVariant, width: 5))),
        padding: const EdgeInsets.fromLTRB(14, 14, 14, 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Text('${c['code']}', style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w900, letterSpacing: 1, fontFamily: 'monospace')),
            const SizedBox(width: 8),
            if (toI(c['count']) > 1)
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                decoration: BoxDecoration(color: heatColor.withOpacity(.12), borderRadius: BorderRadius.circular(99)),
                child: Text('sent ${c['count']}×', style: TextStyle(color: heatColor, fontWeight: FontWeight.w800, fontSize: 12)),
              ),
            const Spacer(),
            Text(open ? 'waiting ${c['waiting']}' : 'resolved', style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
          ]),
          const SizedBox(height: 8),
          for (final m in msgs.take(2))
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Text.rich(TextSpan(children: [
                TextSpan(text: '${'${m['name']}'.isEmpty ? 'Customer' : m['name']}: ', style: const TextStyle(fontWeight: FontWeight.w700)),
                TextSpan(text: '${m['message']}'.isEmpty ? (('${m['shown_error']}'.isEmpty) ? 'My voucher does not work.' : '${m['shown_error']}') : '${m['message']}'),
                TextSpan(text: '  ${ago(m['at'])}', style: TextStyle(color: cs.onSurfaceVariant, fontSize: 12)),
              ])),
            ),
          Container(
            margin: const EdgeInsets.only(top: 4),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: cs.surfaceContainerHighest.withOpacity(.6), borderRadius: BorderRadius.circular(12)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                const Icon(Icons.search, size: 16),
                const SizedBox(width: 6),
                Expanded(child: Text('TapTap: ${now['title'] ?? ''}', style: const TextStyle(fontWeight: FontWeight.w700))),
              ]),
              for (final ch in (now['checks'] as List? ?? []).take(3))
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text('${(ch as List)[0] == 'bad' ? '✖' : (ch[0] == 'warn' ? '!' : '•')} ${ch[1]}', style: const TextStyle(fontSize: 12.5)),
                ),
              if (c['too_many_devices'] == true)
                Padding(
                  padding: const EdgeInsets.only(top: 6),
                  child: Text('Sent from ${(c['macs'] as List).length} phones — the voucher allows ${c['allowed_devices']}.',
                      style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w700, color: Color(0xFFB45309))),
                ),
            ]),
          ),
          if (!open && '${c['note']}'.isNotEmpty)
            Padding(padding: const EdgeInsets.only(top: 8), child: Text('Note by ${c['resolved_by']}: ${c['note']}', style: const TextStyle(fontStyle: FontStyle.italic))),
          const SizedBox(height: 8),
          Wrap(spacing: 8, runSpacing: 4, crossAxisAlignment: WrapCrossAlignment.center, children: [
            if (open && fix != null && fix['kind'] != null)
              FilledButton.icon(
                style: FilledButton.styleFrom(minimumSize: const Size(0, 40)),
                onPressed: fix['can'] == true ? onFix : null,
                icon: const Icon(Icons.auto_fix_high, size: 18),
                label: Text('${fix['label']}'),
              ),
            if (open) OutlinedButton(onPressed: onClose, child: const Text('Close')),
            if (!open) OutlinedButton(onPressed: onReopen, child: const Text('Reopen')),
            for (final p in phones.take(2))
              IconButton.filledTonal(
                tooltip: 'Call ${p['name'] ?? p['phone']}',
                icon: const Icon(Icons.call, size: 20),
                onPressed: () => launchUrl(Uri.parse('tel:${'${p['phone']}'.replaceAll(RegExp(r'[^\d+]'), '')}')),
              ),
            if (phones.isNotEmpty)
              IconButton.filledTonal(
                tooltip: 'WhatsApp',
                icon: const Icon(Icons.chat, size: 20),
                onPressed: () => launchUrl(Uri.parse('https://wa.me/${'${phones.first['phone']}'.replaceAll(RegExp(r'\D'), '')}'),
                    mode: LaunchMode.externalApplication),
              ),
          ]),
          if (open && fix != null && fix['can'] != true && '${fix['hint'] ?? fix['why']}'.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text('${fix['hint']}'.isNotEmpty ? '${fix['hint']}' : '${fix['why']}', style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
            ),
        ]),
      ),
    );
  }
}
