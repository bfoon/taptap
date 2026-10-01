from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0054_traffic_speed_rules'),
    ]

    operations = [
        migrations.AlterField(
            model_name='trafficspeedrule',
            name='scope',
            field=models.CharField(
                choices=[
                    ('all_plans', 'All plans'),
                    ('all_bypass', 'All bypass devices'),
                    ('plans', 'Selected plan(s)'),
                    ('agents', 'Agent(s)'),
                    ('devices', 'Device(s)'),
                ],
                max_length=20,
            ),
        ),
    ]
