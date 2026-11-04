import 'dart:async';

import 'package:flutter/material.dart';

import '../app/api.dart';
import '../app/format.dart';

/// Move a MikroTik to another business (server: core/router_move.py through /api/mobile/v1/routers/<id>/move/).
/// Returns the server's message when the router was moved, null when the person backed out.
Future<String?> showMoveRouter(BuildContext context, {required int routerId, required String routerName}) =>
    showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (_) => _MoveSheet(routerId: routerId, routerName: routerName),
    );

class _MoveSheet extends StatefulWidget {
  const _MoveSheet({required this.routerId, required this.routerName});
  final int routerId;
  final String routerName;
  @override
  State<_MoveSheet> createState() => _MoveSheetState();
}

class _MoveSheetState extends State<_MoveSheet> {
  Map<String, dynamic>? _d;
  String? _err;
  bool _vouchers = true;
  bool _busy = false;
  final _id = TextEditingController();
  final _confirm = TextEditingController();
  Timer? _debounce;

  String get _path => '/api/mobile/v1/routers/${widget.routerId}/move/';
  Map<String, dynamic>? get _target => (_d?['target'] as Map?)?.cast<String, dynamic>();
  bool get _ready => _target != null && _confirm.text.trim().toLowerCase() == widget.routerName.trim().toLowerCase();

  @override
  void initState() {
    super.initState();
    _load();
    _confirm.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _id.dispose();
    _confirm.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final d = await Api.I.get(_path, query: {'vouchers': _vouchers ? '1' : '0', 'business_id': _id.text.trim()});
      if (!mounted) return;
      setState(() {
        _d = d;
        _err = null;
      });
    } on ApiError catch (e) {
      if (mounted) setState(() => _err = e.message);
    }
  }

  void _typed(String _) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 350), _load);
    setState(() {});
  }

  Future<void> _move() async {
    setState(() => _busy = true);
    try {
      final d = await Api.I.post(_path, {
        'business_id': _id.text.trim(),
        'confirm_name': _confirm.text.trim(),
        'with_vouchers': _vouchers,
      });
      if (mounted) Navigator.of(context).pop('${d['message'] ?? 'Moved.'}');
    } on ApiError catch (e) {
      if (mounted) {
        setState(() => _busy = false);
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message), behavior: SnackBarBehavior.floating));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final p = (_d?['preview'] as Map?)?.cast<String, dynamic>() ?? {};
    final v = (p['vouchers'] as Map?)?.cast<String, dynamic>() ?? {};
    final moved = (p['moved'] as Map?)?.cast<String, dynamic>() ?? {};
    final stays = (p['stays'] as Map?)?.cast<String, dynamic>() ?? {};
    final targets = asMaps(_d?['targets']);
    final here = (_d?['this_business'] as Map?)?.cast<String, dynamic>() ?? {};
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 0, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
      child: SingleChildScrollView(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('Move ${widget.routerName}', style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w800)),
          if (here.isNotEmpty)
            Text('Now in ${here['name']} (#${here['id']})', style: TextStyle(color: cs.onSurfaceVariant)),
          if (_err != null) Padding(padding: const EdgeInsets.only(top: 8), child: Text(_err!, style: TextStyle(color: cs.error))),
          const SizedBox(height: 14),
          const Text('To which business?', style: TextStyle(fontWeight: FontWeight.w700)),
          for (final t in targets)
            ListTile(
              contentPadding: EdgeInsets.zero,
              selected: _id.text.trim() == '${t['id']}',
              leading: Icon(_id.text.trim() == '${t['id']}' ? Icons.radio_button_checked : Icons.radio_button_unchecked),
              title: Text('${t['name']}'),
              subtitle: Text('#${t['id']} · you are ${t['role']}'),
              onTap: () {
                _id.text = '${t['id']}';
                _load();
                setState(() {});
              },
            ),
          TextField(
            controller: _id,
            keyboardType: TextInputType.number,
            onChanged: _typed,
            decoration: InputDecoration(
              labelText: 'Business ID',
              prefixText: '# ',
              helperText: targets.isEmpty ? 'You must be the owner or an Admin of that business.' : 'Or type another business ID',
            ),
          ),
          if (_target != null)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Row(children: [
                const Icon(Icons.check_circle, size: 18, color: Color(0xFF16A34A)),
                const SizedBox(width: 6),
                Expanded(child: Text('${_target!['name']} — has ${_target!['routers']} router(s) now')),
              ]),
            )
          else if (_d?['target_error'] != null)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text('${_d!['target_error']}', style: TextStyle(color: cs.error, fontSize: 13)),
            ),
          const SizedBox(height: 8),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: _vouchers,
            onChanged: (x) {
              setState(() => _vouchers = x);
              _load();
            },
            title: const Text('Bring its vouchers and members', style: TextStyle(fontWeight: FontWeight.w700)),
            subtitle: const Text('Recommended: people online keep working and the new business sees them.'),
          ),
          if (_d != null)
            Card(
              child: Padding(
                padding: const EdgeInsets.all(12),
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  const Text('Goes with the router', style: TextStyle(fontWeight: FontWeight.w800)),
                  if (_vouchers) _line('Vouchers & members', v['total'], extra: '${v['in_use'] ?? 0} in use · ${v['unsold'] ?? 0} not sold'),
                  for (final e in moved.entries.take(6)) _line(e.key, e.value),
                  const Divider(height: 20),
                  const Text('Stays here', style: TextStyle(fontWeight: FontWeight.w800)),
                  if (toI(p['sales_stay']) > 0) _line('Sales', p['sales_stay']),
                  for (final e in stays.entries) _line(e.key, e.value),
                  if (!_vouchers && toI(v['left_behind']) > 0) _line('Vouchers (not tracked after the move)', v['left_behind']),
                  const SizedBox(height: 4),
                  Text('Money stays where it was earned: agent balances and past reports do not change.',
                      style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
                ]),
              ),
            ),
          const SizedBox(height: 8),
          TextField(
            controller: _confirm,
            decoration: InputDecoration(labelText: 'Type “${widget.routerName}” to confirm'),
          ),
          const SizedBox(height: 14),
          SizedBox(
            width: double.infinity,
            height: 52,
            child: FilledButton.icon(
              style: FilledButton.styleFrom(backgroundColor: cs.error, foregroundColor: cs.onError),
              onPressed: _ready && !_busy ? _move : null,
              icon: _busy
                  ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.drive_file_move_outline),
              label: const Text('Move router', style: TextStyle(fontWeight: FontWeight.w800)),
            ),
          ),
          const SizedBox(height: 6),
          Text('TapTap Link keeps working — nothing to change on the MikroTik.',
              style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
        ]),
      ),
    );
  }

  Widget _line(String label, dynamic n, {String? extra}) => Padding(
        padding: const EdgeInsets.only(top: 6),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(label),
              if (extra != null) Text(extra, style: const TextStyle(fontSize: 12)),
            ]),
          ),
          Text('${toI(n)}', style: const TextStyle(fontWeight: FontWeight.w800)),
        ]),
      );
}
