import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../app/state.dart';
import '../screens/new_vouchers_screen.dart';
import '../screens/quick_sell.dart';
import '../screens/routers_screen.dart';

/// The things people do many times a day, one tap from Today: sell a voucher to the customer in front of you,
/// make a batch for an agent, see each router and who is on it. Only what the person's role allows is shown.
class QuickActions extends StatelessWidget {
  const QuickActions({super.key});

  @override
  Widget build(BuildContext context) {
    final app = context.watch<AppState>();
    final items = <_Item>[
      if (app.canDo('vouchers_create'))
        _Item(Icons.bolt, 'Quick sell', const Color(0xFFF59E0B), () => showQuickSell(context)),
      if (app.canDo('vouchers_create'))
        _Item(Icons.library_add_outlined, 'New batch', const Color(0xFF1769E0),
            () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const NewVouchersScreen()))),
      if (app.canDo('routers'))
        _Item(Icons.router_outlined, 'Routers', const Color(0xFF16A34A),
            () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const RoutersScreen()))),
    ];
    if (items.isEmpty) return const SizedBox.shrink();
    return Row(children: [
      for (var i = 0; i < items.length; i++) ...[
        if (i > 0) const SizedBox(width: 8),
        Expanded(child: _Tile(item: items[i])),
      ],
    ]);
  }
}

class _Item {
  const _Item(this.icon, this.label, this.color, this.onTap);
  final IconData icon;
  final String label;
  final Color color;
  final VoidCallback onTap;
}

class _Tile extends StatelessWidget {
  const _Tile({required this.item});
  final _Item item;

  @override
  Widget build(BuildContext context) => Material(
        color: item.color.withValues(alpha: .12),
        borderRadius: BorderRadius.circular(18),
        child: InkWell(
          borderRadius: BorderRadius.circular(18),
          onTap: item.onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 8),
            child: Column(children: [
              Icon(item.icon, color: item.color, size: 26),
              const SizedBox(height: 6),
              Text(item.label, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 13)),
            ]),
          ),
        ),
      );
}
