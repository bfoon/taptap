import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart' show getDatabasesPath;

/// Voice messages kept on this phone.
///
/// The first time you press ▶ on a voice message, the app downloads it into its own private storage (next to its
/// database — no other app can read it) and plays it from there. TapTap still deletes it from the server 1 minute
/// after it is first played (core/chat_media.py) — on the web it is gone then, as before — but this phone keeps
/// its copy: it stays in the conversation, plays without internet, and is listed in More → Voice messages on this
/// phone. You delete it from the phone yourself (long-press it, or from that list), like a message. Signing out
/// deletes them all, like everything else the app keeps for an account.
class SavedVoice {
  SavedVoice({
    required this.id,
    required this.file,
    required this.secs,
    required this.sender,
    required this.thread,
    required this.threadTitle,
    required this.at,
    required this.savedAt,
    required this.mine,
    required this.size,
  });

  final int id;                 // the message id on TapTap
  final String file;            // file name inside the voice folder
  final int secs;
  final String sender;
  final int thread;
  final String threadTitle;
  final String at;              // when it was sent (TapTap's ISO time)
  final String savedAt;
  final bool mine;
  final int size;

  Map<String, dynamic> toJson() => {
        'id': id, 'file': file, 'secs': secs, 'sender': sender, 'thread': thread, 'thread_title': threadTitle,
        'at': at, 'saved_at': savedAt, 'mine': mine, 'size': size,
      };

  static SavedVoice? fromJson(Map<String, dynamic> j) {
    final id = int.tryParse('${j['id']}');
    final file = '${j['file'] ?? ''}';
    if (id == null || file.isEmpty || file.contains('/') || file.contains('\\')) return null;
    return SavedVoice(
      id: id,
      file: file,
      secs: int.tryParse('${j['secs']}') ?? 0,
      sender: '${j['sender'] ?? ''}',
      thread: int.tryParse('${j['thread']}') ?? 0,
      threadTitle: '${j['thread_title'] ?? ''}',
      at: '${j['at'] ?? ''}',
      savedAt: '${j['saved_at'] ?? ''}',
      mine: j['mine'] == true,
      size: int.tryParse('${j['size']}') ?? 0,
    );
  }
}

class VoiceStore {
  VoiceStore._();
  static final VoiceStore I = VoiceStore._();

  final Map<int, SavedVoice> _items = {};
  Directory? _dir;
  Future<void>? _loading;

  /// Bumped on every change: the conversation bubbles and the list follow it.
  final ValueNotifier<int> changes = ValueNotifier<int>(0);

  /// Load the list once (cheap: one small JSON file). Every method waits for it.
  Future<void> get ready => _loading ??= _load();

  Future<Directory> _folder() async {
    if (_dir != null) return _dir!;
    final base = await getDatabasesPath();         // the app's own private, kept storage
    final d = Directory(p.join(base, 'taptap_voice'));
    if (!await d.exists()) await d.create(recursive: true);
    return _dir = d;
  }

  File _index(Directory d) => File(p.join(d.path, 'index.json'));

  Future<void> _load() async {
    try {
      final d = await _folder();
      final f = _index(d);
      if (!await f.exists()) return;
      final raw = jsonDecode(await f.readAsString());
      if (raw is! List) return;
      for (final e in raw) {
        if (e is! Map) continue;
        final v = SavedVoice.fromJson(e.cast<String, dynamic>());
        if (v != null && await File(p.join(d.path, v.file)).exists()) _items[v.id] = v;
      }
    } catch (_) {
      // a damaged list is not worth crashing over: the files are found again when played
    }
    changes.value++;                               // bubbles drawn before the list was read show their copy now
  }

  Future<void> _write() async {
    final d = await _folder();
    final tmp = File(p.join(d.path, 'index.json.tmp'));
    await tmp.writeAsString(jsonEncode(_items.values.map((v) => v.toJson()).toList()), flush: true);
    await tmp.rename(_index(d).path);              // all or nothing: never a half-written list
    changes.value++;
  }

  /// Already loaded? (Bubbles ask while building; before [ready] completes they simply say "not saved".)
  SavedVoice? get(int id) => _items[id];
  bool has(int id) => _items.containsKey(id);

  /// The saved voice messages, newest first.
  List<SavedVoice> all() {
    final out = _items.values.toList();
    out.sort((a, b) => b.at.compareTo(a.at));
    return out;
  }

  int get count => _items.length;
  int get bytes => _items.values.fold<int>(0, (s, v) => s + v.size);

  Future<String?> pathOf(int id) async {
    await ready;
    final v = _items[id];
    if (v == null) return null;
    final d = await _folder();
    final f = File(p.join(d.path, v.file));
    if (await f.exists()) return f.path;
    _items.remove(id);                             // the file went missing: forget it
    await _write();
    return null;
  }

  /// Keep a downloaded voice message. Returns the path to play.
  Future<String> save(int id, List<int> data, String ext,
      {required int secs, required String sender, required int thread, required String threadTitle, required String at, required bool mine}) async {
    await ready;
    final d = await _folder();
    final name = 'voice-$id.$ext';
    final f = File(p.join(d.path, name));
    await f.writeAsBytes(data, flush: true);
    _items[id] = SavedVoice(
      id: id, file: name, secs: secs, sender: sender, thread: thread, threadTitle: threadTitle, at: at,
      savedAt: DateTime.now().toUtc().toIso8601String(), mine: mine, size: data.length,
    );
    await _write();
    return f.path;
  }

  /// Delete one from this phone (TapTap is not asked: it deletes its own copy by itself).
  Future<void> remove(int id) async {
    await ready;
    final v = _items.remove(id);
    if (v == null) return;
    try {
      final d = await _folder();
      final f = File(p.join(d.path, v.file));
      if (await f.exists()) await f.delete();
    } catch (_) {}
    await _write();
  }

  /// Delete them all (the list's "Delete all", and signing out).
  Future<void> clear() async {
    await ready;
    _items.clear();
    try {
      final d = await _folder();
      if (await d.exists()) await d.delete(recursive: true);
      _dir = null;
    } catch (_) {}
    changes.value++;
  }
}
