import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';

/// Thrown for any request TapTap refused, with the message to show the person.
class ApiError implements Exception {
  ApiError(this.message, {this.status = 0, this.signedOut = false, this.data});
  final String message;
  final int status;
  final bool signedOut;
  final Map<String, dynamic>? data;
  @override
  String toString() => message;
}

/// One conditional GET (Store.fetch): the answer, or "not modified" — TapTap's ETag matched what this phone keeps.
class Fetched {
  Fetched({this.data, this.body = '', this.etag = '', this.notModified = false});
  final Map<String, dynamic>? data;
  final String body;               // the JSON as it came, to keep on the phone
  final String etag;
  final bool notModified;
}

/// Talks to TapTap. The token from sign-in is kept in the phone's secure storage (Keychain / Keystore).
///
/// The app uses TapTap's own website endpoints for chat (/chat/), calls (/chat/call/) and Fit (/fit/), so Fit and
/// every permission behave exactly as on the web. App-only data lives under /api/mobile/v1/.
class Api {
  Api._();
  static final Api I = Api._();

  static const _storage = FlutterSecureStorage(aOptions: AndroidOptions(encryptedSharedPreferences: true));
  static const defaultServer = 'https://taptapnetwork.com';

  String server = defaultServer;
  String? token;
  /// The HTTP client (tests put package:http's MockClient here).
  @visibleForTesting
  late http.Client client = _fresh();
  http.Client? _made;                // the client this class made itself (a test's client is never replaced)

  /// A connection that cannot be opened in 15 s fails instead of hanging, and idle connections are dropped after
  /// 10 s — a phone moving between Wi-Fi and mobile data otherwise keeps dead connections that only time out.
  http.Client _fresh() {
    final io = HttpClient()
      ..connectionTimeout = const Duration(seconds: 15)
      ..idleTimeout = const Duration(seconds: 10);
    return _made = IOClient(io);
  }

  /// After a timeout or a dropped connection: new requests start on fresh connections (the old client finishes
  /// what it still carries and is closed a little later).
  void _renew() {
    final old = client;
    if (!identical(old, _made)) return;
    client = _fresh();
    Timer(const Duration(minutes: 2), old.close);
  }

  /// Called when TapTap says the token is no longer valid (signed out elsewhere, password changed).
  void Function()? onSignedOut;

  /// Could the phone reach TapTap the last time it tried? (false: no internet, or TapTap too slow to answer)
  final ValueNotifier<bool> online = ValueNotifier(true);

  /// When TapTap last answered (anything at all).
  DateTime? lastContact;
  int _fails = 0;                  // network failures in a row: one slow answer is not "offline" yet

  void _failed() {
    if (++_fails >= 2) online.value = false;
  }

  Future<void> load() async {
    server = (await _storage.read(key: 'server')) ?? defaultServer;
    token = await _storage.read(key: 'token');
  }

  Future<void> save({String? server, String? token}) async {
    if (server != null) {
      this.server = server;
      await _storage.write(key: 'server', value: server);
    }
    this.token = token;
    if (token == null) {
      await _storage.delete(key: 'token');
    } else {
      await _storage.write(key: 'token', value: token);
    }
  }

  bool get signedIn => token != null && token!.isNotEmpty;

  Uri url(String path, [Map<String, String>? query]) {
    final base = server.endsWith('/') ? server.substring(0, server.length - 1) : server;
    return Uri.parse('$base$path').replace(queryParameters: (query == null || query.isEmpty) ? null : query);
  }

  Map<String, String> get headers => {
        'Accept': 'application/json',
        if (signedIn) 'Authorization': 'Bearer $token',
        'X-Requested-With': 'TapTapApp',
      };

  Future<Map<String, dynamic>> get(String path, {Map<String, String>? query, Duration timeout = const Duration(seconds: 20)}) async {
    return _wrap(() => client.get(url(path, query), headers: headers).timeout(timeout));
  }

  Future<Map<String, dynamic>> post(String path, [Map<String, dynamic>? body, Duration timeout = const Duration(seconds: 30)]) async {
    return _wrap(() => client
        .post(url(path), headers: {...headers, 'Content-Type': 'application/json'}, body: jsonEncode(body ?? {}))
        .timeout(timeout));
  }

  /// Multipart upload (chat photos).
  Future<Map<String, dynamic>> upload(String path, Map<String, String> fields, File file, {String field = 'file'}) async {
    return _wrap(() async {
      final req = http.MultipartRequest('POST', url(path))
        ..headers.addAll(headers)
        ..fields.addAll(fields)
        ..files.add(await http.MultipartFile.fromPath(field, file.path));
      return http.Response.fromStream(await client.send(req).timeout(const Duration(seconds: 90)));
    });
  }

  /// An authenticated image URL plus the headers Image.network needs.
  String imageUrl(String path) => url(path).toString();

  /// A GET that sends the ETag of what the phone already keeps: TapTap answers 304 (nothing to download) when
  /// it is still the same. Errors exactly like [get].
  Future<Fetched> getFresh(String path,
      {Map<String, String>? query, String? etag, Duration timeout = const Duration(seconds: 20)}) async {
    final r = await _send(() => client
        .get(url(path, query), headers: {...headers, if (etag != null && etag.isNotEmpty) 'If-None-Match': etag})
        .timeout(timeout));
    final tag = r.headers['etag'] ?? '';
    if (r.statusCode == 304) return Fetched(notModified: true, etag: tag.isEmpty ? (etag ?? '') : tag);
    final data = _decode(r);
    final text = utf8.decode(r.bodyBytes, allowMalformed: true);     // _decode has already read it as JSON
    final body = text.trimLeft().startsWith('{') ? text : jsonEncode(data);
    return Fetched(data: data, body: body, etag: tag);
  }

  Future<Map<String, dynamic>> _wrap(Future<http.Response> Function() run) async => _decode(await _send(run));

  Future<http.Response> _send(Future<http.Response> Function() run) async {
    http.Response r;
    try {
      r = await run();
    } on TimeoutException {
      _failed();
      _renew();
      throw ApiError('TapTap is taking too long to answer. Check your internet.');
    } on SocketException {
      _failed();
      _renew();
      throw ApiError('No internet connection.');
    } on http.ClientException {
      _failed();
      _renew();
      throw ApiError('Could not reach TapTap.');
    } on HandshakeException {
      _failed();
      _renew();
      throw ApiError('Could not open a secure connection to TapTap. Check the phone’s date and time, and your internet.');
    }
    _fails = 0;
    online.value = true;
    lastContact = DateTime.now();
    return r;
  }

  Map<String, dynamic> _decode(http.Response r) {
    Map<String, dynamic> data;
    try {
      final d = jsonDecode(utf8.decode(r.bodyBytes));
      data = d is Map<String, dynamic> ? d : {'ok': true, 'data': d};
    } catch (_) {
      if (r.statusCode == 302 || r.statusCode == 401) {
        _signedOut();
        throw ApiError('Please sign in again.', status: r.statusCode, signedOut: true);
      }
      throw ApiError('TapTap sent an unexpected answer (${r.statusCode}).', status: r.statusCode);
    }
    if (r.statusCode == 401 || data['auth'] == false) {
      _signedOut();
      throw ApiError(data['message']?.toString() ?? 'Please sign in again.', status: 401, signedOut: true);
    }
    if (r.statusCode >= 400 || data['ok'] == false) {
      throw ApiError(data['message']?.toString() ?? 'Something went wrong (${r.statusCode}).', status: r.statusCode, data: data);
    }
    return data;
  }

  void _signedOut() {
    if (!signedIn) return;
    save(token: null);
    onSignedOut?.call();
  }
}
