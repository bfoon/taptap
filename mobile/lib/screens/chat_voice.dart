import 'dart:async';
import 'dart:io';
import 'dart:math' as math;

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:record/record.dart';

import '../app/api.dart';
import '../app/format.dart';

/// Voice messages in chat — up to [voiceMax] seconds, deleted for good 1 minute after someone first plays them.
///
/// Recording: [recordVoice] opens a sheet that records AAC (.m4a) to a temporary file, stops by itself at 30 s,
/// lets you listen back, and returns the file to send (or null). The temporary file is deleted after sending.
///
/// Playing: [VoiceBubble] downloads the recording only when ▶ is pressed (?listen=1 — TapTap counts that as the
/// first listen), plays it from a temporary file and deletes that file as soon as it ends or its minute is up.
/// Nothing is kept on the phone.
const voiceMax = 30;

String _mmss(num s) {
  final v = s.round().clamp(0, 5999);
  return '${v ~/ 60}:${(v % 60).toString().padLeft(2, '0')}';
}

Future<Directory> _tmp() => Directory.systemTemp.createTemp('taptap-voice');

Future<void> _rm(String? path) async {
  if (path == null) return;
  try {
    final f = File(path);
    if (await f.exists()) await f.delete();
    final d = f.parent;
    if (d.path.contains('taptap-voice') && await d.exists()) await d.delete(recursive: true);
  } catch (_) {}
}

/// A finished recording, ready to upload.
class VoiceClip {
  VoiceClip(this.file, this.seconds);
  final File file;
  final double seconds;

  /// Delete the temporary file (after it was sent, or when it is discarded).
  Future<void> discard() => _rm(file.path);
}

/// Open the recorder. Returns the clip to send, or null (cancelled, no microphone permission, too short).
Future<VoiceClip?> recordVoice(BuildContext context) {
  return showModalBottomSheet<VoiceClip>(
    context: context,
    isDismissible: false,
    enableDrag: false,
    showDragHandle: true,
    builder: (_) => const _RecorderSheet(),
  );
}

class _RecorderSheet extends StatefulWidget {
  const _RecorderSheet();
  @override
  State<_RecorderSheet> createState() => _RecorderSheetState();
}

class _RecorderSheetState extends State<_RecorderSheet> {
  final _rec = AudioRecorder();
  final _player = AudioPlayer();
  final _levels = <double>[];
  StreamSubscription<Amplitude>? _amp;
  StreamSubscription<void>? _done;
  Timer? _tick;
  DateTime? _t0;
  String? _path;
  double _secs = 0;
  String _state = 'asking';                        // asking · rec · ready · error
  String _error = '';
  bool _previewing = false;
  bool _handedOver = false;                        // the file now belongs to the caller

  @override
  void initState() {
    super.initState();
    _done = _player.onPlayerComplete.listen((_) {
      if (mounted) setState(() => _previewing = false);
    });
    _start();
  }

  Future<void> _start() async {
    try {
      if (!await _rec.hasPermission()) {
        return setState(() {
          _state = 'error';
          _error = 'Allow the microphone for TapTap in your phone’s settings to send voice messages.';
        });
      }
      final dir = await _tmp();
      _path = '${dir.path}/voice.m4a';
      await _rec.start(const RecordConfig(encoder: AudioEncoder.aacLc, bitRate: 48000, sampleRate: 44100, numChannels: 1),
          path: _path!);
      HapticFeedback.mediumImpact();
      _t0 = DateTime.now();
      _amp = _rec.onAmplitudeChanged(const Duration(milliseconds: 120)).listen((a) {
        final v = ((a.current + 50) / 50).clamp(0.05, 1.0).toDouble();   // dBFS → 0..1
        if (!mounted) return;
        setState(() {
          _levels.add(v);
          if (_levels.length > 40) _levels.removeAt(0);
        });
      });
      _tick = Timer.periodic(const Duration(milliseconds: 200), (_) {
        final s = DateTime.now().difference(_t0!).inMilliseconds / 1000;
        if (s >= voiceMax) {
          _stop();
        } else if (mounted) {
          setState(() => _secs = s);
        }
      });
      if (mounted) setState(() => _state = 'rec');
    } catch (_) {
      if (mounted) {
        setState(() {
          _state = 'error';
          _error = 'This phone could not start recording. Try again.';
        });
      }
    }
  }

  Future<void> _stop() async {
    if (_state != 'rec') return;
    _tick?.cancel();
    await _amp?.cancel();
    final secs = math.min(voiceMax.toDouble(), DateTime.now().difference(_t0!).inMilliseconds / 1000);
    String? p;
    try {
      p = await _rec.stop();
    } catch (_) {}
    p ??= _path;
    if (!mounted) return;
    final f = p == null ? null : File(p);
    if (secs < 1 || f == null || !f.existsSync() || f.lengthSync() < 200) {
      await _rm(p);
      setState(() {
        _state = 'error';
        _error = 'That was too short — record for a second or more.';
      });
      return;
    }
    _path = p;
    HapticFeedback.lightImpact();
    setState(() {
      _secs = secs;
      _state = 'ready';
    });
  }

  Future<void> _preview() async {
    if (_path == null) return;
    if (_previewing) {
      await _player.pause();
      setState(() => _previewing = false);
    } else {
      await _player.play(DeviceFileSource(_path!));
      setState(() => _previewing = true);
    }
  }

  void _send() {
    if (_path == null) return;
    _handedOver = true;
    Navigator.pop(context, VoiceClip(File(_path!), _secs));
  }

  @override
  void dispose() {
    _tick?.cancel();
    _amp?.cancel();
    _done?.cancel();
    _player.dispose();
    () async {
      try {
        if (await _rec.isRecording()) await _rec.stop();
      } catch (_) {}
      await _rec.dispose();
      if (!_handedOver) await _rm(_path);      // cancelled: nothing stays on the phone
    }();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final ending = _state == 'rec' && voiceMax - _secs <= 5.5;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 16),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          const Text('🎙️ Voice message', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w800)),
          Text('Up to $voiceMax seconds · deleted for everyone 1 minute after it is played',
              style: TextStyle(fontSize: 12.5, color: cs.onSurfaceVariant)),
          const SizedBox(height: 18),
          if (_state == 'asking') const Center(child: Padding(padding: EdgeInsets.all(18), child: CircularProgressIndicator())),
          if (_state == 'error') ...[
            Text(_error, textAlign: TextAlign.center),
            const SizedBox(height: 14),
            FilledButton(onPressed: () => Navigator.pop(context), child: const Text('Close')),
          ],
          if (_state == 'rec') ...[
            Row(mainAxisAlignment: MainAxisAlignment.center, children: [
              const _Blink(),
              const SizedBox(width: 10),
              Text(_mmss(_secs) + (ending ? ' · ${(voiceMax - _secs).ceil()}' : ''),
                  style: TextStyle(fontSize: 30, fontWeight: FontWeight.w800, color: ending ? Colors.red : null,
                      fontFeatures: const [FontFeature.tabularFigures()])),
              Text('  / ${_mmss(voiceMax)}', style: TextStyle(color: cs.onSurfaceVariant)),
            ]),
            const SizedBox(height: 12),
            SizedBox(height: 40, child: CustomPaint(painter: _LevelsPainter(_levels, Colors.red))),
            const SizedBox(height: 10),
            LinearProgressIndicator(value: _secs / voiceMax, minHeight: 6, borderRadius: BorderRadius.circular(3)),
            const SizedBox(height: 18),
            Row(children: [
              Expanded(child: OutlinedButton.icon(onPressed: () => Navigator.pop(context), icon: const Icon(Icons.delete_outline), label: const Text('Discard'))),
              const SizedBox(width: 10),
              Expanded(
                child: FilledButton.icon(
                  style: FilledButton.styleFrom(backgroundColor: Colors.red),
                  onPressed: _stop,
                  icon: const Icon(Icons.stop),
                  label: const Text('Stop'),
                ),
              ),
            ]),
          ],
          if (_state == 'ready') ...[
            Row(mainAxisAlignment: MainAxisAlignment.center, children: [
              IconButton.filled(iconSize: 30, onPressed: _preview, icon: Icon(_previewing ? Icons.pause : Icons.play_arrow)),
              const SizedBox(width: 12),
              Text(_mmss(_secs), style: const TextStyle(fontSize: 26, fontWeight: FontWeight.w800)),
            ]),
            const SizedBox(height: 18),
            Row(children: [
              Expanded(child: OutlinedButton.icon(onPressed: () => Navigator.pop(context), icon: const Icon(Icons.delete_outline), label: const Text('Discard'))),
              const SizedBox(width: 10),
              Expanded(child: FilledButton.icon(onPressed: _send, icon: const Icon(Icons.send), label: const Text('Send'))),
            ]),
          ],
        ]),
      ),
    );
  }
}

class _Blink extends StatefulWidget {
  const _Blink();
  @override
  State<_Blink> createState() => _BlinkState();
}

class _BlinkState extends State<_Blink> with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 900))..repeat(reverse: true);
  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => FadeTransition(
        opacity: Tween(begin: 1.0, end: .25).animate(_c),
        child: Container(width: 14, height: 14, decoration: const BoxDecoration(color: Colors.red, shape: BoxShape.circle)),
      );
}

class _LevelsPainter extends CustomPainter {
  _LevelsPainter(this.levels, this.color);
  final List<double> levels;
  final Color color;
  @override
  void paint(Canvas canvas, Size size) {
    final p = Paint()..color = color;
    const w = 4.0, gap = 3.0;
    final n = (size.width / (w + gap)).floor();
    final start = math.max(0, levels.length - n);
    for (var i = start; i < levels.length; i++) {
      final h = math.max(3.0, levels[i] * size.height);
      final x = (i - start) * (w + gap);
      canvas.drawRRect(RRect.fromLTRBR(x, (size.height - h) / 2, x + w, (size.height + h) / 2, const Radius.circular(2)), p);
    }
  }

  @override
  bool shouldRepaint(_LevelsPainter old) => true;
}

/// The voice message inside a chat bubble: ▶, a waveform that fills as it plays, its length, and what happens to it.
class VoiceBubble extends StatefulWidget {
  const VoiceBubble({super.key, required this.m, required this.mine});
  final Map<String, dynamic> m;
  final bool mine;
  @override
  State<VoiceBubble> createState() => _VoiceBubbleState();
}

class _VoiceBubbleState extends State<VoiceBubble> {
  AudioPlayer? _player;
  String? _file;
  bool _loading = false, _playing = false, _goneHere = false;
  Duration _pos = Duration.zero;
  DateTime? _deadline;                             // when it is deleted (from the server's "left", not the phone clock)
  Timer? _tick;
  final _subs = <StreamSubscription>[];

  Map<String, dynamic> get _img => (widget.m['image'] as Map).cast<String, dynamic>();
  int get _id => toI(widget.m['id']);
  int get _secs => math.max(1, toI(_img['secs']));
  bool get _gone => _goneHere || _img['gone'] == true;

  @override
  void initState() {
    super.initState();
    _syncDeadline();
  }

  @override
  void didUpdateWidget(VoiceBubble old) {
    super.didUpdateWidget(old);
    _syncDeadline();
    if (_gone) _stop();
  }

  void _syncDeadline() {
    if (_gone || _img['listened'] == null) return;
    _deadline ??= DateTime.now().add(Duration(seconds: _img['left'] == null ? 60 : toI(_img['left'])));
    _tick ??= Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) return;
      if (_deadline != null && DateTime.now().isAfter(_deadline!)) {
        _stop();
        _tick?.cancel();
        setState(() => _goneHere = true);
      } else {
        setState(() {});
      }
    });
  }

  Future<void> _stop() async {
    final p = _player, f = _file;
    _player = null;
    _file = null;
    for (final s in _subs) {
      await s.cancel();
    }
    _subs.clear();
    if (p != null) {
      try {
        await p.stop();
      } catch (_) {}
      await p.dispose();
    }
    await _rm(f);                                  // the downloaded copy never stays on the phone
    if (mounted) {
      setState(() {
        _playing = false;
        _loading = false;
        _pos = Duration.zero;
      });
    }
  }

  Future<void> _toggle() async {
    if (_gone || _loading) return;
    final p = _player;
    if (p != null) {
      if (_playing) {
        await p.pause();
      } else {
        await p.resume();
      }
      return;
    }
    setState(() => _loading = true);
    try {
      final bytes = await Api.I.bytes('/chat/img/$_id/', query: {'listen': '1'});
      final dir = await _tmp();
      final f = File('${dir.path}/voice-$_id.${_ext(bytes)}');
      await f.writeAsBytes(bytes, flush: true);
      if (!mounted) return _rm(f.path);
      final pl = AudioPlayer();
      _player = pl;
      _file = f.path;
      _subs
        ..add(pl.onPlayerStateChanged.listen((s) {
          if (mounted) setState(() => _playing = s == PlayerState.playing);
        }))
        ..add(pl.onPositionChanged.listen((d) {
          if (mounted) setState(() => _pos = d);
        }))
        ..add(pl.onPlayerComplete.listen((_) => _stop()));
      if (!widget.mine && _img['listened'] == null) {
        _img['listened'] = DateTime.now().toIso8601String();      // its last minute starts now
        _img['left'] = 60;
        _syncDeadline();
      }
      await pl.play(DeviceFileSource(f.path));
      if (mounted) setState(() => _loading = false);
    } on ApiError catch (e) {
      if (mounted) {
        setState(() {
          _loading = false;
          if (e.status == 410) _goneHere = true;
        });
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message), behavior: SnackBarBehavior.floating));
      }
    } catch (_) {
      await _stop();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
            const SnackBar(content: Text('This voice message could not be played.'), behavior: SnackBarBehavior.floating));
      }
    }
  }

  static String _ext(List<int> b) {
    if (b.length > 8 && b[4] == 0x66 && b[5] == 0x74 && b[6] == 0x79 && b[7] == 0x70) return 'm4a';   // ftyp
    if (b.length > 4 && b[0] == 0x4F && b[1] == 0x67 && b[2] == 0x67 && b[3] == 0x53) return 'ogg';   // OggS
    return 'webm';
  }

  @override
  void dispose() {
    _tick?.cancel();
    _stop();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final ink = widget.mine ? Colors.white : cs.onSurface;
    final soft = widget.mine ? Colors.white70 : cs.onSurfaceVariant;
    if (_gone) {
      final heard = _goneHere || _img['listened'] == true;
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(heard ? '🎙️ Voice message · listened' : '🎙️ Voice message expired',
              style: TextStyle(fontWeight: FontWeight.w700, color: ink)),
          Text(heard ? '🔥 Deleted for good 1 minute after it was played' : 'Not played within 7 days',
              style: TextStyle(fontSize: 11.5, color: soft)),
        ]),
      );
    }
    final total = _secs.toDouble();
    final frac = (_pos.inMilliseconds / 1000 / total).clamp(0.0, 1.0);
    String note;
    if (_img['listened'] != null && _deadline != null) {
      final left = math.max(0, _deadline!.difference(DateTime.now()).inSeconds);
      note = '🔥 ${widget.mine ? 'Played · ' : ''}deleted in $left s';
    } else {
      note = widget.mine ? '👂 Not played yet · gone 1 min after it is' : '▶ Play · deleted for everyone 1 min later';
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(mainAxisSize: MainAxisSize.min, children: [
          SizedBox(
            width: 40,
            height: 40,
            child: IconButton.filled(
              padding: EdgeInsets.zero,
              style: IconButton.styleFrom(
                  backgroundColor: widget.mine ? Colors.white : cs.primary, foregroundColor: widget.mine ? cs.primary : Colors.white),
              onPressed: _toggle,
              icon: _loading
                  ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2.4))
                  : Icon(_playing ? Icons.pause : Icons.play_arrow),
            ),
          ),
          const SizedBox(width: 8),
          SizedBox(width: 130, height: 28, child: CustomPaint(painter: _WavePainter(_id, frac, ink))),
          const SizedBox(width: 8),
          Text(_mmss(_pos > Duration.zero ? _pos.inMilliseconds / 1000 : total),
              style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: ink, fontFeatures: const [FontFeature.tabularFigures()])),
        ]),
        const SizedBox(height: 3),
        Text(note, style: TextStyle(fontSize: 11, color: soft, fontWeight: _deadline != null ? FontWeight.w700 : FontWeight.w400)),
      ]),
    );
  }
}

/// A steady waveform per message (drawn, not measured), filled up to how far it has played.
class _WavePainter extends CustomPainter {
  _WavePainter(this.seed, this.frac, this.color);
  final int seed;
  final double frac;
  final Color color;
  @override
  void paint(Canvas canvas, Size size) {
    const n = 26;
    final bw = size.width / n * .62;
    var x = (seed * 2654435761) & 0xFFFFFFFF;
    for (var i = 0; i < n; i++) {
      x = (x * 1103515245 + 12345) & 0xFFFFFFFF;
      final env = math.sin(math.pi * (i + .5) / n);
      final v = (22 + ((x >> 16) % 70) * (.45 + .55 * env)).clamp(10, 100) / 100;
      final h = v * size.height;
      final left = i * size.width / n;
      final on = (i + 1) / n <= frac + .001;
      canvas.drawRRect(RRect.fromLTRBR(left, (size.height - h) / 2, left + bw, (size.height + h) / 2, const Radius.circular(2)),
          Paint()..color = color.withValues(alpha: on ? 1 : .38));
    }
  }

  @override
  bool shouldRepaint(_WavePainter old) => old.frac != frac || old.color != color || old.seed != seed;
}
