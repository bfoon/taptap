import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../widgets/common.dart';
import '../widgets/voucher_widgets.dart';
import 'voucher_detail_screen.dart';
import 'vouchers_screen.dart';

/// Make vouchers — a batch (like Generate on the website) or one voucher for one customer (Single voucher).
/// Pops with `true` once something was made, so the list underneath reloads.
class NewVouchersScreen extends StatefulWidget {
  const NewVouchersScreen({super.key});
  @override
  State<NewVouchersScreen> createState() => _NewVouchersScreenState();
}

class _NewVouchersScreenState extends State<NewVouchersScreen> {
  Map<String, dynamic>? _o;          // /vouchers/new/: plans, routers, agents, methods, code format, limits
  String? _err;
  bool _busy = false;
  bool _single = false;

  Map<String, dynamic>? _plan;
  final _qty = TextEditingController(text: '20');
  final _name = TextEditingController();
  final _note = TextEditingController();
  final _prefix = TextEditingController();
  final _code = TextEditingController();
  final _customer = TextEditingController();
  final _phone = TextEditingController();
  final _reference = TextEditingController();
  int? _batchRouter;                 // batch: no router by default ("create locally only", as on the website)
  int? _singleRouter;                // single: the first router (sent straight away, as on the website)
  int? _agent;
  String _settlement = 'credit';
  String _method = 'cash';
  bool _paid = true;
  int _len = 8;
  String _charset = 'mixed';

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    for (final c in [_qty, _name, _note, _prefix, _code, _customer, _phone, _reference]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final d = await Api.I.get('/api/mobile/v1/vouchers/new/');
      if (!mounted) return;
      final plans = asMaps(d['plans']);
      final routers = asMaps(d['routers']);
      final code = (d['code'] as Map?)?.cast<String, dynamic>() ?? {};
      setState(() {
        _o = d;
        _plan = plans.length == 1 ? plans.first : null;
        _singleRouter = routers.isEmpty ? null : toI(routers.first['id']);
        _len = toI(code['length']) == 0 ? 8 : toI(code['length']);
        _err = null;
      });
    } on ApiError catch (e) {
      if (mounted) setState(() => _err = e.message);
    }
  }

  // ── helpers ──

  String get _cur => '${_o?['currency'] ?? 'D'}';
  Map<String, dynamic> get _codeRules => (_o?['code'] as Map?)?.cast<String, dynamic>() ?? {};
  Map<String, dynamic> get _limits => (_o?['limits'] as Map?)?.cast<String, dynamic>() ?? {};
  List<Map<String, dynamic>> get _routers => asMaps(_o?['routers']);
  List<Map<String, dynamic>> get _agents => asMaps(_o?['agents']);
  List<Map<String, dynamic>> get _methods => asMaps(_o?['methods']);
  int get _quantity => toI(_qty.text.trim());
  int? get _router => _single ? _singleRouter : _batchRouter;

  String _nameOf(List<Map<String, dynamic>> rows, int? id, String none) =>
      id == null ? none : '${rows.firstWhere((r) => toI(r['id']) == id, orElse: () => {'name': none})['name']}';

  String get _methodLabel => '${_methods.firstWhere((m) => m['key'] == _method, orElse: () => {'label': _method})['label']}';

  /// Too few random characters left after the prefix (the server refuses it too).
  String? get _formatProblem {
    if (_single && _code.text.trim().isNotEmpty) return null;
    final left = _len - _prefix.text.trim().length;
    final need = toI(_codeRules['random_min']) == 0 ? 3 : toI(_codeRules['random_min']);
    return left < need ? 'Keep at least $need random characters: make the code longer or the prefix shorter.' : null;
  }

  String _example() {
    const sets = {'mixed': 'ABCDEFGHJKMNPQRSTUVWXYZ23456789', 'numbers': '0123456789', 'letters': 'ABCDEFGHJKMNPQRSTUVWXYZ'};
    final chars = sets[_charset] ?? sets['mixed']!;
    final p = _prefix.text.trim().toUpperCase();
    final r = Random(_len * 31 + _charset.length);
    return p + List.generate(max(0, _len - p.length), (_) => chars[r.nextInt(chars.length)]).join();
  }

  void _toast(String m, {bool error = false}) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(m),
        behavior: SnackBarBehavior.floating,
        backgroundColor: error ? Theme.of(context).colorScheme.error : null,
      ));

  /// A choice from a short list, in a bottom sheet. Returns the picked key (null = backed out).
  Future<T?> _pick<T>(String title, List<(T, String, String)> options, T current) => showModalBottomSheet<T>(
        context: context,
        showDragHandle: true,
        isScrollControlled: true,
        builder: (ctx) => SafeArea(
          child: ConstrainedBox(
            constraints: BoxConstraints(maxHeight: MediaQuery.of(ctx).size.height * .7),
            child: ListView(shrinkWrap: true, children: [
              Padding(padding: const EdgeInsets.fromLTRB(20, 0, 20, 8), child: Text(title, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800))),
              for (final o in options)
                ListTile(
                  leading: Icon(o.$1 == current ? Icons.radio_button_checked : Icons.radio_button_off,
                      color: o.$1 == current ? Theme.of(ctx).colorScheme.primary : null),
                  title: Text(o.$2),
                  subtitle: o.$3.isEmpty ? null : Text(o.$3),
                  onTap: () => Navigator.pop(ctx, o.$1),
                ),
            ]),
          ),
        ),
      );

  // Sentinel for "none" in pickers, since null means "backed out".
  static const _none = -1;

  Future<void> _pickRouter() async {
    final cur = _router ?? _none;
    final v = await _pick<int>('Router', [
      (_none, _single ? 'Don’t send to a router yet' : 'Create in TapTap only', 'They go to a router with the next sync.'),
      for (final r in _routers) (toI(r['id']), '${r['name']}', '${r['status'] ?? ''}'),
    ], cur);
    if (v == null) return;
    setState(() {
      final id = v == _none ? null : v;
      if (_single) {
        _singleRouter = id;
      } else {
        _batchRouter = id;
      }
    });
  }

  Future<void> _pickAgent() async {
    final v = await _pick<int>(_single ? 'Sold by' : 'Who holds them', [
      (_none, _single ? 'Me (the shop)' : 'Your shop — your own stock', ''),
      for (final a in _agents) (toI(a['id']), '${a['name']}', _single ? '' : 'An agent who sells them'),
    ], _agent ?? _none);
    if (v != null) setState(() => _agent = v == _none ? null : v);
  }

  Future<void> _pickMethod() async {
    final v = await _pick<String>('Paid with', [for (final m in _methods) ('${m['key']}', '${m['label']}', '')], _method);
    if (v != null) setState(() => _method = v);
  }

  // ── make them ──

  Future<void> _submit({int? confirmed}) async {
    final plan = _plan;
    if (plan == null) {
      _toast('Choose a plan first.', error: true);
      return;
    }
    if (!_single && _quantity < 1) {
      _toast('Type how many vouchers to make.', error: true);
      return;
    }
    setState(() => _busy = true);
    HapticFeedback.selectionClick();
    final body = <String, dynamic>{
      'kind': _single ? 'single' : 'batch',
      'plan': plan['id'],
      'code_length': _len,
      'code_charset': _charset,
      'code_prefix': _prefix.text.trim(),
      'note': _note.text.trim(),
      if (_router != null) 'router': _router,
      if (_agent != null) 'agent': _agent,
    };
    if (_single) {
      body.addAll({
        'code': _code.text.trim(),
        'customer_name': _customer.text.trim(),
        'customer_phone': _phone.text.trim(),
        'paid': _paid,
        'method': _method,
        'reference': _reference.text.trim(),
      });
    } else {
      body.addAll({'quantity': _quantity, 'name': _name.text.trim()});
      if (_agent != null) body.addAll({'settlement': _settlement, 'pay_method': _method, 'reference': _reference.text.trim()});
      if (confirmed != null) body['confirm_qty'] = confirmed;
    }
    try {
      final d = await Api.I.post('/api/mobile/v1/vouchers/new/', body, const Duration(seconds: 180));
      if (!mounted) return;
      final notes = asMaps(d['notes']);
      if (_single) {
        final v = (d['voucher'] as Map).cast<String, dynamic>();
        final warn = notes.where((n) => n['level'] == 'warning').map((n) => '${n['text']}');
        Navigator.of(context).pushReplacement(
          MaterialPageRoute(
            builder: (_) => VoucherDetailScreen(id: toI(v['id']), initial: v, banner: ['${d['message']}', ...warn].join(' ')),
          ),
          result: true,
        );
      } else {
        Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => BatchMadeScreen(result: d)), result: true);
      }
    } on ApiError catch (e) {
      if (!mounted) return;
      setState(() => _busy = false);
      if (e.data?['confirm'] == true) {
        final n = toI(e.data!['quantity']);
        final ok = await showDialog<bool>(
          context: context,
          builder: (ctx) => AlertDialog(
            title: Text('Make $n vouchers?'),
            content: Text(e.message),
            actions: [
              TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
              FilledButton(onPressed: () => Navigator.pop(ctx, true), child: Text('Make $n')),
            ],
          ),
        );
        if (ok == true) await _submit(confirmed: n);
        return;
      }
      _toast(e.message, error: true);
    } finally {
      if (mounted && _busy) setState(() => _busy = false);
    }
  }

  // ── screen ──

  @override
  Widget build(BuildContext context) {
    final o = _o;
    return Scaffold(
      appBar: AppBar(title: const Text('New vouchers')),
      body: o == null
          ? (_err != null
              ? Padding(padding: const EdgeInsets.all(16), child: ErrorCard(message: _err!, onRetry: _load))
              : const Center(child: CircularProgressIndicator()))
          : _form(context),
      bottomNavigationBar: o == null ? null : _button(context),
    );
  }

  Widget _button(BuildContext context) {
    final plan = _plan;
    final q = _quantity;
    final label = plan == null
        ? 'Choose a plan'
        : _single
            ? 'Make the voucher · ${money(toD(plan['price']), _cur)}'
            : 'Make $q voucher${q == 1 ? '' : 's'} · ${money(toD(plan['price']) * q, _cur)}';
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
        child: FilledButton(
          onPressed: _busy || plan == null || _formatProblem != null || (!_single && q < 1) ? null : () => _submit(),
          child: _busy
              ? const SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2.5, color: Colors.white))
              : Text(label),
        ),
      ),
    );
  }

  Widget _form(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final plans = asMaps(_o?['plans']);
    final confirmAbove = toI(_limits['confirm_above']);
    return ListView(padding: const EdgeInsets.fromLTRB(16, 0, 16, 24), children: [
      SegmentedButton<bool>(
        segments: const [
          ButtonSegment(value: false, label: Text('Batch'), icon: Icon(Icons.layers_outlined)),
          ButtonSegment(value: true, label: Text('One voucher'), icon: Icon(Icons.person_outline)),
        ],
        selected: {_single},
        onSelectionChanged: (s) => setState(() {
          _single = s.first;
          _agent = null;
        }),
      ),
      const SizedBox(height: 12),
      SectionCard(
        title: 'Plan',
        child: plans.isEmpty
            ? const EmptyNote('No active plans yet — add one under Plans on the website.')
            : Column(children: [
                for (final p in plans)
                  _PlanTile(
                    plan: p,
                    currency: _cur,
                    selected: _plan != null && _plan!['id'] == p['id'],
                    onTap: () => setState(() => _plan = p),
                  ),
              ]),
      ),
      const SizedBox(height: 12),
      if (!_single)
        SectionCard(
          title: 'How many',
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            TextField(
              controller: _qty,
              keyboardType: TextInputType.number,
              inputFormatters: [FilteringTextInputFormatter.digitsOnly],
              onChanged: (_) => setState(() {}),
              decoration: InputDecoration(
                hintText: 'Number of vouchers',
                helperText: _quantity > confirmAbove && confirmAbove > 0 ? 'More than $confirmAbove: you will be asked to confirm the amount.' : null,
              ),
            ),
            const SizedBox(height: 8),
            Wrap(spacing: 8, children: [
              for (final n in const [10, 20, 50, 100, 200])
                ChoiceChip(
                  label: Text('$n'),
                  selected: _quantity == n,
                  onSelected: (_) => setState(() => _qty.text = '$n'),
                ),
            ]),
            const SizedBox(height: 12),
            TextField(controller: _name, maxLength: 120, decoration: const InputDecoration(hintText: 'Batch name (optional — plan and date if empty)')),
          ]),
        )
      else
        SectionCard(
          title: 'Customer',
          child: Column(children: [
            TextField(controller: _customer, textCapitalization: TextCapitalization.words, decoration: const InputDecoration(hintText: 'Name (optional)')),
            const SizedBox(height: 8),
            TextField(controller: _phone, keyboardType: TextInputType.phone, decoration: const InputDecoration(hintText: 'Phone (optional — to send the code)')),
            const SizedBox(height: 8),
            TextField(
              controller: _code,
              textCapitalization: TextCapitalization.characters,
              inputFormatters: [FilteringTextInputFormatter.allow(RegExp('[A-Za-z0-9]')), _UpperCase()],
              maxLength: 20,
              onChanged: (_) => setState(() {}),
              decoration: const InputDecoration(hintText: 'Own code (optional — random if empty)'),
            ),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Paid now', style: TextStyle(fontWeight: FontWeight.w700)),
              subtitle: Text(_paid ? 'Booked as a sale of ${_plan == null ? 'the plan price' : money(toD(_plan!['price']), _cur)}' : 'Not booked as a sale yet'),
              value: _paid,
              onChanged: (x) => setState(() => _paid = x),
            ),
            if (_paid) ...[
              _PickTile(label: 'Paid with', value: _methodLabel, onTap: _pickMethod),
              TextField(controller: _reference, decoration: const InputDecoration(hintText: 'Transaction reference (optional)')),
            ],
          ]),
        ),
      const SizedBox(height: 12),
      SectionCard(
        title: 'Where they go',
        child: Column(children: [
          _PickTile(
            label: 'Router',
            value: _nameOf(_routers, _router, _single ? 'Not sent to a router yet' : 'TapTap only — next sync'),
            onTap: _pickRouter,
          ),
          if (_agents.isNotEmpty) _PickTile(label: _single ? 'Sold by' : 'Held by', value: _nameOf(_agents, _agent, 'Your shop'), onTap: _pickAgent),
          if (!_single && _agent != null) ...[
            const SizedBox(height: 8),
            SizedBox(
              width: double.infinity,
              child: SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'credit', label: Text('On credit')),
                  ButtonSegment(value: 'prepaid', label: Text('Paid upfront')),
                ],
                selected: {_settlement},
                onSelectionChanged: (s) => setState(() => _settlement = s.first),
              ),
            ),
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                _settlement == 'credit' ? 'The agent pays for each voucher as it sells.' : 'The agent buys the whole batch now: the sales are booked today.',
                style: TextStyle(fontSize: 12.5, color: cs.onSurfaceVariant),
              ),
            ),
            if (_settlement == 'prepaid') _PickTile(label: 'Paid with', value: _methodLabel, onTap: _pickMethod),
          ],
        ]),
      ),
      const SizedBox(height: 12),
      if (!(_single && _code.text.trim().isNotEmpty))
        Card(
          clipBehavior: Clip.antiAlias,
          child: ExpansionTile(
            shape: const Border(),
            title: const Text('Code format', style: TextStyle(fontWeight: FontWeight.w800)),
            subtitle: Text('e.g. ${_example()}', style: const TextStyle(fontFamily: 'monospace', fontWeight: FontWeight.w700)),
            childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
            expandedCrossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('$_len characters', style: const TextStyle(fontWeight: FontWeight.w700)),
              Slider(
                value: _len.toDouble(),
                min: toD(_codeRules['min'] ?? 4),
                max: toD(_codeRules['max'] ?? 16),
                divisions: max(1, toI(_codeRules['max'] ?? 16) - toI(_codeRules['min'] ?? 4)),
                label: '$_len',
                onChanged: (x) => setState(() => _len = x.round()),
              ),
              Wrap(spacing: 8, runSpacing: 4, children: [
                for (final c in asMaps(_codeRules['charsets']))
                  ChoiceChip(
                    label: Text('${c['label']}'),
                    selected: _charset == c['key'],
                    onSelected: (_) => setState(() => _charset = '${c['key']}'),
                  ),
              ]),
              const SizedBox(height: 10),
              TextField(
                controller: _prefix,
                textCapitalization: TextCapitalization.characters,
                inputFormatters: [FilteringTextInputFormatter.allow(RegExp('[A-Za-z0-9]')), _UpperCase()],
                maxLength: toI(_codeRules['affix_max']) == 0 ? 6 : toI(_codeRules['affix_max']),
                onChanged: (_) => setState(() {}),
                decoration: InputDecoration(hintText: 'Prefix (optional), e.g. KN', errorText: _formatProblem),
              ),
              Text('Your Wi-Fi login page expects ${_codeRules['length']} characters.', style: TextStyle(fontSize: 12.5, color: cs.onSurfaceVariant)),
            ],
          ),
        ),
      const SizedBox(height: 12),
      TextField(controller: _note, maxLength: 255, decoration: const InputDecoration(hintText: 'Note for yourself (optional)')),
    ]);
  }
}

class _PlanTile extends StatelessWidget {
  const _PlanTile({required this.plan, required this.currency, required this.selected, required this.onTap});
  final Map<String, dynamic> plan;
  final String currency;
  final bool selected;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final devices = toI(plan['devices']);
    final sub = [
      '${plan['duration']}',
      if (devices > 1) '$devices devices',
      if ('${plan['speed'] ?? ''}'.isNotEmpty) '${plan['speed']}',
    ].join(' · ');
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Material(
        color: selected ? cs.primaryContainer : cs.surfaceContainerHighest.withValues(alpha: .5),
        borderRadius: BorderRadius.circular(14),
        child: InkWell(
          borderRadius: BorderRadius.circular(14),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            child: Row(children: [
              Icon(selected ? Icons.radio_button_checked : Icons.radio_button_off, color: selected ? cs.primary : cs.onSurfaceVariant),
              const SizedBox(width: 10),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('${plan['name']}', style: const TextStyle(fontWeight: FontWeight.w800)),
                  Text(sub, style: TextStyle(fontSize: 12.5, color: cs.onSurfaceVariant)),
                ]),
              ),
              Text(plan['free'] == true ? 'Free' : money(toD(plan['price']), currency), style: const TextStyle(fontWeight: FontWeight.w900)),
            ]),
          ),
        ),
      ),
    );
  }
}

class _PickTile extends StatelessWidget {
  const _PickTile({required this.label, required this.value, required this.onTap});
  final String label, value;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) => ListTile(
        contentPadding: EdgeInsets.zero,
        title: Text(label, style: TextStyle(fontSize: 12.5, color: Theme.of(context).colorScheme.onSurfaceVariant)),
        subtitle: Text(value, style: const TextStyle(fontWeight: FontWeight.w700, fontSize: 15)),
        trailing: const Icon(Icons.expand_more),
        onTap: onTap,
      );
}

class _UpperCase extends TextInputFormatter {
  @override
  TextEditingValue formatEditUpdate(TextEditingValue oldValue, TextEditingValue newValue) =>
      newValue.copyWith(text: newValue.text.toUpperCase());
}

/// After a batch was made: the codes (tap one to copy it), copy all, WhatsApp, print, open the batch.
class BatchMadeScreen extends StatelessWidget {
  const BatchMadeScreen({super.key, required this.result});
  final Map<String, dynamic> result;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final batch = (result['batch'] as Map?)?.cast<String, dynamic>() ?? {};
    final codes = asMaps(result['codes']);
    final notes = asMaps(result['notes']);
    final more = toI(result['more']);
    final all = codes.map((c) => '${c['code']}').join('\n');
    void copy(String text, String what) {
      Clipboard.setData(ClipboardData(text: text));
      HapticFeedback.selectionClick();
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$what copied'), behavior: SnackBarBehavior.floating));
    }

    return Scaffold(
      appBar: AppBar(title: Text('${result['message'] ?? 'Vouchers made'}')),
      body: ListView(padding: const EdgeInsets.fromLTRB(16, 0, 16, 40), children: [
        BatchSummary(
          batch: batch,
          currency: context.read<AppState>().currency,
          onTap: () => openBatch(context, toI(batch['id']), '${batch['name']}'),
        ),
        for (final n in notes) ...[
          const SizedBox(height: 8),
          Card(
            color: n['level'] == 'warning' ? const Color(0xFFF59E0B).withValues(alpha: .14) : null,
            child: ListTile(
              leading: Icon(n['level'] == 'warning' ? Icons.warning_amber : Icons.info_outline,
                  color: n['level'] == 'warning' ? const Color(0xFFB45309) : cs.primary),
              title: Text('${n['text']}', style: const TextStyle(fontSize: 13.5)),
            ),
          ),
        ],
        const SizedBox(height: 12),
        Wrap(spacing: 8, runSpacing: 8, children: [
          FilledButton.tonalIcon(onPressed: () => copy(all, '${codes.length} codes'), icon: const Icon(Icons.copy_all, size: 18), label: const Text('Copy all')),
          if (codes.length <= 100)
            FilledButton.tonalIcon(
              onPressed: () => launchUrl(Uri.parse('https://wa.me/?text=${Uri.encodeComponent('${batch['name']}\n$all')}'),
                  mode: LaunchMode.externalApplication),
              icon: const Icon(Icons.chat, size: 18),
              label: const Text('WhatsApp'),
            ),
          if ('${batch['print_url'] ?? ''}'.isNotEmpty)
            FilledButton.tonalIcon(
              onPressed: () => launchUrl(Uri.parse('${Api.I.server}${batch['print_url']}'), mode: LaunchMode.externalApplication),
              icon: const Icon(Icons.print_outlined, size: 18),
              label: const Text('Print on the website'),
            ),
        ]),
        const SizedBox(height: 12),
        SectionCard(
          title: 'Codes',
          trailing: Text('${codes.length}${more > 0 ? ' of ${codes.length + more}' : ''}', style: TextStyle(color: cs.onSurfaceVariant)),
          child: Column(children: [
            for (final c in codes)
              InkWell(
                onTap: () => copy('${c['code']}', '${c['code']}'),
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 7),
                  child: Row(children: [
                    Expanded(
                      child: Text('${c['code']}', style: const TextStyle(fontFamily: 'monospace', fontWeight: FontWeight.w800, fontSize: 16, letterSpacing: 1)),
                    ),
                    if ('${c['serial'] ?? ''}'.isNotEmpty) Text('#${c['serial']}', style: TextStyle(color: cs.onSurfaceVariant)),
                    const SizedBox(width: 6),
                    Icon(Icons.copy, size: 16, color: cs.onSurfaceVariant),
                  ]),
                ),
              ),
            if (more > 0)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: TextButton(
                  onPressed: () => openBatch(context, toI(batch['id']), '${batch['name']}'),
                  child: Text('…and $more more — open the batch'),
                ),
              ),
          ]),
        ),
      ]),
    );
  }
}
