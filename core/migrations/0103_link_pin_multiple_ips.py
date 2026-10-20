from django.db import migrations, models


class Migration(migrations.Migration):
    """TapTap Link IP pin: accept several public IPs (one per internet line) instead of one.

    Existing single-IP pins keep working unchanged.
    """

    dependencies = [
        ('core', '0102_investors'),
    ]

    operations = [
        migrations.AlterField(
            model_name='routeragent',
            name='pinned_ip',
            field=models.CharField(blank=True, help_text='Only accept these public IPs, comma-separated (optional)', max_length=255, null=True),
        ),
    ]
