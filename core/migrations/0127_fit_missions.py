from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Fit missions (core/fit_missions.py): jobs Fit does later by itself — at a time, on a timetable, at the quietest
    moment of a window, or when something happens — and who it tells."""

    dependencies = [
        ('core', '0126_chat_message_meta'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FitMission',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('trigger', models.CharField(choices=[('at', 'At a time'), ('repeat', 'On a timetable'), ('quiet', 'At a quiet moment'), ('when', 'When something happens')], max_length=10)),
                ('when', models.JSONField(blank=True, default=dict, help_text='The trigger’s details (core/fit_missions.py)')),
                ('task', models.CharField(choices=[('notify', 'Tell people'), ('restart_router', 'Restart a router'), ('shutdown_router', 'Shut a router down'), ('action', 'A Fit change'), ('report', 'Send a report'), ('ask_fit', 'Ask Fit and send the answer')], max_length=20)),
                ('task_params', models.JSONField(blank=True, default=dict)),
                ('tell', models.JSONField(blank=True, default=dict, help_text='{me, team, emails, chat, before_minutes, message}')),
                ('repeat', models.BooleanField(default=False, help_text='Runs again (timetable, every day, every time it happens)')),
                ('cooldown_minutes', models.PositiveIntegerField(default=60)),
                ('status', models.CharField(choices=[('waiting', 'Waiting'), ('done', 'Done'), ('failed', 'Failed'), ('paused', 'Paused'), ('cancelled', 'Cancelled'), ('expired', 'Expired'), ('missed', 'Missed')], db_index=True, default='waiting', max_length=10)),
                ('next_at', models.DateTimeField(blank=True, db_index=True, help_text='When it is next due / its window opens', null=True)),
                ('expires_at', models.DateTimeField(blank=True, help_text='Give up after this (empty = no limit)', null=True)),
                ('state', models.JSONField(blank=True, default=dict)),
                ('log', models.JSONField(blank=True, default=list, help_text='The latest things it did, newest last')),
                ('runs', models.PositiveIntegerField(default=0)),
                ('last_run_at', models.DateTimeField(blank=True, null=True)),
                ('last_result', models.CharField(blank=True, max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fit_missions', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, help_text='Who approved it: it runs with this person’s rights, checked again each time', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['status', 'next_at', '-id'],
                'indexes': [models.Index(fields=['business', 'status'], name='fitmission_biz_status_idx')],
            },
        ),
    ]
