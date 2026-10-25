from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Country at sign-up, subscription prices per country, and feature modules per business.

    Existing businesses become Gambia (GM) on the "business" tier, which switches every feature
    on — nothing changes for them until the platform changes their tier or switches.
    """

    dependencies = [
        ('core', '0103_link_pin_multiple_ips'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(model_name='business', name='country',
                            field=models.CharField(db_index=True, default='GM', help_text='ISO country code, e.g. GM', max_length=2)),
        migrations.AddField(model_name='business', name='feature_tier',
                            field=models.CharField(default='business', help_text='Bundle of features this business gets (core/features.py TIERS)', max_length=20)),
        migrations.AddField(model_name='business', name='feature_overrides',
                            field=models.JSONField(blank=True, default=dict, help_text='Platform switches on top of the tier: {feature key: true/false}')),
        migrations.AddField(model_name='business', name='features_off',
                            field=models.JSONField(blank=True, default=list, help_text='Optional features the owner switched off for their own business')),
        migrations.AddField(model_name='subscription', name='currency',
                            field=models.CharField(default='GMD', help_text='ISO currency of the amount', max_length=3)),
        migrations.AddField(model_name='subscription', name='country',
                            field=models.CharField(blank=True, help_text='Country price list used for this request', max_length=2)),
        migrations.CreateModel(
            name='CountryPrice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('country', models.CharField(db_index=True, max_length=2)),
                ('package', models.CharField(help_text='Package code, e.g. 1m, 3m, 1y', max_length=10)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('currency', models.CharField(max_length=3)),
                ('active', models.BooleanField(default=True, help_text='Off = this package is not offered in this country')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['country', 'package']},
        ),
        migrations.AddConstraint(model_name='countryprice',
                                 constraint=models.UniqueConstraint(fields=('country', 'package'), name='country_price_unique')),
    ]
