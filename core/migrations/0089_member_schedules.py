from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0088_net_tests_path_kinds'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MemberSchedule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(choices=[('pause', 'Pause'), ('resume', 'Unpause')], max_length=10)),
                ('run_at', models.DateTimeField(db_index=True)),
                ('days_left', models.PositiveSmallIntegerField(blank=True, help_text='Pause when this many days are left (follows renewals); empty = fixed date', null=True)),
                ('reason', models.CharField(blank=True, max_length=255)),
                ('status', models.CharField(choices=[('pending', 'Waiting'), ('running', 'Running'), ('done', 'Done'), ('skipped', 'Skipped'), ('cancelled', 'Cancelled')], db_index=True, default='pending', max_length=10)),
                ('result', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('done_at', models.DateTimeField(blank=True, null=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='member_schedules', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('member', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='member_schedules', to='core.voucher')),
            ],
            options={'ordering': ['run_at', 'pk'], 'indexes': [models.Index(fields=['status', 'run_at'], name='member_sched_due')]},
        ),
    ]
