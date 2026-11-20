from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Fit tables (core/fit_tables.py): saved, filtered tables Fit builds on request — run again on live data each time."""

    dependencies = [
        ('core', '0127_fit_missions'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FitTable',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=120)),
                ('source', models.CharField(choices=[('vouchers', 'Vouchers'), ('sales', 'Sales'), ('routers', 'Routers'), ('reports', 'Customer reports')], max_length=12)),
                ('filters', models.JSONField(blank=True, default=dict)),
                ('columns', models.JSONField(blank=True, default=list)),
                ('sort', models.JSONField(blank=True, default=dict)),
                ('shared', models.BooleanField(default=True, help_text='The team sees it (each person only with their own rights)')),
                ('pinned', models.BooleanField(default=False, help_text='Shown first in Fit’s workspace')),
                ('views', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('business', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fit_tables', to='core.business')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-pinned', '-updated_at'],
                'indexes': [models.Index(fields=['business', '-updated_at'], name='fittable_biz_idx')],
            },
        ),
    ]
