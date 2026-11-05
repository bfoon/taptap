import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../app/sounds.dart';
import '../app/state.dart';

/// The pop-up at the top of the screen when a notification arrives while TapTap is open.
///
/// Phones only draw notifications themselves while the app is in the background; in the foreground the app gets
/// the message and shows it here instead, with the phone's notification sound — tap to open what it is about, swipe it up to dismiss.
class PushBanner {
  static OverlayEntry? _entry;
  static Timer? _timer;

  static void show({required String title, String body = '', IconData icon = Icons.notifications_active, Color? color,
      VoidCallback? onTap, bool sound = true}) {
    final overlay = navigatorKey.currentState?.overlay;
    if (overlay == null) return;
    hide();
    HapticFeedback.mediumImpact();
    if (sound) Sounds.notify();                    // the phone's notification sound, like when TapTap is closed
    final entry = OverlayEntry(
      builder: (_) => _Banner(
        title: title,
        body: body,
        icon: icon,
        color: color,
        onTap: () {
          hide();
          onTap?.call();
        },
        onDismiss: hide,
      ),
    );
    _entry = entry;
    overlay.insert(entry);
    _timer = Timer(const Duration(seconds: 6), hide);
  }

  static void hide() {
    _timer?.cancel();
    _timer = null;
    final e = _entry;
    _entry = null;
    if (e != null && e.mounted) e.remove();
  }
}

class _Banner extends StatelessWidget {
  const _Banner({required this.title, required this.body, required this.icon, required this.onTap, required this.onDismiss, this.color});
  final String title, body;
  final IconData icon;
  final Color? color;
  final VoidCallback onTap, onDismiss;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cs = theme.colorScheme;
    final accent = color ?? cs.primary;
    return Positioned(
      top: 0,
      left: 0,
      right: 0,
      child: SafeArea(
        bottom: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(10, 8, 10, 0),
          child: TweenAnimationBuilder<double>(
            tween: Tween(begin: -1.3, end: 0),
            duration: const Duration(milliseconds: 280),
            curve: Curves.easeOutCubic,
            builder: (context, v, child) => FractionalTranslation(translation: Offset(0, v), child: child),
            child: Dismissible(
              key: const ValueKey('push-banner'),
              direction: DismissDirection.up,
              onDismissed: (_) => onDismiss(),
              child: Material(
                elevation: 10,
                shadowColor: Colors.black54,
                color: theme.cardTheme.color ?? cs.surface,
                borderRadius: BorderRadius.circular(18),
                child: InkWell(
                  borderRadius: BorderRadius.circular(18),
                  onTap: onTap,
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(14, 12, 10, 12),
                    child: Row(children: [
                      Container(
                        width: 38,
                        height: 38,
                        decoration: BoxDecoration(color: accent.withValues(alpha: .14), shape: BoxShape.circle),
                        child: Icon(icon, color: accent, size: 21),
                      ),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                          Text(title, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 14.5)),
                          if (body.isNotEmpty)
                            Text(body, maxLines: 2, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 13, color: cs.onSurfaceVariant)),
                        ]),
                      ),
                      Icon(Icons.chevron_right, color: cs.onSurfaceVariant),
                    ]),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
