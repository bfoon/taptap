import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app/api.dart';

/// Fit's wardrobe in the app — the same looks as on the website (core/fit_skins.py): pick a skin or mask for Fit,
/// "Auto" (follows the calendar: Santa, Halloween, Ramadan & Eid, Tobaski, Independence Day…), or the team's look.
/// Resolves to the new look state when something was picked.
Future<Map<String, dynamic>?> showFitWardrobe(BuildContext context, Map<String, dynamic> look) =>
    showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _Wardrobe(look: look),
    );

class _Wardrobe extends StatefulWidget {
  const _Wardrobe({required this.look});
  final Map<String, dynamic> look;
  @override
  State<_Wardrobe> createState() => _WardrobeState();
}

class _WardrobeState extends State<_Wardrobe> {
  late Map<String, dynamic> _look = widget.look;
  bool _team = false, _busy = false;
  String _error = '';

  List<Map<String, dynamic>> get _list =>
      [for (final x in (_look['list'] as List? ?? const [])) if (x is Map) x.cast<String, dynamic>()];

  Map<String, dynamic> _meta(String id) =>
      _list.firstWhere((x) => x['id'] == id, orElse: () => {'id': 'classic', 'name': 'Classic Fit', 'emoji': '🤖'});

  Future<void> _pick(String id) async {
    setState(() {
      _busy = true;
      _error = '';
    });
    HapticFeedback.selectionClick();
    try {
      final d = await Api.I.post('/fit/look/', {'skin': id, 'team': _team});
      if (!mounted) return;
      setState(() => _look = (d['look'] as Map).cast<String, dynamic>());
      Navigator.pop(context, _look);
    } on ApiError catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final wearing = (_look['wearing'] as Map?)?.cast<String, dynamic>() ?? {};
    final current = _team ? ('${_look['team'] ?? ''}'.isEmpty ? 'auto' : '${_look['team']}') : '${_look['mine'] ?? ''}';
    final season = _meta('${_look['season'] ?? 'classic'}');
    final groups = [for (final g in (_look['groups'] as List? ?? const [])) if (g is Map) g.cast<String, dynamic>()];
    final canTeam = _look['can_set_team'] == true;

    Widget tile(String id, String emoji, String name, {String sub = ''}) {
      final on = current == id;
      return InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: _busy ? null : () => _pick(id),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 180),
          padding: const EdgeInsets.fromLTRB(4, 8, 4, 8),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(16),
            color: on ? cs.primaryContainer : cs.surfaceContainerHighest.withValues(alpha: .5),
            border: Border.all(color: on ? cs.primary : Colors.transparent, width: 2),
          ),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text(emoji, style: const TextStyle(fontSize: 30)),
            const SizedBox(height: 4),
            Text(name, textAlign: TextAlign.center, maxLines: 2, style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700)),
            if (sub.isNotEmpty)
              Text(sub, textAlign: TextAlign.center, maxLines: 2, style: TextStyle(fontSize: 10.5, color: cs.onSurfaceVariant)),
          ]),
        ),
      );
    }

    Widget grid(List<Widget> kids) => GridView.count(
          crossAxisCount: 4,
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          mainAxisSpacing: 8,
          crossAxisSpacing: 8,
          childAspectRatio: .78,
          children: kids,
        );

    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(maxHeight: MediaQuery.of(context).size.height * .88),
        child: ListView(shrinkWrap: true, padding: const EdgeInsets.fromLTRB(18, 0, 18, 24), children: [
          Row(children: [
            Text('${wearing['emoji'] ?? '🤖'}', style: const TextStyle(fontSize: 44)),
            const SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text("Fit's wardrobe", style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant, fontWeight: FontWeight.w700)),
                Text('${wearing['name'] ?? 'Classic Fit'}', style: const TextStyle(fontSize: 19, fontWeight: FontWeight.w900)),
                Text('“${wearing['greeting'] ?? ''}”', style: TextStyle(fontStyle: FontStyle.italic, color: cs.onSurfaceVariant, fontSize: 13)),
              ]),
            ),
          ]),
          const SizedBox(height: 10),
          Row(children: [
            if (canTeam)
              SegmentedButton<bool>(
                segments: const [ButtonSegment(value: false, label: Text('Just me')), ButtonSegment(value: true, label: Text('Whole team'))],
                selected: {_team},
                onSelectionChanged: (s) => setState(() => _team = s.first),
              ),
            const Spacer(),
            TextButton.icon(
              onPressed: _busy
                  ? null
                  : () {
                      final ids = _list.map((x) => '${x['id']}').where((x) => x != 'classic' && x != '${_look['skin']}').toList()..shuffle();
                      if (ids.isNotEmpty) _pick(ids.first);
                    },
              icon: const Text('🎲'),
              label: const Text('Surprise me'),
            ),
          ]),
          if (_busy) const LinearProgressIndicator(),
          if (_error.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 6), child: Text(_error, style: TextStyle(color: cs.error))),
          const SizedBox(height: 10),
          grid([
            if (!_team) tile('', '👥', 'Team look', sub: '${_look['team'] ?? ''}'.isEmpty ? 'Auto' : '${_meta('${_look['team']}')['name']}'),
            tile('auto', '📅', 'Auto', sub: 'now ${season['name']}'),
          ]),
          for (final g in groups) ...[
            Padding(
              padding: const EdgeInsets.fromLTRB(2, 16, 2, 8),
              child: Text('${g['label']}'.toUpperCase(),
                  style: TextStyle(fontSize: 11.5, fontWeight: FontWeight.w800, letterSpacing: .6, color: cs.onSurfaceVariant)),
            ),
            grid([
              for (final x in _list.where((x) => x['group'] == g['key'])) tile('${x['id']}', '${x['emoji']}', '${x['name']}'),
            ]),
          ],
        ]),
      ),
    );
  }
}
