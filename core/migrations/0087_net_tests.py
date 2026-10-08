from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0086_voucher_sticky_exemption'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='NetTest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('doctor', 'Internet check'), ('ping', 'Ping'), ('trace', 'Traceroute'), ('dns', 'DNS lookup'), ('web', 'Website check'), ('speed', 'Speed test')], max_length=10)),
                ('target', models.CharField(blank=True, max_length=320)),
                ('params', models.JSONField(blank=True, default=dict)),
                ('status', models.CharField(choices=[('running', 'Running'), ('waiting', 'Waiting for the router'), ('done', 'Done'), ('failed', 'Failed')], default='running', max_length=10)),
                ('via', models.CharField(blank=True, max_length=20)),
                ('result', models.JSONField(blank=True, default=dict)),
                ('command_id', models.PositiveIntegerField(blank=True, help_text='TapTap Link command that runs it', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='net_tests', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('router', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='net_tests', to='core.router')),
            ],
            options={'ordering': ['-created_at'], 'indexes': [models.Index(fields=['business', '-created_at'], name='nettest_biz_idx')]},
        ),
    ]
