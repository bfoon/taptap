from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0049_app_control'),
    ]

    operations = [
        migrations.AlterField(
            model_name='teammember',
            name='user',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='memberships',
                to='auth.user',
            ),
        ),
        migrations.AddConstraint(
            model_name='teammember',
            constraint=models.UniqueConstraint(
                fields=('business', 'user'),
                name='uniq_team_member_business_user',
            ),
        ),
    ]
