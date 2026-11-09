from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0118_fit_looks'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatimage',
            name='listened_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
