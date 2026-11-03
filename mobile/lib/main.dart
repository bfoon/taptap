import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import 'app/state.dart';
import 'app/theme.dart';
import 'push/push.dart';
import 'screens/login_screen.dart';
import 'screens/shell.dart';

/// TapTap for business owners and their team.
///   Today       live daily sales (first page) and what needs you now
///   Complaints  vouchers customers sent from the Wi-Fi pages — fix, close, call them back
///   Chat        the team room and direct messages, with calls
///   Fit         your AI — exactly what it can do for you on the web
///   More        customer calls, business switcher, signed-in phones
Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(statusBarColor: Colors.transparent));
  await Push.init();
  final app = AppState();
  runApp(ChangeNotifierProvider.value(value: app, child: const TapTapApp()));
  app.boot();
}

class TapTapApp extends StatelessWidget {
  const TapTapApp({super.key});

  @override
  Widget build(BuildContext context) {
    final app = context.watch<AppState>();
    return MaterialApp(
      title: 'TapTap',
      debugShowCheckedModeBanner: false,
      navigatorKey: navigatorKey,
      theme: buildTheme(app.brand, Brightness.light),
      darkTheme: buildTheme(app.brand, Brightness.dark),
      home: app.booting
          ? const _Splash()
          : app.signedIn
              ? const Shell()
              : app.bootError != null
                  ? _Offline(message: app.bootError!, retry: app.retryBoot)
                  : const LoginScreen(),
    );
  }
}

class _Splash extends StatelessWidget {
  const _Splash();
  @override
  Widget build(BuildContext context) => const Scaffold(
        backgroundColor: Color(0xFF1769E0),
        body: Center(child: Icon(Icons.wifi_tethering, size: 72, color: Colors.white)),
      );
}

class _Offline extends StatelessWidget {
  const _Offline({required this.message, required this.retry});
  final String message;
  final VoidCallback retry;
  @override
  Widget build(BuildContext context) => Scaffold(
        body: Center(
          child: Padding(
            padding: const EdgeInsets.all(32),
            child: Column(mainAxisSize: MainAxisSize.min, children: [
              const Icon(Icons.wifi_off, size: 56),
              const SizedBox(height: 12),
              Text(message, textAlign: TextAlign.center),
              const SizedBox(height: 16),
              FilledButton(onPressed: retry, child: const Text('Try again')),
            ]),
          ),
        ),
      );
}
