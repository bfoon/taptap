import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app/api.dart';
import '../app/format.dart';

/// "Can't connect?" — everything that stops this voucher (in TapTap and on the router), and Fix it:
/// Reset devices + the router user set right (time from the router's own counter) + the router read back.
/// Resolves to true when a fix ran, so the voucher page reloads.
Future<bool?> showVoucherFix(BuildContext context, int id, String code) => showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _FixSheet(id: id, code: code),
    );

class _FixSheet extends StatefulWidget {
  const _FixSheet({required this.id, required this.code});
  final int id;
  final String code;
  @override
  State<_FixSheet> createState() => _FixSheetState();
}

class _FixSheetState extends State<_FixSheet> {
  List<Map<String, dynamic>> _found = [];
  bool _loading = true, _busy = false, _ran = false, _fixable = false, _waiting = false;
  String _blocked = '', _message = '', _error = '';
  bool? _ok;
  List<String> _lines = [], _notes = [];
  Timer? _timer;
  int _polls = 0;

  String get _path => '/api/mobile/v1/vouchers/${widget.id}/';

  @override
  void initState() {
    super.initState();
    _check();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _check() async {
    try {
      final d = await Api.I.post(_path, {'action': 'check'});
      if (!mounted) return;
      setState(() {
        _found = asMaps(d['findings']);
        _fixable = d['fixable'] == true;
        _blocked = '${d['blocked'] ?? ''}';
        _loading = false;
      });
    } on ApiError catch (e) {
      if (mounted) setState(() {
        _error = e.message;
        _loading = false;
      });
    }
  }

  Future<void> _fix() async {
    setState(() {
      _busy = true;
      _error = '';
    });
    HapticFeedback.mediumImpact();
    try {
      final d = await Api.I.post(_path, {'action': 'fix', 'reason': 'Customer could not connect'});
      if (!mounted) return;
      setState(() {
        _ran = true;
        _found = asMaps(d['findings']);
        _message = '${d['message'] ?? ''}';
        _notes = [for (final n in (d['notes'] as List? ?? const [])) '$n'];
        _lines = [for (final n in (d['lines'] as List? ?? const [])) '$n'];
        _waiting = d['waiting'] == true;
        _ok = _waiting ? null : d['fixed'] == true;
      });
      final cmd = d['cmd'];
      if (_waiting && cmd != null) _poll('$cmd');
    } on ApiError catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _poll(String cmd) {
    _timer?.cancel();
    if (_polls++ > 60) {
      setState(() {
        _waiting = false;
        _ok = false;
        _lines = ['No answer from the router after 3 minutes. The fix stays queued on the TapTap Link page.'];
      });
      return;
    }
    _timer = Timer(const Duration(seconds: 3), () async {
      try {
        final d = await Api.I.get(_path, query: {'fix': cmd});
        if (!mounted) return;
        final st = '${d['status'] ?? ''}';
        setState(() {
          _lines = [for (final n in (d['lines'] as List? ?? const [])) '$n'];
          _waiting = st == 'waiting';
          if (!_waiting) _ok = st == 'done' && d['ok'] != false;
        });
        if (_waiting) _poll(cmd);
      } on ApiError catch (_) {
        if (mounted) _poll(cmd);
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: MediaQuery.of(context).size.height * .85),
        child: ListView(shrinkWrap: true, padding: const EdgeInsets.fromLTRB(20, 0, 20, 24), children: [
          Text("Why can't ${widget.code} connect?", style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800)),
          const SizedBox(height: 12),
          if (_loading) const Padding(padding: EdgeInsets.all(24), child: Center(child: CircularProgressIndicator())),
          if (_error.isNotEmpty) Text(_error, style: TextStyle(color: cs.error)),
          if (!_loading && _found.isEmpty && _error.isEmpty)
            const _Finding(level: 'ok', title: 'TapTap sees nothing stopping it',
                text: 'If the customer still cannot connect, press Fix it: the router is checked and read back.'),
          for (final f in _found)
            _Finding(level: '${f['level']}', title: '${f['title']}', text: '${f['text'] ?? ''}', fix: '${f['fix'] ?? ''}'),
          const SizedBox(height: 12),
          if (!_loading && _fixable)
            FilledButton.icon(
              style: FilledButton.styleFrom(backgroundColor: cs.error, minimumSize: const Size.fromHeight(48)),
              onPressed: _busy ? null : _fix,
              icon: _busy
                  ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                  : const Icon(Icons.auto_fix_high),
              label: Text(_ran ? 'Fix it again' : 'Fix it'),
            ),
          if (!_loading && !_fixable && _blocked.isNotEmpty)
            Text('Not fixable yet: $_blocked.', style: const TextStyle(color: Color(0xFFB45309))),
          if (!_ran && _fixable)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                "Resets its devices, switches it on, frees it for any phone, sets its time from the router's own counter, "
                'ends stuck sessions — then reads the router back.',
                style: TextStyle(color: cs.onSurfaceVariant, fontSize: 12.5),
              ),
            ),
          if (_ran) ...[
            const SizedBox(height: 14),
            Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(16),
                color: (_ok == null ? Colors.blue : (_ok! ? Colors.green : Colors.red)).withValues(alpha: .08),
                border: Border.all(color: (_ok == null ? Colors.blue : (_ok! ? Colors.green : Colors.red)).withValues(alpha: .35)),
              ),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(_message, style: const TextStyle(fontWeight: FontWeight.w700)),
                for (final n in _notes) Padding(padding: const EdgeInsets.only(top: 4), child: Text('• $n', style: const TextStyle(fontSize: 13))),
                if (_lines.isNotEmpty) ...[
                  const SizedBox(height: 10),
                  Row(children: [
                    _waiting
                        ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                        : Icon(Icons.router, size: 18, color: _ok == true ? Colors.green : cs.error),
                    const SizedBox(width: 8),
                    Text(_waiting ? 'Router' : (_ok == true ? 'The router has it right now' : 'The router says'),
                        style: const TextStyle(fontWeight: FontWeight.w700)),
                  ]),
                  for (final l in _lines) Padding(padding: const EdgeInsets.only(top: 4), child: Text('• $l', style: const TextStyle(fontSize: 13))),
                ],
              ]),
            ),
            const SizedBox(height: 10),
            OutlinedButton(onPressed: () => Navigator.pop(context, true), child: const Text('Done')),
          ],
        ]),
      ),
    );
  }
}

class _Finding extends StatelessWidget {
  const _Finding({required this.level, required this.title, this.text = '', this.fix = ''});
  final String level, title, text, fix;
  @override
  Widget build(BuildContext context) {
    final color = level == 'bad' ? const Color(0xFFC62828) : (level == 'warn' ? const Color(0xFFB26A00) : const Color(0xFF138453));
    final icon = level == 'bad' ? Icons.cancel : (level == 'warn' ? Icons.warning_amber_rounded : Icons.check_circle);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(14),
        color: color.withValues(alpha: .07),
        border: Border.all(color: color.withValues(alpha: .3)),
      ),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Icon(icon, color: color, size: 20),
        const SizedBox(width: 10),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Wrap(spacing: 6, crossAxisAlignment: WrapCrossAlignment.center, children: [
              Text(title, style: const TextStyle(fontWeight: FontWeight.w700)),
              if (fix == 'auto') const _Tag('Fix it does this', Color(0xFF1747A6)),
              if (fix == 'you') const _Tag('You decide', Color(0xFF4B5563)),
            ]),
            if (text.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 3), child: Text(text, style: const TextStyle(fontSize: 13))),
          ]),
        ),
      ]),
    );
  }
}

class _Tag extends StatelessWidget {
  const _Tag(this.text, this.color);
  final String text;
  final Color color;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
        decoration: BoxDecoration(color: color.withValues(alpha: .1), borderRadius: BorderRadius.circular(99)),
        child: Text(text, style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: color)),
      );
}
