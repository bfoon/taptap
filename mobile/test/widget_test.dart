// TapTap app — small checks that run on GitHub with `flutter test` (no phone, no server needed).
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:taptap_app/app/format.dart';
import 'package:taptap_app/widgets/common.dart';

void main() {
  group('money', () {
    test('whole amounts have no decimals', () => expect(money(1250, 'D'), 'D1,250'));
    test('cents are kept', () => expect(money(12.5, 'D'), 'D12.50'));
    test('null is zero', () => expect(money(null, 'D'), 'D0'));
    test('compact', () => expect(compactMoney(15300, 'D'), 'D15.3k'));
  });

  group('parsing', () {
    test('numbers from JSON', () {
      expect(toD('12.5'), 12.5);
      expect(toI(7.9), 7);
      expect(toI('x'), 0);
    });
    test('brand colour', () {
      expect(hexColor('#ff6a3d'), const Color(0xFFFF6A3D));
      expect(hexColor('#abc'), const Color(0xFFAABBCC));
      expect(hexColor('nope'), const Color(0xFF1769E0));
    });
    test('day label', () {
      expect(dayLabel(DateTime.now()), 'Today');
      expect(dayLabel(DateTime.now().subtract(const Duration(days: 1))), 'Yesterday');
    });
  });

  testWidgets('a share row shows its title and value', (tester) async {
    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(body: ShareRow(title: '24 Hours', subtitle: '3 sold', value: 'D75', share: .5)),
    ));
    expect(find.text('24 Hours'), findsOneWidget);
    expect(find.text('D75'), findsOneWidget);
  });
}
