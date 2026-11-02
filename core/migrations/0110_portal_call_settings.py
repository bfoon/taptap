"""Who answers in-app calls from the Wi-Fi pages."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0109_member_plan_bin'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PortalCallSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('online_first', models.BooleanField(default=True, help_text='Ring only the people who have TapTap open right now; ring everyone picked when nobody is.')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('business', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='portal_call_settings', to='core.business')),
                ('handlers', models.ManyToManyField(blank=True, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
