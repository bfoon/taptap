import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../data/store.dart';
import '../widgets/common.dart';
import '../widgets/voucher_widgets.dart';
import 'vouchers_screen.dart';

/// Opens one voucher. Resolves to its latest state when the person goes back — or {'deleted': true} after a
/// delete — so a list can update that one row instead of reloading.
Future<Map<String, dynamic>?> openVoucher(BuildContext context, int id, {Map<String, dynamic>? initial, String? banner}) =>
    Navigator.of(context).push<Map<String, dynamic>>(
      MaterialPageRoute(builder: (_) => VoucherDetailScreen(id: id, initial: initial, banner: banner)),
    );

/// Everything about one voucher, like its page on the website: time, devices, sale, router, history — and the
/// buttons this person's role allows (disable, enable, add time, reset devices, delete).
class VoucherDetailScreen extends StatefulWidget {
  const VoucherDetailScreen({super.key, required this.id, this.initial, this.banner});
  final int id;
  final Map<String, dynamic>? initial;
  final String? banner;
  @override
  State<VoucherDetailScreen> createState() => _VoucherDetailScreenState();
}

class _VoucherDetailScreenState extends State<VoucherDetailScreen> {
  Map<String, dynamic>? _v;
  String? _err;
  bool _busy = false;
  String? _banner;

  @override
  void initState() {
    super.initState();
    _v = widget.initial ?? (Store.I.peek(_path)?['voucher'] as Map?)?.cast<String, dynamic>();   // or as saved on the phone
    _banner = widget.banner;
    _load();
  }

  String get _path => '/api/mobile/v1/vouchers/${widget.id}/';

  Future<void> _load() async {
    try {
      final d = await Store.I.fetch(_path);
      if (!mounted) return;
      setState(() {
        _v = (d['voucher'] as Map).cast<String, dynamic>();
        _err = null;
      });
    } on ApiError catch (e) {
      if (!mounted) return;
      if (e.status == 0 && _v != null) return;              // offline with the saved one on screen
      setState(() => _err = e.message);
    }
  }

  void _toast(String m, {bool error = false, bool warning = false}) {
    final cs = Theme.of(context).colorScheme;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(m),
      behavior: SnackBarBehavior.floating,
      backgroundColor: error ? cs.error : (warning ? const Color(0xFFB45309) : null),
    ));
  }

  Future<void> _act(String action, {String reason = '', int minutes = 0}) async {
    setState(() => _busy = true);
    HapticFeedback.selectionClick();
    try {
      final d = await Api.I.post('/api/mobile/v1/vouchers/${widget.id}/', {
        'action': action,
        if (reason.isNotEmpty) 'reason': reason,
        if (minutes > 0) 'minutes': minutes,
      });
      if (!mounted) return;
      if (d['deleted'] == true) {
        Store.I.remove(_path);
        _toast('${d['message'] ?? 'Moved to the bin.'}');
        Navigator.of(context).pop(<String, dynamic>{'deleted': true, 'id': widget.id});
        return;
      }
      setState(() {
        _v = (d['voucher'] as Map).cast<String, dynamic>();
        _banner = null;
      });
      // the saved copy follows what TapTap just answered
      final saved = Store.I.peek(_path);
      if (saved != null) Store.I.put(_path, {...saved, 'voucher': d['voucher']}, announce: false);
      _toast('${d['message'] ?? 'Done.'}', warning: d['warning'] == true);
    } on ApiError catch (e) {
      if (mounted) _toast(e.message, error: true);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  // ── sheets ──

  /// Asks for a reason. Returns null when the person backs out.
  Future<String?> _reason({required String title, required String text, required String button, bool required = false,
      List<String> quick = const [], bool danger = false}) async {
    final ctl = TextEditingController();
    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, set) => Padding(
          padding: EdgeInsets.fromLTRB(20, 0, 20, MediaQuery.of(ctx).viewInsets.bottom + 24),
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Text(title, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800)),
            const SizedBox(height: 4),
            Text(text),
            const SizedBox(height: 14),
            TextField(
              controller: ctl,
              autofocus: required,
              maxLength: 255,
              onChanged: (_) => set(() {}),
              decoration: InputDecoration(hintText: required ? 'Reason (kept with the record)' : 'Reason (optional)'),
            ),
            if (quick.isNotEmpty)
              Wrap(spacing: 8, children: [
                for (final q in quick)
                  ActionChip(
                    label: Text(q),
                    onPressed: () => set(() => ctl.text = q),
                  ),
              ]),
            const SizedBox(height: 14),
            FilledButton(
              style: danger ? FilledButton.styleFrom(backgroundColor: Theme.of(ctx).colorScheme.error) : null,
              onPressed: required && ctl.text.trim().isEmpty ? null : () => Navigator.pop(ctx, true),
              child: Text(button),
            ),
          ]),
        ),
      ),
    );
    final reason = ctl.text.trim();
    ctl.dispose();
    return ok == true ? reason : null;
  }

  Future<void> _addTime(Map<String, dynamic> v) async {
    final presets = asMaps(v['extend']);
    final days = TextEditingController(), hours = TextEditingController(), mins = TextEditingController();
    var chosen = 0;
    int custom() => toI(days.text) * 1440 + toI(hours.text) * 60 + toI(mins.text);
    final ok = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (ctx) => StatefulBuilder(builder: (ctx, set) {
        final total = custom() > 0 ? custom() : chosen;
        Widget field(TextEditingController c, String label) => Expanded(
              child: TextField(
                controller: c,
                keyboardType: TextInputType.number,
                inputFormatters: [FilteringTextInputFormatter.digitsOnly],
                onChanged: (_) => set(() {}),
                decoration: InputDecoration(labelText: label),
              ),
            );
        return Padding(
          padding: EdgeInsets.fromLTRB(20, 0, 20, MediaQuery.of(ctx).viewInsets.bottom + 24),
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Text('Add time to ${v['code']}', style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800)),
            const SizedBox(height: 4),
            Text(v['used_at'] == null
                ? 'Not used yet: the voucher simply lasts longer from its first login.'
                : (v['expired'] == true
                    ? 'Its time ran out: the extra time counts from now and switches it back on.'
                    : 'The extra time goes on top of when it ends now.')),
            const SizedBox(height: 14),
            Wrap(spacing: 8, runSpacing: 4, children: [
              for (final p in presets)
                ChoiceChip(
                  label: Text('${p['label']}'),
                  selected: custom() == 0 && chosen == toI(p['minutes']),
                  onSelected: (_) => set(() {
                    chosen = toI(p['minutes']);
                    days.clear();
                    hours.clear();
                    mins.clear();
                  }),
                ),
            ]),
            const SizedBox(height: 12),
            Text('Or exactly', style: TextStyle(color: Theme.of(ctx).colorScheme.onSurfaceVariant)),
            const SizedBox(height: 6),
            Row(children: [field(days, 'Days'), const SizedBox(width: 8), field(hours, 'Hours'), const SizedBox(width: 8), field(mins, 'Minutes')]),
            const SizedBox(height: 16),
            FilledButton(
              onPressed: total > 0 ? () => Navigator.pop(ctx, true) : null,
              child: Text(total > 0 ? 'Add ${spanText(total * 60)}' : 'Choose how much'),
            ),
          ]),
        );
      }),
    );
    final total = custom() > 0 ? custom() : chosen;
    days.dispose();
    hours.dispose();
    mins.dispose();
    if (ok == true && total > 0) _act('add_time', minutes: total);
  }

  Future<void> _disable(Map<String, dynamic> v) async {
    final r = await _reason(
      title: 'Disable ${v['code']}',
      text: 'The internet stops for this voucher straight away, on the router too. You can enable it again later.',
      button: 'Disable',
      danger: true,
      quick: const ['Customer asked', 'Lost or stolen', 'Shared with too many people'],
    );
    if (r != null) _act('disable', reason: r);
  }

  Future<void> _reset(Map<String, dynamic> v) async {
    final r = await _reason(
      title: 'Reset devices',
      text: 'Frees every device slot of ${v['code']}: the next phones to log in take them. Use it when a customer changed phones.',
      button: 'Reset devices',
      quick: const ['New phone', 'Phone was reset', 'Wrong device locked'],
    );
    if (r != null) _act('reset_devices', reason: r);
  }

  Future<void> _delete(Map<String, dynamic> v) async {
    final r = await _reason(
      title: 'Delete ${v['code']}',
      text: 'It moves to the bin with your reason and comes off the router. Its code is never issued again. '
          'A sale on it is taken out of finance.',
      button: 'Move to the bin',
      required: true,
      danger: true,
      quick: const ['Printed by mistake', 'Damaged card', 'Duplicate'],
    );
    if (r != null) _act('delete', reason: r);
  }

  // ── sharing ──

  String _digits(String phone) => phone.replaceAll(RegExp(r'\D'), '');

  void _whatsapp(Map<String, dynamic> v) {
    final phone = _digits('${v['customer_phone'] ?? ''}');
    launchUrl(Uri.parse('https://wa.me/$phone?text=${Uri.encodeComponent('${v['share']}')}'), mode: LaunchMode.externalApplication);
  }

  void _sms(Map<String, dynamic> v) {
    final phone = '${v['customer_phone'] ?? ''}'.replaceAll(RegExp(r'[^\d+]'), '');
    launchUrl(Uri.parse('sms:$phone?body=${Uri.encodeComponent('${v['share']}')}'));
  }

  void _copy(String text, String what) {
    Clipboard.setData(ClipboardData(text: text));
    HapticFeedback.selectionClick();
    _toast('$what copied');
  }

  @override
  Widget build(BuildContext context) {
    final v = _v;
    return PopScope<Object?>(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) Navigator.of(context).pop(_v);
      },
      child: Scaffold(
        appBar: AppBar(
          title: Text(v == null ? 'Voucher' : '${v['code']}'),
          actions: [
            if (v != null) IconButton(tooltip: 'Copy code', icon: const Icon(Icons.copy), onPressed: () => _copy('${v['code']}', 'Code')),
          ],
        ),
        body: v == null
            ? (_err != null
                ? Padding(padding: const EdgeInsets.all(16), child: ErrorCard(message: _err!, onRetry: _load))
                : const Center(child: CircularProgressIndicator()))
            : RefreshIndicator(onRefresh: _load, child: _body(context, v)),
      ),
    );
  }

  Widget _body(BuildContext context, Map<String, dynamic> v) {
    final cs = Theme.of(context).colorScheme;
    final cur = context.read<AppState>().currency;
    final can = (v['can'] as Map?)?.cast<String, dynamic>() ?? {};
    final devices = asMaps(v['devices']);
    final sale = (v['sale'] as Map?)?.cast<String, dynamic>();
    final history = asMaps(v['timeline']);
    final anyAction = can.values.any((x) => x == true);
    return ListView(padding: const EdgeInsets.fromLTRB(16, 0, 16, 40), children: [
      if (_banner != null) ...[
        Card(
          color: const Color(0xFF16A34A).withValues(alpha: .12),
          child: ListTile(
            leading: const Icon(Icons.check_circle, color: Color(0xFF16A34A)),
            title: Text(_banner!, style: const TextStyle(fontWeight: FontWeight.w700)),
          ),
        ),
        const SizedBox(height: 12),
      ],
      _CodeCard(v: v, currency: cur, onCopy: () => _copy('${v['code']}', 'Code')),
      const SizedBox(height: 12),
      Wrap(spacing: 8, runSpacing: 8, children: [
        FilledButton.tonalIcon(onPressed: () => _whatsapp(v), icon: const Icon(Icons.chat, size: 18), label: const Text('WhatsApp')),
        FilledButton.tonalIcon(onPressed: () => _sms(v), icon: const Icon(Icons.sms_outlined, size: 18), label: const Text('SMS')),
        FilledButton.tonalIcon(
            onPressed: () => _copy('${v['share']}', 'Message'), icon: const Icon(Icons.content_copy, size: 18), label: const Text('Copy message')),
      ]),
      if (anyAction) ...[
        const SizedBox(height: 12),
        SectionCard(
          title: 'Actions',
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            if (_busy) const Padding(padding: EdgeInsets.only(bottom: 10), child: LinearProgressIndicator()),
            Wrap(spacing: 8, runSpacing: 8, children: [
              if (can['add_time'] == true)
                FilledButton.icon(
                  style: FilledButton.styleFrom(minimumSize: const Size(0, 44)),
                  onPressed: _busy ? null : () => _addTime(v),
                  icon: const Icon(Icons.more_time, size: 18),
                  label: const Text('Add time'),
                ),
              if (can['enable'] == true)
                FilledButton.tonalIcon(
                  style: FilledButton.styleFrom(minimumSize: const Size(0, 44)),
                  onPressed: _busy ? null : () => _act('enable'),
                  icon: const Icon(Icons.play_arrow, size: 18),
                  label: const Text('Enable'),
                ),
              if (can['disable'] == true)
                OutlinedButton.icon(onPressed: _busy ? null : () => _disable(v), icon: const Icon(Icons.block, size: 18), label: const Text('Disable')),
              if (can['reset_devices'] == true)
                OutlinedButton.icon(
                    onPressed: _busy ? null : () => _reset(v), icon: const Icon(Icons.phonelink_erase, size: 18), label: const Text('Reset devices')),
              if (can['delete'] == true)
                TextButton.icon(
                  style: TextButton.styleFrom(foregroundColor: cs.error),
                  onPressed: _busy ? null : () => _delete(v),
                  icon: const Icon(Icons.delete_outline, size: 18),
                  label: const Text('Delete'),
                ),
            ]),
          ]),
        ),
      ],
      const SizedBox(height: 12),
      SectionCard(
        title: 'Details',
        child: Column(children: [
          _Line('Plan', '${v['plan']} · ${money(toD(v['price']), cur)}'),
          _Line('Time', v['unlimited'] == true ? 'No time limit' : '${v['duration']}, from the first login'),
          _Line('Devices', '${devices.length} of ${v['max_devices']} in use'),
          _Line('Held by', '${v['holder']}'),
          if ('${v['batch'] ?? ''}'.isNotEmpty)
            _Line('Batch', '${v['batch']}',
                onTap: v['batch_id'] == null ? null : () => openBatch(context, toI(v['batch_id']), '${v['batch']}')),
          if ('${v['customer_name'] ?? ''}'.isNotEmpty || '${v['customer_phone'] ?? ''}'.isNotEmpty)
            _Line('Customer', ['${v['customer_name'] ?? ''}', '${v['customer_phone'] ?? ''}'].where((x) => x.isNotEmpty).join(' · ')),
          _Line('Router', '${v['router'] ?? ''}'.isEmpty ? 'TapTap only (not on a router)' : '${v['router']} · ${v['channel']}'),
          if ('${v['router_sync'] ?? ''}'.isNotEmpty) _Line('On the router', '${v['router_sync']}'),
          if ('${v['router_error'] ?? ''}'.isNotEmpty) _Line('Router said', '${v['router_error']}', color: cs.error),
          if (v['used_at'] != null) _Line('First used', whenText(v['used_at'])),
          _Line('Made', whenText(v['created_at'])),
          if ('${v['note'] ?? ''}'.isNotEmpty) _Line('Note', '${v['note']}'),
        ]),
      ),
      if (sale != null) ...[
        const SizedBox(height: 12),
        SectionCard(
          title: 'Sale',
          trailing: Text(money(toD(sale['amount']), cur), style: const TextStyle(fontWeight: FontWeight.w900, fontSize: 16)),
          child: Column(children: [
            _Line('Paid by', '${sale['method']}'),
            _Line('Sold by', '${sale['seller']}'),
            _Line('When', whenText(sale['at'])),
            if ('${sale['customer'] ?? ''}'.isNotEmpty) _Line('Customer', '${sale['customer']}'),
          ]),
        ),
      ],
      if (devices.isNotEmpty) ...[
        const SizedBox(height: 12),
        SectionCard(
          title: 'Devices',
          child: Column(children: [
            for (final d in devices)
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: CircleAvatar(radius: 16, child: Text('${d['slot']}', style: const TextStyle(fontWeight: FontWeight.w800))),
                title: Text('${d['label'] ?? ''}'.isEmpty ? '${d['mac']}' : '${d['label']}'),
                subtitle: Text('${'${d['label'] ?? ''}'.isEmpty ? '' : '${d['mac']} · '}seen ${ago(d['seen'])}'),
              ),
          ]),
        ),
      ],
      const SizedBox(height: 12),
      SectionCard(
        title: 'History',
        child: history.isEmpty
            ? const EmptyNote('Nothing has happened to it yet.')
            : Column(children: [for (final h in history) _HistoryItem(h: h)]),
      ),
    ]);
  }
}

class _CodeCard extends StatelessWidget {
  const _CodeCard({required this.v, required this.currency, required this.onCopy});
  final Map<String, dynamic> v;
  final String currency;
  final VoidCallback onCopy;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final state = '${v['state']}';
    final pct = v['time_pct'];
    final left = toI(v['time_left']);
    String clockLine;
    if (v['frozen'] == true) {
      clockLine = 'Clock stopped${'${v['freeze_reason'] ?? ''}'.isEmpty ? '' : ' — ${v['freeze_reason']}'}';
    } else if (v['unlimited'] == true) {
      clockLine = 'No time limit';
    } else if (state == 'stock' || state == 'sold') {
      clockLine = '${v['duration']} — starts when the customer first connects';
    } else if (left > 0) {
      clockLine = '${spanText(left)} left · ends ${whenText(v['ends_at'])}';
    } else if (v['ends_at'] != null) {
      clockLine = 'Time ran out ${whenText(v['ends_at'])}';
    } else {
      clockLine = '${v['state_label']}';
    }
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(18, 16, 18, 18),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            StatePill(state: state, label: '${v['state_label']}'),
            const Spacer(),
            if ('${v['serial'] ?? ''}'.isNotEmpty) Text('#${v['serial']}', style: TextStyle(color: cs.onSurfaceVariant, fontWeight: FontWeight.w700)),
          ]),
          const SizedBox(height: 10),
          InkWell(
            onTap: onCopy,
            borderRadius: BorderRadius.circular(8),
            child: FittedBox(
              fit: BoxFit.scaleDown,
              alignment: Alignment.centerLeft,
              child: Text('${v['code']}',
                  style: const TextStyle(fontFamily: 'monospace', fontSize: 32, fontWeight: FontWeight.w900, letterSpacing: 3)),
            ),
          ),
          const SizedBox(height: 4),
          Text('${v['plan']} · ${money(toD(v['price']), currency)}', style: const TextStyle(fontWeight: FontWeight.w700)),
          const SizedBox(height: 12),
          if (pct != null && v['frozen'] != true) ...[
            ClipRRect(
              borderRadius: BorderRadius.circular(99),
              child: LinearProgressIndicator(
                value: (toD(pct) / 100).clamp(0.0, 1.0),
                minHeight: 8,
                color: stateColor(context, state),
                backgroundColor: stateColor(context, state).withValues(alpha: .12),
              ),
            ),
            const SizedBox(height: 6),
          ],
          Text(clockLine, style: TextStyle(color: cs.onSurfaceVariant)),
        ]),
      ),
    );
  }
}

class _Line extends StatelessWidget {
  const _Line(this.label, this.value, {this.onTap, this.color});
  final String label, value;
  final VoidCallback? onTap;
  final Color? color;
  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SizedBox(width: 110, child: Text(label, style: TextStyle(color: cs.onSurfaceVariant))),
          Expanded(
            child: Text(value,
                style: TextStyle(fontWeight: FontWeight.w600, color: color ?? (onTap != null ? cs.primary : null))),
          ),
          if (onTap != null) Icon(Icons.chevron_right, size: 18, color: cs.primary),
        ]),
      ),
    );
  }
}

class _HistoryItem extends StatelessWidget {
  const _HistoryItem({required this.h});
  final Map<String, dynamic> h;
  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final tone = switch ('${h['tone']}') {
      'success' => const Color(0xFF16A34A),
      'danger' => cs.error,
      'warning' => const Color(0xFFD97706),
      'info' => const Color(0xFF0891B2),
      'primary' => cs.primary,
      _ => cs.outline,
    };
    final meta = [
      if ('${h['who'] ?? ''}'.isNotEmpty) '${h['who']}',
      if ('${h['via'] ?? ''}'.isNotEmpty) '${h['via']}',
      ago(h['at']),
    ].join(' · ');
    final extra = ['${h['text'] ?? ''}', '${h['reason'] ?? ''}'.isEmpty ? '' : 'Why: ${h['reason']}', '${h['result'] ?? ''}']
        .where((x) => x.isNotEmpty)
        .join('\n');
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 7),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Container(width: 10, height: 10, margin: const EdgeInsets.only(top: 5, right: 12), decoration: BoxDecoration(color: tone, shape: BoxShape.circle)),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('${h['title']}', style: const TextStyle(fontWeight: FontWeight.w700)),
            if (extra.isNotEmpty) Text(extra, style: const TextStyle(fontSize: 13)),
            Text(meta, style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
          ]),
        ),
      ]),
    );
  }
}
