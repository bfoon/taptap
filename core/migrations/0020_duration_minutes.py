"""Plan and voucher durations in minutes, with the unit the owner chose.

Existing hours are copied exactly (hours x 60). Plans get the unit that reads
best: 720 h -> 1 month, 168 h -> 7 days, 12 h -> 12 hours.
"""
from django.db import migrations, models


def forwards(apps, schema_editor):
    VoucherPlan = apps.get_model('core', 'VoucherPlan')
    Voucher = apps.get_model('core', 'Voucher')
    for plan in VoucherPlan.objects.all().only('pk', 'duration_hours'):
        hours = int(plan.duration_hours or 24)
        unit = 'months' if hours % 720 == 0 else ('days' if hours % 24 == 0 else 'hours')
        VoucherPlan.objects.filter(pk=plan.pk).update(duration_minutes=hours * 60, duration_unit=unit)
    # One UPDATE for all vouchers (there can be many).
    Voucher.objects.update(duration_minutes=models.F('duration_hours') * 60)


def backwards(apps, schema_editor):
    VoucherPlan = apps.get_model('core', 'VoucherPlan')
    Voucher = apps.get_model('core', 'Voucher')
    for model in (VoucherPlan, Voucher):
        for obj in model.objects.all().only('pk', 'duration_minutes'):
            model.objects.filter(pk=obj.pk).update(duration_hours=max(1, -(-int(obj.duration_minutes or 60) // 60)))


class Migration(migrations.Migration):
    dependencies = [('core', '0019_voucher_events')]
    operations = [
        migrations.AddField('voucherplan', 'duration_minutes', models.PositiveIntegerField(default=1440)),
        migrations.AddField('voucherplan', 'duration_unit', models.CharField(
            max_length=10, default='hours',
            choices=[('minutes', 'Minutes'), ('hours', 'Hours'), ('days', 'Days'), ('months', 'Months')])),
        migrations.AddField('voucher', 'duration_minutes', models.PositiveIntegerField(default=1440)),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField('voucherplan', 'duration_hours'),
        migrations.RemoveField('voucher', 'duration_hours'),
    ]
