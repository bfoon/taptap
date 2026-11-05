// TapTap app — small checks that run on GitHub with `flutter test` (no phone, no server needed).
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:taptap_app/app/format.dart';
import 'package:taptap_app/app/state.dart';
import 'package:taptap_app/screens/notifications_screen.dart';
import 'package:taptap_app/widgets/common.dart';
import 'package:taptap_app/widgets/push_banner.dart';
import 'package:taptap_app/widgets/voucher_widgets.dart';

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

  group('vouchers', () {
    test('time spans', () {
      expect(spanText(0), '0 min');
      expect(spanText(45 * 60), '45 min');
      expect(spanText(7200), '2 h');
      expect(spanText(8100), '2 h 15 min');
      expect(spanText(90000), '1 day 1 h');
      expect(spanText(3 * 86400), '3 days');
    });
    test('JSON lists of objects', () {
      expect(asMaps([
        {'a': 1},
        'x',
        null,
      ]).length, 1);
      expect(asMaps(null), isEmpty);
    });
    test('a moment today', () => expect(whenText(DateTime.now().toIso8601String()), startsWith('today ')));
    test('the line under a voucher', () {
      expect(voucherLine({'state': 'stock', 'created_at': DateTime.now().toIso8601String(), 'holder': 'Musa', 'batch': 'Morning'}),
          'made just now · Musa · Morning');
    });
  });

  testWidgets('a voucher tile shows its code, state and plan', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: VoucherTile(currency: 'D', v: const {
          'id': 1, 'code': 'KN4F7Q2A', 'state': 'used', 'state_label': 'In use', 'plan': 'Day', 'price': 25, 'serial': '0042', 'holder': 'Shop',
        }),
      ),
    ));
    expect(find.text('KN4F7Q2A'), findsOneWidget);
    expect(find.text('In use'), findsOneWidget);
    expect(find.text('Day · D25 · #0042'), findsOneWidget);
  });

  testWidgets('a batch shows how many are left', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: BatchSummary(currency: 'D', batch: const {'id': 3, 'name': 'Market day', 'plan': 'Day', 'price': 25, 'count': 20, 'left': 8, 'used': 5}),
      ),
    ));
    expect(find.text('Market day'), findsOneWidget);
    expect(find.text('8 of 20 left'), findsOneWidget);
    expect(find.text('5 used by customers'), findsOneWidget);
  });

  testWidgets('a notification while the app is open pops up and opens on tap', (tester) async {
    var opened = false;
    await tester.pumpWidget(MaterialApp(navigatorKey: navigatorKey, home: const Scaffold(body: SizedBox())));
    PushBanner.show(title: 'Awa reported voucher KN4F7Q2A', body: 'It says wrong code', onTap: () => opened = true);
    await tester.pumpAndSettle();
    expect(find.text('It says wrong code'), findsOneWidget);
    await tester.tap(find.text('It says wrong code'));
    await tester.pump();
    expect(opened, isTrue);
    expect(find.text('It says wrong code'), findsNothing);
  });

  testWidgets('check delivery: each stop shows what was found, and the fix where it stops', (tester) async {
    var fixed = false;
    final road = [
      DeliveryStop('Google accepted a test push')
        ..state = StopState.ok
        ..text = 'Google took it for delivery.',
      DeliveryStop('This phone shows TapTap notifications')
        ..state = StopState.fail
        ..text = 'Pushes arrive, but the phone hides them.'
        ..fixLabel = 'Turn on'
        ..fix = () async => fixed = true,
      DeliveryStop('It also arrives while TapTap is closed')..state = StopState.skipped,
    ];
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Column(children: [
          for (var i = 0; i < road.length; i++)
            DeliveryStopRow(stop: road[i], last: i == road.length - 1, passed: road[i].state == StopState.ok),
        ]),
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.text('Google took it for delivery.'), findsOneWidget);
    expect(find.byIcon(Icons.check), findsOneWidget);
    expect(find.byIcon(Icons.close), findsOneWidget);
    await tester.tap(find.text('Turn on'));
    expect(fixed, isTrue);
  });
}
