from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0050_team_multi_business'),
    ]

    operations = [
        migrations.CreateModel(
            name='VoucherArchivePolicy',
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
                    'enabled',
                    models.BooleanField(
                        default=True,
                        help_text=(
                            'Automatically remove old expired vouchers from '
                            'MikroTik and keep them archived in TapTap.'
                        ),
                    ),
                ),
                (
                    'retention_days',
                    models.PositiveSmallIntegerField(
                        default=7,
                        help_text=(
                            'How many days an expired voucher stays on MikroTik '
                            'before TapTap removes it.'
                        ),
                    ),
                ),
                (
                    'updated_at',
                    models.DateTimeField(auto_now=True),
                ),
                (
                    'business',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='voucher_archive_policy',
                        to='core.business',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Voucher archive policy',
                'verbose_name_plural': 'Voucher archive policies',
            },
        ),
    ]
