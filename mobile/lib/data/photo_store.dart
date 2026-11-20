import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart' show getDatabasesPath;

import '../app/api.dart';
import '../app/format.dart';

/// Chat photos kept on this phone — until their normal time.
///
/// A photo sent in chat is downloaded into the app's own private storage (next to its database — no other app can
/// read it) as soon as it arrives or its conversation is opened. It then shows at once, without internet, and opens
/// full screen from the phone. TapTap deletes every chat photo after its normal time (7 days, CHAT_IMAGE_DAYS on the
/// server); this phone deletes its copy at that same moment, by itself — so a photo is gone everywhere at the same
/// time, and photos never pile up on the phone. You can delete one earlier (long-press it, or More → Photos on this
/// phone). View-once photos are never kept. Signing out deletes them all.
class SavedPhoto {
  SavedPhoto({
    required this.id,
    required this.file,
    required this.sender,
    required this.thread,
    required this.threadTitle,
    required this.at,
    required this.expires,
    required this.mine,
    required this.size,
  });

  final int id;                 // the message id on TapTap
  final String file;            // file name inside the photo folder
  final String sender;
  final int thread;
  final String threadTitle;
  final String at;              // when it was sent (TapTap's ISO time)
  final String expires;         // when TapTap deletes it — and so does this phone
  final bool mine;
  final int size;

  DateTime? get expiresAt => parseTime(expires);
  bool get expired {
    final e = expiresAt;
    return e != null && !e.isAfter(DateTime.now());
  }

  Map<String, dynamic> toJson() => {
        'id': id, 'file': file, 'sender': sender, 'thread': thread, 'thread_title': threadTitle, 'at': at,
        'expires': expires, 'mine': mine, 'size': size,
      };

  static SavedPhoto? fromJson(Map<String, dynamic> j) {
    final id = int.tryParse('${j['id']}');
    final file = '${j['file'] ?? ''}';
    if (id == null || file.isEmpty || file.contains('/') || file.contains('\\')) return null;
    return SavedPhoto(
      id: id,
      file: file,
      sender: '${j['sender'] ?? ''}',
      thread: int.tryParse('${j['thread']}') ?? 0,
      threadTitle: '${j['thread_title'] ?? ''}',
      at: '${j['at'] ?? ''}',
      expires: '${j['expires'] ?? ''}',
      mine: j['mine'] == true,
      size: int.tryParse('${j['size']}') ?? 0,
    );
  }
}

class PhotoStore {
  PhotoStore._();
  static final PhotoStore I = PhotoStore._();

  final Map<int, SavedPhoto> _items = {};
  final Map<int, Future<String?>> _loading = {};      // downloads in progress (one per photo)
  final Set<int> _failed = {};                         // gone or refused: not asked again this session
  Directory? _dir;
  Future<void>? _ready;
  Timer? _timer;

  /// Bumped on every change: the bubbles and the list follow it.
  final ValueNotifier<int> changes = ValueNotifier<int>(0);

  Future<void> get ready => _ready ??= _load();

  Future<Directory> _folder() async {
    if (_dir != null) return _dir!;
    final base = await getDatabasesPath();
    final d = Directory(p.join(base, 'taptap_photos'));
    if (!await d.exists()) await d.create(recursive: true);
    return _dir = d;
  }

  File _index(Directory d) => File(p.join(d.path, 'index.json'));

  Future<void> _load() async {
    try {
      final d = await _folder();
      final f = _index(d);
      if (await f.exists()) {
        final raw = jsonDecode(await f.readAsString());
        if (raw is List) {
          for (final e in raw) {
            if (e is! Map) continue;
            final v = SavedPhoto.fromJson(e.cast<String, dynamic>());
            if (v != null && await File(p.join(d.path, v.file)).exists()) _items[v.id] = v;
          }
        }
      }
    } catch (_) {
      // a damaged list is not worth crashing over: photos are downloaded again while TapTap still has them
    }
    await sweep();
    // the phone deletes each photo at its normal time even while the app stays open
    _timer ??= Timer.periodic(const Duration(minutes: 15), (_) => sweep());
    changes.value++;
  }

  Future<void> _write() async {
    final d = await _folder();
    final tmp = File(p.join(d.path, 'index.json.tmp'));
    await tmp.writeAsString(jsonEncode(_items.values.map((v) => v.toJson()).toList()), flush: true);
    await tmp.rename(_index(d).path);
    changes.value++;
  }

  /// Delete every photo whose normal time is up (TapTap deleted its copy at the same moment).
  Future<int> sweep() async {
    final gone = _items.values.where((v) => v.expired).toList();
    if (gone.isEmpty) return 0;
    final d = await _folder();
    for (final v in gone) {
      _items.remove(v.id);
      try {
        final f = File(p.join(d.path, v.file));
        if (await f.exists()) await f.delete();
      } catch (_) {}
    }
    await _write();
    return gone.length;
  }

  SavedPhoto? get(int id) {
    final v = _items[id];
    return v == null || v.expired ? null : v;
  }

  bool has(int id) => get(id) != null;
  bool loading(int id) => _loading.containsKey(id);

  /// Newest first.
  List<SavedPhoto> all() {
    final out = _items.values.where((v) => !v.expired).toList();
    out.sort((a, b) => b.at.compareTo(a.at));
    return out;
  }

  int get count => all().length;
  int get bytes => all().fold<int>(0, (s, v) => s + v.size);

  Future<File?> fileOf(int id) async {
    await ready;
    final v = get(id);
    if (v == null) return null;
    final d = await _folder();
    final f = File(p.join(d.path, v.file));
    if (await f.exists()) return f;
    _items.remove(id);
    await _write();
    return null;
  }

  /// Is this chat message a photo the phone should keep? (not view-once, not expired, not a video / voice)
  static bool keepable(Map<String, dynamic> m) {
    final img = m['image'];
    if (img is! Map || m['deleted'] == true) return false;
    final kind = '${img['kind'] ?? 'image'}';
    return kind == 'image' && img['gone'] != true && img['once'] != true && toI(img['id']) > 0;
  }

  /// Download a photo message into the phone (once; later calls wait for the same download). Returns its path.
  Future<String?> fetch(Map<String, dynamic> m, {String threadTitle = ''}) async {
    if (!keepable(m)) return null;
    await ready;
    final id = toI(m['id']);
    final have = await fileOf(id);
    if (have != null) return have.path;
    if (_failed.contains(id)) return null;
    final expires = '${(m['image'] as Map)['expires'] ?? ''}';
    final e = parseTime(expires);
    if (e != null && !e.isAfter(DateTime.now())) return null;
    return _loading[id] ??= _download(id, m, expires, threadTitle).whenComplete(() => _loading.remove(id));
  }

  Future<String?> _download(int id, Map<String, dynamic> m, String expires, String threadTitle) async {
    try {
      final data = await Api.I.bytes('/chat/img/$id/', what: 'photo', timeout: const Duration(seconds: 60));
      final d = await _folder();
      final name = 'photo-$id.${_ext(data)}';
      final f = File(p.join(d.path, name));
      await f.writeAsBytes(data, flush: true);
      _items[id] = SavedPhoto(
        id: id,
        file: name,
        sender: m['mine'] == true ? 'You' : '${m['sender'] ?? ''}',
        thread: toI(m['thread']),
        threadTitle: threadTitle,
        at: '${m['at'] ?? ''}',
        // no expiry from TapTap (an old server): keep it 7 days, TapTap's normal time
        expires: expires.isNotEmpty ? expires : DateTime.now().add(const Duration(days: 7)).toUtc().toIso8601String(),
        mine: m['mine'] == true,
        size: data.length,
      );
      await _write();
      return f.path;
    } on ApiError catch (e) {
      if (e.status == 410 || e.status == 403 || e.status == 404) _failed.add(id);   // gone: never asked again
      return null;
    } catch (_) {
      return null;                                     // no internet now: tried again next time
    }
  }

  /// Download several at once, a few at a time (a conversation opened, new messages arrived).
  Future<void> fetchAll(Iterable<Map<String, dynamic>> messages, {String threadTitle = ''}) async {
    final todo = messages.where(keepable).where((m) => !has(toI(m['id']))).toList();
    for (var i = 0; i < todo.length; i += 3) {
      await Future.wait(todo.skip(i).take(3).map((m) => fetch(m, threadTitle: threadTitle)));
    }
  }

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

  Future<void> clear() async {
    await ready;
    _items.clear();
    _failed.clear();
    try {
      final d = await _folder();
      if (await d.exists()) await d.delete(recursive: true);
      _dir = null;
    } catch (_) {}
    changes.value++;
  }

  static String _ext(List<int> b) {
    if (b.length > 3 && b[0] == 0xFF && b[1] == 0xD8) return 'jpg';
    if (b.length > 8 && b[0] == 0x89 && b[1] == 0x50) return 'png';
    if (b.length > 12 && b[0] == 0x52 && b[8] == 0x57) return 'webp';
    if (b.length > 3 && b[0] == 0x47 && b[1] == 0x49) return 'gif';
    return 'img';
  }
}
