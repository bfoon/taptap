import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../calls/call_engine.dart';
import '../calls/call_screen.dart';
import '../data/store.dart';
import '../data/sync_engine.dart';
import '../push/push.dart';
import '../widgets/common.dart';
import 'notifications_screen.dart';
import 'quick_sell.dart';
import 'router_schedules_screen.dart';
import 'routers_screen.dart';

class MoreScreen extends StatefulWidget {
  const MoreScreen({super.key});
  @override
  State<MoreScreen> createState() => _MoreScreenState();
}

class _MoreScreenState extends State<MoreScreen> {
  List<Map>? _calls;
  List<Map>? _callers;                             // who called: phone, voucher, MAC address; "Call back" rings their page
  List<Map>? _devices;
  StreamSubscription<String>? _saved;

  @override
  void initState() {
    super.initState();
    _calls = _list(Store.I.peek(Pages.customerCalls), 'calls');     // saved on the phone: shown at once
    _callers = _list(Store.I.peek(Pages.customerCalls), 'callers');
    _devices = _list(Store.I.peek(Pages.devices), 'devices');
    _saved = Store.I.changes.listen((key) {
      if (!mounted || (key != Pages.customerCalls && key != Pages.devices)) return;
      final d = Store.I.peekKey(key);
      if (d == null) return;
      setState(() {
        if (key == Pages.customerCalls) {
          _calls = _list(d, 'calls') ?? _calls;
          _callers = _list(d, 'callers') ?? _callers;
        } else {
          _devices = _list(d, 'devices') ?? _devices;
        }
      });
    });
    _load();
  }

  @override
  void dispose() {
    _saved?.cancel();
    super.dispose();
  }

  static List<Map>? _list(Map<String, dynamic>? d, String name) => d?[name] is List ? (d![name] as List).whereType<Map>().toList() : null;

  Future<void> _load() async {
    try {
      final r = await Future.wait([Store.I.fetch(Pages.customerCalls), Store.I.fetch(Pages.devices)]);
      if (!mounted) return;
      setState(() {
        _calls = _list(r[0], 'calls') ?? _calls;
        _callers = _list(r[0], 'callers') ?? _callers;
        _devices = _list(r[1], 'devices') ?? _devices;
      });
    } catch (_) {}
  }

  static String _norm(String s) => s.trim().replaceAll(RegExp(r'\s+'), ' ').toLowerCase();

  /// Leave the current business: what happens, then type its name.
  Future<void> _leave(AppState app) async {
    final name = '${app.business['name'] ?? ''}';
    final ctl = TextEditingController();
    final messenger = ScaffoldMessenger.of(context);
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => StatefulBuilder(
        builder: (c, set) => AlertDialog(
          icon: const Icon(Icons.door_front_door_outlined),
          title: Text('Leave $name?'),
          content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Text('You lose access to this business straight away. Your login and your other businesses stay. '
                'Nothing is deleted — the owner is told and can let you back in.'),
            const SizedBox(height: 12),
            TextField(
              controller: ctl,
              onChanged: (_) => set(() {}),
              decoration: InputDecoration(labelText: 'Type “$name” to confirm'),
            ),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Stay')),
            FilledButton(
              style: FilledButton.styleFrom(backgroundColor: Theme.of(c).colorScheme.error),
              onPressed: _norm(ctl.text) == _norm(name) ? () => Navigator.pop(c, true) : null,
              child: const Text('Leave'),
            ),
          ],
        ),
      ),
    );
    final typed = ctl.text;
    ctl.dispose();
    if (ok != true) return;
    try {
      final msg = await app.leaveBusiness(typed);
      messenger.showSnackBar(SnackBar(content: Text(msg)));
    } on ApiError catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final app = context.watch<AppState>();
    final b = app.business;
    final businesses = (app.me?['businesses'] as List? ?? []).cast<Map>();
    final cs = Theme.of(context).colorScheme;
    return Scaffold(
      appBar: AppBar(title: const Text('More')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 0, 16, 110), children: [
          Card(
            child: ListTile(
              contentPadding: const EdgeInsets.all(14),
              leading: Avatar(text: '${app.user['initials'] ?? ''}', radius: 26, color: app.brand),
              title: Text('${app.user['name']}', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 17)),
              subtitle: Text('${b['role']} · ${b['name']}'),
            ),
          ),
          if (app.canDo('routers') || app.canDo('vouchers_create')) ...[
            const SizedBox(height: 12),
            Card(
              child: Column(children: [
                if (app.canDo('routers'))
                  ListTile(
                    leading: const Icon(Icons.router_outlined),
                    title: const Text('Routers', style: TextStyle(fontWeight: FontWeight.w700)),
                    subtitle: const Text('Each MikroTik on its own tab: who is online, sync, reboot, move'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const RoutersScreen())),
                  ),
                if (app.canDo('routers'))
                  ListTile(
                    leading: const Icon(Icons.event_repeat),
                    title: const Text('Router schedules', style: TextStyle(fontWeight: FontWeight.w700)),
                    subtitle: const Text('Restart or shut down on a timetable, with a heads-up and “back online”'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => openRouterSchedules(context),
                  ),
                if (app.canDo('vouchers_create'))
                  ListTile(
                    leading: const Icon(Icons.bolt, color: Color(0xFFF59E0B)),
                    title: const Text('Quick sell', style: TextStyle(fontWeight: FontWeight.w700)),
                    subtitle: const Text('One paid voucher for the customer in front of you'),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () => showQuickSell(context),
                  ),
              ]),
            ),
          ],
          const SizedBox(height: 12),
          if (app.canDo('customer_calls'))
            const InfoCard(
              icon: Icons.support_agent,
              title: 'You answer customer calls',
              text: 'Customers who tap Call on your Wi-Fi page ring this phone. Keep notifications on.',
            ),
          if (app.canDo('customer_calls')) const SizedBox(height: 12),
          if ((_callers ?? const []).isNotEmpty) ...[
            SectionCard(
              title: 'Customers who called',
              child: Column(children: [
                for (final c in _callers!.take(15))
                  ListTile(
                    contentPadding: EdgeInsets.zero,
                    isThreeLine: true,
                    leading: Icon(c['here'] == true ? Icons.wifi_calling_3 : Icons.person_outline,
                        color: c['here'] == true ? const Color(0xFF16A34A) : cs.onSurfaceVariant),
                    title: Text('${'${c['name'] ?? ''}'.isEmpty ? 'Wi-Fi customer' : c['name']}${'${c['phone'] ?? ''}'.isEmpty ? '' : ' · ${c['phone']}'}',
                        style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w700)),
                    subtitle: Text(
                        [
                          if ('${c['line'] ?? ''}'.isNotEmpty) '${c['line']}',
                          if ('${c['why'] ?? ''}'.isNotEmpty) '💡 ${c['why']}',
                          '${c['here'] == true ? 'On the Wi-Fi page now · ' : ''}${ago(c['at'])}',
                        ].join('\n'),
                        style: const TextStyle(fontSize: 12.5)),
                    trailing: c['can_ring'] == true
                        ? IconButton(
                            tooltip: 'Call back on the Wi-Fi page',
                            icon: const Icon(Icons.phone_callback, color: Color(0xFF16A34A)),
                            onPressed: () => openCall(
                                context,
                                CallEngine.outgoing(
                                    thread: 0,
                                    title: '${'${c['name'] ?? ''}'.isEmpty ? 'Wi-Fi customer' : c['name']}',
                                    customer: toI(c['id']))),
                          )
                        : null,
                  ),
              ]),
            ),
            const SizedBox(height: 12),
          ],
          SectionCard(
            title: 'Customer calls',
            child: _calls == null
                ? const LinearProgressIndicator()
                : _calls!.isEmpty
                    ? const EmptyNote('No calls from your Wi-Fi pages yet.')
                    : Column(children: [
                        for (final c in _calls!.take(20))
                          ListTile(
                            contentPadding: EdgeInsets.zero,
                            leading: Icon(c['missed'] == true ? Icons.phone_missed : Icons.phone_in_talk,
                                color: c['missed'] == true ? cs.error : const Color(0xFF16A34A)),
                            title: Text('${c['text']}'.replaceFirst('📶 ', ''), style: const TextStyle(fontSize: 13.5)),
                            subtitle: Text(ago(c['at'])),
                            trailing: '${c['phone']}'.isEmpty
                                ? null
                                : IconButton(
                                    icon: const Icon(Icons.call),
                                    onPressed: () => launchUrl(Uri.parse('tel:${'${c['phone']}'.replaceAll(RegExp(r'[^\d+]'), '')}'))),
                          ),
                      ]),
          ),
          if (businesses.length > 1) ...[
            const SizedBox(height: 12),
            SectionCard(
              title: 'Switch business',
              child: Column(children: [
                for (final x in businesses)
                  RadioListTile<int>(
                    contentPadding: EdgeInsets.zero,
                    value: toI(x['id']),
                    groupValue: toI(b['id']),
                    title: Text('${x['name']}'),
                    subtitle: Text('${x['role']}'),
                    onChanged: (v) async {
                      // this screen is rebuilt for the new business, so keep the messenger (it stays) for the note
                      final messenger = ScaffoldMessenger.of(context);
                      await app.switchBusiness(v!);
                      messenger.showSnackBar(SnackBar(content: Text('Now on ${x['name']}')));
                    },
                  ),
              ]),
            ),
          ],
          const SizedBox(height: 12),
          SectionCard(
            title: 'Phones signed in',
            child: Column(children: [
              for (final d in _devices ?? <Map>[])
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(d['platform'] == 'ios' ? Icons.phone_iphone : Icons.phone_android),
                  title: Text('${d['name']}${d['this'] == true ? ' · this phone' : ''}'),
                  subtitle: Text('Last used ${ago(d['seen'])}'),
                  trailing: d['this'] == true
                      ? null
                      : TextButton(
                          onPressed: () async {
                            await Api.I.post('/api/mobile/v1/devices/', {'id': d['id']});
                            _load();
                          },
                          child: const Text('Sign out'),
                        ),
                ),
            ]),
          ),
          const SizedBox(height: 12),
          Card(
            child: Column(children: [
              ListTile(
                leading: const Icon(Icons.open_in_browser),
                title: const Text('Open TapTap on the web'),
                onTap: () => launchUrl(Uri.parse(Api.I.server), mode: LaunchMode.externalApplication),
              ),
              ListTile(
                leading: Icon(Push.ready && app.me?['push'] == true ? Icons.notifications_active : Icons.notifications_off),
                title: const Text('Notifications'),
                subtitle: Text(Push.ready && app.me?['push'] == true
                    ? 'Choose what pops up on this phone, and send a test.'
                    : 'Not fully set up yet — see what is missing.'),
                trailing: const Icon(Icons.chevron_right),
                onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const NotificationsScreen())),
              ),
              if ('${b['role_key'] ?? 'owner'}' != 'owner')
                ListTile(
                  leading: const Icon(Icons.door_front_door_outlined),
                  title: Text('Leave ${b['name']}', style: const TextStyle(fontWeight: FontWeight.w700)),
                  subtitle: const Text('Remove yourself from this team — your login and other businesses stay'),
                  onTap: () => _leave(app),
                ),
              ListTile(
                leading: Icon(Icons.logout, color: cs.error),
                title: Text('Sign out', style: TextStyle(color: cs.error, fontWeight: FontWeight.w700)),
                onTap: () async {
                  await Push.unregister();
                  await app.signOut();
                },
              ),
            ]),
          ),
          const SizedBox(height: 16),
          Center(child: Text('TapTap app 1.2 · ${Api.I.server}', style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant))),
        ]),
      ),
    );
  }
}
