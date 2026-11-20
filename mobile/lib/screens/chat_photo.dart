import 'dart:io';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../data/photo_store.dart';

/// A photo inside a chat bubble.
///
/// It is downloaded into this phone ([PhotoStore]) and shown from there: at once, and without internet. TapTap
/// deletes chat photos after their normal time (7 days); this phone deletes its copy at that same moment. Tap it to
/// see it full screen. View-once photos are never kept: they open once, full screen, from memory.
class PhotoBubble extends StatefulWidget {
  const PhotoBubble({super.key, required this.m, required this.mine, this.threadTitle = ''});
  final Map<String, dynamic> m;
  final bool mine;
  final String threadTitle;
  @override
  State<PhotoBubble> createState() => _PhotoBubbleState();
}

class _PhotoBubbleState extends State<PhotoBubble> {
  File? _file;
  bool _tried = false;
  bool _openedOnce = false;

  int get _id => toI(widget.m['id']);
  Map<String, dynamic> get _img => (widget.m['image'] as Map).cast<String, dynamic>();

  @override
  void initState() {
    super.initState();
    PhotoStore.I.changes.addListener(_onStore);
    _find();
  }

  @override
  void didUpdateWidget(PhotoBubble old) {
    super.didUpdateWidget(old);
    if (toI(old.m['id']) != _id) {
      _file = null;
      _tried = false;
      _find();
    }
  }

  @override
  void dispose() {
    PhotoStore.I.changes.removeListener(_onStore);
    super.dispose();
  }

  void _onStore() {
    if (!mounted) return;
    if (!PhotoStore.I.has(_id) && _file != null) {
      setState(() => _file = null);                // deleted from the phone, or its time is up
    } else if (PhotoStore.I.has(_id) && _file == null) {
      _find();
    }
  }

  Future<void> _find() async {
    if (_img['once'] == true) return;
    final f = await PhotoStore.I.fileOf(_id);
    if (!mounted) return;
    if (f != null) {
      setState(() => _file = f);
      return;
    }
    if (_tried) return;
    _tried = true;
    final path = await PhotoStore.I.fetch(widget.m, threadTitle: widget.threadTitle);
    if (mounted) setState(() => _file = path == null ? null : File(path));
  }

  Future<void> _openOnce() async {
    if (widget.mine) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('You sent a view-once photo: it opens once for the other person, then it is gone.'),
          behavior: SnackBarBehavior.floating));
      return;
    }
    try {
      final bytes = await Api.I.bytes('/chat/img/$_id/', what: 'photo');
      if (!mounted) return;
      setState(() => _openedOnce = true);
      await openPhoto(context, bytes: bytes, title: '${widget.m['sender'] ?? ''}', at: widget.m['at'], once: true);
    } on ApiError catch (e) {
      if (!mounted) return;
      setState(() => _openedOnce = true);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message), behavior: SnackBarBehavior.floating));
    }
  }

  @override
  Widget build(BuildContext context) {
    final img = _img;
    final soft = widget.mine ? Colors.white70 : Theme.of(context).colorScheme.onSurfaceVariant;
    if (img['once'] == true) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: OutlinedButton.icon(
          style: OutlinedButton.styleFrom(foregroundColor: widget.mine ? Colors.white : null,
              side: BorderSide(color: widget.mine ? Colors.white54 : Theme.of(context).colorScheme.outline)),
          onPressed: _openedOnce ? null : _openOnce,
          icon: const Icon(Icons.visibility_outlined),
          label: Text(_openedOnce ? 'View-once photo · opened' : widget.mine ? 'View-once photo' : 'View-once photo — tap to open'),
        ),
      );
    }
    final w = toD(img['w']), h = toD(img['h']);
    final ratio = (w > 0 && h > 0) ? (w / h).clamp(.5, 2.0).toDouble() : 4 / 3;
    final exp = parseTime(img['expires']);
    Widget pic;
    if (_file != null) {
      pic = GestureDetector(
        onTap: () => openPhoto(context, file: _file, title: widget.mine ? 'You' : '${widget.m['sender'] ?? ''}', at: widget.m['at'], expires: exp),
        child: Image.file(_file!, fit: BoxFit.cover, errorBuilder: (_, __, ___) => const Text('📷 Photo')),
      );
    } else {
      // still downloading (or no internet yet): the server copy, as before
      pic = GestureDetector(
        onTap: _find,
        child: Image.network(Api.I.imageUrl('/chat/img/$_id/'), headers: Api.I.headers, fit: BoxFit.cover,
            loadingBuilder: (_, child, p) => p == null ? child : const Center(child: CircularProgressIndicator(strokeWidth: 2)),
            errorBuilder: (_, __, ___) => const Padding(padding: EdgeInsets.all(12), child: Text('📷 Photo — tap to try again'))),
      );
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        ClipRRect(borderRadius: BorderRadius.circular(12), child: AspectRatio(aspectRatio: ratio, child: pic)),
        if (_file != null && exp != null)
          Padding(
            padding: const EdgeInsets.only(top: 3),
            child: Text('📱 On this phone · deleted ${whenText(exp.toIso8601String())}', style: TextStyle(fontSize: 10.5, color: soft)),
          ),
      ]),
    );
  }
}

/// Full screen: pinch to zoom. [once]: a view-once photo shown from memory, never saved.
Future<void> openPhoto(BuildContext context, {File? file, Uint8List? bytes, String title = '', dynamic at, DateTime? expires, bool once = false}) {
  return Navigator.of(context).push(MaterialPageRoute(
    fullscreenDialog: true,
    builder: (_) => Scaffold(
      backgroundColor: Colors.black,
      appBar: AppBar(
        backgroundColor: Colors.black,
        foregroundColor: Colors.white,
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(title, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
          if (parseTime(at) != null)
            Text(DateFormat('EEE d MMM yyyy · HH:mm').format(parseTime(at)!), style: const TextStyle(fontSize: 12, color: Colors.white70)),
        ]),
      ),
      body: Column(children: [
        Expanded(
          child: InteractiveViewer(
            maxScale: 5,
            child: Center(
              child: file != null
                  ? Image.file(file, fit: BoxFit.contain)
                  : (bytes != null ? Image.memory(bytes, fit: BoxFit.contain) : const SizedBox.shrink()),
            ),
          ),
        ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Text(
              once
                  ? '👁 View-once: it is already deleted from TapTap and is not kept on this phone.'
                  : (expires != null ? '📱 Kept on this phone until ${whenText(expires.toIso8601String())}, when TapTap deletes it too.' : ''),
              textAlign: TextAlign.center,
              style: const TextStyle(color: Colors.white70, fontSize: 12.5),
            ),
          ),
        ),
      ]),
    ),
  ));
}
