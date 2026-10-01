from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0053_fup_exempt_devices'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TrafficSpeedRule',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('name', models.CharField(max_length=160)),
                (
                    'scope',
                    models.CharField(
                        choices=[
                            ('all_plans', 'All plans'),
                            ('plans', 'Selected plan(s)'),
                            ('agents', 'Agent(s)'),
                            ('devices', 'Device(s)'),
                        ],
                        max_length=20,
                    ),
                ),
                ('devices', models.JSONField(blank=True, default=list)),
                (
                    'down_mbps',
                    models.DecimalField(
                        decimal_places=2,
                        help_text='Maximum download speed per matching online device.',
                        max_digits=8,
                    ),
                ),
                (
                    'up_mbps',
                    models.DecimalField(
                        decimal_places=2,
                        help_text='Maximum upload speed per matching online device.',
                        max_digits=8,
                    ),
                ),
                ('enabled', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'business',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='traffic_speed_rules',
                        to='core.business',
                    ),
                ),
                (
                    'created_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    'agents',
                    models.ManyToManyField(
                        blank=True,
                        related_name='traffic_speed_rules',
                        to='core.agent',
                    ),
                ),
                (
                    'plans',
                    models.ManyToManyField(
                        blank=True,
                        related_name='traffic_speed_rules',
                        to='core.voucherplan',
                    ),
                ),
            ],
            options={
                'ordering': ['-updated_at', '-pk'],
            },
        ),
    ]
