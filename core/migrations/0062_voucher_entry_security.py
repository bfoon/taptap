from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0061_agent_order_quantity'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='VoucherEntryPolicy',
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
                        default=False,
                        help_text='Count wrong voucher/member credentials and temporarily block that device from trying again.',
                    ),
                ),
                (
                    'max_attempts',
                    models.PositiveSmallIntegerField(
                        default=5,
                        help_text='Wrong entries allowed inside the attempt window before the device is blocked.',
                    ),
                ),
                (
                    'warning_remaining',
                    models.PositiveSmallIntegerField(
                        default=2,
                        help_text='Start showing a strong warning when this many attempts remain.',
                    ),
                ),
                (
                    'window_minutes',
                    models.PositiveSmallIntegerField(
                        default=10,
                        help_text='Wrong attempts older than this are forgotten.',
                    ),
                ),
                (
                    'block_minutes',
                    models.PositiveIntegerField(
                        default=30,
                        help_text='How long voucher entry stays blocked after the threshold is reached.',
                    ),
                ),
                (
                    'warning_text',
                    models.TextField(
                        blank=True,
                        help_text='Optional custom warning shown before the threshold is reached.',
                    ),
                ),
                (
                    'blocked_text',
                    models.TextField(
                        blank=True,
                        help_text='Optional custom message shown while the device is blocked.',
                    ),
                ),
                ('updated_at', models.DateTimeField(auto_now=True)),
                (
                    'business',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='voucher_entry_policy',
                        to='core.business',
                    ),
                ),
            ],
            options={
                'verbose_name': 'voucher entry security policy',
                'verbose_name_plural': 'voucher entry security policies',
            },
        ),
        migrations.CreateModel(
            name='VoucherEntryDevice',
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
                ('device_key', models.CharField(max_length=80)),
                ('mac_address', models.CharField(blank=True, max_length=32)),
                ('fingerprint_hash', models.CharField(blank=True, max_length=64)),
                ('ip_address', models.CharField(blank=True, max_length=64)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('window_started_at', models.DateTimeField(blank=True, null=True)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('blocked_at', models.DateTimeField(blank=True, null=True)),
                ('blocked_until', models.DateTimeField(blank=True, null=True)),
                ('last_unblocked_at', models.DateTimeField(blank=True, null=True)),
                ('last_seen_at', models.DateTimeField(auto_now=True)),
                (
                    'business',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='voucher_entry_devices',
                        to='core.business',
                    ),
                ),
                (
                    'last_unblocked_by',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+',
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'ordering': ['-blocked_until', '-last_attempt_at', '-pk'],
            },
        ),
        migrations.AddConstraint(
            model_name='voucherentrydevice',
            constraint=models.UniqueConstraint(
                fields=('business', 'device_key'),
                name='uniq_voucher_entry_device_business_key',
            ),
        ),
        migrations.AddIndex(
            model_name='voucherentrydevice',
            index=models.Index(
                fields=['business', 'blocked_until'],
                name='core_vouche_busines_81cb39_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='voucherentrydevice',
            index=models.Index(
                fields=['business', 'mac_address'],
                name='core_vouche_busines_b81a67_idx',
            ),
        ),
    ]
