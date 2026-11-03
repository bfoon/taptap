import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../app/state.dart';
import '../calls/call_engine.dart';
import '../calls/call_screen.dart';
import '../push/push.dart';
import 'chat_screens.dart';
import 'complaints_screen.dart';
import 'fit_screen.dart';
import 'more_screen.dart';
import 'today_screen.dart';

/// Tabs (by meaning, so goTab() works whatever the role hides): today · complaints · chat · fit · more.
enum AppTab { today, complaints, chat, fit, more }

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
    Push.register();
    Push.acceptedAtLaunch().then((extra) {
      if (extra != null && extra['cid'] != null) Push.onAccept?.call('${extra['cid']}', '${extra['from'] ?? 'Call'}', extra['video'] == true);
    });
  }

  void _ring(Ring r) {
    final ctx = navigatorKey.currentContext;
    if (ctx == null) return;
    Navigator.of(ctx, rootNavigator: true).push(MaterialPageRoute(fullscreenDialog: true, builder: (_) => IncomingCallScreen(ring: r)));
  }

  void _openFromPush(Map<String, dynamic> d) {
    switch (d['type']) {
      case 'chat':
        final ctx = navigatorKey.currentContext;
        if (ctx != null) openThread(ctx, int.tryParse('${d['thread']}') ?? 0, 'Chat');
        break;
      case 'complaint':
        goTab(AppTab.complaints);
        break;
      case 'missed_call':
        goTab(AppTab.more);
        break;
    }
  }

  List<AppTab> _tabs(AppState app) => [
        AppTab.today,
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
      body: IndexedStack(index: idx, children: [for (final t in tabs) _page(t)]),
      bottomNavigationBar: NavigationBar(
        selectedIndex: idx,
        onDestinationSelected: (i) => setState(() => _tab = tabs[i]),
        destinations: [
          for (final t in tabs)
            switch (t) {
              AppTab.today => const NavigationDestination(icon: Icon(Icons.insights_outlined), selectedIcon: Icon(Icons.insights), label: 'Today'),
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
    );
  }
}
