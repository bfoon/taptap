"""Scheduled router restarts and shutdowns (core/router_schedules.py)."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0115_mobile_device_push_health'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RouterSchedule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(choices=[('restart', 'Restart'), ('shutdown', 'Shut down (power off)')], default='restart', max_length=10)),
                ('repeat', models.CharField(choices=[('once', 'Once'), ('daily', 'Every day'), ('weekly', 'Every week'), ('monthly', 'Every month')], default='daily', max_length=10)),
                ('run_on', models.DateField(blank=True, help_text='Once: the day', null=True)),
                ('at_time', models.TimeField(help_text='Local time it is due')),
                ('weekdays', models.JSONField(blank=True, default=list, help_text='Weekly: 0 = Monday … 6 = Sunday')),
                ('month_day', models.PositiveSmallIntegerField(default=1, help_text='Monthly: 1–31 (a shorter month uses its last day)')),
                ('window_minutes', models.PositiveSmallIntegerField(default=30, help_text='Wait up to this long for a quiet moment (0 = exactly on time)')),
                ('warn_minutes', models.PositiveSmallIntegerField(default=10, help_text='Heads-up this long before (0 = none)')),
                ('email', models.BooleanField(default=True)),
                ('note', models.CharField(blank=True, max_length=160)),
                ('active', models.BooleanField(default=True)),
                ('skip_at', models.DateTimeField(blank=True, help_text='This one occurrence is skipped', null=True)),
                ('next_at', models.DateTimeField(blank=True, db_index=True, help_text='When it is next due (start of its window)', null=True)),
                ('last_run_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('router', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='schedules', to='core.router')),
            ],
            options={
                'ordering': ['next_at', 'id'],
            },
        ),
        migrations.CreateModel(
            name='RouterScheduleRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(default='restart', max_length=10)),
                ('planned_at', models.DateTimeField(db_index=True)),
                ('status', models.CharField(choices=[('warned', 'Heads-up sent'), ('waiting', 'Waiting for a quiet moment'), ('sent', 'Command sent'), ('back', 'Back online'), ('off', 'Switched off'), ('late', 'Not back yet'), ('failed', 'Failed'), ('skipped', 'Skipped'), ('missed', 'Missed')], default='warned', max_length=10)),
                ('online_seen', models.JSONField(blank=True, default=list, help_text='People online at each check while waiting')),
                ('online_at_run', models.IntegerField(blank=True, null=True)),
                ('ran_at', models.DateTimeField(blank=True, null=True)),
                ('back_at', models.DateTimeField(blank=True, null=True)),
                ('down_seconds', models.PositiveIntegerField(blank=True, null=True)),
                ('result', models.CharField(blank=True, max_length=300)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('by', models.ForeignKey(blank=True, help_text='Someone who skipped it or ran it now', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('router', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='schedule_runs', to='core.router')),
                ('schedule', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='runs', to='core.routerschedule')),
            ],
            options={
                'ordering': ['-planned_at', '-id'],
                'constraints': [models.UniqueConstraint(fields=('schedule', 'planned_at'), name='router_schedule_run_once')],
            },
        ),
    ]
