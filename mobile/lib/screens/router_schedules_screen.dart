import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../data/store.dart';
import '../data/sync_engine.dart';
import '../widgets/common.dart';

/// Router schedules — restart or switch off routers on a timetable, the same as Routers → Schedules on the website
/// (server: core/router_schedules.py, core/views_router_schedules.py). TapTap sends a heads-up before, waits for the
/// quietest moment, and says when the router is back; this screen shows what is next, lets you skip just the next
/// time, pause, change, delete — and restart / shut down right now.
Future<void> openRouterSchedules(BuildContext context, {int? routerId}) =>
    Navigator.of(context).push(MaterialPageRoute(builder: (_) => RouterSchedulesScreen(routerId: routerId)));

class RouterSchedulesScreen extends StatefulWidget {
  const RouterSchedulesScreen({super.key, this.routerId});
  final int? routerId;
  @override
  State<RouterSchedulesScreen> createState() => _RouterSchedulesScreenState();
}

class _RouterSchedulesScreenState extends State<RouterSchedulesScreen> {
  Map<String, dynamic>? _d;
  String? _err;
  final Set<String> _busy = {};
  StreamSubscription<String>? _saved;
  Timer? _tick;

  @override
  void initState() {
    super.initState();
    _d = Store.I.peek(Pages.schedules);                         // saved on the phone: shown at once
    _saved = Store.I.changes.listen((key) {
      if (!mounted || key != Pages.schedules) return;
      final d = Store.I.peekKey(key);
      if (d != null) setState(() => _d = d);
    });
    _load();
    _tick = Timer.periodic(const Duration(seconds: 30), (_) {
      if (mounted) setState(() {});                             // "in 12 min" keeps counting
    });
  }

  @override
  void dispose() {
    _saved?.cancel();
    _tick?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final d = await Store.I.fetch(Pages.schedules);
      if (!mounted) return;
      setState(() {
        _d = d;
        _err = null;
      });
    } on ApiError catch (e) {
      if (!mounted) return;
      if (e.status == 0 && _d != null) return;                  // offline with the saved one on screen
      setState(() => _err = e.message);
    }
  }

  bool get _manage => _d?['can_manage'] == true;
  List<Map<String, dynamic>> get _routers => asMaps(_d?['routers']);
  List<Map<String, dynamic>> get _schedules =>
      asMaps(_d?['schedules']).where((s) => widget.routerId == null || toI(s['router']) == widget.routerId).toList();
  List<Map<String, dynamic>> get _runs =>
      asMaps(_d?['runs']).where((s) => widget.routerId == null || toI(s['router']) == widget.routerId).toList();

  String get _title {
    if (widget.routerId == null) return 'Router schedules';
    final r = _routers.where((r) => toI(r['id']) == widget.routerId).toList();
    return r.isEmpty ? 'Schedules' : 'Schedules · ${r.first['name']}';
  }

  void _toast(String m, {bool error = false}) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(m),
        behavior: SnackBarBehavior.floating,
        backgroundColor: error ? Theme.of(context).colorScheme.error : null,
      ));

  Future<bool> _post(String busyKey, Map<String, dynamic> body) async {
    setState(() => _busy.add(busyKey));
    try {
      final d = await Api.I.post(Pages.schedules, body);
      HapticFeedback.selectionClick();
      _toast('${d['message'] ?? 'Done.'}');
      await _load();
      return true;
    } on ApiError catch (e) {
      _toast(e.message, error: true);
      return false;
    } finally {
      if (mounted) setState(() => _busy.remove(busyKey));
    }
  }

  Future<void> _edit([Map<String, dynamic>? s]) async {
    final body = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _Editor(routers: _routers, schedule: s, routerId: widget.routerId, maxWindow: toI(_d?['max_window']) == 0 ? 180 : toI(_d?['max_window'])),
    );
    if (body != null) await _post('save', body);
  }

  Future<void> _delete(Map<String, dynamic> s) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Delete this schedule?'),
        content: Text('${s['router_name']}: ${s['action_label']} · ${s['text']}'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Theme.of(c).colorScheme.error),
            onPressed: () => Navigator.pop(c, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok == true) await _post('delete${s['id']}', {'action': 'delete', 'id': s['id']});
  }

  Future<void> _now(String what) async {
    final routers = widget.routerId == null ? _routers : _routers.where((r) => toI(r['id']) == widget.routerId).toList();
    if (routers.isEmpty) return;
    var picked = toI(routers.first['id']);
    final ctl = TextEditingController();
    final off = what == 'shutdown';
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, set) {
          final name = '${routers.firstWhere((r) => toI(r['id']) == picked, orElse: () => routers.first)['name']}';
          return AlertDialog(
            icon: Icon(off ? Icons.power_settings_new : Icons.restart_alt, color: off ? Theme.of(c).colorScheme.error : null),
            title: Text(off ? 'Shut down now?' : 'Restart now?'),
            content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
              if (routers.length > 1)
                DropdownButtonFormField<int>(
                  value: picked,
                  items: [for (final r in routers) DropdownMenuItem(value: toI(r['id']), child: Text('${r['name']}'))],
                  onChanged: (v) => set(() => picked = v ?? picked),
                ),
              const SizedBox(height: 8),
              Text(off
                  ? '$name switches OFF and stays off until someone unplugs it and plugs it back in. Everyone on it is offline until then.'
                  : 'Everyone on $name drops for a minute or two (their time is kept). TapTap tells you when it is back.'),
              const SizedBox(height: 12),
              TextField(controller: ctl, autofocus: true, decoration: InputDecoration(labelText: 'Type “$name” to confirm')),
            ]),
            actions: [
              TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
              FilledButton(
                style: off ? FilledButton.styleFrom(backgroundColor: Theme.of(c).colorScheme.error) : null,
                onPressed: () => Navigator.pop(c, true),
                child: Text(off ? 'Shut down' : 'Restart'),
              ),
            ],
          );
        },
      ),
    );
    final confirm = ctl.text;
    ctl.dispose();
    if (ok == true) await _post('now$what', {'action': 'run_now', 'router': picked, 'what': what, 'confirm': confirm});
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final schedules = _schedules;
    final upcoming = schedules.where((s) => s['active'] == true && s['next_at'] != null).toList()
      ..sort((a, b) => '${a['next_at']}'.compareTo('${b['next_at']}'));
    final runs = _runs;
    return Scaffold(
      appBar: AppBar(title: Text(_title)),
      floatingActionButton: _manage && _routers.isNotEmpty
          ? FloatingActionButton.extended(onPressed: () => _edit(), icon: const Icon(Icons.add_alarm), label: const Text('New schedule'))
          : null,
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 4, 16, 110), children: [
          if (_err != null) ErrorCard(message: _err!, onRetry: _load),
          if (_d == null && _err == null) ...[const SkeletonCard(height: 140), const SizedBox(height: 12), const SkeletonCard(height: 120)],
          if (_d != null) ...[
            _NextUp(upcoming: upcoming.take(4).toList(), busy: _busy, manage: _manage,
                onSkip: (s) => _post('skip${s['id']}', {'action': s['skipped'] == true ? 'unskip' : 'skip', 'id': s['id']})),
            const SizedBox(height: 14),
            SectionCard(
              title: 'Schedules',
              trailing: Text('${schedules.length}', style: TextStyle(color: cs.onSurfaceVariant, fontWeight: FontWeight.w700)),
              child: schedules.isEmpty
                  ? EmptyNote(_manage ? 'Nothing planned. Tap “New schedule” — every night at 03:00 is a good start.' : 'Nothing planned.')
                  : Column(children: [
                      for (final s in schedules)
                        _ScheduleTile(
                          s: s,
                          manage: _manage,
                          busy: _busy,
                          showRouter: widget.routerId == null,
                          onToggle: (on) => _post('toggle${s['id']}', {'action': on ? 'resume' : 'pause', 'id': s['id']}),
                          onSkip: () => _post('skip${s['id']}', {'action': s['skipped'] == true ? 'unskip' : 'skip', 'id': s['id']}),
                          onEdit: () => _edit(s),
                          onDelete: () => _delete(s),
                        ),
                    ]),
            ),
            if (_manage && _routers.isNotEmpty) ...[
              const SizedBox(height: 14),
              SectionCard(
                title: 'Right now',
                child: Wrap(spacing: 8, runSpacing: 8, children: [
                  FilledButton.tonalIcon(
                    onPressed: _busy.contains('nowrestart') ? null : () => _now('restart'),
                    icon: const Icon(Icons.restart_alt, size: 18),
                    label: const Text('Restart now'),
                  ),
                  OutlinedButton.icon(
                    style: OutlinedButton.styleFrom(foregroundColor: cs.error),
                    onPressed: _busy.contains('nowshutdown') ? null : () => _now('shutdown'),
                    icon: const Icon(Icons.power_settings_new, size: 18),
                    label: const Text('Shut down now'),
                  ),
                ]),
              ),
            ],
            const SizedBox(height: 14),
            SectionCard(
              title: 'What happened',
              child: runs.isEmpty
                  ? const EmptyNote('Nothing has run yet.')
                  : Column(children: [for (final r in runs.take(20)) _RunTile(r: r, showRouter: widget.routerId == null)]),
            ),
          ],
        ]),
      ),
    );
  }
}

String _inText(dynamic iso) {
  final t = parseTime(iso);
  if (t == null) return '';
  final m = t.difference(DateTime.now()).inMinutes;
  if (m <= 0) return 'due now';
  if (m < 60) return 'in $m min';
  if (m < 1440) return 'in ${m ~/ 60} h ${m % 60} min';
  final d = (m / 1440).round();
  return 'in $d day${d == 1 ? '' : 's'}';
}

class _NextUp extends StatelessWidget {
  const _NextUp({required this.upcoming, required this.busy, required this.manage, required this.onSkip});
  final List<Map<String, dynamic>> upcoming;
  final Set<String> busy;
  final bool manage;
  final void Function(Map<String, dynamic> s) onSkip;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(24),
        gradient: const LinearGradient(begin: Alignment.topLeft, end: Alignment.bottomRight, colors: [Color(0xFF16304C), Color(0xFF0E1A2A)]),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        const Row(children: [
          Icon(Icons.event_available, color: Colors.white),
          SizedBox(width: 8),
          Text('Next up', style: TextStyle(color: Colors.white, fontWeight: FontWeight.w900, fontSize: 18)),
        ]),
        const SizedBox(height: 4),
        const Text('Heads-up before · waits for the quietest moment · tells you when it is back',
            style: TextStyle(color: Colors.white70, fontSize: 12)),
        const SizedBox(height: 12),
        if (upcoming.isEmpty)
          const Text('Nothing planned yet.', style: TextStyle(color: Colors.white70)),
        for (final s in upcoming)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Opacity(
              opacity: s['skipped'] == true ? .55 : 1,
              child: Row(children: [
                SizedBox(
                  width: 58,
                  child: Column(children: [
                    Text(clock(s['next_at']),
                        style: TextStyle(color: Colors.white, fontWeight: FontWeight.w900, fontSize: 17,
                            decoration: s['skipped'] == true ? TextDecoration.lineThrough : null)),
                    Text(whenText(s['next_at']).split(' ').first, style: const TextStyle(color: Colors.white60, fontSize: 10.5)),
                  ]),
                ),
                Icon(s['action'] == 'shutdown' ? Icons.power_settings_new : Icons.restart_alt,
                    color: s['action'] == 'shutdown' ? const Color(0xFFFF9D9D) : const Color(0xFF7CC4FF)),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('${s['router_name']} · ${s['action_label']}',
                        style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w700), overflow: TextOverflow.ellipsis),
                    Text(s['skipped'] == true ? 'Skipped this time' : _inText(s['next_at']),
                        style: const TextStyle(color: Colors.white60, fontSize: 12)),
                  ]),
                ),
                if (manage)
                  TextButton(
                    style: TextButton.styleFrom(foregroundColor: Colors.white),
                    onPressed: busy.contains('skip${s['id']}') ? null : () => onSkip(s),
                    child: Text(s['skipped'] == true ? 'Undo' : 'Skip'),
                  ),
              ]),
            ),
          ),
      ]),
    );
  }
}

class _ScheduleTile extends StatelessWidget {
  const _ScheduleTile({required this.s, required this.manage, required this.busy, required this.showRouter, required this.onToggle,
      required this.onSkip, required this.onEdit, required this.onDelete});
  final Map<String, dynamic> s;
  final bool manage, showRouter;
  final Set<String> busy;
  final void Function(bool on) onToggle;
  final VoidCallback onSkip, onEdit, onDelete;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final off = s['action'] == 'shutdown';
    final active = s['active'] == true;
    final skipped = s['skipped'] == true;
    final window = toI(s['window_minutes']);
    final warn = toI(s['warn_minutes']);
    final details = [
      window > 0 ? 'quiet moment within $window min' : 'exactly on time',
      warn > 0 ? 'heads-up $warn min before' : 'no heads-up',
      if (s['email'] == true) 'email',
      if ('${s['note'] ?? ''}'.isNotEmpty) '${s['note']}',
    ].join(' · ');
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        CircleAvatar(
          backgroundColor: (off ? cs.error : cs.primary).withValues(alpha: .12),
          child: Icon(off ? Icons.power_settings_new : Icons.restart_alt, color: off ? cs.error : cs.primary),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('${showRouter ? '${s['router_name']} · ' : ''}${s['action_label']}', style: const TextStyle(fontWeight: FontWeight.w800)),
            Text('${s['text']}', style: const TextStyle(fontWeight: FontWeight.w600)),
            Text(details, style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
            const SizedBox(height: 4),
            Text(
              !active
                  ? (s['repeat'] == 'once' && s['last_run_at'] != null ? 'Done' : 'Paused')
                  : skipped
                      ? 'Skipping ${whenText(s['next_at'])}${s['after_skip'] != null ? ' — then ${whenText(s['after_skip'])}' : ''}'
                      : 'Next ${whenText(s['next_at'])} (${_inText(s['next_at'])})',
              style: TextStyle(fontSize: 12.5, fontWeight: FontWeight.w700, color: active && !skipped ? cs.primary : cs.onSurfaceVariant),
            ),
            if (manage)
              Wrap(spacing: 4, children: [
                if (active)
                  TextButton(onPressed: busy.contains('skip${s['id']}') ? null : onSkip, child: Text(skipped ? 'Undo skip' : 'Skip next')),
                TextButton(onPressed: onEdit, child: const Text('Change')),
                TextButton(onPressed: onDelete, style: TextButton.styleFrom(foregroundColor: cs.error), child: const Text('Delete')),
              ]),
          ]),
        ),
        if (manage)
          Switch(value: active, onChanged: busy.contains('toggle${s['id']}') ? null : onToggle),
      ]),
    );
  }
}

class _RunTile extends StatelessWidget {
  const _RunTile({required this.r, required this.showRouter});
  final Map<String, dynamic> r;
  final bool showRouter;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final st = '${r['status']}';
    final color = switch (st) {
      'back' => const Color(0xFF16A34A),
      'failed' || 'late' || 'missed' => cs.error,
      'skipped' => cs.onSurfaceVariant,
      _ => const Color(0xFFF59E0B),
    };
    final bits = [
      if ('${r['down']}'.isNotEmpty) 'back after ${r['down']}',
      if (r['online_at_run'] != null) '${r['online_at_run']} online when it went',
      if (r['manual'] == true) 'by hand',
    ].join(' · ');
    return ListTile(
      contentPadding: EdgeInsets.zero,
      dense: true,
      leading: Icon(r['action'] == 'shutdown' ? Icons.power_settings_new : Icons.restart_alt, color: color),
      title: Text('${showRouter ? '${r['router_name']} · ' : ''}${r['status_label']}', style: const TextStyle(fontWeight: FontWeight.w700)),
      subtitle: Text([whenText(r['ran_at'] ?? r['planned_at']), if (bits.isNotEmpty) bits, if ('${r['result'] ?? ''}'.isNotEmpty) '${r['result']}']
          .join('\n')),
    );
  }
}

/// New / change: router, restart or shut down, when, the quiet window, the heads-up.
class _Editor extends StatefulWidget {
  const _Editor({required this.routers, this.schedule, this.routerId, required this.maxWindow});
  final List<Map<String, dynamic>> routers;
  final Map<String, dynamic>? schedule;
  final int? routerId;
  final int maxWindow;
  @override
  State<_Editor> createState() => _EditorState();
}

class _EditorState extends State<_Editor> {
  static const _days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  static const _warns = [0, 5, 10, 15, 30, 60, 120];
  late int _router;
  String _what = 'restart';
  String _repeat = 'daily';
  TimeOfDay _time = const TimeOfDay(hour: 3, minute: 0);
  DateTime _day = DateTime.now();
  final Set<int> _weekdays = {};
  int _monthDay = 1;
  double _window = 30;
  int _warn = 10;
  bool _email = true;
  bool _understood = false;
  final _note = TextEditingController();

  @override
  void initState() {
    super.initState();
    final s = widget.schedule;
    final ids = widget.routers.map((r) => toI(r['id'])).toList();
    _router = toI(s?['router']) > 0 ? toI(s!['router']) : (widget.routerId ?? (ids.isEmpty ? 0 : ids.first));
    if (!ids.contains(_router) && ids.isNotEmpty) _router = ids.first;
    if (s != null) {
      _what = '${s['action']}' == 'shutdown' ? 'shutdown' : 'restart';
      _repeat = const ['once', 'daily', 'weekly', 'monthly'].contains(s['repeat']) ? '${s['repeat']}' : 'daily';
      final hm = '${s['at_time']}'.split(':');
      if (hm.length >= 2) _time = TimeOfDay(hour: toI(hm[0]).clamp(0, 23), minute: toI(hm[1]).clamp(0, 59));
      _day = DateTime.tryParse('${s['run_on']}') ?? DateTime.now();
      _weekdays.addAll((s['weekdays'] as List? ?? const []).map(toI).where((d) => d >= 0 && d <= 6));
      _monthDay = toI(s['month_day']).clamp(1, 31);
      _window = toI(s['window_minutes']).clamp(0, widget.maxWindow).toDouble();
      _warn = _warns.contains(toI(s['warn_minutes'])) ? toI(s['warn_minutes']) : 10;
      _email = s['email'] == true;
      _understood = _what == 'shutdown';
      _note.text = '${s['note'] ?? ''}';
    }
  }

  @override
  void dispose() {
    _note.dispose();
    super.dispose();
  }

  String get _hm => '${_time.hour.toString().padLeft(2, '0')}:${_time.minute.toString().padLeft(2, '0')}';

  String? get _problem {
    if (widget.routers.isEmpty) return 'Add a router first.';
    if (_repeat == 'weekly' && _weekdays.isEmpty) return 'Choose at least one day.';
    if (_what == 'shutdown' && !_understood) return 'Tick that you understand it stays off.';
    return null;
  }

  void _preset(String repeat, int h, [List<int> days = const []]) {
    HapticFeedback.selectionClick();
    setState(() {
      _repeat = repeat;
      _time = TimeOfDay(hour: h, minute: 0);
      _weekdays
        ..clear()
        ..addAll(days);
      if (repeat == 'monthly') _monthDay = 1;
    });
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final w = _window.round();
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 0, 20, MediaQuery.of(context).viewInsets.bottom + 20),
      child: SingleChildScrollView(
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
          Text(widget.schedule == null ? 'New schedule' : 'Change schedule', style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w900)),
          const SizedBox(height: 10),
          if (widget.routerId == null || widget.schedule != null)
            DropdownButtonFormField<int>(
              value: widget.routers.any((r) => toI(r['id']) == _router) ? _router : null,
              decoration: const InputDecoration(labelText: 'Router'),
              items: [
                for (final r in widget.routers)
                  DropdownMenuItem(
                    value: toI(r['id']),
                    child: Text('${r['name']}${r['online'] == true ? (r['people_online'] != null ? ' · ${r['people_online']} online' : '') : ' · offline'}'),
                  ),
              ],
              onChanged: (v) => setState(() => _router = v ?? _router),
            ),
          const SizedBox(height: 12),
          SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: 'restart', label: Text('Restart'), icon: Icon(Icons.restart_alt)),
              ButtonSegment(value: 'shutdown', label: Text('Shut down'), icon: Icon(Icons.power_settings_new)),
            ],
            selected: {_what},
            onSelectionChanged: (v) => setState(() => _what = v.first),
          ),
          if (_what == 'shutdown') ...[
            const SizedBox(height: 10),
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(color: cs.errorContainer, borderRadius: BorderRadius.circular(14)),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text('A shutdown switches the router OFF. It stays off until someone unplugs it and plugs it back in — '
                    'TapTap cannot switch it on again. Everyone on it is offline until then, members too.',
                    style: TextStyle(color: cs.onErrorContainer, fontSize: 13)),
                CheckboxListTile(
                  contentPadding: EdgeInsets.zero,
                  value: _understood,
                  onChanged: (v) => setState(() => _understood = v == true),
                  title: Text('I understand — someone will power it on', style: TextStyle(color: cs.onErrorContainer, fontWeight: FontWeight.w700)),
                ),
              ]),
            ),
          ],
          const SizedBox(height: 12),
          Wrap(spacing: 6, runSpacing: 6, children: [
            ActionChip(label: const Text('Every night 03:00'), onPressed: () => _preset('daily', 3)),
            ActionChip(label: const Text('Sundays 04:00'), onPressed: () => _preset('weekly', 4, [6])),
            ActionChip(label: const Text('Weekdays 03:00'), onPressed: () => _preset('weekly', 3, [0, 1, 2, 3, 4])),
            ActionChip(label: const Text('1st of the month'), onPressed: () => _preset('monthly', 4)),
          ]),
          const SizedBox(height: 12),
          Wrap(spacing: 6, children: [
            for (final e in const {'once': 'Once', 'daily': 'Every day', 'weekly': 'Chosen days', 'monthly': 'Every month'}.entries)
              ChoiceChip(label: Text(e.value), selected: _repeat == e.key, onSelected: (_) => setState(() => _repeat = e.key)),
          ]),
          const SizedBox(height: 8),
          Row(children: [
            Expanded(
              child: OutlinedButton.icon(
                icon: const Icon(Icons.schedule),
                label: Text(_hm, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w800)),
                onPressed: () async {
                  final t = await showTimePicker(context: context, initialTime: _time);
                  if (t != null) setState(() => _time = t);
                },
              ),
            ),
            if (_repeat == 'once') ...[
              const SizedBox(width: 8),
              Expanded(
                child: OutlinedButton.icon(
                  icon: const Icon(Icons.event),
                  label: Text(dayLabel(_day)),
                  onPressed: () async {
                    final now = DateTime.now();
                    final d = await showDatePicker(
                        context: context, initialDate: _day.isBefore(now) ? now : _day, firstDate: DateUtils.dateOnly(now),
                        lastDate: now.add(const Duration(days: 365)));
                    if (d != null) setState(() => _day = d);
                  },
                ),
              ),
            ],
            if (_repeat == 'monthly') ...[
              const SizedBox(width: 8),
              Expanded(
                child: DropdownButtonFormField<int>(
                  value: _monthDay,
                  decoration: const InputDecoration(labelText: 'Day of the month', isDense: true),
                  items: [for (var i = 1; i <= 31; i++) DropdownMenuItem(value: i, child: Text('$i'))],
                  onChanged: (v) => setState(() => _monthDay = v ?? _monthDay),
                ),
              ),
            ],
          ]),
          if (_repeat == 'weekly') ...[
            const SizedBox(height: 8),
            Wrap(spacing: 4, children: [
              for (var i = 0; i < 7; i++)
                FilterChip(
                  label: Text(_days[i]),
                  selected: _weekdays.contains(i),
                  onSelected: (on) => setState(() => on ? _weekdays.add(i) : _weekdays.remove(i)),
                ),
            ]),
          ],
          const SizedBox(height: 14),
          Text(w == 0 ? 'Exactly on time' : 'Wait up to $w min for a quiet moment', style: const TextStyle(fontWeight: FontWeight.w800)),
          Slider(
            value: _window.clamp(0, widget.maxWindow.toDouble()),
            min: 0,
            max: widget.maxWindow.toDouble(),
            divisions: widget.maxWindow ~/ 5,
            label: w == 0 ? 'on time' : '$w min',
            onChanged: (v) => setState(() => _window = v),
          ),
          Text(
            w == 0
                ? 'It goes exactly at $_hm, whoever is online.'
                : 'From $_hm TapTap watches who is online: it goes the moment nobody is; after ${(w * 2 / 3).round()} min it takes the quietest moment so far; at $w min it goes anyway.',
            style: TextStyle(fontSize: 12.5, color: cs.onSurfaceVariant),
          ),
          const SizedBox(height: 12),
          Row(children: [
            Expanded(
              child: DropdownButtonFormField<int>(
                value: _warn,
                decoration: const InputDecoration(labelText: 'Heads-up', isDense: true),
                items: [for (final m in _warns) DropdownMenuItem(value: m, child: Text(m == 0 ? 'None' : '$m min before'))],
                onChanged: (v) => setState(() => _warn = v ?? _warn),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: SwitchListTile(
                contentPadding: EdgeInsets.zero,
                value: _email,
                onChanged: (v) => setState(() => _email = v),
                title: const Text('Email too'),
              ),
            ),
          ]),
          TextField(controller: _note, maxLength: 160, decoration: const InputDecoration(hintText: 'Note (optional)')),
          Text('The owner and everyone who manages routers get the heads-up and the result.',
              style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
          const SizedBox(height: 12),
          if (_problem != null) Padding(padding: const EdgeInsets.only(bottom: 8), child: Text(_problem!, style: TextStyle(color: cs.error))),
          FilledButton.icon(
            onPressed: _problem != null
                ? null
                : () => Navigator.pop(context, <String, dynamic>{
                      'action': 'save',
                      if (widget.schedule != null) 'id': widget.schedule!['id'],
                      'router': _router,
                      'what': _what,
                      'repeat': _repeat,
                      'at_time': _hm,
                      if (_repeat == 'once') 'run_on': ymd(_day),
                      if (_repeat == 'weekly') 'weekdays': (_weekdays.toList()..sort()),
                      if (_repeat == 'monthly') 'month_day': _monthDay,
                      'window_minutes': w,
                      'warn_minutes': _warn,
                      'email': _email,
                      'note': _note.text.trim(),
                      if (_what == 'shutdown') 'understood': _understood,
                    }),
            icon: const Icon(Icons.check),
            label: Text(widget.schedule == null ? 'Save schedule' : 'Save changes'),
          ),
        ]),
      ),
    );
  }
}
