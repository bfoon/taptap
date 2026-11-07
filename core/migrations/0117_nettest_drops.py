"""Network tools: "Why people drop" reads the router's log of hotspot sign-outs (core/nettools.py)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0116_router_schedules'),
    ]

    operations = [
        migrations.AlterField(
            model_name='nettest',
            name='kind',
            field=models.CharField(choices=[('doctor', 'Internet check'), ('ping', 'Ping'), ('trace', 'Traceroute'), ('dns', 'DNS lookup'),
                                            ('web', 'Website check'), ('speed', 'Speed test'), ('whoami', 'Find my device'),
                                            ('hops', 'Path check'), ('mypath', 'My connection'), ('drops', 'Why people drop')],
                                   max_length=10),
        ),
    ]
