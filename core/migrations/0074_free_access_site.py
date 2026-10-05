from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0073_notification_recipients'),
    ]

    operations = [
        migrations.CreateModel(
            name='FreeAccessSite',
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
                (
                    'host',
                    models.CharField(
                        help_text='Hostname only, e.g. pay.example.com',
                        max_length=253,
                    ),
                ),
                (
                    'label',
                    models.CharField(
                        blank=True,
                        help_text='Friendly name shown in Settings.',
                        max_length=100,
                    ),
                ),
                (
                    'purpose',
                    models.CharField(
                        choices=[
                            ('advert', 'Advertisement / sponsor'),
                            ('portal', 'Customer / payment portal'),
                            ('public', 'Public information'),
                            ('other', 'Other'),
                        ],
                        default='other',
                        max_length=12,
                    ),
                ),
                (
                    'include_subdomains',
                    models.BooleanField(
                        default=True,
                        help_text='Also allow *.host.',
                    ),
                ),
                (
                    'enabled',
                    models.BooleanField(default=True),
                ),
                (
                    'created_at',
                    models.DateTimeField(auto_now_add=True),
                ),
                (
                    'updated_at',
                    models.DateTimeField(auto_now=True),
                ),
                (
                    'business',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='free_access_sites',
                        to='core.business',
                    ),
                ),
            ],
            options={
                'ordering': ['purpose', 'label', 'host'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('business', 'host'),
                        name='uniq_free_access_business_host',
                    ),
                ],
            },
        ),
    ]
