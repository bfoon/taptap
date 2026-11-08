from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0117_nettest_drops'),
    ]

    operations = [
        migrations.AddField(
            model_name='fitsettings',
            name='looks',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
