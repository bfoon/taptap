import 'package:flutter/material.dart';

import '../app/format.dart';

/// Colour of a voucher state (keys from core/voucher_history.display_state).
Color stateColor(BuildContext context, String state) {
  final cs = Theme.of(context).colorScheme;
  switch (state) {
    case 'stock':
      return cs.primary;
    case 'sold':
      return const Color(0xFFD97706);
    case 'used':
      return const Color(0xFF16A34A);
    case 'disabled':
      return cs.error;
    case 'frozen':
    case 'warned':
      return const Color(0xFF0891B2);
    default:
      return cs.onSurfaceVariant;           // expired, archived
  }
}

class StatePill extends StatelessWidget {
  const StatePill({super.key, required this.state, required this.label});
  final String state, label;
  @override
  Widget build(BuildContext context) {
    final c = stateColor(context, state);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
      decoration: BoxDecoration(color: c.withValues(alpha: .12), borderRadius: BorderRadius.circular(99)),
      child: Text(label, style: TextStyle(color: c, fontWeight: FontWeight.w800, fontSize: 11.5)),
    );
  }
}

/// The second line under a voucher: what matters most about it right now.
String voucherLine(Map<String, dynamic> v) {
  final state = '${v['state']}';
  final parts = <String>[];
  if (state == 'used' && v['ends_at'] != null) {
    parts.add('ends ${whenText(v['ends_at'])}');
  } else if (state == 'expired' && v['ends_at'] != null) {
    parts.add('ended ${whenText(v['ends_at'])}');
  } else if (state == 'sold' && v['sold_at'] != null) {
    parts.add('sold ${ago(v['sold_at'])}');
  } else if (v['created_at'] != null) {
    parts.add('made ${ago(v['created_at'])}');
  }
  if ('${v['customer'] ?? ''}'.isNotEmpty) parts.add('${v['customer']}');
  if ('${v['holder'] ?? 'Shop'}' != 'Shop') parts.add('${v['holder']}');
  if ('${v['batch'] ?? ''}'.isNotEmpty) parts.add('${v['batch']}');
  return parts.join(' · ');
}

class VoucherTile extends StatelessWidget {
  const VoucherTile({super.key, required this.v, required this.currency, this.onTap});
  final Map<String, dynamic> v;
  final String currency;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final serial = '${v['serial'] ?? ''}';
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(22),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 10, 12),
          child: Row(children: [
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  Flexible(
                    child: Text('${v['code']}',
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontFamily: 'monospace', fontWeight: FontWeight.w900, fontSize: 16.5, letterSpacing: 1)),
                  ),
                  const SizedBox(width: 8),
                  StatePill(state: '${v['state']}', label: '${v['state_label']}'),
                ]),
                const SizedBox(height: 4),
                Text('${v['plan']} · ${money(toD(v['price']), currency)}${serial.isEmpty ? '' : ' · #$serial'}',
                    style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 13)),
                if (voucherLine(v).isNotEmpty)
                  Text(voucherLine(v), maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
              ]),
            ),
            if (onTap != null) Icon(Icons.chevron_right, color: cs.onSurfaceVariant),
          ]),
        ),
      ),
    );
  }
}

/// A batch: plan, holder, how many are left, and a bar of how much of it is sold or used.
class BatchSummary extends StatelessWidget {
  const BatchSummary({super.key, required this.batch, required this.currency, this.onTap});
  final Map<String, dynamic> batch;
  final String currency;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final count = toI(batch['count']);
    final left = toI(batch['left']);
    final gone = count == 0 ? 0.0 : (count - left) / count;
    final holder = '${batch['holder'] ?? 'Shop'}';
    final settlement = '${batch['settlement'] ?? ''}';
    return Card(
      child: InkWell(
        borderRadius: BorderRadius.circular(22),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 14, 12, 14),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Expanded(child: Text('${batch['name']}', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 15.5))),
              Text('$left of $count left', style: const TextStyle(fontWeight: FontWeight.w800)),
              if (onTap != null) Icon(Icons.chevron_right, color: cs.onSurfaceVariant),
            ]),
            const SizedBox(height: 2),
            Text(
              [
                if ('${batch['plan'] ?? ''}'.isNotEmpty) '${batch['plan']} · ${money(toD(batch['price']), currency)}',
                holder == 'Shop' ? 'your shop' : (settlement.isEmpty ? holder : '$holder — ${settlement.split(' —').first.toLowerCase()}'),
                'made ${ago(batch['created_at'])}',
              ].join(' · '),
              style: TextStyle(fontSize: 12.5, color: cs.onSurfaceVariant),
            ),
            const SizedBox(height: 8),
            ClipRRect(
              borderRadius: BorderRadius.circular(99),
              child: LinearProgressIndicator(value: gone.clamp(0.0, 1.0), minHeight: 6, backgroundColor: cs.primary.withValues(alpha: .1)),
            ),
            if (toI(batch['used']) > 0) ...[
              const SizedBox(height: 4),
              Text('${batch['used']} used by customers', style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
            ],
          ]),
        ),
      ),
    );
  }
}
