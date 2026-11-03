import 'package:fl_chart/fl_chart.dart';
import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../app/format.dart';

/// 24 hourly bars; the busiest hour stands out and the current hour is marked.
class HourBars extends StatelessWidget {
  const HourBars({super.key, required this.hours, required this.currency, this.isToday = true, this.height = 150});
  final List hours;
  final String currency;
  final bool isToday;
  final double height;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final vals = [for (final h in hours) toD((h as Map)['amount'])];
    final peak = vals.fold<double>(0, (a, b) => b > a ? b : a);
    final now = DateTime.now().hour;
    return SizedBox(
      height: height,
      child: BarChart(BarChartData(
        maxY: peak <= 0 ? 1 : peak * 1.15,
        alignment: BarChartAlignment.spaceBetween,
        gridData: const FlGridData(show: false),
        borderData: FlBorderData(show: false),
        titlesData: FlTitlesData(
          leftTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
          rightTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
          topTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
          bottomTitles: AxisTitles(
            sideTitles: SideTitles(
              showTitles: true,
              reservedSize: 20,
              getTitlesWidget: (v, _) => v.toInt() % 6 == 0
                  ? Text('${v.toInt()}h', style: TextStyle(fontSize: 10, color: cs.onSurfaceVariant))
                  : const SizedBox.shrink(),
            ),
          ),
        ),
        barTouchData: BarTouchData(
          touchTooltipData: BarTouchTooltipData(
            getTooltipColor: (_) => cs.inverseSurface,
            getTooltipItem: (g, _, r, __) => BarTooltipItem(
              '${g.x.toString().padLeft(2, '0')}:00\n${money(r.toY, currency)} · ${toI((hours[g.x] as Map)['count'])} sold',
              TextStyle(color: cs.onInverseSurface, fontWeight: FontWeight.w600, fontSize: 12),
            ),
          ),
        ),
        barGroups: [
          for (var i = 0; i < vals.length; i++)
            BarChartGroupData(x: i, barRods: [
              BarChartRodData(
                toY: vals[i],
                width: 8,
                borderRadius: BorderRadius.circular(4),
                color: vals[i] == peak && peak > 0
                    ? cs.primary
                    : (isToday && i == now ? cs.tertiary : cs.primary.withOpacity(.35)),
                backDrawRodData: BackgroundBarChartRodData(show: true, toY: peak <= 0 ? 1 : peak * 1.15, color: cs.primary.withOpacity(.05)),
              ),
            ]),
        ],
      )),
    );
  }
}

/// The last 7 days, the chosen day highlighted.
class WeekBars extends StatelessWidget {
  const WeekBars({super.key, required this.week, required this.currency, this.onTapDay});
  final List week;
  final String currency;
  final void Function(DateTime day)? onTapDay;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final vals = [for (final d in week) toD((d as Map)['amount'])];
    final peak = vals.fold<double>(0, (a, b) => b > a ? b : a);
    return SizedBox(
      height: 140,
      child: BarChart(BarChartData(
        maxY: peak <= 0 ? 1 : peak * 1.2,
        gridData: const FlGridData(show: false),
        borderData: FlBorderData(show: false),
        titlesData: FlTitlesData(
          leftTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
          rightTitles: const AxisTitles(sideTitles: SideTitles(showTitles: false)),
          topTitles: AxisTitles(
            sideTitles: SideTitles(
              showTitles: true,
              reservedSize: 18,
              getTitlesWidget: (v, _) => Text(compactMoney(vals[v.toInt()], currency),
                  style: TextStyle(fontSize: 9.5, color: cs.onSurfaceVariant, fontWeight: FontWeight.w600)),
            ),
          ),
          bottomTitles: AxisTitles(
            sideTitles: SideTitles(
              showTitles: true,
              reservedSize: 20,
              getTitlesWidget: (v, _) {
                final d = DateTime.tryParse('${(week[v.toInt()] as Map)['day']}');
                return Text(d == null ? '' : DateFormat.E().format(d).substring(0, 2),
                    style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant));
              },
            ),
          ),
        ),
        barTouchData: BarTouchData(
          enabled: onTapDay != null,
          touchCallback: (e, r) {
            if (e is FlTapUpEvent && r?.spot != null) {
              final d = DateTime.tryParse('${(week[r!.spot!.touchedBarGroupIndex] as Map)['day']}');
              if (d != null) onTapDay?.call(d);
            }
          },
          touchTooltipData: BarTouchTooltipData(getTooltipItem: (_, __, ___, ____) => null),
        ),
        barGroups: [
          for (var i = 0; i < vals.length; i++)
            BarChartGroupData(x: i, barRods: [
              BarChartRodData(
                toY: vals[i],
                width: 22,
                borderRadius: BorderRadius.circular(7),
                color: i == vals.length - 1 ? cs.primary : cs.primary.withOpacity(.3),
              ),
            ]),
        ],
      )),
    );
  }
}

/// A Fit chart card ({type, labels, series:[{name,data}], tiles}) — TapTap computes the figures, the phone draws them.
class FitChart extends StatelessWidget {
  const FitChart({super.key, required this.data});
  final Map<String, dynamic> data;

  @override
  Widget build(BuildContext context) {
    final cs = Theme.of(context).colorScheme;
    final spec = (data['spec'] as Map?)?.cast<String, dynamic>() ?? {};
    final type = '${spec['type'] ?? data['type'] ?? 'bar'}';
    final title = '${spec['title'] ?? ''}';
    final labels = [for (final l in (data['labels'] as List? ?? [])) '$l'];
    final series = (data['series'] as List? ?? []).cast<Map>();
    final palette = [cs.primary, cs.tertiary, const Color(0xFF22C55E), const Color(0xFFF59E0B), const Color(0xFFEF4444)];
    Widget body;
    if (type == 'kpi') {
      final tiles = (data['tiles'] as List? ?? []).cast<Map>();
      body = Wrap(spacing: 8, runSpacing: 8, children: [
        for (final t in tiles)
          Container(
            width: 140,
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(color: cs.primaryContainer.withOpacity(.5), borderRadius: BorderRadius.circular(14)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('${t['name']}', style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
              const SizedBox(height: 4),
              Text('${t['display'] ?? t['value']}', style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w800)),
            ]),
          ),
      ]);
    } else if (series.isEmpty || labels.isEmpty) {
      body = const Text('No figures for this yet.');
    } else if (type == 'pie' || type == 'doughnut') {
      final s = (series.first['data'] as List).map(toD).toList();
      final total = s.fold<double>(0, (a, b) => a + b);
      body = SizedBox(
        height: 180,
        child: Row(children: [
          Expanded(
            child: PieChart(PieChartData(
              centerSpaceRadius: type == 'doughnut' ? 38 : 0,
              sectionsSpace: 2,
              sections: [
                for (var i = 0; i < s.length; i++)
                  PieChartSectionData(value: s[i], color: palette[i % palette.length], radius: 52, title: total > 0 && s[i] / total > .08 ? '${(s[i] / total * 100).round()}%' : '',
                      titleStyle: const TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.w700)),
              ],
            )),
          ),
          Column(mainAxisAlignment: MainAxisAlignment.center, crossAxisAlignment: CrossAxisAlignment.start, children: [
            for (var i = 0; i < labels.length && i < 6; i++)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 2),
                child: Row(children: [
                  Container(width: 10, height: 10, decoration: BoxDecoration(color: palette[i % palette.length], shape: BoxShape.circle)),
                  const SizedBox(width: 6),
                  Text(labels[i], style: const TextStyle(fontSize: 12)),
                ]),
              ),
          ]),
        ]),
      );
    } else {
      final line = type == 'line' || type == 'area';
      final maxY = series.expand((s) => (s['data'] as List).map(toD)).fold<double>(0, (a, b) => b > a ? b : a);
      final step = (labels.length / 6).ceil().clamp(1, 999);
      final bottom = AxisTitles(
        sideTitles: SideTitles(
          showTitles: true,
          reservedSize: 22,
          getTitlesWidget: (v, _) => v.toInt() % step == 0 && v.toInt() < labels.length
              ? Text(labels[v.toInt()], style: TextStyle(fontSize: 9.5, color: cs.onSurfaceVariant))
              : const SizedBox.shrink(),
        ),
      );
      const none = AxisTitles(sideTitles: SideTitles(showTitles: false));
      final titles = FlTitlesData(bottomTitles: bottom, leftTitles: none, rightTitles: none, topTitles: none);
      body = SizedBox(
        height: 180,
        child: line
            ? LineChart(LineChartData(
                maxY: maxY <= 0 ? 1 : maxY * 1.15,
                gridData: const FlGridData(show: false),
                borderData: FlBorderData(show: false),
                titlesData: titles,
                lineBarsData: [
                  for (var j = 0; j < series.length; j++)
                    LineChartBarData(
                      isCurved: true,
                      color: palette[j % palette.length],
                      barWidth: 3,
                      dotData: const FlDotData(show: false),
                      belowBarData: BarAreaData(show: type == 'area' || j == 0, color: palette[j % palette.length].withOpacity(.12)),
                      spots: [
                        for (var i = 0; i < (series[j]['data'] as List).length; i++) FlSpot(i.toDouble(), toD((series[j]['data'] as List)[i])),
                      ],
                    ),
                ],
              ))
            : BarChart(BarChartData(
                maxY: maxY <= 0 ? 1 : maxY * 1.15,
                gridData: const FlGridData(show: false),
                borderData: FlBorderData(show: false),
                titlesData: titles,
                barGroups: [
                  for (var i = 0; i < labels.length; i++)
                    BarChartGroupData(x: i, barRods: [
                      for (var j = 0; j < series.length; j++)
                        BarChartRodData(
                            toY: i < (series[j]['data'] as List).length ? toD((series[j]['data'] as List)[i]) : 0,
                            color: palette[j % palette.length],
                            width: series.length > 1 ? 5 : 10,
                            borderRadius: BorderRadius.circular(3)),
                    ]),
                ],
              )),
      );
    }
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      if (title.isNotEmpty) Padding(padding: const EdgeInsets.only(bottom: 10), child: Text(title, style: const TextStyle(fontWeight: FontWeight.w800))),
      body,
      if (series.length > 1 && type != 'pie' && type != 'doughnut')
        Padding(
          padding: const EdgeInsets.only(top: 8),
          child: Wrap(spacing: 12, children: [
            for (var j = 0; j < series.length; j++)
              Row(mainAxisSize: MainAxisSize.min, children: [
                Container(width: 10, height: 10, decoration: BoxDecoration(color: palette[j % palette.length], shape: BoxShape.circle)),
                const SizedBox(width: 5),
                Text('${series[j]['name'] ?? ''}', style: const TextStyle(fontSize: 12)),
              ]),
          ]),
        ),
    ]);
  }
}
