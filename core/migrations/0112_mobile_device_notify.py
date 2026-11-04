"""What each phone wants to be told (Notifications screen in the mobile app)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0111_mobile_device'),
    ]

    operations = [
        migrations.AddField(
            model_name='mobiledevice',
            name='notify',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
