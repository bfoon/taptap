import 'package:flutter/material.dart';

class SectionCard extends StatelessWidget {
  const SectionCard({super.key, required this.title, required this.child, this.trailing});
  final String title;
  final Widget child;
  final Widget? trailing;
  @override
  Widget build(BuildContext context) => Card(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Expanded(child: Text(title, style: const TextStyle(fontSize: 15.5, fontWeight: FontWeight.w800))),
              if (trailing != null) trailing!,
            ]),
            const SizedBox(height: 12),
            child,
          ]),
        ),
      );
}

/// A row with a share bar underneath (plans, sellers).
class ShareRow extends StatelessWidget {
  const ShareRow({super.key, required this.title, required this.subtitle, required this.value, required this.share, this.onTap, this.leading});
  final String title, subtitle, value;
  final double share;
  final VoidCallback? onTap;
  final Widget? leading;
  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(12),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Row(children: [
          if (leading != null) ...[leading!, const SizedBox(width: 10)],
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                Expanded(child: Text(title, style: const TextStyle(fontWeight: FontWeight.w700), overflow: TextOverflow.ellipsis)),
                Text(value, style: const TextStyle(fontWeight: FontWeight.w800)),
              ]),
              const SizedBox(height: 2),
              Text(subtitle, style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
              const SizedBox(height: 6),
              ClipRRect(
                borderRadius: BorderRadius.circular(99),
                child: LinearProgressIndicator(value: share.clamp(0, 1), minHeight: 6, backgroundColor: cs.primary.withValues(alpha: .1)),
              ),
            ]),
          ),
          if (onTap != null) Icon(Icons.chevron_right, color: cs.onSurfaceVariant),
        ]),
      ),
    );
  }
}

class ErrorCard extends StatelessWidget {
  const ErrorCard({super.key, required this.message, this.onRetry});
  final String message;
  final VoidCallback? onRetry;
  @override
  Widget build(BuildContext context) => Card(
        color: Theme.of(context).colorScheme.errorContainer,
        child: ListTile(
          leading: const Icon(Icons.wifi_off),
          title: Text(message),
          trailing: onRetry == null ? null : TextButton(onPressed: onRetry, child: const Text('Retry')),
        ),
      );
}

class InfoCard extends StatelessWidget {
  const InfoCard({super.key, required this.icon, required this.title, required this.text});
  final IconData icon;
  final String title, text;
  @override
  Widget build(BuildContext context) => Card(
        child: ListTile(leading: Icon(icon), title: Text(title, style: const TextStyle(fontWeight: FontWeight.w700)), subtitle: Text(text)),
      );
}

class EmptyNote extends StatelessWidget {
  const EmptyNote(this.text, {super.key});
  final String text;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Text(text, style: TextStyle(color: Theme.of(context).colorScheme.onSurfaceVariant)),
      );
}

/// A soft shimmering placeholder while figures load.
class SkeletonCard extends StatefulWidget {
  const SkeletonCard({super.key, this.height = 120});
  final double height;
  @override
  State<SkeletonCard> createState() => _SkeletonCardState();
}

class _SkeletonCardState extends State<SkeletonCard> with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(vsync: this, duration: const Duration(milliseconds: 1100))..repeat(reverse: true);
  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => FadeTransition(
        opacity: Tween(begin: .45, end: 1.0).animate(_c),
        child: Container(
          height: widget.height,
          decoration: BoxDecoration(color: Theme.of(context).colorScheme.surfaceContainerHighest, borderRadius: BorderRadius.circular(24)),
        ),
      );
}

class Avatar extends StatelessWidget {
  const Avatar({super.key, required this.text, this.online = false, this.radius = 22, this.color});
  final String text;
  final bool online;
  final double radius;
  final Color? color;
  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    return Stack(children: [
      CircleAvatar(
        radius: radius,
        backgroundColor: color ?? cs.primaryContainer,
        child: Text(text, style: TextStyle(fontWeight: FontWeight.w800, fontSize: radius * .62, color: color == null ? cs.onPrimaryContainer : Colors.white)),
      ),
      if (online)
        Positioned(
          right: 0,
          bottom: 0,
          child: Container(
            width: radius * .55,
            height: radius * .55,
            decoration: BoxDecoration(color: const Color(0xFF22C55E), shape: BoxShape.circle, border: Border.all(color: Theme.of(context).cardColor, width: 2)),
          ),
        ),
    ]);
  }
}
