import 'dart:io';

import 'package:flutter/material.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../push/push.dart';
import '../widgets/common.dart';

/// Notifications on this phone: can they reach it at all (this build of the app, the TapTap server, the phone's
/// permission), which kinds it gets (saved on the server for this phone), a test, and the phone's own settings
/// for pop-up, sound and lock screen.
///
/// Check delivery sends one real test from the TapTap server through Google back to this phone and lights up each
/// stop on the way — server key, this app's Firebase, the push address, the same Firebase project on both ends,
/// Google's answer, the arrival (timed), and finally whether the phone shows it and lets TapTap receive while closed.
/// Where it stops, it says why in plain words and offers the fix.
class NotificationsScreen extends StatefulWidget {
  const NotificationsScreen({super.key});
  @override
  State<NotificationsScreen> createState() => _NotificationsScreenState();
}

class _NotificationsScreenState extends State<NotificationsScreen> with WidgetsBindingObserver {
  Map<String, dynamic>? _d;
  String? _err;
  String _perm = '';
  bool _bgBlocked = false;                         // Android battery saver may stop TapTap in the background
  bool _testing = false;
  List<DeliveryStop>? _road;                              // Check delivery: the stops, lit as the test push passes them
  bool _checking = false;
  String? _verdict;                                // one line under the road when the check is done
  bool _verdictOk = false;

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
    final bg = await Push.backgroundBlocked();
    if (mounted) setState(() => _bgBlocked = bg);
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
    final p = await Push.turnOn();                 // the prompt, or the phone's settings when it no longer asks
    if (p != 'allowed' && mounted) _toast('Open Settings → Apps → TapTap → Notifications and turn them on.');
    await _load();
  }

  Future<void> _test() async {
    setState(() => _testing = true);
    try {
      final d = await Api.I.post('/api/mobile/v1/notifications/', {'test': true});
      _toast('${d['message'] ?? 'Sent.'}');
    } on ApiError catch (e) {
      _toast(e.message, error: true);              // Google's own answer, in plain words (server: mobile_push.verdict)
    } finally {
      if (mounted) setState(() => _testing = false);
      _load();
    }
  }

  // ─────────────────────────────── Check delivery ───────────────────────────────

  List<DeliveryStop> _newRoad() => [
        DeliveryStop('Your TapTap server can send'),
        DeliveryStop('This copy of the app can receive'),
        DeliveryStop('TapTap has this phone’s push address'),
        DeliveryStop('Same Firebase project on both ends'),
        DeliveryStop('Google accepted a test push'),
        DeliveryStop('It arrived on this phone'),
        DeliveryStop('This phone shows TapTap notifications'),
        if (Platform.isAndroid) DeliveryStop('It also arrives while TapTap is closed'),
      ];

  Future<void> _check() async {
    if (_checking) return;
    final road = _newRoad();
    setState(() {
      _road = road;
      _checking = true;
      _verdict = null;
    });
    try {
      await _drive(road);
    } finally {
      if (mounted) {
        DeliveryStop? stopAt;
        for (final r in road) {
          if (r.state == StopState.fail) {
            stopAt = r;
            break;
          }
        }
        final arrived = road.length > 5 && road[5].state == StopState.ok;
        final amber = road.any((r) => r.state == StopState.warn);
        final String? verdict = stopAt != null
            ? 'Stops at “${stopAt.title}”. Fix that, then check again.'
            : arrived
                ? 'Pushes reach this phone.${amber ? ' Look at the amber notes too.' : ''}'
                : null;
        final ok = stopAt == null && arrived;
        setState(() {
          _checking = false;
          _verdictOk = ok;
          _verdict = verdict;
        });
      }
      _load();
    }
  }

  /// Walk the road: each stop lights up in turn. After a stop fails the rest of the road waits for it — except
  /// the phone's own settings, which are checked anyway (they are on the phone, not on the road).
  Future<void> _drive(List<DeliveryStop> road) async {
    var open = true;
    Future<void> stop(int i, Future<StopState> Function(DeliveryStop s) run, {bool always = false}) async {
      if (!mounted) return;
      final s = road[i];
      if (!open && !always) {
        setState(() {
          s.state = StopState.skipped;
          s.text = 'Waits for the stop above.';
        });
        return;
      }
      setState(() => s.state = StopState.running);
      StopState st;
      try {
        st = await run(s);
      } on ApiError catch (e) {
        s.text = e.message;
        st = StopState.fail;
      } catch (e) {
        s.text = '$e';
        st = StopState.fail;
      }
      if (!mounted) return;
      setState(() => s.state = st);
      if (st == StopState.fail && !always) open = false;
      await Future<void>.delayed(const Duration(milliseconds: 180));     // so each stop visibly lights up in turn
    }

    Map<String, dynamic>? reg;
    await stop(0, (s) async {
      final d = await Api.I.get('/api/mobile/v1/notifications/');
      if (d['server'] == true) {
        s.text = 'Its key is for Firebase project “${d['server_project'] ?? ''}”.';
        return StopState.ok;
      }
      s.text = 'The server has no Firebase key yet: FCM_SERVICE_ACCOUNT in .env, then restart (mobile/README.md, part 2).';
      return StopState.fail;
    });
    await stop(1, (s) async {
      if (Push.ready) {
        s.text = 'Built with Firebase project “${Push.projectId}”.';
        return StopState.ok;
      }
      s.text = 'This copy was built without Firebase (google-services.json), so nothing can be pushed to it. Whoever '
          'builds the app sets the GOOGLE_SERVICES_JSON secret (mobile/README.md, part 2).';
      return StopState.fail;
    });
    await stop(2, (s) async {
      reg = await Push.registerNow();
      s.text = 'Sent just now.';
      return StopState.ok;
    });
    await stop(3, (s) async {
      final m = reg?['project_match'];
      final mine = Push.projectId, theirs = '${reg?['server_project'] ?? ''}';
      if (m == false) {
        s.text = 'The app is from “$mine”, the server key from “$theirs”. Google refuses every push until both come '
            'from the same Firebase project: rebuild the app with that project’s google-services.json, or change '
            'FCM_SERVICE_ACCOUNT on the server.';
        return StopState.fail;
      }
      if (m == true) {
        s.text = 'Both use “$mine”.';
        return StopState.ok;
      }
      s.text = 'Could not compare (the server is older than this app). The next stop will tell.';
      return StopState.warn;
    });
    final nonce = Push.newNonce();
    final back = Push.echo(nonce, const Duration(seconds: 25));       // listen before asking, it can be quick
    await stop(4, (s) async {
      await Api.I.post('/api/mobile/v1/notifications/', {'test': true, 'nonce': nonce});
      s.text = 'Google took it for delivery.';
      return StopState.ok;
    });
    await stop(5, (s) async {
      setState(() => s.text = 'On its way…');
      final took = await back;
      if (took != null) {
        s.text = 'Round trip ${(took.inMilliseconds / 1000).toStringAsFixed(1)} s, from your server through Google to here.';
        return StopState.ok;
      }
      s.text = 'Google took it, but nothing arrived within 25 s. Usually the phone’s internet blocks Google’s push port '
          '(5228), data saver blocks TapTap, or Google Play services is off or missing on this phone.';
      return StopState.fail;
    });
    await stop(6, (s) async {
      final p = await Push.permission();
      if (p != 'allowed') {
        s.text = 'Pushes arrive, but the phone hides them: notifications for TapTap are off.';
        s.fixLabel = 'Turn on';
        s.fix = () async {
          await Push.turnOn();
          await _check();
        };
        return StopState.fail;
      }
      final off = await Push.blockedKinds();
      if (off.isNotEmpty) {
        s.text = 'Allowed, but these kinds are switched off in the phone’s settings: ${off.join(', ')}.';
        s.fixLabel = 'Open settings';
        s.fix = () async {
          await Push.openPhoneSettings();
        };
        return StopState.warn;
      }
      s.text = 'Allowed, every kind on.';
      return StopState.ok;
    }, always: true);
    if (Platform.isAndroid) {
      await stop(7, (s) async {
        if (!await Push.backgroundBlocked()) {
          s.text = 'The battery saver lets TapTap receive while it is closed.';
          return StopState.ok;
        }
        s.text = 'The battery saver may stop TapTap when it is closed, and then pushes wait until you open it. Allow it; '
            'on Tecno, Infinix, itel and Xiaomi also turn on Auto-start for TapTap.';
        s.fixLabel = 'Allow';
        s.fix = () async {
          if (!await Push.allowBackground()) _toast('Open Settings → Battery → TapTap and choose “Unrestricted”.');
        };
        return StopState.warn;
      }, always: true);
    }
  }

  @override
  Widget build(BuildContext context) {
    final d = _d;
    final cs = Theme.of(context).colorScheme;
    final server = d?['server'] == true;
    final registered = d?['registered'] == true;
    final mismatch = d?['project_match'] == false;   // app and server key from different Firebase projects
    final ready = _perm == 'allowed' && server && registered && !mismatch;
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
                  action: registered || !Push.ready
                      ? null
                      : TextButton(
                          onPressed: () async {
                            await Push.register();
                            await _load();
                          },
                          child: const Text('Try again')),
                ),
                if (mismatch)
                  _Check(
                    ok: false,
                    title: 'Same Firebase project on both ends',
                    text: 'The app is from “${d['app_project']}”, the server key from “${d['server_project']}”. Google refuses '
                        'every push until both come from the same Firebase project.',
                  ),
                if (d['last'] is Map)
                  _Check(
                    ok: d['last']['ok'] == true,
                    title: 'Last push to this phone',
                    text: d['last']['ok'] == true
                        ? 'Google accepted it ${ago(d['last']['at'])}.'
                        : '${ago(d['last']['at'])}: ${d['last']['reason'] ?? ''} ${d['last']['fix'] ?? ''}'.trim(),
                  ),
                if (Platform.isAndroid)
                  _Check(
                    ok: !_bgBlocked,
                    title: 'TapTap may run in the background',
                    text: _bgBlocked
                        ? 'The battery saver can stop TapTap when it is closed — then calls do not ring and notifications '
                            'do not arrive. Allow it (on some phones also turn on “Auto-start” for TapTap).'
                        : 'Allowed — calls and notifications reach TapTap while it is closed.',
                    action: _bgBlocked
                        ? TextButton(
                            onPressed: () async {
                              if (!await Push.allowBackground()) _toast('Open Settings → Battery → TapTap and choose “Unrestricted”.');
                            },
                            child: const Text('Allow'))
                        : null,
                  ),
              ]),
            ),
            const SizedBox(height: 12),
            SectionCard(
              title: 'Check delivery',
              child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                Text('Sends one test from your TapTap server through Google to this phone, and shows where it stops.',
                    style: TextStyle(fontSize: 13, color: cs.onSurfaceVariant)),
                if (_road != null) ...[
                  const SizedBox(height: 16),
                  for (var i = 0; i < _road!.length; i++)
                    DeliveryStopRow(stop: _road![i], last: i == _road!.length - 1, passed: _road![i].state == StopState.ok || _road![i].state == StopState.warn),
                  if (_verdict != null)
                    Container(
                      margin: const EdgeInsets.only(top: 14),
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
                      decoration: BoxDecoration(
                        color: (_verdictOk ? const Color(0xFF16A34A) : cs.error).withValues(alpha: .1),
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: Text(_verdict!,
                          style: TextStyle(fontWeight: FontWeight.w700, color: _verdictOk ? const Color(0xFF15803D) : cs.error)),
                    ),
                ],
                const SizedBox(height: 14),
                FilledButton.icon(
                  onPressed: _checking ? null : _check,
                  icon: _checking
                      ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.alt_route, size: 18),
                  label: Text(_checking ? 'Checking…' : _road == null ? 'Check delivery' : 'Check again'),
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

/// One stop on the delivery road.
enum StopState { waiting, running, ok, warn, fail, skipped }

class DeliveryStop {
  DeliveryStop(this.title);
  final String title;
  StopState state = StopState.waiting;
  String text = '';
  String? fixLabel;
  Future<void> Function()? fix;
}

/// A stop: a dot on the road (empty → spinning → ✓ / ! / ✕), the stretch of road under it turning green once the
/// push has passed, and what was found — with the fix when there is one.
class DeliveryStopRow extends StatelessWidget {
  const DeliveryStopRow({required this.stop, required this.last, required this.passed});
  final DeliveryStop stop;
  final bool last, passed;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    const green = Color(0xFF16A34A);
    final color = switch (stop.state) {
      StopState.ok => green,
      StopState.warn => const Color(0xFFD97706),
      StopState.fail => cs.error,
      StopState.running => cs.primary,
      _ => cs.outline,
    };
    final filled = stop.state == StopState.ok || stop.state == StopState.warn || stop.state == StopState.fail;
    final Widget glyph = switch (stop.state) {
      StopState.running => SizedBox(width: 12, height: 12, child: CircularProgressIndicator(strokeWidth: 2, color: color)),
      StopState.ok => const Icon(Icons.check, size: 15, color: Colors.white),
      StopState.warn => const Icon(Icons.priority_high, size: 15, color: Colors.white),
      StopState.fail => const Icon(Icons.close, size: 15, color: Colors.white),
      _ => const SizedBox.shrink(),
    };
    final muted = stop.state == StopState.waiting || stop.state == StopState.skipped;
    return IntrinsicHeight(
      child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SizedBox(
          width: 24,
          child: Column(children: [
            AnimatedContainer(
              duration: const Duration(milliseconds: 220),
              width: 24,
              height: 24,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: filled ? color : cs.surface,
                border: Border.all(color: color, width: 2),
              ),
              child: Center(child: glyph),
            ),
            if (!last)
              Expanded(
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 320),
                  width: 3,
                  margin: const EdgeInsets.symmetric(vertical: 3),
                  decoration: BoxDecoration(
                    color: passed ? green : cs.outlineVariant,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
          ]),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Padding(
            padding: EdgeInsets.only(top: 2, bottom: last ? 0 : 14),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(stop.title,
                  style: TextStyle(fontWeight: FontWeight.w700, color: muted ? cs.onSurfaceVariant : null)),
              if (stop.text.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(stop.text, style: TextStyle(fontSize: 13, color: cs.onSurfaceVariant)),
                ),
              if (stop.fix != null && stop.fixLabel != null && (stop.state == StopState.fail || stop.state == StopState.warn))
                TextButton(
                  style: TextButton.styleFrom(padding: EdgeInsets.zero, minimumSize: const Size(0, 36),
                      tapTargetSize: MaterialTapTargetSize.shrinkWrap),
                  onPressed: () {
                    stop.fix?.call();
                  },
                  child: Text(stop.fixLabel!),
                ),
            ]),
          ),
        ),
      ]),
    );
  }
}
