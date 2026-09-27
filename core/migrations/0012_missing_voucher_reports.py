# Generated for TapTap missing voucher reporting.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_live_sync_enforcement'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MissingVoucherReport',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('report_type', models.CharField(choices=[('single', 'Single voucher'), ('group', 'Group of vouchers'), ('batch', 'Entire batch')], default='single', max_length=20)),
                ('voucher_codes', models.JSONField(blank=True, default=list)),
                ('voucher_count', models.PositiveIntegerField(default=0)),
                ('details', models.TextField(blank=True, help_text='What is missing / what happened.')),
                ('action_state', models.CharField(choices=[('planned', 'Action planned'), ('done', 'Action already taken')], default='planned', max_length=20)),
                ('action_notes', models.TextField(blank=True, help_text='What was done or what will be done.')),
                ('reference', models.CharField(blank=True, help_text='Optional reference, incident number, police report number, etc.', max_length=160)),
                ('status', models.CharField(choices=[('open', 'Open'), ('resolved', 'Resolved')], db_index=True, default='open', max_length=20)),
                ('reported_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('resolution_notes', models.TextField(blank=True)),
                ('batch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='missing_reports', to='core.voucherbatch')),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='missing_voucher_reports', to='core.business')),
                ('reported_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='missing_voucher_reports_created', to=settings.AUTH_USER_MODEL)),
                ('resolved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='missing_voucher_reports_resolved', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-reported_at'],
                'indexes': [models.Index(fields=['business', 'status', '-reported_at'], name='missing_voucher_status_idx')],
            },
        ),
    ]
