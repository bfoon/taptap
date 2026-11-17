"""Connection lab, internet-line switch and DHCP lease tools (core/conn_lab.py, core/line_steer.py)."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0121_config_studio'),
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
                                            ('dhcp', 'DHCP lease'), ('dhcpset', 'DHCP lease change')],
                                   max_length=10),
        ),
        migrations.CreateModel(
            name='LinePin',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(help_text='MAC address, or ip-a-b-c-d when the MAC is not known', max_length=40)),
                ('mac', models.CharField(blank=True, max_length=32)),
                ('ip', models.CharField(blank=True, max_length=64)),
                ('code', models.CharField(blank=True, help_text='Voucher code or member username', max_length=120)),
                ('mark', models.CharField(help_text='Connection / routing mark of the line', max_length=60)),
                ('label', models.CharField(blank=True, max_length=80)),
                ('from_label', models.CharField(blank=True, max_length=80)),
                ('confirmed', models.BooleanField(default=False, help_text='The router answered that the rule is in place')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('removed_at', models.DateTimeField(blank=True, null=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='line_pins', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('removed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('router', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='line_pins', to='core.router')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['router', 'removed_at'], name='linepin_router_idx')],
            },
        ),
    ]
