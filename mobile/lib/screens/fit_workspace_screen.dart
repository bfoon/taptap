import 'dart:async';

import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../app/api.dart';
import '../app/format.dart';
import 'missions_screen.dart';
import 'voucher_detail_screen.dart';

/// Open a TapTap web page (a dashboard, the workspace) in the browser.
Future<void> openOnWeb(String link) async {
  final base = Api.I.server.endsWith('/') ? Api.I.server.substring(0, Api.I.server.length - 1) : Api.I.server;
  await launchUrl(Uri.parse(link.startsWith('http') ? link : '$base$link'), mode: LaunchMode.externalApplication);
}

/// Fit → workspace: everything Fit built for this business — tables (open here), dashboards (open on the web),
/// missions — and ideas to ask for. Picking an idea returns its question to the Fit screen, which asks it.
class FitWorkspaceScreen extends StatefulWidget {
  const FitWorkspaceScreen({super.key});
  @override
  State<FitWorkspaceScreen> createState() => _FitWorkspaceScreenState();
}

class _FitWorkspaceScreenState extends State<FitWorkspaceScreen> {
  Map<String, dynamic>? _d;
  String? _error;

  static const _ideas = [
    ('🎟️', 'Vouchers older than a month', 'Make a table of vouchers older than 30 days that were never used, oldest first.'),
    ('🧊', 'Frozen & warned vouchers', 'Make a table of every frozen or warned voucher with its plan and router.'),
    ('⏳', 'Members ending this week', 'Make a table of members whose time ends within 7 days, soonest first, with their phone.'),
    ('💰', 'This month’s sales', 'Make a table of this month’s sales, biggest first, with the agent and how they paid.'),
    ('📡', 'Offline routers', 'Make a table of my routers that are offline, with their last contact.'),
    ('📊', 'A live dashboard', 'Build me a live dashboard with users online, router load and today’s sales by hour.'),
  ];

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final d = await Api.I.get('/fit/workspace/data/');
      if (mounted) setState(() => _d = d);
    } on ApiError catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final d = _d;
    final tables = asMaps(d?['tables']);
    final dashes = asMaps(d?['dashboards']);
    final missions = asMaps(d?['missions']);
    return Scaffold(
      appBar: AppBar(title: const Text('Fit workspace'), actions: [
        IconButton(tooltip: 'Open on the web', icon: const Icon(Icons.open_in_browser), onPressed: () => openOnWeb('/fit/workspace/')),
      ]),
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(padding: const EdgeInsets.fromLTRB(12, 8, 12, 32), children: [
          if (d == null && _error == null) const Padding(padding: EdgeInsets.all(40), child: Center(child: CircularProgressIndicator())),
          if (_error != null) Padding(padding: const EdgeInsets.all(16), child: Text(_error!, style: TextStyle(color: cs.error))),
          if (d != null) ...[
            Row(children: [
              _tile('📋', 'Tables', '${tables.length}', null),
              _tile('📊', 'Dashboards', '${dashes.length}', () => openOnWeb('/fit/dashboards/')),
              _tile('🎯', 'Missions', '${missions.length} waiting',
                  () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const MissionsScreen()))),
              _tile('👀', 'Monitors', '${d['monitors'] ?? 0} on', () => openOnWeb('/fit/monitors/')),
            ]),
            _head('📋 Tables'),
            if (tables.isEmpty) _note('No tables yet — pick an idea below and Fit builds it.'),
            for (final t in tables)
              Card(
                child: ListTile(
                  leading: Text('${t['icon']}', style: const TextStyle(fontSize: 24)),
                  title: Text('${t['title']}${t['pinned'] == true ? '  📌' : ''}', style: const TextStyle(fontWeight: FontWeight.w700)),
                  subtitle: Text('${t['source_label']} · ${(t['filters'] as List? ?? []).join(' · ')}', maxLines: 2, overflow: TextOverflow.ellipsis),
                  trailing: const Icon(Icons.chevron_right),
                  onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => FitTableScreen(id: toI(t['id']), title: '${t['title']}'))),
                ),
              ),
            if (dashes.isNotEmpty) _head('📊 Dashboards'),
            for (final x in dashes)
              Card(
                child: ListTile(
                  leading: const Text('📊', style: TextStyle(fontSize: 24)),
                  title: Text('${x['title']}', style: const TextStyle(fontWeight: FontWeight.w700)),
                  subtitle: Text('${x['widgets']} charts · live · opens on the web'),
                  trailing: const Icon(Icons.open_in_new, size: 18),
                  onTap: () => openOnWeb('${x['link']}'),
                ),
              ),
            if (missions.isNotEmpty) _head('🎯 Missions waiting'),
            for (final m in missions)
              Card(
                child: ListTile(
                  leading: const Text('🎯', style: TextStyle(fontSize: 22)),
                  title: Text('${m['name']}', style: const TextStyle(fontWeight: FontWeight.w700)),
                  subtitle: Text('${m['when_text']}'),
                  onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const MissionsScreen())),
                ),
              ),
            _head('✨ Ask Fit to build one'),
            Wrap(spacing: 8, runSpacing: 8, children: [
              for (final i in _ideas)
                ActionChip(avatar: Text(i.$1), label: Text(i.$2), onPressed: () => Navigator.pop(context, i.$3)),
            ]),
          ],
        ]),
      ),
    );
  }

  Widget _tile(String icon, String label, String sub, VoidCallback? onTap) => Expanded(
        child: Card(
          child: InkWell(
            borderRadius: BorderRadius.circular(12),
            onTap: onTap,
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 10),
              child: Column(children: [
                Text(icon, style: const TextStyle(fontSize: 20)),
                Text(label, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 11.5)),
                Text(sub, style: const TextStyle(fontSize: 10.5)),
              ]),
            ),
          ),
        ),
      );

  Widget _head(String t) => Padding(
        padding: const EdgeInsets.fromLTRB(4, 16, 4, 6),
        child: Text(t, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15)),
      );

  Widget _note(String t) => Padding(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
        child: Text(t, style: TextStyle(color: Theme.of(context).colorScheme.onSurfaceVariant)),
      );
}

/// One saved table, live: tap a column to sort, the chips to narrow it, search, page through; a voucher row opens
/// the voucher. Changing the saved filters and CSV are on the web page.
class FitTableScreen extends StatefulWidget {
  const FitTableScreen({super.key, required this.id, this.title = ''});
  final int id;
  final String title;
  @override
  State<FitTableScreen> createState() => _FitTableScreenState();
}

class _FitTableScreenState extends State<FitTableScreen> {
  Map<String, dynamic>? _d;
  String? _error;
  int _page = 1;
  String _sort = '', _dir = '';
  final Map<String, String> _extra = {};          // chip filters added on the phone (f_<key>)
  final _search = TextEditingController();
  Timer? _debounce;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _search.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final q = <String, String>{
        'page': '$_page',
        if (_sort.isNotEmpty) 'sort': _sort,
        if (_dir.isNotEmpty) 'dir': _dir,
        if (_search.text.trim().isNotEmpty) 'f_search': _search.text.trim(),
        ..._extra,
      };
      final d = await Api.I.get('/fit/tables/${widget.id}/data/', query: q);
      if (mounted) {
        setState(() {
          _d = d;
          _error = null;
        });
      }
    } on ApiError catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  void _sortBy(String key) {
    final cur = (_d?['sort'] as Map?) ?? {};
    setState(() {
      _sort = key;
      _dir = cur['by'] == key && cur['dir'] == 'asc' ? 'desc' : 'asc';
      _page = 1;
    });
    _load();
  }

  void _openRow(Map<String, dynamic> r) {
    final m = RegExp(r'^/vouchers/(\d+)/$').firstMatch('${r['link'] ?? ''}');
    if (m != null) {
      Navigator.of(context).push(MaterialPageRoute(builder: (_) => VoucherDetailScreen(id: int.parse(m.group(1)!))));
    } else if ('${r['link'] ?? ''}'.isNotEmpty) {
      openOnWeb('${r['link']}');
    }
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final d = _d;
    final cols = asMaps(d?['columns']);
    final rows = asMaps(d?['rows']);
    final chips = asMaps(d?['chips']);
    final sort = (d?['sort'] as Map?) ?? {};
    final t = (d?['table'] as Map?) ?? {};
    return Scaffold(
      appBar: AppBar(
        title: Text('${t['title'] ?? widget.title}'),
        actions: [
          if ('${t['print'] ?? ''}'.isNotEmpty)
            IconButton(tooltip: 'Print these vouchers as tickets', icon: const Icon(Icons.print_outlined),
                onPressed: () => openOnWeb('${t['print']}')),
          IconButton(tooltip: 'Open on the web (filters, columns, CSV)', icon: const Icon(Icons.open_in_browser),
              onPressed: () => openOnWeb('/fit/tables/${widget.id}/')),
        ],
      ),
      body: _error != null
          ? Center(child: Padding(padding: const EdgeInsets.all(24), child: Text(_error!, textAlign: TextAlign.center)))
          : d == null
              ? const Center(child: CircularProgressIndicator())
              : RefreshIndicator(
                  onRefresh: _load,
                  child: ListView(padding: const EdgeInsets.fromLTRB(12, 8, 12, 24), children: [
                    Text('${t['icon'] ?? ''} ${d['source_label']} · ${d['total']} row${toI(d['total']) == 1 ? '' : 's'} · live',
                        style: TextStyle(color: cs.onSurfaceVariant, fontSize: 12.5)),
                    const SizedBox(height: 6),
                    Wrap(spacing: 6, runSpacing: 6, children: [
                      for (final f in (d['filters'] as List? ?? []))
                        Chip(label: Text('$f', style: const TextStyle(fontSize: 11.5)), visualDensity: VisualDensity.compact),
                      for (final e in _extra.entries)
                        InputChip(
                          label: Text('${e.key.substring(2)}: ${e.value}', style: const TextStyle(fontSize: 11.5)),
                          onDeleted: () {
                            setState(() => _extra.remove(e.key));
                            _load();
                          },
                        ),
                    ]),
                    if (chips.isNotEmpty) ...[
                      const SizedBox(height: 6),
                      Wrap(spacing: 6, runSpacing: 6, children: [
                        for (final c in chips)
                          ActionChip(
                            label: Text('${c['label']}  ${c['value']}', style: const TextStyle(fontSize: 12)),
                            onPressed: c['filter'] is Map
                                ? () {
                                    final f = (c['filter'] as Map).entries.first;
                                    setState(() {
                                      _extra['f_${f.key}'] = '${f.value}';
                                      _page = 1;
                                    });
                                    _load();
                                  }
                                : null,
                          ),
                      ]),
                    ],
                    const SizedBox(height: 8),
                    TextField(
                      controller: _search,
                      decoration: const InputDecoration(prefixIcon: Icon(Icons.search), hintText: 'Search in this table', isDense: true),
                      onChanged: (_) {
                        _debounce?.cancel();
                        _debounce = Timer(const Duration(milliseconds: 450), () {
                          _page = 1;
                          _load();
                        });
                      },
                    ),
                    const SizedBox(height: 8),
                    Card(
                      clipBehavior: Clip.antiAlias,
                      child: SingleChildScrollView(
                        scrollDirection: Axis.horizontal,
                        child: DataTable(
                          headingRowHeight: 40,
                          dataRowMinHeight: 38,
                          dataRowMaxHeight: 52,
                          columnSpacing: 18,
                          showCheckboxColumn: false,
                          sortColumnIndex: cols.indexWhere((c) => c['key'] == sort['by']) < 0 ? null : cols.indexWhere((c) => c['key'] == sort['by']),
                          sortAscending: sort['dir'] != 'desc',
                          columns: [
                            for (final c in cols)
                              DataColumn(
                                label: Text('${c['label']}', style: const TextStyle(fontWeight: FontWeight.w800)),
                                numeric: c['kind'] == 'money' || c['kind'] == 'num',
                                onSort: (_, __) => _sortBy('${c['key']}'),
                              ),
                          ],
                          rows: [
                            for (final r in rows)
                              DataRow(
                                onSelectChanged: '${r['link'] ?? ''}'.isEmpty ? null : (_) => _openRow(r),
                                cells: [
                                  for (var i = 0; i < cols.length; i++)
                                    DataCell(Text(i < (r['cells'] as List? ?? []).length ? '${(r['cells'] as List)[i]}' : '')),
                                ],
                              ),
                          ],
                        ),
                      ),
                    ),
                    if (rows.isEmpty) Padding(padding: const EdgeInsets.all(16), child: Text('Nothing matches right now.', style: TextStyle(color: cs.onSurfaceVariant))),
                    Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
                      Text('Page ${d['page']} of ${d['pages']}', style: TextStyle(color: cs.onSurfaceVariant, fontSize: 12.5)),
                      Row(children: [
                        IconButton(
                          icon: const Icon(Icons.chevron_left),
                          onPressed: toI(d['page']) > 1
                              ? () {
                                  setState(() => _page = toI(d['page']) - 1);
                                  _load();
                                }
                              : null,
                        ),
                        IconButton(
                          icon: const Icon(Icons.chevron_right),
                          onPressed: toI(d['page']) < toI(d['pages'])
                              ? () {
                                  setState(() => _page = toI(d['page']) + 1);
                                  _load();
                                }
                              : null,
                        ),
                      ]),
                    ]),
                  ]),
                ),
    );
  }
}

/// A saved table in Fit's chat: the first rows and the total — tap it to open the table.
class FitTableCard extends StatelessWidget {
  const FitTableCard({super.key, required this.card});
  final Map<String, dynamic> card;
  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final cols = (card['columns'] as List? ?? []).map((e) => '$e').toList();
    final rows = asMaps(card['rows']);
    final total = toI(card['total']);
    return Card(
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => FitTableScreen(id: toI(card['id']), title: '${card['title']}'))),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Text('${card['icon'] ?? '📋'}', style: const TextStyle(fontSize: 22)),
              const SizedBox(width: 8),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('${card['title']}', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15)),
                  Text('${card['source_label']} · $total row${total == 1 ? '' : 's'} · live', style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
                ]),
              ),
              Text('Open ›', style: TextStyle(fontWeight: FontWeight.w800, color: cs.primary)),
            ]),
            const SizedBox(height: 6),
            Wrap(spacing: 4, runSpacing: 4, children: [
              for (final f in (card['filters'] as List? ?? []))
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
                  decoration: BoxDecoration(color: cs.primary.withValues(alpha: .1), borderRadius: BorderRadius.circular(99)),
                  child: Text('$f', style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: cs.primary)),
                ),
            ]),
            if (rows.isNotEmpty) ...[
              const SizedBox(height: 6),
              SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: Table(
                  defaultColumnWidth: const IntrinsicColumnWidth(),
                  children: [
                    TableRow(children: [
                      for (final c in cols)
                        Padding(padding: const EdgeInsets.fromLTRB(0, 4, 14, 4), child: Text(c, style: TextStyle(fontSize: 11.5, fontWeight: FontWeight.w800, color: cs.onSurfaceVariant))),
                    ]),
                    for (final r in rows)
                      TableRow(children: [
                        for (var i = 0; i < cols.length; i++)
                          Padding(
                            padding: const EdgeInsets.fromLTRB(0, 3, 14, 3),
                            child: Text(i < (r['cells'] as List? ?? []).length ? '${(r['cells'] as List)[i]}' : '', style: const TextStyle(fontSize: 12.5)),
                          ),
                      ]),
                  ],
                ),
              ),
            ],
            if (total > rows.length)
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text('+ ${total - rows.length} more — tap to open the table', style: TextStyle(fontSize: 11.5, color: cs.onSurfaceVariant)),
              ),
            if ('${card['print'] ?? ''}'.isNotEmpty && total > 0)
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  onPressed: () => openOnWeb('${card['print']}'),
                  icon: const Icon(Icons.print_outlined, size: 18),
                  label: Text('Print $total as tickets'),
                ),
              ),
          ]),
        ),
      ),
    );
  }
}
