import 'dart:async';

import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart';

/// One answer from TapTap kept on the phone.
class SavedPage {
  SavedPage({required this.key, required this.body, this.etag = '', required this.savedAt});
  final String key;           // path + sorted query, e.g. /api/mobile/v1/vouchers/?page=1
  final String body;          // the JSON exactly as TapTap sent it
  final String etag;          // TapTap's ETag for it: sent back as If-None-Match
  final DateTime savedAt;     // when TapTap last confirmed it (a 304 counts)

  SavedPage touched(DateTime at, [String? newTag]) =>
      SavedPage(key: key, body: body, etag: (newTag == null || newTag.isEmpty) ? etag : newTag, savedAt: at);
}

/// The phone's own database (SQLite): every page TapTap sent, per account and business ("scope"), so screens open
/// at once — also with no internet. If the database cannot be opened, the app simply works online as before.
class LocalDb {
  LocalDb._();
  static final LocalDb I = LocalDb._();

  static const _file = 'taptap_local.db';
  static const maxRows = 400;                         // per scope; the oldest go first
  static const maxAge = Duration(days: 21);
  static const maxBody = 3 * 1024 * 1024;            // bigger answers are not kept

  Database? _db;
  Future<Database?>? _opening;
  bool _broken = false;

  Future<Database?> _open() {
    if (_db != null) return Future.value(_db);
    if (_broken) return Future.value(null);
    return _opening ??= _doOpen();
  }

  Future<Database?> _doOpen() async {
    try {
      // never let the phone's database hold up start-up or sign-in: if it does not open in time, work online
      final dir = await getDatabasesPath().timeout(const Duration(seconds: 8));
      final db = await openDatabase(
        p.join(dir, _file),
        version: 1,
        onCreate: (db, version) async {
          await db.execute('CREATE TABLE pages ('
              'scope TEXT NOT NULL, '
              'k TEXT NOT NULL, '
              'body TEXT NOT NULL, '
              "etag TEXT NOT NULL DEFAULT '', "
              'saved_at INTEGER NOT NULL, '
              'PRIMARY KEY (scope, k))');
          await db.execute('CREATE INDEX pages_by_age ON pages (scope, saved_at)');
        },
      ).timeout(const Duration(seconds: 8));
      _db = db;
      return db;
    } catch (_) {
      _broken = true;
      return null;
    } finally {
      _opening = null;
    }
  }

  Future<List<SavedPage>> load(String scope) async {
    try {
      final db = await _open();
      if (db == null) return [];
      final rows = await db.query('pages', where: 'scope = ?', whereArgs: [scope]);
      return [
        for (final r in rows)
          SavedPage(
            key: '${r['k']}',
            body: '${r['body']}',
            etag: '${r['etag'] ?? ''}',
            savedAt: DateTime.fromMillisecondsSinceEpoch((r['saved_at'] as int?) ?? 0),
          ),
      ];
    } catch (_) {
      return [];
    }
  }

  Future<SavedPage?> one(String scope, String key) async {
    try {
      final db = await _open();
      if (db == null) return null;
      final rows = await db.query('pages', where: 'scope = ? AND k = ?', whereArgs: [scope, key], limit: 1);
      if (rows.isEmpty) return null;
      final r = rows.first;
      return SavedPage(
        key: key,
        body: '${r['body']}',
        etag: '${r['etag'] ?? ''}',
        savedAt: DateTime.fromMillisecondsSinceEpoch((r['saved_at'] as int?) ?? 0),
      );
    } catch (_) {
      return null;
    }
  }

  Future<void> save(String scope, SavedPage page) async {
    if (page.body.length > maxBody) return;
    try {
      final db = await _open();
      await db?.insert(
        'pages',
        {'scope': scope, 'k': page.key, 'body': page.body, 'etag': page.etag, 'saved_at': page.savedAt.millisecondsSinceEpoch},
        conflictAlgorithm: ConflictAlgorithm.replace,
      );
    } catch (_) {}
  }

  Future<void> touch(String scope, String key, DateTime at, String etag) async {
    try {
      final db = await _open();
      await db?.update('pages', {'saved_at': at.millisecondsSinceEpoch, if (etag.isNotEmpty) 'etag': etag},
          where: 'scope = ? AND k = ?', whereArgs: [scope, key]);
    } catch (_) {}
  }

  Future<void> remove(String scope, String key) async {
    try {
      final db = await _open();
      await db?.delete('pages', where: 'scope = ? AND k = ?', whereArgs: [scope, key]);
    } catch (_) {}
  }

  /// Old pages out, and never more than [maxRows] per scope.
  Future<void> prune(String scope) async {
    try {
      final db = await _open();
      if (db == null) return;
      final cutoff = DateTime.now().subtract(maxAge).millisecondsSinceEpoch;
      await db.delete('pages', where: 'scope = ? AND saved_at < ?', whereArgs: [scope, cutoff]);
      await db.rawDelete(
          'DELETE FROM pages WHERE scope = ? AND k NOT IN '
          '(SELECT k FROM pages WHERE scope = ? ORDER BY saved_at DESC LIMIT ?)',
          [scope, scope, maxRows]);
    } catch (_) {}
  }

  /// Everything off the phone (sign-out).
  Future<void> wipe() async {
    try {
      final db = await _open();
      await db?.delete('pages');
    } catch (_) {}
  }
}
