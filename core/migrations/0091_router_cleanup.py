from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0090_member_charges'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RouterCleanup',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(choices=[('scan', 'Scan'), ('clean', 'Clean')], max_length=10)),
                ('status', models.CharField(choices=[('running', 'Running'), ('waiting', 'Waiting for the router'), ('done', 'Done'), ('failed', 'Failed')], default='running', max_length=10)),
                ('via', models.CharField(blank=True, max_length=20)),
                ('params', models.JSONField(blank=True, default=dict)),
                ('result', models.JSONField(blank=True, default=dict)),
                ('command_id', models.PositiveIntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='router_cleanups', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('router', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='cleanups', to='core.router')),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
