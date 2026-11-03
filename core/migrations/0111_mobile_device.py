"""Phones signed in to the TapTap mobile app."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0110_portal_call_settings'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MobileDevice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token_hash', models.CharField(max_length=64, unique=True)),
                ('session_key', models.CharField(max_length=40)),
                ('name', models.CharField(blank=True, max_length=80)),
                ('platform', models.CharField(choices=[('android', 'Android'), ('ios', 'iPhone'), ('other', 'Other')], default='other', max_length=10)),
                ('app_version', models.CharField(blank=True, max_length=20)),
                ('push_token', models.CharField(blank=True, db_index=True, max_length=300)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_seen_at', models.DateTimeField(blank=True, null=True)),
                ('last_ip', models.CharField(blank=True, max_length=64)),
                ('revoked_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='mobile_devices', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-last_seen_at', '-created_at']},
        ),
    ]
