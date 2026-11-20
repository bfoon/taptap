import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app/api.dart';
import '../app/format.dart';

/// More → Fit missions: jobs Fit does later by itself (core/fit_missions.py) — a time, a timetable, the quietest
/// moment of a window, or when something happens. Set them by asking Fit; here you see them, and pause, resume,
/// cancel or run one now (yours — or any, for the Owner and Admins).
class MissionsScreen extends StatefulWidget {
  const MissionsScreen({super.key});
  @override
  State<MissionsScreen> createState() => _MissionsScreenState();
}

class _MissionsScreenState extends State<MissionsScreen> {
  List<Map<String, dynamic>> _rows = [];
  bool _loading = true, _allowed = true;
  String? _error;
  final Set<int> _busy = {};

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final d = await Api.I.get('/fit/missions/data/');
      if (!mounted) return;
      setState(() {
        _rows = asMaps(d['missions']);
        _allowed = d['allowed'] != false;
        _error = null;
        _loading = false;
      });
    } on ApiError catch (e) {
      if (mounted) {
        setState(() {
          _error = e.message;
          _loading = false;
        });
      }
    }
  }

  Future<void> _op(Map<String, dynamic> m, String op) async {
    if (op == 'cancel' || op == 'run') {
      final ok = await showDialog<bool>(
        context: context,
        builder: (c) => AlertDialog(
          title: Text(op == 'cancel' ? 'Cancel this mission?' : 'Do it now?'),
          content: Text(op == 'cancel' ? '“${m['name']}” will never run. Its log is kept.' : 'Fit does “${m['task_text']}” right now.'),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('No')),
            FilledButton(onPressed: () => Navigator.pop(c, true), child: Text(op == 'cancel' ? 'Cancel it' : 'Do it now')),
          ],
        ),
      );
      if (ok != true) return;
    }
    final id = toI(m['id']);
    setState(() => _busy.add(id));
    try {
      final d = await Api.I.post('/fit/missions/$id/', {'op': op});
      HapticFeedback.mediumImpact();
      _toast('${d['message'] ?? 'Done'}');
      final fresh = (d['mission'] as Map?)?.cast<String, dynamic>();
      if (fresh != null && mounted) {
        setState(() => _rows = [for (final r in _rows) toI(r['id']) == id ? fresh : r]);
      }
    } on ApiError catch (e) {
      _toast(e.message);
      final fresh = (e.data?['mission'] as Map?)?.cast<String, dynamic>();
      if (fresh != null && mounted) setState(() => _rows = [for (final r in _rows) toI(r['id']) == id ? fresh : r]);
    } finally {
      if (mounted) setState(() => _busy.remove(id));
    }
  }

  void _toast(String m) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(m), behavior: SnackBarBehavior.floating));
  }

  static String _icon(String task) => switch (task) {
        'restart_router' => '🔄',
        'shutdown_router' => '⏻',
        'report' => '📊',
        'ask_fit' => '🤖',
        'action' => '⚙️',
        _ => '🔔',
      };

  Color _statusColor(ColorScheme cs, String s) => switch (s) {
        'waiting' => cs.primary,
        'done' => const Color(0xFF18A66A),
        'failed' || 'missed' => cs.error,
        _ => cs.outline,
      };

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final live = _rows.where((r) => r['status'] == 'waiting' || r['status'] == 'paused').toList();
    final past = _rows.where((r) => r['status'] != 'waiting' && r['status'] != 'paused').toList();
    return Scaffold(
      appBar: AppBar(title: const Text('Fit missions')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(padding: const EdgeInsets.fromLTRB(12, 8, 12, 32), children: [
          if (_loading) const Padding(padding: EdgeInsets.all(40), child: Center(child: CircularProgressIndicator())),
          if (_error != null) Padding(padding: const EdgeInsets.all(16), child: Text(_error!, style: TextStyle(color: cs.error))),
          if (!_loading && !_allowed)
            Card(
              color: const Color(0xFFFFF3C4),
              child: const Padding(
                padding: EdgeInsets.all(14),
                child: Text('Missions are not switched on. They only run once the owner allows “Missions” in Fit settings on TapTap.',
                    style: TextStyle(color: Color(0xFF5B4500))),
              ),
            ),
          if (!_loading && _rows.isEmpty && _error == null)
            Padding(
              padding: const EdgeInsets.all(24),
              child: Text(
                'No missions yet.\n\nAsk Fit, for example:\n'
                '“Tonight restart Mast when fewer than 3 people are online, and email Awa when it is back.”\n'
                '“Every Monday at 8 tell me which agents sold less than last week.”\n'
                '“When Tower is offline for 10 minutes, tell the team in chat.”',
                textAlign: TextAlign.center,
                style: TextStyle(color: cs.onSurfaceVariant, height: 1.4),
              ),
            ),
          if (live.isNotEmpty) _head('Waiting'),
          for (final m in live) _card(m, cs),
          if (past.isNotEmpty) _head('Finished'),
          for (final m in past) _card(m, cs),
        ]),
      ),
    );
  }

  Widget _head(String t) => Padding(
        padding: const EdgeInsets.fromLTRB(6, 14, 6, 6),
        child: Text(t, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15)),
      );

  Widget _card(Map<String, dynamic> m, ColorScheme cs) {
    final status = '${m['status']}';
    final id = toI(m['id']);
    final busy = _busy.contains(id);
    final canChange = m['can_change'] == true && (status == 'waiting' || status == 'paused');
    final next = m['next_at'];
    final log = asMaps(m['log']);
    return Card(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 12, 10, 8),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Text(_icon('${m['task']}'), style: const TextStyle(fontSize: 22)),
            const SizedBox(width: 10),
            Expanded(child: Text('${m['name']}', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15))),
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              decoration: BoxDecoration(color: _statusColor(cs, status).withValues(alpha: .12), borderRadius: BorderRadius.circular(99)),
              child: Text('${m['status_label']}', style: TextStyle(fontSize: 11.5, fontWeight: FontWeight.w700, color: _statusColor(cs, status))),
            ),
          ]),
          const SizedBox(height: 6),
          _line('When', '${m['when_text']}'),
          _line('Then', '${m['task_text']}'),
          _line('Tells', '${m['tell_text']}'),
          const SizedBox(height: 4),
          Text(
            [
              if (status == 'waiting' && next != null && m['trigger'] != 'when') 'next ${whenText(next)}',
              if ('${m['now'] ?? ''}'.isNotEmpty) 'now: ${m['now']}',
              '${m['runs']} run${toI(m['runs']) == 1 ? '' : 's'}',
              if (m['expires_at'] != null) 'until ${whenText(m['expires_at'])}',
              if ('${m['by'] ?? ''}'.isNotEmpty) 'by ${m['by']}',
            ].join(' · '),
            style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant),
          ),
          if (m['following'] == true)
            Padding(padding: const EdgeInsets.only(top: 4), child: Text('👀 Watching the router come back…', style: TextStyle(fontSize: 12.5, color: cs.primary))),
          if ('${m['last_result'] ?? ''}'.isNotEmpty)
            Padding(padding: const EdgeInsets.only(top: 6), child: Text('→ ${m['last_result']}', style: const TextStyle(fontSize: 13))),
          if (log.isNotEmpty)
            Theme(
              data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
              child: ExpansionTile(
                tilePadding: EdgeInsets.zero,
                title: Text('Log', style: TextStyle(fontSize: 13, color: cs.onSurfaceVariant)),
                children: [
                  for (final e in log)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        SizedBox(width: 92, child: Text(whenText(e['at']), style: TextStyle(fontSize: 11.5, color: cs.onSurfaceVariant))),
                        Expanded(child: Text('${e['text']}', style: TextStyle(fontSize: 12.5, color: e['ok'] == false ? cs.error : null))),
                      ]),
                    ),
                ],
              ),
            ),
          if (canChange)
            Wrap(spacing: 4, children: [
              TextButton.icon(onPressed: busy ? null : () => _op(m, 'run'), icon: const Icon(Icons.bolt, size: 18), label: const Text('Now')),
              if (status == 'waiting')
                TextButton.icon(onPressed: busy ? null : () => _op(m, 'pause'), icon: const Icon(Icons.pause, size: 18), label: const Text('Pause'))
              else
                TextButton.icon(onPressed: busy ? null : () => _op(m, 'resume'), icon: const Icon(Icons.play_arrow, size: 18), label: const Text('Resume')),
              TextButton.icon(
                style: TextButton.styleFrom(foregroundColor: cs.error),
                onPressed: busy ? null : () => _op(m, 'cancel'),
                icon: const Icon(Icons.close, size: 18),
                label: const Text('Cancel'),
              ),
            ]),
        ]),
      ),
    );
  }

  Widget _line(String k, String v) => Padding(
        padding: const EdgeInsets.only(top: 2),
        child: Text.rich(TextSpan(children: [
          TextSpan(text: '$k  ', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 13)),
          TextSpan(text: v, style: const TextStyle(fontSize: 13)),
        ])),
      );
}
