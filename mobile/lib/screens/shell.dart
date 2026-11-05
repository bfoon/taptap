import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app/api.dart';
import '../app/state.dart';
import '../calls/call_engine.dart';
import '../calls/call_screen.dart';
import '../push/push.dart';
import '../widgets/offline_strip.dart';
import '../widgets/push_banner.dart';
import 'chat_screens.dart';
import 'complaints_screen.dart';
import 'fit_screen.dart';
import 'more_screen.dart';
import 'today_screen.dart';
import 'voucher_detail_screen.dart';
import 'vouchers_screen.dart';

/// Tabs (by meaning, so goTab() works whatever the role hides): today · vouchers · complaints · chat · fit · more.
enum AppTab { today, vouchers, complaints, chat, fit, more }

class Shell extends StatefulWidget {
  const Shell({super.key});
  @override
  State<Shell> createState() => _ShellState();
}

class _ShellState extends State<Shell> {
  AppTab _tab = AppTab.today;
  final _fitQuestion = ValueNotifier<String?>(null);

  @override
  void initState() {
    super.initState();
    final app = context.read<AppState>();
    app.onRing = _ring;
    Push.onAccept = (cid, from, video) {
      final ctx = navigatorKey.currentContext;
      if (ctx != null) openCall(ctx, CallEngine.incoming(id: cid, from: from, video: video));
    };
    Push.onOpen = _openFromPush;
    Push.onBanner = _banner;
    Push.onRing = app.ringFromPush;
    Push.onRingEnded = app.ringEnded;
    Push.register().then((_) => _afterRegister());
    Push.acceptedAtLaunch().then((extra) {
      if (extra != null && extra['cid'] != null) Push.onAccept?.call('${extra['cid']}', '${extra['from'] ?? 'Call'}', extra['video'] == true);
    });
  }

  /// After start: notifications switched off on the phone come first (they silently hide every push — a phone can
  /// be stuck that way from an older build that could not show the prompt); then the battery saver.
  Future<void> _afterRegister() async {
    if (!mounted || !Push.ready) return;
    if (await Push.permission() == 'allowed') return _askBackground();
    if (Push.justAsked || !await Push.nudgeDue() || !mounted) return;   // they just answered the prompt themselves
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    final yes = await showDialog<bool>(
      context: ctx,
      builder: (c) => AlertDialog(
        icon: const Icon(Icons.notifications_off_outlined),
        title: const Text('Notifications are off for TapTap'),
        content: const Text('While TapTap is closed, this phone stays silent for calls, customer complaints and '
            'messages.\n\nTurn notifications on?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Not now')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Turn on')),
        ],
      ),
    );
    if (yes == true) await Push.turnOn();
  }

  /// Once: phones that stop apps in the background to save battery also drop TapTap's calls and notifications.
  Future<void> _askBackground() async {
    if (!await Push.shouldAskBackground() || !mounted) return;
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    final yes = await showDialog<bool>(
      context: ctx,
      builder: (c) => AlertDialog(
        icon: const Icon(Icons.battery_alert),
        title: const Text('Let TapTap ring when closed'),
        content: const Text('Your phone’s battery saver can stop TapTap in the background — then calls do not ring and '
            'notifications do not arrive until you open the app.\n\nAllow TapTap to run in the background?'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Not now')),
          FilledButton(onPressed: () => Navigator.pop(c, true), child: const Text('Allow')),
        ],
      ),
    );
    if (yes == true) await Push.allowBackground();
  }

  void _ring(Ring r) {
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    Navigator.of(ctx, rootNavigator: true).push(MaterialPageRoute(fullscreenDialog: true, builder: (_) => IncomingCallScreen(ring: r)));
  }

  /// A notification tapped (or its in-app banner): open what it is about.
  void _openFromPush(Map<String, dynamic> d) {
    final ctx = navigatorKey.currentContext;
    final app = context.read<AppState>();
    switch (d['type']) {
      case 'chat':
        if (ctx != null) openThread(ctx, int.tryParse('${d['thread']}') ?? 0, 'Chat');
        break;
      case 'complaint':
        goTab(AppTab.complaints);
        break;
      case 'missed_call':
        goTab(AppTab.more);
        break;
      case 'alert':
        _openLink('${d['link'] ?? ''}');
        break;
      case 'vouchers':
        final v = int.tryParse('${d['voucher'] ?? ''}');
        final b = int.tryParse('${d['batch'] ?? ''}');
        if (ctx != null && app.canDo('vouchers') && v != null) {
          openVoucher(ctx, v);
        } else if (ctx != null && app.canDo('vouchers') && b != null) {
          openBatch(ctx, b, 'New batch');
        } else {
          goTab(AppTab.vouchers);
        }
        break;
    }
  }

  /// An alert's website link (/vouchers/12/, /batches/3/, /routers/…): open it in the app when the app has that
  /// page, else on the website.
  void _openLink(String link) {
    final ctx = navigatorKey.currentContext;
    final app = context.read<AppState>();
    final v = RegExp(r'^/vouchers/(\d+)/').firstMatch(link);
    final b = RegExp(r'^/batches/(\d+)/').firstMatch(link);
    if (ctx != null && app.canDo('vouchers') && v != null) {
      openVoucher(ctx, int.parse(v.group(1)!));
    } else if (ctx != null && app.canDo('vouchers') && b != null) {
      openBatch(ctx, int.parse(b.group(1)!), 'Batch');
    } else if (app.canDo('vouchers') && (link.startsWith('/vouchers') || link.startsWith('/plans') || link.startsWith('/batches'))) {
      goTab(AppTab.vouchers);
    } else if (link.startsWith('/') && !link.startsWith('//')) {
      launchUrl(Uri.parse('${Api.I.server}$link'), mode: LaunchMode.externalApplication);
    }
  }

  /// A push while the app is open: pop it up at the top (not a chat you are reading right now).
  void _banner(String title, String body, Map<String, dynamic> d) {
    final app = context.read<AppState>();
    final type = '${d['type'] ?? ''}';
    app.sync.nudge(type);                             // pull what it is about now, so the screen already shows it
    if (type == 'chat' && app.openThread != null && '${app.openThread}' == '${d['thread']}') return;
    IconData icon = Icons.notifications_active;
    Color? color;
    switch (type) {
      case 'chat':
        icon = Icons.chat_bubble;
      case 'complaint':
        icon = Icons.report;
        color = const Color(0xFFDC2626);
      case 'missed_call':
        icon = Icons.phone_missed;
        color = const Color(0xFFDC2626);
      case 'vouchers':
        icon = Icons.confirmation_number;
        color = const Color(0xFF16A34A);
      case 'test':
        icon = Icons.check_circle;
        color = const Color(0xFF16A34A);
    }
    PushBanner.show(title: title, body: body, icon: icon, color: color, onTap: type == 'test' ? null : () => _openFromPush(d));
  }

  List<AppTab> _tabs(AppState app) => [
        AppTab.today,
        if (app.canDo('vouchers')) AppTab.vouchers,
        if (app.canDo('complaints')) AppTab.complaints,
        AppTab.chat,
        if (app.canDo('fit')) AppTab.fit,
        AppTab.more,
      ];

  void goTab(AppTab t) {
    final app = context.read<AppState>();
    if (_tabs(app).contains(t)) setState(() => _tab = t);
  }

  void askFit(String q) {
    final app = context.read<AppState>();
    if (!app.canDo('fit')) return;
    setState(() => _tab = AppTab.fit);
    _fitQuestion.value = q;
  }

  Widget _page(AppTab t) {
    switch (t) {
      case AppTab.today:
        return TodayScreen(goTab: (i) => goTab(const [AppTab.today, AppTab.complaints, AppTab.chat, AppTab.fit, AppTab.more][i]), askFit: askFit);
      case AppTab.vouchers:
        return const VouchersScreen();
      case AppTab.complaints:
        return ComplaintsScreen(askFit: askFit);
      case AppTab.chat:
        return const ChatListScreen();
      case AppTab.fit:
        return FitScreen(question: _fitQuestion);
      case AppTab.more:
        return const MoreScreen();
    }
  }

  @override
  Widget build(BuildContext context) {
    final app = context.watch<AppState>();
    final tabs = _tabs(app);
    if (!tabs.contains(_tab)) _tab = AppTab.today;
    final idx = tabs.indexOf(_tab);
    return Scaffold(
      extendBody: true,
      // keyed by business: switching business starts every tab fresh, from that business's own saved pages
      body: KeyedSubtree(
        key: ValueKey('business-${app.business['id']}'),
        child: IndexedStack(index: idx, children: [for (final t in tabs) _page(t)]),
      ),
      bottomNavigationBar: Column(mainAxisSize: MainAxisSize.min, children: [
        const OfflineStrip(),
        NavigationBar(
          selectedIndex: idx,
          // six tabs (owner / admin) do not fit with every label on a small phone: show the open tab's label only
          labelBehavior: tabs.length > 5 ? NavigationDestinationLabelBehavior.onlyShowSelected : null,
          onDestinationSelected: (i) => setState(() => _tab = tabs[i]),
          destinations: [
            for (final t in tabs)
              switch (t) {
                AppTab.today => const NavigationDestination(icon: Icon(Icons.insights_outlined), selectedIcon: Icon(Icons.insights), label: 'Today'),
                AppTab.vouchers => const NavigationDestination(
                    icon: Icon(Icons.confirmation_number_outlined), selectedIcon: Icon(Icons.confirmation_number), label: 'Vouchers'),
                AppTab.complaints => const NavigationDestination(
                    icon: Icon(Icons.report_gmailerrorred_outlined), selectedIcon: Icon(Icons.report), label: 'Complaints'),
                AppTab.chat => NavigationDestination(
                    icon: Badge(isLabelVisible: app.unread > 0, label: Text('${app.unread}'), child: const Icon(Icons.chat_bubble_outline)),
                    selectedIcon: const Icon(Icons.chat_bubble),
                    label: 'Chat'),
                AppTab.fit => const NavigationDestination(icon: Icon(Icons.auto_awesome_outlined), selectedIcon: Icon(Icons.auto_awesome), label: 'Fit'),
                AppTab.more => const NavigationDestination(icon: Icon(Icons.menu), label: 'More'),
              },
          ],
        ),
      ]),
    );
  }
}
