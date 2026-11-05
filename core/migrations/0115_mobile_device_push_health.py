"""Push health per phone: which Firebase project its app belongs to, and what Google said last time."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0114_chat_edit_reply_delete'),
    ]

    operations = [
        migrations.AddField(
            model_name='mobiledevice',
            name='push_project',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='mobiledevice',
            name='push_health',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
