from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0096_staff_payroll'),
    ]

    operations = [
        migrations.AddField(
            model_name='routerinterfacerole',
            name='isp_name',
            field=models.CharField(blank=True, max_length=60),
        ),
    ]
