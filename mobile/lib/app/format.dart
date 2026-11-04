import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

/// Money in the business's currency ("D" for dalasi): D1,250 or D12.50.
String money(num? v, String currency) {
  final n = (v ?? 0).toDouble();
  final whole = n == n.roundToDouble();
  return '$currency${NumberFormat(whole ? '#,##0' : '#,##0.00').format(n)}';
}

String compactMoney(num? v, String currency) {
  final n = (v ?? 0).toDouble();
  if (n.abs() >= 1000000) return '$currency${(n / 1000000).toStringAsFixed(1)}M';
  if (n.abs() >= 10000) return '$currency${(n / 1000).toStringAsFixed(1)}k';
  return money(n, currency);
}

double toD(dynamic v) => v is num ? v.toDouble() : double.tryParse('$v') ?? 0;
int toI(dynamic v) => v is num ? v.toInt() : int.tryParse('$v') ?? 0;

DateTime? parseTime(dynamic s) => s == null || s == '' ? null : DateTime.tryParse('$s')?.toLocal();

String clock(dynamic s) {
  final t = parseTime(s);
  return t == null ? '' : DateFormat.Hm().format(t);
}

String ago(dynamic s) {
  final t = parseTime(s);
  if (t == null) return '';
  final d = DateTime.now().difference(t);
  if (d.inSeconds < 60) return 'just now';
  if (d.inMinutes < 60) return '${d.inMinutes} min ago';
  if (d.inHours < 24) return '${d.inHours} h ago';
  if (d.inDays < 7) return DateFormat.E().add_Hm().format(t);
  return DateFormat.MMMd().format(t);
}

String dayLabel(DateTime d) {
  final today = DateUtils.dateOnly(DateTime.now());
  final x = DateUtils.dateOnly(d);
  if (x == today) return 'Today';
  if (x == today.subtract(const Duration(days: 1))) return 'Yesterday';
  return DateFormat('EEE d MMM').format(x);
}

String ymd(DateTime d) => DateFormat('yyyy-MM-dd').format(d);

Color hexColor(String? hex, [Color fallback = const Color(0xFF1769E0)]) {
  var h = (hex ?? '').replaceAll('#', '').trim();
  if (h.length == 3) h = h.split('').map((c) => '$c$c').join();
  final v = int.tryParse(h, radix: 16);
  return (h.length == 6 && v != null) ? Color(0xFF000000 | v) : fallback;
}

/// A JSON list of objects as string-keyed maps (anything else in the list is skipped).
List<Map<String, dynamic>> asMaps(dynamic v) =>
    ((v is List) ? v : const []).whereType<Map>().map((e) => e.cast<String, dynamic>()).toList();

/// A moment near now, past or coming: "today 14:30", "tomorrow 09:00", "Fri 12 Jun 09:00".
String whenText(dynamic s) {
  final t = parseTime(s);
  if (t == null) return '';
  final today = DateUtils.dateOnly(DateTime.now());
  final d = DateUtils.dateOnly(t);
  final hm = DateFormat.Hm().format(t);
  if (d == today) return 'today $hm';
  if (d == today.add(const Duration(days: 1))) return 'tomorrow $hm';
  if (d == today.subtract(const Duration(days: 1))) return 'yesterday $hm';
  return '${DateFormat('EEE d MMM').format(t)} $hm';
}

/// A length of time from seconds: "45 min", "2 h 15 min", "3 days 4 h".
String spanText(int seconds) {
  if (seconds <= 0) return '0 min';
  final m = (seconds / 60).ceil();
  final d = m ~/ 1440, h = (m % 1440) ~/ 60, mm = m % 60;
  if (d > 0) return '$d day${d == 1 ? '' : 's'}${h > 0 ? ' $h h' : ''}';
  if (h > 0) return '$h h${mm > 0 ? ' $mm min' : ''}';
  return '$mm min';
}
