import 'dart:io';

import 'package:flutter/material.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../push/push.dart';
import '../widgets/common.dart';

/// Notifications on this phone: can they reach it at all (this build of the app, the TapTap server, the phone's
/// permission), which kinds it gets (saved on the server for this phone), a test, and the phone's own settings
/// for pop-up, sound and lock screen.
class NotificationsScreen extends StatefulWidget {
  const NotificationsScreen({super.key});
  @override
  State<NotificationsScreen> createState() => _NotificationsScreenState();
}

class _NotificationsScreenState extends State<NotificationsScreen> with WidgetsBindingObserver {
  Map<String, dynamic>? _d;
  String? _err;
  String _perm = '';
  bool _testing = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _load();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _load();      // back from the phone's settings
  }

  Future<void> _load() async {
    final perm = await Push.permission();
    try {
      final d = await Api.I.get('/api/mobile/v1/notifications/');
      if (!mounted) return;
      setState(() {
        _perm = perm;
        _d = d;
        _err = null;
      });
    } on ApiError catch (e) {
      if (mounted) {
        setState(() {
          _perm = perm;
          _err = e.message;
        });
      }
    }
  }

  void _toast(String m, {bool error = false}) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(m),
        behavior: SnackBarBehavior.floating,
        backgroundColor: error ? Theme.of(context).colorScheme.error : null,
      ));

  Future<void> _toggle(String key, bool on) async {
    final kinds = asMaps(_d?['kinds']);
    setState(() {
      for (final k in kinds) {
        if (k['key'] == key) k['on'] = on;
      }
      _d = {...?_d, 'kinds': kinds};
    });
    try {
      final d = await Api.I.post('/api/mobile/v1/notifications/', {
        'prefs': {key: on},
      });
      if (mounted) setState(() => _d = d);
    } on ApiError catch (e) {
      _toast(e.message, error: true);
      _load();
    }
  }

  Future<void> _allow() async {
    if (_perm == 'denied') {
      if (!await Push.openPhoneSettings()) _toast('Open Settings → Apps → TapTap → Notifications on your phone.');
      return;
    }
    await Push.register();
    await _load();
  }

  Future<void> _test() async {
    setState(() => _testing = true);
    try {
      final d = await Api.I.post('/api/mobile/v1/notifications/', {'test': true});
      _toast('${d['message'] ?? 'Sent.'}');
    } on ApiError catch (e) {
      _toast(e.message, error: true);
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final d = _d;
    final cs = Theme.of(context).colorScheme;
    final server = d?['server'] == true;
    final registered = d?['registered'] == true;
    final ready = _perm == 'allowed' && server && registered;
    final kinds = asMaps(d?['kinds']);
    return Scaffold(
      appBar: AppBar(title: const Text('Notifications')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 0, 16, 40), children: [
          if (_err != null) ...[ErrorCard(message: _err!, onRetry: _load), const SizedBox(height: 12)],
          if (d == null && _err == null) const SkeletonCard(height: 180),
          if (d != null) ...[
            Card(
              color: ready ? const Color(0xFF16A34A).withValues(alpha: .12) : const Color(0xFFF59E0B).withValues(alpha: .14),
              child: ListTile(
                leading: Icon(ready ? Icons.notifications_active : Icons.notifications_paused,
                    color: ready ? const Color(0xFF16A34A) : const Color(0xFFB45309)),
                title: Text(ready ? 'Notifications reach this phone' : 'Notifications are not fully set up',
                    style: const TextStyle(fontWeight: FontWeight.w800)),
                subtitle: Text(ready
                    ? 'Calls ring and alerts pop up even when TapTap is closed.'
                    : 'Until then, TapTap shows calls and messages only while it is open.'),
              ),
            ),
            const SizedBox(height: 12),
            SectionCard(
              title: 'What has to be ready',
              child: Column(children: [
                _Check(
                  ok: _perm == 'allowed',
                  title: 'This phone allows TapTap notifications',
                  text: switch (_perm) {
                    'allowed' => 'Allowed.',
                    'denied' => 'Turned off in the phone’s settings.',
                    'off' => 'This copy of the app was built without Firebase (google-services.json), so nothing can be '
                        'pushed to it. Whoever builds the app adds it — mobile/README.md, part 2.',
                    _ => 'Not allowed yet.',
                  },
                  action: _perm == 'denied' || _perm == 'ask' ? TextButton(onPressed: _allow, child: Text(_perm == 'denied' ? 'Turn on' : 'Allow')) : null,
                ),
                _Check(
                  ok: server,
                  title: 'Your TapTap server can send',
                  text: server
                      ? 'Set up.'
                      : 'Not yet: the server needs its Firebase key (FCM_SERVICE_ACCOUNT in .env) — mobile/README.md, part 2.',
                ),
                _Check(
                  ok: registered,
                  title: 'TapTap knows this phone',
                  text: registered ? 'Registered.' : 'Not yet — allow notifications, then close and reopen the app.',
                ),
              ]),
            ),
            const SizedBox(height: 12),
            SectionCard(
              title: 'What this phone gets',
              child: Column(children: [
                for (final k in kinds)
                  SwitchListTile(
                    contentPadding: EdgeInsets.zero,
                    title: Text('${k['label']}', style: const TextStyle(fontWeight: FontWeight.w700)),
                    subtitle: Text(k['key'] == 'calls' && k['on'] != true
                        ? 'Off: calls only ring while TapTap is open on this phone.'
                        : '${k['text']}'),
                    value: k['on'] == true,
                    onChanged: (on) => _toggle('${k['key']}', on),
                  ),
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text('Only for this phone. Your other phones and the website keep their own settings.',
                      style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
                ),
              ]),
            ),
            const SizedBox(height: 12),
            FilledButton.tonalIcon(
              onPressed: _testing || !server || !registered ? null : _test,
              icon: _testing
                  ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.send, size: 18),
              label: const Text('Send a test notification'),
            ),
            const SizedBox(height: 4),
            Text('Tip: send it, then leave the app — it should pop up at the top of the screen.',
                textAlign: TextAlign.center, style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
            const SizedBox(height: 12),
            Card(
              child: ListTile(
                leading: const Icon(Icons.tune),
                title: const Text('Pop-up, sound and lock screen', style: TextStyle(fontWeight: FontWeight.w700)),
                subtitle: Text(Platform.isAndroid
                    ? 'In the phone’s settings each kind is its own category — keep “Pop on screen” on for the ones you want to see at once.'
                    : 'Choose banners, sounds and the lock screen in Settings → TapTap → Notifications.'),
                trailing: const Icon(Icons.open_in_new),
                onTap: () async {
                  if (!await Push.openPhoneSettings()) _toast('Open your phone’s Settings → TapTap → Notifications.');
                },
              ),
            ),
          ],
        ]),
      ),
    );
  }
}

class _Check extends StatelessWidget {
  const _Check({required this.ok, required this.title, required this.text, this.action});
  final bool ok;
  final String title, text;
  final Widget? action;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Icon(ok ? Icons.check_circle : Icons.error_outline, color: ok ? const Color(0xFF16A34A) : const Color(0xFFD97706)),
          const SizedBox(width: 10),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(title, style: const TextStyle(fontWeight: FontWeight.w700)),
              Text(text, style: TextStyle(fontSize: 13, color: Theme.of(context).colorScheme.onSurfaceVariant)),
            ]),
          ),
          if (action != null) action!,
        ]),
      );
}
