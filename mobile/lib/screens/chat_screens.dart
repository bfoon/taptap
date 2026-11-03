import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:provider/provider.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../calls/call_engine.dart';
import '../calls/call_screen.dart';
import '../widgets/common.dart';

/// The team room, direct messages and TapTap support — TapTap's own chat (/chat/).
class ChatListScreen extends StatelessWidget {
  const ChatListScreen({super.key});

  Future<void> _newChat(BuildContext context) async {
    final app = context.read<AppState>();
    final p = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      showDragHandle: true,
      builder: (ctx) => ListView(shrinkWrap: true, children: [
        const Padding(padding: EdgeInsets.fromLTRB(20, 0, 20, 8), child: Text('Message someone', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w800))),
        for (final u in app.people)
          ListTile(
            leading: Avatar(text: '${u['initials']}', online: u['online'] == true),
            title: Text('${u['name']}'),
            subtitle: Text(u['online'] == true ? 'Online' : 'Offline'),
            onTap: () => Navigator.pop(ctx, u),
          ),
        if (app.people.isEmpty) const Padding(padding: EdgeInsets.all(20), child: Text('Add team members on TapTap to chat with them.')),
      ]),
    );
    if (p == null) return;
    try {
      final d = await Api.I.post('/chat/direct/', {'user': p['id']});
      if (context.mounted) openThread(context, toI(d['thread']), '${p['name']}');
    } on ApiError catch (e) {
      if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final app = context.watch<AppState>();
    final online = app.people.where((p) => p['online'] == true).toList();
    return Scaffold(
      appBar: AppBar(title: const Text('Chat')),
      floatingActionButton: Padding(
        padding: const EdgeInsets.only(bottom: 80),
        child: FloatingActionButton(onPressed: () => _newChat(context), child: const Icon(Icons.edit)),
      ),
      body: RefreshIndicator(
        onRefresh: app.pollChat,
        child: ListView(padding: const EdgeInsets.only(bottom: 110), children: [
          if (online.isNotEmpty)
            SizedBox(
              height: 92,
              child: ListView(scrollDirection: Axis.horizontal, padding: const EdgeInsets.symmetric(horizontal: 12), children: [
                for (final u in online)
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 6),
                    child: InkWell(
                      borderRadius: BorderRadius.circular(16),
                      onTap: () async {
                        final d = await Api.I.post('/chat/direct/', {'user': u['id']});
                        if (context.mounted) openThread(context, toI(d['thread']), '${u['name']}');
                      },
                      child: SizedBox(
                        width: 64,
                        child: Column(children: [
                          Avatar(text: '${u['initials']}', online: true, radius: 26),
                          const SizedBox(height: 6),
                          Text('${u['name']}'.split(' ').first, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12)),
                        ]),
                      ),
                    ),
                  ),
              ]),
            ),
          for (final t in app.threads)
            ListTile(
              leading: Avatar(
                text: t['kind'] == 'team' ? '👥' : '${t['initials']}',
                online: t['online'] == true,
                color: t['kind'] == 'support' ? const Color(0xFF7C3AED) : null,
              ),
              title: Text('${t['title']}', style: TextStyle(fontWeight: toI(t['unread']) > 0 ? FontWeight.w800 : FontWeight.w600)),
              subtitle: Text('${t['last']}'.isEmpty ? '${t['sub']}' : '${t['last']}', maxLines: 1, overflow: TextOverflow.ellipsis),
              trailing: Column(mainAxisAlignment: MainAxisAlignment.center, crossAxisAlignment: CrossAxisAlignment.end, children: [
                Text(ago(t['last_at']), style: const TextStyle(fontSize: 11)),
                const SizedBox(height: 4),
                if (toI(t['unread']) > 0)
                  Badge(label: Text('${t['unread']}'), backgroundColor: t['muted'] == true ? Colors.grey : null)
                else if (t['muted'] == true)
                  const Icon(Icons.notifications_off, size: 14),
              ]),
              onTap: () => openThread(context, toI(t['id']), '${t['title']}'),
            ),
        ]),
      ),
    );
  }
}

void openThread(BuildContext context, int id, String title) {
  Navigator.of(context).push(MaterialPageRoute(builder: (_) => ThreadScreen(id: id, title: title)));
}

class ThreadScreen extends StatefulWidget {
  const ThreadScreen({super.key, required this.id, required this.title});
  final int id;
  final String title;
  @override
  State<ThreadScreen> createState() => _ThreadScreenState();
}

class _ThreadScreenState extends State<ThreadScreen> {
  final _msgs = <Map<String, dynamic>>[];
  final _text = TextEditingController();
  final _scroll = ScrollController();
  Map<String, dynamic> _thread = {};
  StreamSubscription? _sub;
  bool _more = false, _sending = false;
  late final AppState _app;
  Timer? _typingOff;

  @override
  void initState() {
    super.initState();
    _app = context.read<AppState>();
    _app.openThread = widget.id;
    _sub = _app.newMessages.listen((m) {
      if (toI(m['thread']) != widget.id || _msgs.any((x) => x['id'] == m['id'])) return;
      setState(() => _msgs.add(m));
      _read();
      _toBottom();
    });
    _load();
    _text.addListener(() {
      _app.typing = _text.text.isNotEmpty;
      _typingOff?.cancel();
      _typingOff = Timer(const Duration(seconds: 4), () => _app.typing = false);
    });
  }

  @override
  void dispose() {
    _sub?.cancel();
    _app.openThread = null;
    _app.typing = false;
    _typingOff?.cancel();
    super.dispose();
  }

  Future<void> _load({bool older = false}) async {
    try {
      final before = older && _msgs.isNotEmpty ? '${_msgs.first['id']}' : null;
      final d = await Api.I.get('/chat/t/${widget.id}/', query: {if (before != null) 'before': before});
      final rows = (d['messages'] as List).cast<Map>().map((e) => e.cast<String, dynamic>()).toList();
      setState(() {
        _thread = (d['thread'] as Map).cast<String, dynamic>();
        _more = d['more'] == true;
        if (older) {
          _msgs.insertAll(0, rows);
        } else {
          _msgs
            ..clear()
            ..addAll(rows);
        }
      });
      if (!older) _toBottom(jump: true);
      _app.pollChat();
    } on ApiError catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  void _read() {
    if (_msgs.isEmpty) return;
    Api.I.post('/chat/read/', {'thread': widget.id, 'upto': _msgs.last['id']}).catchError((_) => <String, dynamic>{});
  }

  void _toBottom({bool jump = false}) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      final end = _scroll.position.maxScrollExtent;
      jump ? _scroll.jumpTo(end) : _scroll.animateTo(end, duration: const Duration(milliseconds: 250), curve: Curves.easeOut);
    });
  }

  Future<void> _send() async {
    final body = _text.text.trim();
    if (body.isEmpty || _sending) return;
    setState(() => _sending = true);
    try {
      final d = await Api.I.post('/chat/send/', {'thread': widget.id, 'body': body});
      _text.clear();
      final m = (d['message'] as Map).cast<String, dynamic>();
      if (!_msgs.any((x) => x['id'] == m['id'])) setState(() => _msgs.add(m));
      _toBottom();
    } on ApiError catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  Future<void> _photo(ImageSource src) async {
    final x = await ImagePicker().pickImage(source: src, maxWidth: 1600, imageQuality: 82);
    if (x == null) return;
    setState(() => _sending = true);
    try {
      final d = await Api.I.upload('/chat/upload/', {'thread': '${widget.id}', 'body': _text.text.trim()}, File(x.path));
      _text.clear();
      setState(() => _msgs.add((d['message'] as Map).cast<String, dynamic>()));
      _toBottom();
    } on ApiError catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  void _call({bool video = false}) {
    openCall(context, CallEngine.outgoing(thread: widget.id, title: '${_thread['title'] ?? widget.title}', video: video));
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final app = context.watch<AppState>();
    final typing = app.openExtra['typing'];
    return Scaffold(
      appBar: AppBar(
        titleSpacing: 0,
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${_thread['title'] ?? widget.title}', style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w800)),
          Text(typing is List && typing.isNotEmpty ? '${typing.map((t) => '${(t as Map)['name']}'.split(' ').first).join(', ')} typing…' : '${_thread['sub'] ?? ''}',
              style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
        ]),
        actions: [
          if (_thread['can_call'] == true) IconButton(icon: const Icon(Icons.call), onPressed: _call),
        ],
      ),
      body: Column(children: [
        Expanded(
          child: ListView.builder(
            controller: _scroll,
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
            itemCount: _msgs.length + (_more ? 1 : 0),
            itemBuilder: (_, i) {
              if (_more && i == 0) return Center(child: TextButton(onPressed: () => _load(older: true), child: const Text('Earlier messages')));
              final m = _msgs[i - (_more ? 1 : 0)];
              return _Bubble(m: m);
            },
          ),
        ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
            child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
              IconButton(
                icon: const Icon(Icons.add_photo_alternate_outlined),
                onPressed: _sending
                    ? null
                    : () => showModalBottomSheet(
                          context: context,
                          builder: (ctx) => SafeArea(
                            child: Wrap(children: [
                              ListTile(leading: const Icon(Icons.photo_camera), title: const Text('Take a photo'),
                                  onTap: () { Navigator.pop(ctx); _photo(ImageSource.camera); }),
                              ListTile(leading: const Icon(Icons.photo), title: const Text('From the gallery'),
                                  onTap: () { Navigator.pop(ctx); _photo(ImageSource.gallery); }),
                              const ListTile(dense: true, title: Text('Photos are deleted after 7 days.')),
                            ]),
                          ),
                        ),
              ),
              Expanded(
                child: TextField(
                  controller: _text,
                  minLines: 1,
                  maxLines: 5,
                  textCapitalization: TextCapitalization.sentences,
                  decoration: const InputDecoration(hintText: 'Message'),
                ),
              ),
              const SizedBox(width: 6),
              IconButton.filled(onPressed: _sending ? null : _send, icon: const Icon(Icons.send)),
            ]),
          ),
        ),
      ]),
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({required this.m});
  final Map<String, dynamic> m;
  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final mine = m['mine'] == true;
    if (m['system'] == true) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 6),
        child: Center(
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            decoration: BoxDecoration(color: cs.surfaceContainerHighest, borderRadius: BorderRadius.circular(99)),
            child: Text('${m['body']}', textAlign: TextAlign.center, style: const TextStyle(fontSize: 12.5)),
          ),
        ),
      );
    }
    final img = (m['image'] as Map?)?.cast<String, dynamic>();
    return Align(
      alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * .78),
        margin: const EdgeInsets.symmetric(vertical: 3),
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 6),
        decoration: BoxDecoration(
          color: mine ? cs.primary : (m['mention_me'] == true ? const Color(0xFFFFF3C4) : Theme.of(context).cardColor),
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(18),
            topRight: const Radius.circular(18),
            bottomLeft: Radius.circular(mine ? 18 : 4),
            bottomRight: Radius.circular(mine ? 4 : 18),
          ),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          if (!mine) Text('${m['sender']}', style: TextStyle(fontSize: 12, fontWeight: FontWeight.w800, color: cs.primary)),
          if (img != null && img['gone'] != true && img['kind'] != 'video')
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: Image.network(Api.I.imageUrl('/chat/img/${img['id']}/'), headers: Api.I.headers, fit: BoxFit.cover,
                    errorBuilder: (_, __, ___) => const Text('📷 Photo')),
              ),
            )
          else if (img != null)
            Text(img['gone'] == true ? '📷 Photo (expired)' : '🎬 Video clip — open TapTap on the web to play it',
                style: TextStyle(fontStyle: FontStyle.italic, color: mine ? Colors.white70 : null)),
          if ('${m['body']}'.isNotEmpty)
            Text('${m['body']}', style: TextStyle(color: mine ? cs.onPrimary : null, fontSize: 15)),
          if ('${m['page_title'] ?? ''}'.isNotEmpty)
            Text('🔗 ${m['page_title']}', style: TextStyle(fontSize: 12, color: mine ? Colors.white70 : cs.onSurfaceVariant)),
          const SizedBox(height: 2),
          Text(clock(m['at']), style: TextStyle(fontSize: 10.5, color: mine ? Colors.white70 : cs.onSurfaceVariant)),
        ]),
      ),
    );
  }
}
