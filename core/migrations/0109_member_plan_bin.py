"""Member plans get the recycle bin: a switched-off plan with no members can be deleted and kept in the bin.

Only live plans need a unique name now, so a deleted plan's name can be used again.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0108_agent_benefits'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='memberplan',
            name='uniq_member_plan_business_name',
        ),
        migrations.AddField(
            model_name='memberplan',
            name='deleted_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='memberplan',
            name='deleted_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='+', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='memberplan',
            name='delete_reason',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='memberplan',
            name='delete_info',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddConstraint(
            model_name='memberplan',
            constraint=models.UniqueConstraint(condition=models.Q(('deleted_at__isnull', True)),
                                               fields=('business', 'name'), name='uniq_live_member_plan_name'),
        ),
    ]
