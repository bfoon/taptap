"""Each activity entry can belong to one router: a business with several MikroTiks sees each one's own history."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0112_mobile_device_notify'),
    ]

    operations = [
        migrations.AddField(
            model_name='activity',
            name='router',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='activities', to='core.router'),
        ),
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['business', 'router', '-created_at'], name='core_activity_router_idx'),
        ),
    ]
