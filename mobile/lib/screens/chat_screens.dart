import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:image_picker/image_picker.dart';
import 'package:provider/provider.dart';

import '../app/api.dart';
import '../app/format.dart';
import '../app/state.dart';
import '../calls/call_engine.dart';
import '../calls/call_screen.dart';
import '../data/store.dart';
import '../data/sync_engine.dart';
import '../widgets/common.dart';
import 'chat_voice.dart';

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

/// One conversation. Long-press a message (or swipe it to the right) to reply; long-press your own message to
/// edit it, or to delete it for everyone within 1 hour of sending. Edits and deletes by others arrive with the
/// regular chat check (AppState.changedMessages).
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
  final _focus = FocusNode();
  final _keys = <int, GlobalKey>{};              // to scroll to a quoted message
  Map<String, dynamic> _thread = {};
  StreamSubscription? _sub, _subChanged;
  bool _more = false, _sending = false;
  late final AppState _app;
  Timer? _typingOff, _tick;
  Map<String, dynamic>? _replyTo;                 // composing a reply to this message
  Map<String, dynamic>? _editing;                 // editing this (own) message
  int? _flash;                                    // message highlighted after a tap on its quote

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
    _subChanged = _app.changedMessages.listen((m) {
      if (toI(m['thread']) != widget.id) return;
      setState(() => _put(m));
      final id = m['id'];
      if (m['deleted'] == true && (_editing?['id'] == id || _replyTo?['id'] == id)) _cancelCompose();
    });
    // the conversation as saved on the phone (the sync saves new messages ahead): on screen at once
    final saved = Store.I.peek(Pages.thread(widget.id));
    if (saved != null && saved['messages'] is List) {
      _thread = (saved['thread'] as Map?)?.cast<String, dynamic>() ?? {};
      _more = saved['more'] == true;
      _msgs.addAll(asMaps(saved['messages']));
      _toBottom(jump: true);
    }
    _load();
    _text.addListener(() {
      _app.typing = _text.text.isNotEmpty;
      _typingOff?.cancel();
      _typingOff = Timer(const Duration(seconds: 4), () => _app.typing = false);
    });
    // the "Delete" choice ends 1 hour after sending: refresh now and then so it disappears on time
    _tick = Timer.periodic(const Duration(minutes: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _sub?.cancel();
    _subChanged?.cancel();
    _app.openThread = null;
    _app.typing = false;
    _typingOff?.cancel();
    _tick?.cancel();
    _focus.dispose();
    super.dispose();
  }

  Future<void> _load({bool older = false}) async {
    try {
      final before = older && _msgs.isNotEmpty ? '${_msgs.first['id']}' : null;
      // the latest page is kept on the phone (opening it marks it read, as before); earlier pages are not
      final d = before == null
          ? await Store.I.fetch(Pages.thread(widget.id))
          : await Api.I.get(Pages.thread(widget.id), query: {'before': before});
      if (!mounted) return;
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
      // no internet with the saved conversation on screen: the offline strip says so
      if (mounted && !(e.status == 0 && !older && _msgs.isNotEmpty)) _toast(e.message);
    }
  }

  void _toast(String m) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(m), behavior: SnackBarBehavior.floating));

  /// Swap in the new version of a message (edited / deleted), and update replies that quote it.
  void _put(Map<String, dynamic> m) {
    for (var i = 0; i < _msgs.length; i++) {
      final x = _msgs[i];
      if (x['id'] == m['id']) {
        _msgs[i] = m;
      } else if (x['reply'] is Map && (x['reply'] as Map)['id'] == m['id']) {
        final r = Map<String, dynamic>.from(x['reply'] as Map);
        r['deleted'] = m['deleted'] == true;
        if (m['deleted'] != true) r['text'] = '${m['body'] ?? r['text']}';
        _msgs[i] = {...x, 'reply': r};
      }
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

  // ───────── send · reply · edit · delete ─────────
  Future<void> _send() async {
    final body = _text.text.trim();
    if (_sending) return;
    if (_editing != null) return _saveEdit(body);
    if (body.isEmpty) return;
    setState(() => _sending = true);
    final reply = _replyTo;
    try {
      final d = await Api.I.post('/chat/send/', {'thread': widget.id, 'body': body, if (reply != null) 'reply_to': reply['id']});
      _text.clear();
      final m = (d['message'] as Map).cast<String, dynamic>();
      setState(() {
        _replyTo = null;
        if (!_msgs.any((x) => x['id'] == m['id'])) _msgs.add(m);
      });
      _toBottom();
    } on ApiError catch (e) {
      _toast(e.message);
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  Future<void> _saveEdit(String body) async {
    final m = _editing!;
    if (body == '${m['body'] ?? ''}') return _cancelCompose();
    if (body.isEmpty && m['image'] == null && '${m['page_url'] ?? ''}'.isEmpty) {
      return _toast('Type a message — or delete it.');
    }
    setState(() => _sending = true);
    try {
      final d = await Api.I.post('/chat/message/', {'id': m['id'], 'action': 'edit', 'body': body});
      setState(() => _put((d['message'] as Map).cast<String, dynamic>()));
      _cancelCompose();
    } on ApiError catch (e) {
      _toast(e.message);
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  void _startReply(Map<String, dynamic> m) {
    HapticFeedback.selectionClick();
    setState(() {
      _editing = null;
      _replyTo = m;
    });
    _focus.requestFocus();
  }

  void _startEdit(Map<String, dynamic> m) {
    setState(() {
      _replyTo = null;
      _editing = m;
    });
    _text.text = '${m['body'] ?? ''}';
    _text.selection = TextSelection.collapsed(offset: _text.text.length);
    _focus.requestFocus();
  }

  void _cancelCompose() {
    final wasEdit = _editing != null;
    setState(() {
      _editing = null;
      _replyTo = null;
    });
    if (wasEdit) _text.clear();
  }

  static bool _canDelete(Map<String, dynamic> m) {
    final until = parseTime(m['delete_until']);
    return m['mine'] == true && until != null && until.isAfter(DateTime.now());
  }

  Future<void> _delete(Map<String, dynamic> m) async {
    if (!_canDelete(m)) return _toast('Messages can only be deleted within 1 hour of sending.');
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Delete for everyone?'),
        content: const Text('The message is removed for everyone in this chat. This cannot be undone.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Cancel')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Theme.of(c).colorScheme.error),
            onPressed: () => Navigator.pop(c, true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true) return;
    try {
      final d = await Api.I.post('/chat/message/', {'id': m['id'], 'action': 'delete'});
      setState(() => _put((d['message'] as Map).cast<String, dynamic>()));
      if (_editing?['id'] == m['id'] || _replyTo?['id'] == m['id']) _cancelCompose();
    } on ApiError catch (e) {
      _toast(e.message);
    }
  }

  /// Long-press a message: what you can do with it.
  void _menu(Map<String, dynamic> m) {
    if (m['system'] == true || m['deleted'] == true) return;
    HapticFeedback.mediumImpact();
    final mine = m['mine'] == true;
    final text = '${m['body'] ?? ''}';
    final until = parseTime(m['delete_until']);
    showModalBottomSheet(
      context: context,
      showDragHandle: true,
      builder: (ctx) => SafeArea(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          ListTile(
            leading: const Icon(Icons.reply),
            title: const Text('Reply'),
            onTap: () {
              Navigator.pop(ctx);
              _startReply(m);
            },
          ),
          if (text.isNotEmpty)
            ListTile(
              leading: const Icon(Icons.copy),
              title: const Text('Copy text'),
              onTap: () {
                Navigator.pop(ctx);
                Clipboard.setData(ClipboardData(text: text));
                _toast('Copied');
              },
            ),
          if (mine && m['can_edit'] == true)
            ListTile(
              leading: const Icon(Icons.edit),
              title: const Text('Edit'),
              onTap: () {
                Navigator.pop(ctx);
                _startEdit(m);
              },
            ),
          if (_canDelete(m))
            ListTile(
              leading: Icon(Icons.delete_outline, color: Theme.of(ctx).colorScheme.error),
              title: Text('Delete for everyone', style: TextStyle(color: Theme.of(ctx).colorScheme.error)),
              subtitle: until == null ? null : Text('Possible until ${clock(m['delete_until'])}'),
              onTap: () {
                Navigator.pop(ctx);
                _delete(m);
              },
            )
          else if (mine)
            const ListTile(
              enabled: false,
              leading: Icon(Icons.delete_outline),
              title: Text('Delete for everyone'),
              subtitle: Text('Only within 1 hour of sending'),
            ),
        ]),
      ),
    );
  }

  /// Tap a quote: scroll to the message it quotes (when it is loaded).
  void _jump(int id) {
    final ctx = _keys[id]?.currentContext;
    if (ctx == null) return _toast('That message is further up — load earlier messages.');
    Scrollable.ensureVisible(ctx, alignment: .4, duration: const Duration(milliseconds: 300));
    setState(() => _flash = id);
    Timer(const Duration(milliseconds: 1500), () {
      if (mounted) setState(() => _flash = null);
    });
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
      _toast(e.message);
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  /// Record a voice message (up to 30 s) and send it; the recording on the phone is deleted afterwards.
  Future<void> _voice() async {
    if (_sending) return;
    final clip = await recordVoice(context);
    if (clip == null || !mounted) {
      await clip?.discard();
      return;
    }
    setState(() => _sending = true);
    final reply = _replyTo;
    try {
      final d = await Api.I.upload('/chat/upload/', {
        'thread': '${widget.id}',
        'kind': 'voice',
        'secs': clip.seconds.toStringAsFixed(1),
        if (reply != null) 'reply_to': '${reply['id']}',
      }, clip.file);
      final m = (d['message'] as Map).cast<String, dynamic>();
      if (mounted) {
        setState(() {
          _replyTo = null;
          if (!_msgs.any((x) => x['id'] == m['id'])) _msgs.add(m);
        });
      }
      _toBottom();
    } on ApiError catch (e) {
      _toast(e.message);
    } finally {
      await clip.discard();
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
    final me = toI(app.user['id']);
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
              final id = toI(m['id']);
              return _Bubble(
                key: _keys.putIfAbsent(id, () => GlobalKey()),
                m: m,
                meId: me,
                flash: _flash == id,
                onLongPress: () => _menu(m),
                onSwipeReply: () {
                  if (m['system'] != true && m['deleted'] != true) _startReply(m);
                },
                onQuoteTap: (qid) => _jump(qid),
              );
            },
          ),
        ),
        if (_replyTo != null || _editing != null) _composeBar(cs),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(8, 4, 8, 8),
            child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
              if (_editing == null)
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
                  focusNode: _focus,
                  minLines: 1,
                  maxLines: 5,
                  textCapitalization: TextCapitalization.sentences,
                  decoration: InputDecoration(hintText: _editing != null ? 'Edit your message' : (_replyTo != null ? 'Reply' : 'Message')),
                ),
              ),
              const SizedBox(width: 6),
              // an empty box shows the microphone: hold a 30-second voice message instead of typing
              ValueListenableBuilder<TextEditingValue>(
                valueListenable: _text,
                builder: (_, v, __) => v.text.trim().isEmpty && _editing == null
                    ? IconButton.filledTonal(
                        tooltip: 'Voice message (up to $voiceMax s)',
                        onPressed: _sending ? null : _voice,
                        icon: const Icon(Icons.mic))
                    : IconButton.filled(onPressed: _sending ? null : _send, icon: Icon(_editing != null ? Icons.check : Icons.send)),
              ),
            ]),
          ),
        ),
      ]),
    );
  }

  /// Above the text box: who you are replying to, or that you are editing.
  Widget _composeBar(ColorScheme cs) {
    final r = _replyTo;
    final editing = _editing != null;
    final who = r == null ? '' : (r['mine'] == true ? 'yourself' : '${r['sender']}');
    final what = r == null ? '' : _preview(r);
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 4, 12, 0),
      padding: const EdgeInsets.fromLTRB(12, 8, 4, 8),
      decoration: BoxDecoration(
        color: cs.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(14),
        border: Border(left: BorderSide(color: cs.primary, width: 4)),
      ),
      child: Row(children: [
        Icon(editing ? Icons.edit : Icons.reply, size: 18, color: cs.primary),
        const SizedBox(width: 8),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
            Text(editing ? 'Editing your message' : 'Replying to $who',
                style: TextStyle(fontWeight: FontWeight.w800, fontSize: 13, color: cs.primary)),
            if (!editing)
              Text(what, maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 13, color: cs.onSurfaceVariant)),
          ]),
        ),
        IconButton(icon: const Icon(Icons.close), tooltip: 'Cancel', onPressed: _cancelCompose),
      ]),
    );
  }
}

/// One line about a message (for the reply bar).
String _preview(Map<String, dynamic> m) {
  final body = '${m['body'] ?? ''}';
  if (body.isNotEmpty) return body;
  final img = m['image'];
  if (img is Map) return img['kind'] == 'voice' ? '🎙️ Voice message' : img['kind'] == 'video' ? '🎬 Video clip' : '📷 Photo';
  return '${m['page_title'] ?? ''}'.isNotEmpty ? '🔗 ${m['page_title']}' : 'Message';
}

class _Bubble extends StatefulWidget {
  const _Bubble({super.key, required this.m, required this.meId, required this.onLongPress, required this.onSwipeReply,
      required this.onQuoteTap, this.flash = false});
  final Map<String, dynamic> m;
  final int meId;
  final bool flash;
  final VoidCallback onLongPress, onSwipeReply;
  final void Function(int id) onQuoteTap;
  @override
  State<_Bubble> createState() => _BubbleState();
}

class _BubbleState extends State<_Bubble> {
  double _dx = 0;                                  // swipe-to-reply offset

  @override
  Widget build(BuildContext context) {
    final m = widget.m;
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
    final deleted = m['deleted'] == true;
    final img = (m['image'] as Map?)?.cast<String, dynamic>();
    final reply = (m['reply'] as Map?)?.cast<String, dynamic>();
    final ink = mine && !deleted ? cs.onPrimary : null;
    final soft = mine && !deleted ? Colors.white70 : cs.onSurfaceVariant;
    final bubble = AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * .78),
      margin: const EdgeInsets.symmetric(vertical: 3),
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 6),
      decoration: BoxDecoration(
        color: deleted
            ? Colors.transparent
            : mine
                ? cs.primary
                : (m['mention_me'] == true ? const Color(0xFFFFF3C4) : Theme.of(context).cardColor),
        border: deleted ? Border.all(color: cs.outlineVariant) : null,
        boxShadow: widget.flash ? [const BoxShadow(color: Color(0xFFF59E0B), spreadRadius: 3)] : null,
        borderRadius: BorderRadius.only(
          topLeft: const Radius.circular(18),
          topRight: const Radius.circular(18),
          bottomLeft: Radius.circular(mine ? 18 : 4),
          bottomRight: Radius.circular(mine ? 4 : 18),
        ),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (!mine) Text('${m['sender']}', style: TextStyle(fontSize: 12, fontWeight: FontWeight.w800, color: cs.primary)),
        if (deleted)
          Text(mine ? '🚫 You deleted this message' : '🚫 This message was deleted',
              style: TextStyle(fontStyle: FontStyle.italic, color: cs.onSurfaceVariant))
        else ...[
          if (reply != null) _quote(context, reply, mine),
          if (img != null && img['kind'] == 'voice')
            VoiceBubble(m: m, mine: mine)
          else if (img != null && img['gone'] != true && img['kind'] != 'video')
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
          if ('${m['body']}'.isNotEmpty) Text('${m['body']}', style: TextStyle(color: ink, fontSize: 15)),
          if ('${m['page_title'] ?? ''}'.isNotEmpty)
            Text('🔗 ${m['page_title']}', style: TextStyle(fontSize: 12, color: soft)),
        ],
        const SizedBox(height: 2),
        Text('${m['edited'] == true ? 'edited · ' : ''}${clock(m['at'])}', style: TextStyle(fontSize: 10.5, color: soft)),
      ]),
    );
    return GestureDetector(
      onLongPress: widget.onLongPress,
      onHorizontalDragUpdate: deleted ? null : (d) => setState(() => _dx = (_dx + d.delta.dx).clamp(0.0, 72.0).toDouble()),
      onHorizontalDragEnd: deleted
          ? null
          : (_) {
              if (_dx > 56) widget.onSwipeReply();
              setState(() => _dx = 0);
            },
      child: Stack(alignment: Alignment.centerLeft, children: [
        if (_dx > 8)
          Opacity(opacity: (_dx / 56).clamp(0, 1).toDouble(), child: Icon(Icons.reply, color: cs.primary)),
        Transform.translate(
          offset: Offset(_dx, 0),
          child: Align(alignment: mine ? Alignment.centerRight : Alignment.centerLeft, child: bubble),
        ),
      ]),
    );
  }

  Widget _quote(BuildContext context, Map<String, dynamic> r, bool mine) {
    final cs = Theme.of(context).colorScheme;
    final who = toI(r['sender_id']) == widget.meId ? 'You' : '${r['sender'] ?? ''}';
    final text = r['deleted'] == true ? '🚫 Message deleted' : '${r['text'] ?? ''}';
    return GestureDetector(
      onTap: () => widget.onQuoteTap(toI(r['id'])),
      child: Container(
        width: double.infinity,
        margin: const EdgeInsets.only(top: 2, bottom: 5),
        padding: const EdgeInsets.fromLTRB(8, 4, 8, 4),
        decoration: BoxDecoration(
          color: mine ? Colors.white.withValues(alpha: .18) : cs.primary.withValues(alpha: .08),
          borderRadius: BorderRadius.circular(8),
          border: Border(left: BorderSide(color: mine ? Colors.white : cs.primary, width: 3)),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(who, style: TextStyle(fontSize: 11.5, fontWeight: FontWeight.w800, color: mine ? Colors.white : cs.primary)),
          Text(text, maxLines: 2, overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: 12.5, color: mine ? Colors.white70 : cs.onSurfaceVariant)),
        ]),
      ),
    );
  }
}
