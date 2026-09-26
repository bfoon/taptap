from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0003_mikrotik_control_center'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RouterSyncJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('celery_task_id', models.CharField(blank=True, max_length=255)),
                ('status', models.CharField(choices=[('queued','Queued'),('running','Running'),('success','Success'),('failed','Failed')], default='queued', max_length=20)),
                ('progress', models.PositiveSmallIntegerField(default=0)),
                ('phase', models.CharField(default='Waiting for background worker', max_length=180)),
                ('summary', models.JSONField(blank=True, default=dict)),
                ('error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='router_sync_jobs', to='core.business')),
                ('requested_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='router_sync_jobs', to=settings.AUTH_USER_MODEL)),
                ('router', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='sync_jobs', to='core.router')),
            ],
            options={'ordering':['-created_at']},
        ),
        migrations.AddIndex(
            model_name='routersyncjob',
            index=models.Index(fields=['business','status','-created_at'], name='syncjob_business_status_idx'),
        ),
        migrations.AddIndex(
            model_name='routersyncjob',
            index=models.Index(fields=['router','status','-created_at'], name='syncjob_router_status_idx'),
        ),
    ]
