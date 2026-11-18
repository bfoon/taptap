import 'dart:async';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/material.dart';

import '../app/format.dart';
import '../data/voice_store.dart';

/// More → Voice messages on this phone: every voice message this phone kept when it was played (see [VoiceStore]).
/// Play them without internet, delete one (swipe it away or the bin), or delete them all.
class SavedVoiceScreen extends StatefulWidget {
  const SavedVoiceScreen({super.key});
  @override
  State<SavedVoiceScreen> createState() => _SavedVoiceScreenState();
}

class _SavedVoiceScreenState extends State<SavedVoiceScreen> {
  final _player = AudioPlayer();
  final _subs = <StreamSubscription>[];
  int? _playingId;
  Duration _pos = Duration.zero;
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    VoiceStore.I.changes.addListener(_changed);
    VoiceStore.I.ready.then((_) {
      if (mounted) setState(() => _ready = true);
    });
    _subs
      ..add(_player.onPositionChanged.listen((d) {
        if (mounted) setState(() => _pos = d);
      }))
      ..add(_player.onPlayerComplete.listen((_) {
        if (mounted) {
          setState(() {
            _playingId = null;
            _pos = Duration.zero;
          });
        }
      }));
  }

  void _changed() {
    if (!mounted) return;
    if (_playingId != null && !VoiceStore.I.has(_playingId!)) {
      _player.stop();
      _playingId = null;
    }
    setState(() {});
  }

  @override
  void dispose() {
    VoiceStore.I.changes.removeListener(_changed);
    for (final s in _subs) {
      s.cancel();
    }
    _player.dispose();
    super.dispose();
  }

  Future<void> _toggle(SavedVoice v) async {
    if (_playingId == v.id) {
      await _player.stop();
      setState(() {
        _playingId = null;
        _pos = Duration.zero;
      });
      return;
    }
    final path = await VoiceStore.I.pathOf(v.id);
    if (path == null) {
      if (mounted) _snack('That recording is no longer on this phone.');
      return;
    }
    await _player.stop();
    setState(() {
      _playingId = v.id;
      _pos = Duration.zero;
    });
    try {
      await _player.play(DeviceFileSource(path));
    } catch (_) {
      if (mounted) {
        setState(() => _playingId = null);
        _snack('This phone could not play it.');
      }
    }
  }

  Future<bool> _confirm(String title, String body) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: Text(body),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Theme.of(ctx).colorScheme.error),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    return ok == true;
  }

  void _snack(String m) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(m), behavior: SnackBarBehavior.floating));

  static String _size(int b) => b >= 1048576 ? '${(b / 1048576).toStringAsFixed(1)} MB' : '${(b / 1024).ceil()} KB';
  static String _len(int s) => '${s ~/ 60}:${(s % 60).toString().padLeft(2, '0')}';

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final items = VoiceStore.I.all();
    return Scaffold(
      appBar: AppBar(
        title: const Text('Voice messages on this phone'),
        actions: [
          if (items.isNotEmpty)
            IconButton(
              tooltip: 'Delete all',
              icon: const Icon(Icons.delete_sweep_outlined),
              onPressed: () async {
                if (await _confirm('Delete all ${items.length} from this phone?',
                    'They are deleted from this phone only. Those TapTap already deleted cannot be played anywhere after this.')) {
                  await _player.stop();
                  await VoiceStore.I.clear();
                }
              },
            ),
        ],
      ),
      body: !_ready
          ? const Center(child: CircularProgressIndicator())
          : items.isEmpty
              ? Center(
                  child: Padding(
                    padding: const EdgeInsets.all(28),
                    child: Text(
                      'Nothing yet.\n\nWhen you play a voice message in chat, this phone keeps it — TapTap deletes its own copy 1 minute after it is played, this one stays here until you delete it.',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: cs.onSurfaceVariant),
                    ),
                  ),
                )
              : ListView.separated(
                  itemCount: items.length + 1,
                  separatorBuilder: (_, __) => const Divider(height: 1),
                  itemBuilder: (_, i) {
                    if (i == 0) {
                      return Padding(
                        padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
                        child: Text('${items.length} kept · ${_size(VoiceStore.I.bytes)} · only on this phone',
                            style: TextStyle(fontSize: 12.5, color: cs.onSurfaceVariant)),
                      );
                    }
                    final v = items[i - 1];
                    final on = _playingId == v.id;
                    final when = parseTime(v.at);
                    return Dismissible(
                      key: ValueKey('voice-${v.id}'),
                      direction: DismissDirection.endToStart,
                      background: Container(
                        color: cs.errorContainer,
                        alignment: Alignment.centerRight,
                        padding: const EdgeInsets.only(right: 20),
                        child: Icon(Icons.delete_outline, color: cs.onErrorContainer),
                      ),
                      confirmDismiss: (_) => _confirm('Delete from this phone?', 'Only this phone’s copy is deleted.'),
                      onDismissed: (_) => VoiceStore.I.remove(v.id),
                      child: ListTile(
                        leading: IconButton.filled(
                          onPressed: () => _toggle(v),
                          icon: Icon(on ? Icons.stop : Icons.play_arrow),
                        ),
                        title: Text(v.mine ? 'You' : (v.sender.isEmpty ? 'Voice message' : v.sender),
                            style: const TextStyle(fontWeight: FontWeight.w700)),
                        subtitle: Text([
                          if (v.threadTitle.isNotEmpty) v.threadTitle,
                          if (when != null) clock(v.at),
                          on ? '${_len(_pos.inSeconds)} / ${_len(v.secs)}' : _len(v.secs),
                          _size(v.size),
                        ].join(' · ')),
                        trailing: IconButton(
                          tooltip: 'Delete from this phone',
                          icon: const Icon(Icons.delete_outline),
                          onPressed: () async {
                            if (await _confirm('Delete from this phone?', 'Only this phone’s copy is deleted.')) {
                              if (on) await _player.stop();
                              await VoiceStore.I.remove(v.id);
                            }
                          },
                        ),
                      ),
                    );
                  },
                ),
    );
  }
}
