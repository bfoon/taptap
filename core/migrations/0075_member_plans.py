# Generated for TapTap Member Plans.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0074_free_access_site'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MemberPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=80)),
                ('price', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('duration_minutes', models.PositiveIntegerField(default=43200, help_text='0 means unlimited.')),
                ('duration_unit', models.CharField(
                    choices=[
                        ('minutes', 'Minutes'),
                        ('hours', 'Hours'),
                        ('days', 'Days'),
                        ('months', 'Months'),
                        ('unlimited', 'Unlimited'),
                    ],
                    default='months',
                    max_length=10,
                )),
                ('max_devices', models.PositiveSmallIntegerField(default=1)),
                ('speed_limit', models.CharField(
                    blank=True,
                    help_text='RouterOS rate-limit, e.g. 5M/5M. Blank means full speed.',
                    max_length=50,
                )),
                ('active', models.BooleanField(
                    default=True,
                    help_text='Inactive plans stay on existing members but cannot be assigned to new members.',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('business', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='member_plans',
                    to='core.business',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['price', 'name'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('business', 'name'),
                        name='uniq_member_plan_business_name',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='MemberPlanAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('assigned_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assigned_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('member', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='member_plan_assignment',
                    to='core.voucher',
                )),
                ('plan', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='assignments',
                    to='core.memberplan',
                )),
            ],
            options={'ordering': ['-updated_at']},
        ),
    ]
