from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0091_router_cleanup')]

    operations = [
        migrations.AlterField(
            model_name='routercleanup', name='action',
            field=models.CharField(choices=[('scan', 'Scan'), ('clean', 'Clean'), ('copy', 'Save to computer')], max_length=10),
        ),
    ]
