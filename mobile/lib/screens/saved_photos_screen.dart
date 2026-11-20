import 'dart:io';

import 'package:flutter/material.dart';

import '../app/format.dart';
import '../data/photo_store.dart';
import 'chat_photo.dart';

/// More → Photos on this phone: the chat photos this phone downloaded, grouped by day. Each one is deleted by itself
/// at its normal time (when TapTap deletes it too); you can delete one — or all — earlier.
class SavedPhotosScreen extends StatefulWidget {
  const SavedPhotosScreen({super.key});
  @override
  State<SavedPhotosScreen> createState() => _SavedPhotosScreenState();
}

class _SavedPhotosScreenState extends State<SavedPhotosScreen> {
  bool _ready = false;
  final Map<int, File?> _files = {};

  @override
  void initState() {
    super.initState();
    PhotoStore.I.changes.addListener(_changed);
    PhotoStore.I.ready.then((_) async {
      await PhotoStore.I.sweep();
      if (mounted) setState(() => _ready = true);
    });
  }

  @override
  void dispose() {
    PhotoStore.I.changes.removeListener(_changed);
    super.dispose();
  }

  void _changed() {
    if (mounted) setState(() => _files.removeWhere((id, _) => !PhotoStore.I.has(id)));
  }

  Future<File?> _file(int id) async => _files[id] ??= await PhotoStore.I.fileOf(id);

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

  static String _size(int b) => b >= 1048576 ? '${(b / 1048576).toStringAsFixed(1)} MB' : '${(b / 1024).ceil()} KB';

  Future<void> _open(SavedPhoto v) async {
    final f = await _file(v.id);
    if (f == null || !mounted) return;
    await openPhoto(context, file: f, title: [v.sender, if (v.threadTitle.isNotEmpty) v.threadTitle].join(' · '), at: v.at, expires: v.expiresAt);
  }

  Future<void> _sheet(SavedPhoto v) async {
    final when = parseTime(v.at);
    await showModalBottomSheet(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(
            leading: const Icon(Icons.info_outline),
            title: Text('${v.sender}${v.threadTitle.isNotEmpty ? ' · ${v.threadTitle}' : ''}'),
            subtitle: Text([
              if (when != null) 'Sent ${whenText(v.at)}',
              if (v.expiresAt != null) 'deleted ${whenText(v.expires)}',
              _size(v.size),
            ].join(' · ')),
          ),
          ListTile(
            leading: Icon(Icons.delete_outline, color: Theme.of(ctx).colorScheme.error),
            title: const Text('Delete from this phone now'),
            onTap: () async {
              Navigator.pop(ctx);
              if (await _confirm('Delete from this phone?', 'Only this phone’s copy is deleted. TapTap keeps its copy until its normal time.')) {
                await PhotoStore.I.remove(v.id);
              }
            },
          ),
        ]),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final items = PhotoStore.I.all();
    final days = <String, List<SavedPhoto>>{};
    for (final v in items) {
      final t = parseTime(v.at);
      days.putIfAbsent(t == null ? 'Earlier' : dayLabel(t), () => []).add(v);
    }
    return Scaffold(
      appBar: AppBar(
        title: const Text('Photos on this phone'),
        actions: [
          if (items.isNotEmpty)
            IconButton(
              tooltip: 'Delete all',
              icon: const Icon(Icons.delete_sweep_outlined),
              onPressed: () async {
                if (await _confirm('Delete all ${items.length} from this phone?', 'Only this phone’s copies are deleted.')) {
                  await PhotoStore.I.clear();
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
                      'No photos yet.\n\nPhotos sent in chat are downloaded to this phone, so they open at once and without internet. '
                      'Each one is deleted from this phone at its normal time — when TapTap deletes it too.',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: cs.onSurfaceVariant),
                    ),
                  ),
                )
              : ListView(padding: const EdgeInsets.only(bottom: 24), children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
                    child: Text('${items.length} photo${items.length == 1 ? '' : 's'} · ${_size(PhotoStore.I.bytes)} · each deleted at its normal time',
                        style: TextStyle(fontSize: 12.5, color: cs.onSurfaceVariant)),
                  ),
                  for (final e in days.entries) ...[
                    Padding(
                      padding: const EdgeInsets.fromLTRB(16, 14, 16, 6),
                      child: Text(e.key, style: const TextStyle(fontWeight: FontWeight.w800)),
                    ),
                    GridView.count(
                      crossAxisCount: 3,
                      shrinkWrap: true,
                      physics: const NeverScrollableScrollPhysics(),
                      padding: const EdgeInsets.symmetric(horizontal: 12),
                      mainAxisSpacing: 4,
                      crossAxisSpacing: 4,
                      children: [
                        for (final v in e.value)
                          GestureDetector(
                            onTap: () => _open(v),
                            onLongPress: () => _sheet(v),
                            child: ClipRRect(
                              borderRadius: BorderRadius.circular(8),
                              child: FutureBuilder<File?>(
                                future: _file(v.id),
                                builder: (_, s) => s.data == null
                                    ? Container(color: cs.surfaceContainerHighest)
                                    : Stack(fit: StackFit.expand, children: [
                                        Image.file(s.data!, fit: BoxFit.cover, cacheWidth: 360,
                                            errorBuilder: (_, __, ___) => Container(color: cs.surfaceContainerHighest)),
                                        Positioned(
                                          left: 4,
                                          bottom: 4,
                                          child: Container(
                                            padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                                            decoration: BoxDecoration(color: Colors.black54, borderRadius: BorderRadius.circular(6)),
                                            child: Text(_left(v), style: const TextStyle(color: Colors.white, fontSize: 10)),
                                          ),
                                        ),
                                      ]),
                              ),
                            ),
                          ),
                      ],
                    ),
                  ],
                ]),
    );
  }

  /// "6 d left", "5 h left".
  static String _left(SavedPhoto v) {
    final e = v.expiresAt;
    if (e == null) return '';
    final d = e.difference(DateTime.now());
    if (d.inDays >= 1) return '${d.inDays} d left';
    if (d.inHours >= 1) return '${d.inHours} h left';
    return '${d.inMinutes.clamp(1, 59)} min left';
  }
}
