import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;

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
  final _client = http.Client();

  /// Called when TapTap says the token is no longer valid (signed out elsewhere, password changed).
  void Function()? onSignedOut;

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
    return _wrap(() => _client.get(url(path, query), headers: headers).timeout(timeout));
  }

  Future<Map<String, dynamic>> post(String path, [Map<String, dynamic>? body, Duration timeout = const Duration(seconds: 30)]) async {
    return _wrap(() => _client
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
      return http.Response.fromStream(await _client.send(req).timeout(const Duration(seconds: 90)));
    });
  }

  /// An authenticated image URL plus the headers Image.network needs.
  String imageUrl(String path) => url(path).toString();

  Future<Map<String, dynamic>> _wrap(Future<http.Response> Function() run) async {
    http.Response r;
    try {
      r = await run();
    } on TimeoutException {
      throw ApiError('TapTap is taking too long to answer. Check your internet.');
    } on SocketException {
      throw ApiError('No internet connection.');
    } on http.ClientException {
      throw ApiError('Could not reach TapTap.');
    }
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
