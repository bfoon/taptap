import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0099_fit_ai'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FitMonitor',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('kind', models.CharField(choices=[('slot_refused', 'A new device is refused because all slots are full'), ('slot_filled', 'A voucher or member fills its last device slot'), ('idle_slots', 'Free slots held by devices that have not been seen for days'), ('voucher_online', 'A voucher or member comes online'), ('sharing', 'More devices online on one voucher than it allows'), ('heavy_user', 'One customer downloads very fast for a long time'), ('online_drop', 'Customers online suddenly drop on a router'), ('router_offline', 'A router is offline')], max_length=20)),
                ('params', models.JSONField(blank=True, default=dict)),
                ('response', models.CharField(choices=[('notify', 'Only tell me'), ('free_slot', 'Free one idle slot (the new device gets in)'), ('disconnect', 'Disconnect the extra device')], default='notify', max_length=12)),
                ('audience', models.CharField(choices=[('me', 'Only me'), ('team', 'The whole team')], default='me', max_length=6)),
                ('email', models.BooleanField(default=False, help_text='Also email the business notification recipients')),
                ('notify_member', models.BooleanField(default=False, help_text='Email the member / customer when it is about their account')),
                ('cooldown_minutes', models.PositiveSmallIntegerField(default=30)),
                ('active', models.BooleanField(default=True)),
                ('expires_at', models.DateTimeField(blank=True, help_text='Stop watching after this time (empty = until paused)', null=True)),
                ('source', models.CharField(default='fit', max_length=10)),
                ('hits', models.PositiveIntegerField(default=0)),
                ('firing', models.BooleanField(default=False)),
                ('state', models.JSONField(blank=True, default=dict, help_text='What is true right now (so each thing is told once)')),
                ('last_fired_at', models.DateTimeField(blank=True, null=True)),
                ('last_checked_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fit_monitors', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-active', '-created_at'],
                'indexes': [models.Index(fields=['business', 'kind', 'active'], name='fitmon_biz_kind_idx')],
            },
        ),
        migrations.CreateModel(
            name='FitMonitorEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(blank=True, max_length=20)),
                ('title', models.CharField(max_length=160)),
                ('body', models.CharField(blank=True, max_length=500)),
                ('link', models.CharField(blank=True, max_length=200)),
                ('voucher_code', models.CharField(blank=True, max_length=64)),
                ('mac', models.CharField(blank=True, max_length=32)),
                ('action', models.CharField(blank=True, help_text='admitted, freed_slot, disconnected, nothing_idle…', max_length=20)),
                ('result', models.CharField(blank=True, max_length=300)),
                ('member_emailed', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fit_monitor_events', to='core.business')),
                ('monitor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='events', to='core.fitmonitor')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
