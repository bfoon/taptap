import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:speech_to_text/speech_to_text.dart';

import '../app/api.dart';
import '../widgets/charts.dart';

/// Fit, your AI technician — the same Fit as on the website (/fit/), so it can do exactly what it may do for you
/// there: same tools, same permissions, same "Allowed" groups in Fit settings, same confirmations before it acts.
class FitScreen extends StatefulWidget {
  const FitScreen({super.key, this.question});
  final ValueNotifier<String?>? question;     // another screen asks Fit something ("Brief me", "Why were sales…")
  @override
  State<FitScreen> createState() => FitScreenState();
}

class FitScreenState extends State<FitScreen> {
  final _msgs = <Map<String, dynamic>>[];
  final _text = TextEditingController();
  final _scroll = ScrollController();
  final _speech = SpeechToText();
  Map<String, dynamic>? _state;
  int? _conv;
  bool _thinking = false, _listening = false, _speechOk = false;
  String? _err;
  int _dots = 0;
  Timer? _dotTimer;

  @override
  void initState() {
    super.initState();
    _load();
    widget.question?.addListener(_external);
  }

  @override
  void dispose() {
    widget.question?.removeListener(_external);
    _dotTimer?.cancel();
    super.dispose();
  }

  void _external() {
    final q = widget.question?.value;
    if (q != null && q.isNotEmpty) {
      widget.question!.value = null;
      ask(q);
    }
  }

  Future<void> _load() async {
    try {
      final d = await Api.I.get('/fit/state/');
      setState(() {
        _state = d;
        _conv = d['conversation'] as int?;
        _msgs
          ..clear()
          ..addAll((d['messages'] as List? ?? []).cast<Map>().map((e) => e.cast<String, dynamic>()));
        _err = null;
      });
      _toBottom();
      _external();
    } on ApiError catch (e) {
      setState(() => _err = e.message);
    }
  }

  Future<void> ask(String q) async {
    q = q.trim();
    if (q.isEmpty || _thinking) return;
    HapticFeedback.lightImpact();
    _text.clear();
    setState(() {
      _msgs.add({'role': 'user', 'text': q, 'at': DateTime.now().toIso8601String()});
      _thinking = true;
    });
    _dotTimer = Timer.periodic(const Duration(milliseconds: 450), (_) => setState(() => _dots = (_dots + 1) % 4));
    _toBottom();
    try {
      final d = await Api.I.post('/fit/ask/', {
        'message': q,
        if (_conv != null) 'conversation': _conv,
        'page': {'title': 'TapTap mobile app', 'path': '/app/'},
      }, const Duration(seconds: 120));
      _conv = d['conversation'] as int?;
      setState(() => _msgs.add({'role': 'assistant', 'text': d['reply'], 'cards': d['cards'] ?? {}, 'mood': d['mood']}));
      if (d['mood'] == 'alert') HapticFeedback.mediumImpact();
    } on ApiError catch (e) {
      setState(() => _msgs.add({'role': 'assistant', 'text': '⚠️ ${e.message}', 'cards': {}}));
    } finally {
      _dotTimer?.cancel();
      if (mounted) setState(() => _thinking = false);
      _toBottom();
    }
  }

  Future<void> _newChat() async {
    try {
      final d = await Api.I.post('/fit/new/');
      setState(() {
        _conv = d['conversation'] as int?;
        _msgs.clear();
      });
    } on ApiError catch (e) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  Future<void> _listen() async {
    if (_listening) {
      await _speech.stop();
      setState(() => _listening = false);
      return;
    }
    _speechOk = _speechOk || await _speech.initialize(onStatus: (s) {
      if (s == 'done' || s == 'notListening') {
        if (mounted) setState(() => _listening = false);
        if (_text.text.trim().isNotEmpty) ask(_text.text);
      }
    });
    if (!_speechOk) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Voice input is not available on this phone.')));
      return;
    }
    HapticFeedback.selectionClick();
    setState(() => _listening = true);
    await _speech.listen(
      onResult: (r) => setState(() => _text.text = r.recognizedWords),
      listenFor: const Duration(seconds: 30),
      pauseFor: const Duration(seconds: 3),
    );
  }

  void _toBottom() => WidgetsBinding.instance.addPostFrameCallback((_) {
        if (_scroll.hasClients) {
          _scroll.animateTo(_scroll.position.maxScrollExtent, duration: const Duration(milliseconds: 300), curve: Curves.easeOut);
        }
      });

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final st = _state;
    final enabled = st == null || st['enabled'] != false;
    return Scaffold(
      appBar: AppBar(
        title: Row(children: [
          _FitFace(thinking: _thinking),
          const SizedBox(width: 10),
          const Text('Fit'),
        ]),
        actions: [IconButton(tooltip: 'New conversation', icon: const Icon(Icons.add_comment_outlined), onPressed: _newChat)],
      ),
      body: Column(children: [
        Expanded(
          child: _err != null
              ? Center(child: Padding(padding: const EdgeInsets.all(24), child: Text(_err!, textAlign: TextAlign.center)))
              : !enabled
                  ? const Center(child: Padding(padding: EdgeInsets.all(24), child: Text('Fit is switched off for this business.', textAlign: TextAlign.center)))
                  : ListView(controller: _scroll, padding: const EdgeInsets.fromLTRB(14, 4, 14, 12), children: [
                      if (_msgs.isEmpty) _welcome(st),
                      for (final m in _msgs) _MsgView(m: m, onAsk: ask, onCardChanged: () => setState(() {})),
                      if (_thinking)
                        Padding(
                          padding: const EdgeInsets.symmetric(vertical: 10),
                          child: Row(children: [
                            const SizedBox(width: 4),
                            const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)),
                            const SizedBox(width: 10),
                            Text('Fit is looking into it${'.' * _dots}', style: TextStyle(color: cs.onSurfaceVariant)),
                          ]),
                        ),
                    ]),
        ),
        if (enabled)
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(10, 4, 10, 90),
              child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
                Expanded(
                  child: TextField(
                    controller: _text,
                    minLines: 1,
                    maxLines: 5,
                    textCapitalization: TextCapitalization.sentences,
                    onSubmitted: ask,
                    decoration: InputDecoration(
                      hintText: _listening ? 'Listening…' : 'Ask Fit or tell it what to do',
                      prefixIcon: const Icon(Icons.auto_awesome),
                    ),
                  ),
                ),
                const SizedBox(width: 6),
                GestureDetector(
                  onTap: _listen,
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    width: 50,
                    height: 50,
                    decoration: BoxDecoration(
                      color: _listening ? const Color(0xFFEF4444) : cs.secondaryContainer,
                      shape: BoxShape.circle,
                      boxShadow: _listening ? [BoxShadow(color: const Color(0xFFEF4444).withValues(alpha: .5), blurRadius: 18)] : null,
                    ),
                    child: Icon(_listening ? Icons.stop : Icons.mic, color: _listening ? Colors.white : cs.onSecondaryContainer),
                  ),
                ),
                const SizedBox(width: 6),
                IconButton.filled(onPressed: _thinking ? null : () => ask(_text.text), icon: const Icon(Icons.arrow_upward)),
              ]),
            ),
          ),
      ]),
    );
  }

  Widget _welcome(Map<String, dynamic>? st) {
    final cs = Theme.of(context).colorScheme;
    final insights = (st?['insights'] as List? ?? []).cast<Map>();
    final sugg = (st?['suggestions'] as List? ?? []).cast<String>();
    final name = (st?['me'] as Map?)?['name'] ?? '';
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      const SizedBox(height: 10),
      Text('Hi $name 👋', style: const TextStyle(fontSize: 26, fontWeight: FontWeight.w900)),
      const SizedBox(height: 4),
      Text('I can check your routers, explain sales, sort customer reports and — when you allow it — fix things for you. '
          'Tap the mic and just say it.', style: TextStyle(color: cs.onSurfaceVariant)),
      if (st?['ready'] == false)
        const Padding(padding: EdgeInsets.only(top: 8), child: Text('Fit is not connected to its AI yet — ask the owner to set the key on the server.')),
      const SizedBox(height: 16),
      for (final i in insights)
        Card(
          margin: const EdgeInsets.only(bottom: 8),
          child: ListTile(
            leading: Icon(i['level'] == 'bad' ? Icons.error : Icons.warning_amber,
                color: i['level'] == 'bad' ? const Color(0xFFDC2626) : const Color(0xFFF59E0B)),
            title: Text('${i['text']}'),
            trailing: i['ask'] == null ? null : const Icon(Icons.chevron_right),
            onTap: i['ask'] == null ? null : () => ask('${i['ask']}'),
          ),
        ),
      const SizedBox(height: 8),
      Wrap(spacing: 8, runSpacing: 8, children: [
        ActionChip(avatar: const Text('☀️'), label: const Text('Brief me'), onPressed: () => ask('Brief me on today: sales, anything wrong on the network, open customer reports.')),
        for (final q in sugg) ActionChip(label: Text(q), onPressed: () => ask(q)),
      ]),
    ]);
  }
}

class _FitFace extends StatelessWidget {
  const _FitFace({required this.thinking});
  final bool thinking;
  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      width: 34,
      height: 34,
      decoration: BoxDecoration(
        gradient: LinearGradient(colors: [cs.primary, cs.tertiary]),
        shape: BoxShape.circle,
        boxShadow: thinking ? [BoxShadow(color: cs.primary.withValues(alpha: .6), blurRadius: 14)] : null,
      ),
      child: const Icon(Icons.smart_toy, color: Colors.white, size: 20),
    );
  }
}

class _MsgView extends StatelessWidget {
  const _MsgView({required this.m, required this.onAsk, required this.onCardChanged});
  final Map<String, dynamic> m;
  final void Function(String) onAsk;
  final VoidCallback onCardChanged;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    if (m['role'] == 'user') {
      return Align(
        alignment: Alignment.centerRight,
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 6),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * .8),
          decoration: BoxDecoration(color: cs.primary, borderRadius: BorderRadius.circular(18)),
          child: Text('${m['text']}', style: TextStyle(color: cs.onPrimary, fontSize: 15)),
        ),
      );
    }
    final cards = (m['cards'] as Map?)?.cast<String, dynamic>() ?? {};
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if ('${m['text'] ?? ''}'.isNotEmpty)
          MarkdownBody(
            data: '${m['text']}',
            selectable: true,
            styleSheet: MarkdownStyleSheet.fromTheme(Theme.of(context)).copyWith(p: const TextStyle(fontSize: 15, height: 1.45)),
          ),
        for (final c in (cards['charts'] as List? ?? []).cast<Map>())
          Padding(
            padding: const EdgeInsets.only(top: 10),
            child: Card(child: Padding(padding: const EdgeInsets.all(14), child: FitChart(data: c.cast<String, dynamic>()))),
          ),
        for (final t in (cards['tests'] as List? ?? []).cast<Map>())
          Padding(
            padding: const EdgeInsets.only(top: 10),
            child: Card(
              child: ListTile(
                leading: Icon(t['status'] == 'done' ? Icons.network_check : Icons.error_outline),
                title: Text('${t['kind']} · ${t['router']}', style: const TextStyle(fontWeight: FontWeight.w700)),
                subtitle: Text('${t['summary'] ?? t['status']}'),
              ),
            ),
          ),
        for (final a in (cards['actions'] as List? ?? []).cast<Map>())
          Padding(padding: const EdgeInsets.only(top: 10), child: ActionCard(card: a.cast<String, dynamic>(), onChanged: onCardChanged)),
        for (final d in (cards['dashboards'] as List? ?? []).cast<Map>())
          Padding(
            padding: const EdgeInsets.only(top: 10),
            child: Card(child: ListTile(leading: const Icon(Icons.dashboard), title: Text('${d['title']}'), subtitle: const Text('Dashboard saved — open it on TapTap web'))),
          ),
      ]),
    );
  }
}

/// Something Fit proposes to do. Nothing happens until you press Run (and type the confirmation for big changes).
class ActionCard extends StatefulWidget {
  const ActionCard({super.key, required this.card, required this.onChanged});
  final Map<String, dynamic> card;
  final VoidCallback onChanged;
  @override
  State<ActionCard> createState() => _ActionCardState();
}

class _ActionCardState extends State<ActionCard> {
  bool _busy = false;
  final _confirm = TextEditingController();
  late Map<String, dynamic> c = widget.card;

  Future<void> _decide(String decision) async {
    setState(() => _busy = true);
    try {
      final d = await Api.I.post('/fit/action/${c['id']}/', {'decision': decision, if (_confirm.text.isNotEmpty) 'confirm': _confirm.text});
      setState(() => c = {...c, ...((d['card'] as Map?)?.cast<String, dynamic>() ?? {})});
      if ('${d['message'] ?? ''}'.isNotEmpty) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('${d['message']}')));
      }
      HapticFeedback.mediumImpact();
    } on ApiError catch (e) {
      final card = (e.data?['card'] as Map?)?.cast<String, dynamic>();
      if (card != null) setState(() => c = {...c, ...card});
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    } finally {
      if (mounted) setState(() => _busy = false);
      widget.onChanged();
    }
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final status = '${c['status']}';
    final danger = '${c['danger']}';
    final color = danger == 'high' ? const Color(0xFFDC2626) : (danger == 'medium' ? const Color(0xFFF59E0B) : cs.primary);
    final proposed = status == 'proposed';
    final confirm = '${c['confirm'] ?? ''}';
    return Card(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(18), side: BorderSide(color: color.withValues(alpha: .5), width: 1.5)),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Icon(proposed ? Icons.bolt : (status == 'done' ? Icons.check_circle : Icons.info_outline), color: proposed ? color : null),
            const SizedBox(width: 8),
            Expanded(child: Text('${c['title']}', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15))),
          ]),
          if ('${c['detail'] ?? ''}'.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 6), child: Text('${c['detail']}')),
          for (final f in (c['facts'] as List? ?? [])) Padding(padding: const EdgeInsets.only(top: 3), child: Text('• $f', style: const TextStyle(fontSize: 13))),
          for (final w in (c['warnings'] as List? ?? []))
            Padding(padding: const EdgeInsets.only(top: 3), child: Text('⚠️ $w', style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600))),
          const SizedBox(height: 10),
          if (proposed && c['authorized'] == false)
            Text('Fit is not allowed to do “${c['group_label']}” yet — the owner can allow it in Fit settings on TapTap.',
                style: TextStyle(fontSize: 12.5, color: cs.onSurfaceVariant))
          else if (proposed) ...[
            if (confirm.isNotEmpty) ...[
              Text('Type $confirm to confirm', style: const TextStyle(fontWeight: FontWeight.w700)),
              const SizedBox(height: 6),
              TextField(controller: _confirm, decoration: InputDecoration(hintText: confirm, isDense: true), onChanged: (_) => setState(() {})),
              const SizedBox(height: 8),
            ],
            Row(children: [
              FilledButton.icon(
                style: FilledButton.styleFrom(backgroundColor: color, minimumSize: const Size(0, 42)),
                onPressed: _busy || (confirm.isNotEmpty && _confirm.text.trim() != confirm) ? null : () => _decide('run'),
                icon: _busy ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white)) : const Icon(Icons.play_arrow),
                label: const Text('Run'),
              ),
              const SizedBox(width: 8),
              TextButton(onPressed: _busy ? null : () => _decide('dismiss'), child: const Text('Dismiss')),
            ]),
          ] else
            Text(
              status == 'done'
                  ? '✅ Done${'${c['result'] ?? ''}'.isNotEmpty ? ' — ${c['result']}' : ''}'
                  : (status == 'failed' ? '✖ ${c['result'] ?? 'Failed'}' : (status == 'expired' ? 'Expired — ask again.' : 'Dismissed')),
              style: TextStyle(fontWeight: FontWeight.w700, color: status == 'failed' ? cs.error : null),
            ),
        ]),
      ),
    );
  }
}
