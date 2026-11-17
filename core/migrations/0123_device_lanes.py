"""Speed per device: each device of a shared voucher keeps its own plan speed (core/device_lanes.py)."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0122_flow_lab'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='nettest',
            name='kind',
            field=models.CharField(choices=[('doctor', 'Internet check'), ('ping', 'Ping'), ('trace', 'Traceroute'), ('dns', 'DNS lookup'),
                                            ('web', 'Website check'), ('speed', 'Speed test'), ('whoami', 'Find my device'),
                                            ('hops', 'Path check'), ('mypath', 'My connection'), ('drops', 'Why people drop'),
                                            ('client', 'Device check'), ('cspeed', 'Speed check'), ('slowscan', 'Slow scan'),
                                            ('lane', 'Internet line'), ('lanepin', 'Line switch'), ('laneoff', 'Line release'),
                                            ('dhcp', 'DHCP lease'), ('dhcpset', 'DHCP lease change'),
                                            ('lanes', 'Speed per device'), ('lanefix', 'Speed per device fix')],
                                   max_length=10),
        ),
        migrations.CreateModel(
            name='DeviceLane',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('scope', models.CharField(choices=[('voucher', 'One voucher'), ('shared', 'Every shared voucher')], default='voucher', max_length=10)),
                ('reason', models.CharField(blank=True, max_length=160)),
                ('until', models.DateTimeField(blank=True, help_text='Empty = until stopped', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('removed_at', models.DateTimeField(blank=True, null=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='device_lanes', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('removed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('router', models.ForeignKey(blank=True, help_text='Empty = every router of the business', null=True, on_delete=django.db.models.deletion.CASCADE, related_name='device_lanes', to='core.router')),
                ('voucher', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='device_lanes', to='core.voucher')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['business', 'removed_at'], name='devicelane_biz_idx')],
            },
        ),
    ]
