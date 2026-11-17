"""The owner can lock or seal a Member Plan (core/plan_lock.py)."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0124_lab_steps'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='memberplan',
            name='lock_kind',
            field=models.CharField(blank=True, choices=[('', 'Open'), ('locked', 'Locked'), ('sealed', 'Sealed')], default='', max_length=10),
        ),
        migrations.AddField(
            model_name='memberplan',
            name='lock_reason',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='memberplan',
            name='locked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='memberplan',
            name='locked_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL),
        ),
    ]
