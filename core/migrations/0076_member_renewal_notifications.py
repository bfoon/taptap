# Generated for TapTap member renewal receipts, customer email and expiry reminders.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0075_member_plans'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MemberNotificationSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(blank=True, max_length=254)),
                ('reminders_enabled', models.BooleanField(
                    default=False,
                    help_text='Send expiry reminders to this member when an email address is present.',
                )),
                ('remind_7_days', models.BooleanField(default=True)),
                ('remind_2_days', models.BooleanField(default=True)),
                ('remind_1_day', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('member', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='member_notification_settings',
                    to='core.voucher',
                )),
                ('updated_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['member__code']},
        ),
        migrations.CreateModel(
            name='MemberRenewal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('member_username', models.CharField(max_length=120)),
                ('customer_name', models.CharField(blank=True, max_length=120)),
                ('plan_name', models.CharField(max_length=120)),
                ('plan_price', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('amount_collected', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('currency', models.CharField(default='GMD', max_length=12)),
                ('payment_method', models.CharField(default='cash', max_length=30)),
                ('reference', models.CharField(blank=True, max_length=120)),
                ('duration_minutes', models.PositiveIntegerField(default=0)),
                ('max_devices', models.PositiveSmallIntegerField(default=1)),
                ('speed_limit', models.CharField(blank=True, max_length=50)),
                ('old_expires_at', models.DateTimeField(blank=True, null=True)),
                ('new_expires_at', models.DateTimeField(blank=True, null=True)),
                ('renewed_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('email_to', models.EmailField(blank=True, max_length=254)),
                ('email_sent_at', models.DateTimeField(blank=True, null=True)),
                ('email_error', models.CharField(blank=True, max_length=500)),
                ('business', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='member_renewals',
                    to='core.business',
                )),
                ('member', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='member_renewals',
                    to='core.voucher',
                )),
                ('plan', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='renewals',
                    to='core.memberplan',
                )),
                ('recorded_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('sale', models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='member_renewal',
                    to='core.vouchersale',
                )),
            ],
            options={
                'ordering': ['-renewed_at', '-id'],
                'indexes': [
                    models.Index(
                        fields=['business', 'renewed_at'],
                        name='member_renewal_business_at',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='MemberReminderLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('expiry_at', models.DateTimeField()),
                ('days_before', models.PositiveSmallIntegerField()),
                ('email_to', models.EmailField(max_length=254)),
                ('status', models.CharField(
                    choices=[('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed')],
                    default='pending',
                    max_length=12,
                )),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('error', models.CharField(blank=True, max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('member', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='member_reminder_logs',
                    to='core.voucher',
                )),
            ],
            options={
                'ordering': ['-created_at'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('member', 'expiry_at', 'days_before'),
                        name='uniq_member_reminder_period',
                    ),
                ],
            },
        ),
    ]
