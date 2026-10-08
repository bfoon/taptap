from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0087_net_tests')]

    operations = [
        migrations.AlterField(
            model_name='nettest', name='kind',
            field=models.CharField(choices=[('doctor', 'Internet check'), ('ping', 'Ping'), ('trace', 'Traceroute'), ('dns', 'DNS lookup'),
                                            ('web', 'Website check'), ('speed', 'Speed test'), ('whoami', 'Find my device'),
                                            ('hops', 'Path check'), ('mypath', 'My connection')], max_length=10),
        ),
    ]
