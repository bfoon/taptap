import 'package:flutter/material.dart';

import '../app/api.dart';
import '../app/format.dart';

/// A thin strip above the tabs while TapTap cannot be reached: the screens keep showing what this phone saved.
class OfflineStrip extends StatelessWidget {
  const OfflineStrip({super.key});

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<bool>(
      valueListenable: Api.I.online,
      builder: (context, online, _) {
        final last = Api.I.lastContact;
        return AnimatedSize(
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeOut,
          child: online
              ? const SizedBox(width: double.infinity)
              : Material(
                  color: const Color(0xFF334155),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
                    child: Row(children: [
                      const Icon(Icons.cloud_off, size: 16, color: Colors.white),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          last == null
                              ? 'Offline — showing what this phone saved'
                              : 'Offline — showing what this phone saved · last reached TapTap ${ago(last.toIso8601String())}',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(color: Colors.white, fontSize: 12, fontWeight: FontWeight.w600),
                        ),
                      ),
                    ]),
                  ),
                ),
        );
      },
    );
  }
}
