import 'dart:io';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../app/api.dart';
import '../app/state.dart';
import '../push/push.dart';

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final _email = TextEditingController();
  final _pw = TextEditingController();
  final _code = TextEditingController();
  final _server = TextEditingController(text: Api.I.server);
  bool _busy = false, _showServer = false, _hide = true;
  String? _err, _ticket, _sentTo;

  Map<String, dynamic> get _device => {
        'device': Platform.isAndroid ? 'Android phone' : (Platform.isIOS ? 'iPhone' : 'Phone'),
        'platform': Platform.isAndroid ? 'android' : (Platform.isIOS ? 'ios' : 'other'),
        'app_version': '1.1.0',
      };

  Future<void> _run(Future<void> Function() f) async {
    setState(() {
      _busy = true;
      _err = null;
    });
    try {
      await f();
    } on ApiError catch (e) {
      setState(() => _err = e.message);
      if (e.data?['restart'] == true) setState(() => _ticket = null);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _signIn() => _run(() async {
        var s = _server.text.trim();
        if (!s.startsWith('http')) s = 'https://$s';
        await Api.I.save(server: s, token: null);
        final d = await Api.I.post('/api/mobile/v1/auth/login/', {'email': _email.text.trim(), 'password': _pw.text, ..._device});
        if (d['otp'] == true) {
          setState(() {
            _ticket = '${d['ticket']}';
            _sentTo = '${d['email'] ?? 'your email'}';
          });
        } else {
          await _done('${d['token']}');
        }
      });

  Future<void> _verify() => _run(() async {
        final d = await Api.I.post('/api/mobile/v1/auth/verify/', {'ticket': _ticket, 'code': _code.text.trim()});
        await _done('${d['token']}');
      });

  Future<void> _done(String token) async {
    await context.read<AppState>().signedInWith(token);
    Push.register();
  }

  @override
  Widget build(BuildContext context) {
    final t = Theme.of(context);
    return Scaffold(
      body: Container(
        decoration: const BoxDecoration(
          gradient: LinearGradient(begin: Alignment.topLeft, end: Alignment.bottomRight, colors: [Color(0xFF1769E0), Color(0xFF0B1220)]),
        ),
        child: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  const Icon(Icons.wifi_tethering, size: 56, color: Colors.white),
                  const SizedBox(height: 12),
                  const Text('TapTap', textAlign: TextAlign.center,
                      style: TextStyle(color: Colors.white, fontSize: 34, fontWeight: FontWeight.w900, letterSpacing: -.5)),
                  const SizedBox(height: 6),
                  const Text('Your Wi-Fi business in your pocket', textAlign: TextAlign.center, style: TextStyle(color: Colors.white70)),
                  const SizedBox(height: 32),
                  Card(
                    child: Padding(
                      padding: const EdgeInsets.all(20),
                      child: AnimatedSwitcher(
                        duration: const Duration(milliseconds: 250),
                        child: _ticket == null ? _form(t) : _otp(t),
                      ),
                    ),
                  ),
                ]),
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _form(ThemeData t) => Column(key: const ValueKey('form'), crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text('Sign in', style: t.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w800)),
        const SizedBox(height: 4),
        Text('Use the same email and password as TapTap on the web.', style: t.textTheme.bodySmall),
        const SizedBox(height: 18),
        TextField(controller: _email, keyboardType: TextInputType.emailAddress, autofillHints: const [AutofillHints.email],
            decoration: const InputDecoration(labelText: 'Email', prefixIcon: Icon(Icons.mail_outline))),
        const SizedBox(height: 12),
        TextField(
          controller: _pw,
          obscureText: _hide,
          autofillHints: const [AutofillHints.password],
          onSubmitted: (_) => _signIn(),
          decoration: InputDecoration(
            labelText: 'Password',
            prefixIcon: const Icon(Icons.lock_outline),
            suffixIcon: IconButton(icon: Icon(_hide ? Icons.visibility : Icons.visibility_off), onPressed: () => setState(() => _hide = !_hide)),
          ),
        ),
        if (_showServer) ...[
          const SizedBox(height: 12),
          TextField(controller: _server, keyboardType: TextInputType.url,
              decoration: const InputDecoration(labelText: 'TapTap address', prefixIcon: Icon(Icons.dns_outlined))),
        ],
        if (_err != null) ...[const SizedBox(height: 12), Text(_err!, style: TextStyle(color: t.colorScheme.error))],
        const SizedBox(height: 18),
        FilledButton(onPressed: _busy ? null : _signIn, child: _busy ? const _Dots() : const Text('Sign in')),
        TextButton(
          onPressed: () => setState(() => _showServer = !_showServer),
          child: Text(_showServer ? 'Hide server address' : 'Using your own TapTap server?'),
        ),
      ]);

  Widget _otp(ThemeData t) => Column(key: const ValueKey('otp'), crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text('Check your email', style: t.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w800)),
        const SizedBox(height: 4),
        Text('We sent a code to $_sentTo.', style: t.textTheme.bodySmall),
        const SizedBox(height: 18),
        TextField(
          controller: _code,
          autofocus: true,
          keyboardType: TextInputType.number,
          textAlign: TextAlign.center,
          autofillHints: const [AutofillHints.oneTimeCode],
          style: const TextStyle(fontSize: 26, letterSpacing: 8, fontWeight: FontWeight.w800),
          onSubmitted: (_) => _verify(),
          decoration: const InputDecoration(hintText: '••••••'),
        ),
        if (_err != null) ...[const SizedBox(height: 12), Text(_err!, style: TextStyle(color: t.colorScheme.error))],
        const SizedBox(height: 18),
        FilledButton(onPressed: _busy ? null : _verify, child: _busy ? const _Dots() : const Text('Verify')),
        Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
          TextButton(onPressed: () => setState(() => _ticket = null), child: const Text('Back')),
          TextButton(
            onPressed: _busy
                ? null
                : () => _run(() async {
                      final d = await Api.I.post('/api/mobile/v1/auth/resend/', {'ticket': _ticket});
                      setState(() => _err = '${d['message'] ?? 'Sent again.'}');
                    }),
            child: const Text('Send again'),
          ),
        ]),
      ]);
}

class _Dots extends StatelessWidget {
  const _Dots();
  @override
  Widget build(BuildContext context) =>
      const SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2.5, color: Colors.white));
}
