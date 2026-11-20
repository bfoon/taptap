from django.db import migrations, models


class Migration(migrations.Migration):
    """Network tools: "Why the internet blinks" (core/blink_watch.py) — a new kind of test (choices only, no data change)."""

    dependencies = [
        ('core', '0128_fit_tables'),
    ]

    operations = [
        migrations.AlterField(
            model_name='nettest',
            name='kind',
            field=models.CharField(choices=[('doctor', 'Internet check'), ('ping', 'Ping'), ('trace', 'Traceroute'), ('dns', 'DNS lookup'), ('web', 'Website check'), ('speed', 'Speed test'), ('whoami', 'Find my device'), ('hops', 'Path check'), ('mypath', 'My connection'), ('drops', 'Why people drop'), ('blinks', 'Why the internet blinks'), ('client', 'Device check'), ('cspeed', 'Speed check'), ('slowscan', 'Slow scan'), ('lane', 'Internet line'), ('lanepin', 'Line switch'), ('laneoff', 'Line release'), ('dhcp', 'DHCP lease'), ('dhcpset', 'DHCP lease change'), ('lanes', 'Speed per device'), ('lanefix', 'Speed per device fix'), ('labq', 'Connection lab'), ('labd', 'Connection lab (details)')], max_length=10),
        ),
    ]
